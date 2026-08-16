"""Celery task handlers.

Two tasks, and the second is why the first is allowed to be unreliable.

``execute_run``
    Handles one delivered pointer. Leases the named run and executes it. A
    duplicate delivery finds the run already leased or finished and does
    nothing.

``sweep``
    The backstop. Reaps expired leases and picks up runs that are ``pending``
    with no pointer in flight — a message the broker lost, a relay that died
    between publishing and marking, a run returned by the reaper. Without it the
    system is only as reliable as Redis, which is the coupling the outbox was
    built to avoid.

Together these give at-least-once execution with at-most-once *effect*: the
broker may deliver a pointer zero, one or many times, and the run still executes
its steps exactly once because leases and ``(run_id, step)`` claims decide, not
the message.
"""

from __future__ import annotations

import logging
import uuid
from datetime import timedelta

from sqlalchemy import text

from platform_core.adapters.local.celery_app import TASK_RUN, TASK_SWEEP, celery_app
from platform_core.correctness import leases
from platform_core.db.engine import relay_session, system_session
from platform_core.identity.principal import Tenant
from platform_core.observability.telemetry import start_span

logger = logging.getLogger("platform.worker.tasks")

# How long a run may be pending before the sweeper assumes its pointer was lost.
# Long enough that the normal path always wins the race, so the sweeper does not
# create pointless duplicate deliveries.
LOST_POINTER_AFTER = timedelta(seconds=45)


def _tenant(tenant_id: str) -> Tenant | None:
    # Tenant identity is platform-wide read-only data for the application role.
    # A normal run worker therefore never receives the cross-tenant relay
    # credential merely to turn an id from a trusted queue pointer into a slug.
    with system_session(reason="celery task: resolve tenant") as s:
        row = s.execute(
            text("SELECT id, slug FROM tenant WHERE id = :id"), {"id": uuid.UUID(tenant_id)}
        ).one_or_none()
    return Tenant(id=row.id, slug=row.slug) if row else None


@celery_app.task(name=TASK_RUN, bind=True)
def execute_run(self, *, tenant_id: str, run_id: str, workload: str,
                trace_context: dict | None = None) -> dict:
    """Execute one run named by a delivered pointer."""
    from opentelemetry.context import attach, detach
    from opentelemetry.propagate import extract
    from opentelemetry.trace import SpanKind

    from apps.worker.runner import _load_workloads, execute_leased, worker_id

    parent = extract(trace_context or {})
    token = attach(parent)
    try:
        with start_span(
            "platform.run.consume",
            kind=SpanKind.CONSUMER,
            attributes={
                "messaging.system": "celery",
                "messaging.operation.name": "process",
                "platform.run.id": run_id,
                "platform.workload": workload,
            },
        ):
            _load_workloads()
            tenant = _tenant(tenant_id)
            if tenant is None:
                # The tenant was deleted after the pointer was published. Nothing to do,
                # and nothing to retry — returning rather than raising stops Celery
                # redelivering a message that can never succeed.
                logger.warning("tenant %s no longer exists — dropping pointer", tenant_id)
                return {"outcome": "tenant_gone"}

            wid = worker_id()
            leased = leases.acquire_specific(
                tenant, uuid.UUID(run_id), worker_id=wid, lease=leases.DEFAULT_LEASE
            )
            if leased is None:
                # The expected outcome for a duplicate delivery: already leased by
                # someone, or already finished. Not an error.
                logger.info("run %s not available to lease — duplicate or already done", run_id)
                return {"outcome": "not_available", "run_id": run_id}

            return execute_leased(leased, wid=wid)
    finally:
        detach(token)


@celery_app.task(name=TASK_SWEEP)
def sweep() -> dict:
    """Reap expired leases, re-publish stranded runs, and enforce retention.

    Runs on a beat schedule. This is what makes the broker's reliability
    irrelevant to correctness: whatever Redis did or failed to do, a run that is
    ``pending`` in Postgres will be picked up.

    Retention runs here too, because this is the only cross-tenant periodic task
    in the platform and a retention rule that nothing invokes is not a retention
    rule. ``sessions.purge_expired`` had been exactly that since Phase 5 —
    written, tested, and called by nothing, so expired conversations were
    invisible to ``load`` and retained for ever. Adding a second uncalled purge
    beside it would have been careless.
    """
    from platform_core.adapters.local.celery_queue import CeleryJobQueue
    from platform_core.governance import continuous_eval, retention
    from platform_core.ports.job_queue import QueueMessage

    reaped = leases.reap_expired()

    with relay_session(reason="sweeper: find stranded pending runs") as s:
        stranded = s.execute(
            text(
                "WITH ranked AS ("
                "  SELECT r.id, r.tenant_id, r.workload, r.available_at, "
                "    row_number() OVER (PARTITION BY r.tenant_id "
                "                       ORDER BY r.priority DESC, r.available_at, r.created_at) "
                "      AS tenant_position "
                "  FROM run r WHERE r.status = 'pending' AND r.attempt < r.max_attempts "
                "    AND r.available_at <= now() AND r.cancel_requested_at IS NULL "
                "    AND (r.last_enqueued_at IS NULL "
                "      OR (r.attempt = 0 AND r.last_enqueued_at < now() - :age) "
                "      OR (r.attempt > 0 AND r.last_enqueued_at < r.available_at))"
                ") SELECT id, tenant_id, workload FROM ranked "
                "ORDER BY tenant_position, available_at LIMIT 100"
            ),
            {"age": LOST_POINTER_AFTER},
        ).all()

    queue = CeleryJobQueue()
    republished = 0
    for row in stranded:
        try:
            queue.publish(
                QueueMessage(
                    id=f"sweep-{row.id}", run_id=row.id, tenant_id=row.tenant_id,
                    workload=row.workload, payload={}, delivery_count=0,
                    enqueued_at=None, trace_context={},
                )
            )
            with relay_session(reason="sweeper: mark run pointer published") as session:
                session.execute(
                    text("UPDATE run SET last_enqueued_at = now() WHERE id = :id"),
                    {"id": row.id},
                )
            republished += 1
        except Exception:
            logger.exception("sweeper could not republish run %s", row.id)

    # ── retention ─────────────────────────────────────────────────────────
    # Deliberately after the correctness work and individually guarded: a failed
    # purge must not stop leases being reaped, which is what keeps runs moving.
    scheduled_evals = 0
    retention_counts: dict[str, int] = {}
    try:
        scheduled_evals = continuous_eval.schedule_due(limit=100)
    except Exception:
        logger.exception("sweeper could not schedule continuous evaluations")
    try:
        retention_counts = retention.enforce()
    except Exception:
        logger.exception("sweeper could not enforce retention")

    purged_sessions = retention_counts.get("sessions", 0)
    purged_gaps = retention_counts.get("gaps", 0)

    if reaped or republished or scheduled_evals or any(retention_counts.values()):
        logger.warning(
            "sweep: reaped %d expired lease(s), republished %d stranded run(s), "
            "scheduled %d continuous eval(s), retention=%s",
            len(reaped), republished, scheduled_evals, retention_counts,
        )
    return {
        "reaped": len(reaped), "republished": republished,
        "scheduled_evals": scheduled_evals,
        "purged_sessions": purged_sessions, "purged_gaps": purged_gaps,
        "retention": retention_counts,
    }
