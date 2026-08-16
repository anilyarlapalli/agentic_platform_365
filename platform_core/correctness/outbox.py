"""Transactional outbox: the intent to publish commits with the state change.

## The gap this closes

The Azure API inserts a job row, then enqueues a message. Two systems, two
writes, nothing spanning them. A crash in between leaves a ``queued`` row that
nobody will ever deliver. Its own docstring names the trade honestly — "a lost
message leaves a visible `queued` row rather than an upload that silently
vanished" — and visible is genuinely better than silent. It is still stuck, and
nothing in the system will unstick it.

## The mechanism

The run row and the outbox row are written in **one Postgres transaction**. Either
both exist or neither does; there is no state where work is admitted but
undeliverable.

A relay then moves outbox rows to the broker. The gap moves from "between two
systems, unrecoverable" to "between a committed row and its delivery,
recoverable by re-reading the table". That is the whole trade: at-least-once
delivery in exchange for never losing intent.

## At-least-once, and why that is fine

The relay may publish a row and crash before marking it published, so the
message is delivered twice. That is expected and handled downstream: leases stop
two workers running one run, and ``(run_id, step)`` stops one step applying
twice. Trying to make the relay exactly-once would just move the same two-writes
problem somewhere else.

## Ordering

Rows are drained in ``id`` order — a monotonic identity column, not a timestamp.
Two rows can share a timestamp, and the wall clock can move backwards; neither
is true of the sequence.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from platform_core.db.engine import relay_session
from platform_core.identity.principal import RequestContext
from platform_core.observability.telemetry import record_queue_publish, start_span

logger = logging.getLogger("platform.correctness.outbox")


@dataclass(frozen=True, slots=True)
class OutboxRow:
    id: int
    tenant_id: uuid.UUID
    run_id: uuid.UUID
    workload: str
    payload: dict[str, Any]
    trace_context: dict[str, str]


def enqueue_run(
    session: Session,
    ctx: RequestContext,
    *,
    workload: str,
    payload: dict[str, Any],
    idempotency_key: str | None = None,
    max_attempts: int = 3,
) -> tuple[uuid.UUID, bool]:
    """Admit a run and record the intent to publish it, in the caller's transaction.

    Takes a ``Session`` rather than opening its own — that is the entire point.
    The caller's state change and this write must commit together, so this must
    join the caller's transaction rather than start a second one.

    Returns ``(run_id, created)``. ``created=False`` means an existing run with
    the same idempotency key was returned instead: a retried request resolves to
    the original run rather than starting a second one. In the Azure build two
    uploads in quick succession create two jobs that then race on a full index
    rebuild of the same domain.
    """
    key = idempotency_key or ctx.idempotency_key

    if key:
        existing = session.execute(
            text(
                "SELECT id FROM run WHERE tenant_id = :t AND idempotency_key = :k"
            ),
            {"t": ctx.tenant.id, "k": key},
        ).scalar_one_or_none()
        if existing is not None:
            logger.info("idempotency key %r resolves to existing run %s", key, existing)
            return existing, False

    run_id = session.execute(
        text(
            "INSERT INTO run (tenant_id, workload, status, idempotency_key, "
            "  requested_by, input, max_attempts, release, correlation_id) "
            "VALUES (:t, :w, 'pending', :k, :p, :input, :max, :rel, :corr) "
            # A concurrent request carrying the same key loses the race here and
            # gets the winner's row, rather than an error the caller has to
            # interpret. The unique constraint is what makes the check above a
            # fast path rather than the guarantee.
            "ON CONFLICT (tenant_id, idempotency_key) DO UPDATE "
            "  SET workload = run.workload "
            "RETURNING id, (xmax = 0) AS inserted"
        ),
        {
            "t": ctx.tenant.id, "w": workload, "k": key, "p": ctx.principal.id,
            "input": json.dumps(payload), "max": max_attempts,
            "rel": _release(), "corr": ctx.correlation_id or ctx.request_id,
        },
    ).one()

    if not run_id.inserted:
        return run_id.id, False

    session.execute(
        text(
            "INSERT INTO outbox (tenant_id, run_id, workload, payload, trace_context) "
            "VALUES (:t, :r, :w, :payload, :trace)"
        ),
        {
            "t": ctx.tenant.id, "r": run_id.id, "w": workload,
            "payload": json.dumps(payload), "trace": json.dumps(_trace_carrier()),
        },
    )
    return run_id.id, True


def _release() -> str:
    from platform_core.settings import get_settings

    return get_settings().release


def _trace_carrier() -> dict[str, str]:
    """W3C traceparent captured at write time.

    Captured here, not at publish time: the causally interesting moment is the
    request that admitted the work, and the relay may run minutes later in a
    different process with no relationship to it.
    """
    try:
        from opentelemetry.propagate import inject

        carrier: dict[str, str] = {}
        inject(carrier)
        return carrier
    except Exception:
        return {}


def drain(publish, *, batch_size: int = 100) -> int:
    """Publish unpublished outbox rows. Returns how many were published.

    ``publish`` is called with an :class:`OutboxRow` and must raise on failure.

    Runs under the relay credential because one relay serves every tenant. That
    privilege is a role the relay process holds, not a flag it sets — see
    ``migration 0003`` for why the first version of this got that wrong.

    ``FOR UPDATE SKIP LOCKED`` lets several relay replicas run without either
    blocking on each other or publishing the same row twice.
    """
    published = 0
    with relay_session(reason="outbox relay: drain unpublished rows") as s:
        rows = s.execute(
            text(
                "WITH ranked AS MATERIALIZED ("
                "  SELECT id, row_number() OVER (PARTITION BY tenant_id ORDER BY id) AS pos "
                "  FROM outbox WHERE published_at IS NULL"
                "), candidates AS ("
                "  SELECT id, pos FROM ranked "
                "  ORDER BY pos, id LIMIT :n"
                ") "
                "SELECT o.id, o.tenant_id, o.run_id, o.workload, o.payload, "
                "       o.trace_context "
                "FROM outbox o JOIN candidates c ON c.id = o.id "
                "ORDER BY c.pos, o.id FOR UPDATE OF o SKIP LOCKED"
            ),
            {"n": batch_size},
        ).all()

        for row in rows:
            entry = OutboxRow(
                id=row.id, tenant_id=row.tenant_id, run_id=row.run_id,
                workload=row.workload, payload=row.payload,
                trace_context=row.trace_context or {},
            )
            try:
                from opentelemetry.trace import SpanKind

                with start_span(
                    "platform.outbox.publish",
                    kind=SpanKind.PRODUCER,
                    attributes={
                        "messaging.system": "celery",
                        "messaging.operation.name": "publish",
                        "platform.run.id": str(entry.run_id),
                        "platform.workload": entry.workload,
                    },
                ):
                    publish(entry)
            except Exception as exc:
                record_queue_publish("celery", "failed")
                # Left unpublished so the next pass retries it. The attempt
                # counter and the error are recorded so a row that can never be
                # published is visible rather than silently retried forever.
                s.execute(
                    text(
                        "UPDATE outbox SET publish_attempts = publish_attempts + 1, "
                        "last_error = :e WHERE id = :id"
                    ),
                    {"e": f"{type(exc).__name__}: {exc}"[:2000], "id": row.id},
                )
                logger.exception("outbox row %s failed to publish", row.id)
                continue

            s.execute(
                text("UPDATE outbox SET published_at = now() WHERE id = :id"),
                {"id": row.id},
            )
            s.execute(
                text("UPDATE run SET last_enqueued_at = now() WHERE id = :id"),
                {"id": row.run_id},
            )
            published += 1
            record_queue_publish("celery", "succeeded")

    return published


def backlog() -> tuple[int, float]:
    """``(unpublished count, age in seconds of the oldest)``.

    Age is the number that matters. A backlog of 3 that has not moved in an hour
    is a stalled relay; a backlog of 3,000 draining steadily is not an incident.
    Azure's own queue-length metric has no age dimension, which is why the Azure
    build had to peek at messages to derive one.
    """
    with relay_session(reason="outbox relay: report backlog") as s:
        row = s.execute(
            text(
                "SELECT count(*) AS n, "
                "  coalesce(extract(epoch from (now() - min(created_at))), 0) AS age "
                "FROM outbox WHERE published_at IS NULL"
            )
        ).one()
    return int(row.n), float(row.age)


def poison_rows(*, min_attempts: int = 5) -> list[dict]:
    """Rows that have repeatedly failed to publish. Never retried silently forever."""
    with relay_session(reason="outbox relay: list poison rows") as s:
        rows = s.execute(
            text(
                "SELECT id, run_id, workload, publish_attempts, last_error "
                "FROM outbox WHERE published_at IS NULL AND publish_attempts >= :n "
                "ORDER BY publish_attempts DESC LIMIT 100"
            ),
            {"n": min_attempts},
        ).all()
    return [
        {
            "outbox_id": r.id, "run_id": str(r.run_id), "workload": r.workload,
            "attempts": r.publish_attempts, "last_error": r.last_error,
        }
        for r in rows
    ]
