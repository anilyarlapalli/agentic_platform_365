"""Leases: work is held, not claimed, and a holder that dies loses its grip.

## Why a lease rather than a claim

The Azure build's ``jobs.claim`` is one conditional UPDATE, ``queued → running``.
That correctly stops two workers starting the same job. What it lacks is an
expiry, and the gap is observable: a worker that dies mid-run leaves the row in
``running`` forever, the redelivered message finds the job un-claimable, the
handler returns normally, and the message is deleted. Nothing retries it.

A lease has a deadline. The holder extends it by heartbeating; if it stops, the
lease expires and the reaper returns the work to ``pending``. Because the reaper
only ever acts on an **expired** lease, there is no moment at which two live
workers hold the same run.

## The fencing problem

A lease alone is not enough. A worker can be stalled — a long GC pause, a
network partition, a suspended container — past its deadline, have the run
reaped and taken by a second worker, then wake up and finish, applying its
effects on top of the new holder's. The lease expired; the process did not know.

Two defences, both here:

**Heartbeat returns a boolean.** :func:`heartbeat` reports whether the lease is
still held. A worker that ignores a false return and continues is the one way
two workers can both apply an effect, so long-running steps must check it.

**Completion is fenced.** :func:`complete` and :func:`fail` update only when
``leased_by`` still matches, so a woken zombie's write is rejected rather than
silently overwriting the new holder's outcome.
"""

from __future__ import annotations

import logging
import random
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text

from platform_core.db.engine import relay_session, tenant_session
from platform_core.identity.principal import Tenant
from platform_core.observability.telemetry import record_run_recovery
from platform_core.settings import get_settings

logger = logging.getLogger("platform.correctness.leases")

DEFAULT_LEASE = timedelta(seconds=60)
# Heartbeat well inside the lease so a single slow beat does not lose it. A
# ratio rather than a constant, so tuning the lease tunes the beat with it.
HEARTBEAT_RATIO = 0.3


def retry_delay(attempt: int, *, retry_after: timedelta | None = None) -> timedelta:
    """Bounded exponential backoff with jitter, never below provider guidance."""
    settings = get_settings()
    exponential = min(
        settings.run_retry_max_seconds,
        settings.run_retry_base_seconds * (2 ** max(0, attempt - 1)),
    )
    floor = retry_after.total_seconds() if retry_after is not None else 0.0
    base = max(exponential, floor)
    jitter = base * settings.run_retry_jitter_ratio * random.random()
    return timedelta(seconds=min(settings.run_retry_max_seconds, base + jitter))


@dataclass(frozen=True, slots=True)
class LeasedRun:
    run_id: uuid.UUID
    tenant: Tenant
    workload: str
    input: dict[str, Any]
    attempt: int
    max_attempts: int
    leased_by: str
    lease_expires_at: datetime

    @property
    def heartbeat_interval(self) -> timedelta:
        remaining = self.lease_expires_at - datetime.now(UTC)
        return max(timedelta(seconds=1), remaining * HEARTBEAT_RATIO)


def acquire(
    tenant: Tenant,
    *,
    worker_id: str,
    workload: str | None = None,
    lease: timedelta = DEFAULT_LEASE,
) -> LeasedRun | None:
    """Lease the next pending run for a tenant. None when there is nothing.

    ``FOR UPDATE SKIP LOCKED`` is what makes this safe under concurrency: two
    workers issuing this simultaneously take *different* rows rather than
    blocking on each other or both reading the same one. Without SKIP LOCKED,
    N workers serialise behind one lock and the queue drains at single-worker
    speed.
    """
    with tenant_session(tenant) as s:
        row = s.execute(
            text(
                "WITH next_run AS ("
                "  SELECT id FROM run"
                "  WHERE status = 'pending' AND available_at <= now()"
                "    AND cancel_requested_at IS NULL"
                # CAST(...) rather than `:workload::text`: SQLAlchemy's bind
                # syntax claims the first colon of a `::` cast, so the parameter
                # name comes out as `workload:` and Postgres sees a syntax error.
                "    AND (CAST(:workload AS text) IS NULL OR workload = :workload)"
                "    AND attempt < max_attempts"
                "  ORDER BY priority DESC, available_at, created_at"
                "  FOR UPDATE SKIP LOCKED"
                "  LIMIT 1"
                ") "
                "UPDATE run SET status = 'leased', leased_by = :worker, "
                "  lease_expires_at = now() + :lease, last_heartbeat_at = now(), "
                "  started_at = coalesce(started_at, now()), attempt = attempt + 1 "
                "FROM next_run WHERE run.id = next_run.id "
                "RETURNING run.id, run.workload, run.input, run.attempt, "
                "          run.max_attempts, run.lease_expires_at"
            ),
            {"workload": workload, "worker": worker_id, "lease": lease},
        ).one_or_none()

    if row is None:
        return None
    return LeasedRun(
        run_id=row.id, tenant=tenant, workload=row.workload, input=row.input,
        attempt=row.attempt, max_attempts=row.max_attempts, leased_by=worker_id,
        lease_expires_at=row.lease_expires_at,
    )


def acquire_specific(
    tenant: Tenant, run_id: uuid.UUID, *, worker_id: str, lease: timedelta = DEFAULT_LEASE
) -> LeasedRun | None:
    """Lease one named run. None when it is not available.

    Used on the Celery path, where the message names the run it is about. None
    is the normal outcome for a duplicate delivery — the run is already leased,
    or already finished — so callers treat it as "nothing to do", not an error.

    Deliberately still conditional on ``status = 'pending'``: the message is a
    hint that work exists, never an authorisation to execute. Trusting the
    message would let a redelivery start a second execution of a run another
    worker currently holds.
    """
    with tenant_session(tenant) as s:
        row = s.execute(
            text(
                "UPDATE run SET status = 'leased', leased_by = :worker, "
                "  lease_expires_at = now() + :lease, last_heartbeat_at = now(), "
                "  started_at = coalesce(started_at, now()), attempt = attempt + 1 "
                "WHERE id = :id AND status = 'pending' AND attempt < max_attempts "
                "  AND available_at <= now() AND cancel_requested_at IS NULL "
                "RETURNING id, workload, input, attempt, max_attempts, lease_expires_at"
            ),
            {"worker": worker_id, "lease": lease, "id": run_id},
        ).one_or_none()

    if row is None:
        return None
    return LeasedRun(
        run_id=row.id, tenant=tenant, workload=row.workload, input=row.input,
        attempt=row.attempt, max_attempts=row.max_attempts, leased_by=worker_id,
        lease_expires_at=row.lease_expires_at,
    )


def heartbeat(run: LeasedRun, *, extend: timedelta = DEFAULT_LEASE) -> bool:
    """Extend the lease. **False means it was lost — stop working now.**

    A worker that ignores this and finishes anyway can apply effects on top of
    whoever took the run after it was reaped. Returning a bool rather than
    raising keeps it cheap enough to call inside a loop, which is the only way
    a long step actually checks it.
    """
    with tenant_session(run.tenant) as s:
        held = s.execute(
            text(
                "UPDATE run SET lease_expires_at = now() + :extend, "
                "last_heartbeat_at = now() "
                "WHERE id = :id AND status = 'leased' AND leased_by = :worker "
                "  AND cancel_requested_at IS NULL "
                "RETURNING id"
            ),
            {"extend": extend, "id": run.run_id, "worker": run.leased_by},
        ).scalar_one_or_none()

    if held is None:
        logger.warning(
            "run %s: lease lost by %s — it was reaped or taken over. Stop working.",
            run.run_id, run.leased_by,
        )
    return held is not None


def complete(run: LeasedRun, *, result: dict | None = None) -> bool:
    """Mark succeeded. Fenced on the lease holder.

    False means this worker no longer held the lease and the write was rejected
    — a stalled worker waking up after its run was reaped must not overwrite the
    new holder's outcome.
    """
    with tenant_session(run.tenant) as s:
        import json

        updated = s.execute(
            text(
                "UPDATE run SET status = 'succeeded', finished_at = now(), "
                "result = :result, leased_by = NULL, lease_expires_at = NULL "
                "WHERE id = :id AND status = 'leased' AND leased_by = :worker "
                "  AND cancel_requested_at IS NULL "
                "RETURNING id"
            ),
            {"result": json.dumps(result or {}), "id": run.run_id, "worker": run.leased_by},
        ).scalar_one_or_none()

    if updated is None:
        logger.error(
            "run %s: %s tried to complete a lease it no longer holds — rejected",
            run.run_id, run.leased_by,
        )
    return updated is not None


def fail(
    run: LeasedRun,
    *,
    error: str,
    retryable: bool,
    retry_after: timedelta | None = None,
) -> bool:
    """Release the run as failed, or return it for another attempt.

    ``retryable`` comes from the error **type**, not a guess — see
    ``platform_core.ports.errors``. The Azure worker collapses this distinction:
    it catches everything, records a permanent failure, and deletes the message,
    so a Storage blip and a malformed PDF are indistinguishable afterwards.
    """
    terminal = (not retryable) or run.attempt >= run.max_attempts
    delay = retry_delay(run.attempt, retry_after=retry_after) if not terminal else timedelta(0)
    with tenant_session(run.tenant) as s:
        updated = s.execute(
            text(
                "UPDATE run SET "
                "  status = CASE WHEN cancel_requested_at IS NOT NULL THEN 'cancelled' "
                "                WHEN :terminal THEN 'failed' ELSE 'pending' END, "
                "  error = :error, "
                "  finished_at = CASE WHEN :terminal OR cancel_requested_at IS NOT NULL "
                "                     THEN now() ELSE NULL END, "
                "  available_at = CASE WHEN :terminal OR cancel_requested_at IS NOT NULL "
                "                      THEN available_at ELSE now() + :delay END, "
                "  leased_by = NULL, lease_expires_at = NULL "
                "WHERE id = :id AND status = 'leased' AND leased_by = :worker "
                "RETURNING id"
            ),
            {
                "error": error[:4000], "terminal": terminal,
                "delay": delay, "id": run.run_id, "worker": run.leased_by,
            },
        ).scalar_one_or_none()
    return updated is not None


def acknowledge_cancellation(run: LeasedRun) -> bool:
    """Fence the terminal cancellation write to the current lease holder."""
    with tenant_session(run.tenant) as session:
        updated = session.execute(
            text(
                "UPDATE run SET status = 'cancelled', finished_at = now(), "
                "leased_by = NULL, lease_expires_at = NULL "
                "WHERE id = :id AND status = 'leased' AND leased_by = :worker "
                "AND cancel_requested_at IS NOT NULL RETURNING id"
            ),
            {"id": run.run_id, "worker": run.leased_by},
        ).scalar_one_or_none()
    return updated is not None


def reap_expired(*, grace: timedelta = timedelta(seconds=0)) -> list[dict]:
    """Return expired leases to ``pending``. Cross-tenant; relay credential only.

    Only ever touches leases that are **already expired**, so it can never race
    a live holder: by the time a run is a candidate, its holder has failed to
    heartbeat for longer than the whole lease duration.

    A run that has exhausted its attempts goes to ``failed`` rather than back to
    ``pending``, so a job that reliably kills its worker cannot loop forever —
    the poison-message case, handled by attempt count rather than by a delivery
    count the queue happens to expose.
    """
    settings = get_settings()
    with relay_session(reason="reaper: return expired leases to pending") as s:
        rows = s.execute(
            text(
                "UPDATE run SET "
                "  status = CASE WHEN cancel_requested_at IS NOT NULL THEN 'cancelled' "
                "                WHEN attempt >= max_attempts THEN 'failed' ELSE 'pending' END, "
                "  error = CASE WHEN cancel_requested_at IS NOT NULL THEN error "
                "               WHEN attempt >= max_attempts "
                "          THEN 'lease expired and max attempts reached' ELSE error END, "
                "  finished_at = CASE WHEN cancel_requested_at IS NOT NULL "
                "                          OR attempt >= max_attempts THEN now() ELSE NULL END, "
                "  available_at = CASE WHEN cancel_requested_at IS NOT NULL "
                "                          OR attempt >= max_attempts THEN available_at "
                "                      ELSE now() + "
                "                          LEAST(:retry_max, "
                "                            :retry_base * power(2, GREATEST(attempt - 1, 0)) "
                "                            * (1 + :retry_jitter * random())) "
                "                          * interval '1 second' END, "
                "  leased_by = NULL, lease_expires_at = NULL "
                "WHERE status = 'leased' AND lease_expires_at < now() - :grace "
                "RETURNING id, tenant_id, workload, attempt, max_attempts, status"
            ),
            {
                "grace": grace,
                "retry_base": settings.run_retry_base_seconds,
                "retry_max": settings.run_retry_max_seconds,
                "retry_jitter": settings.run_retry_jitter_ratio,
            },
        ).all()

        # Release the step claims held by the attempt that just died.
        #
        # A run lease and a side-effect claim are two grips held by the same
        # process, with independent deadlines: the lease might be 60s while the
        # step claim is 5 minutes, because a step can legitimately run longer
        # than one heartbeat interval. So a crashed worker's lease expires and
        # the run returns to `pending` while its step claims are still live —
        # and the next worker is refused with EffectAlreadyRunning by a claimant
        # that no longer exists. Recovery deadlocks until the longer TTL lapses.
        #
        # The reaper is the one recovery mechanism, so it releases both. This
        # cannot touch a *live* worker's claims: a run is only a candidate here
        # once its lease has already expired, which means its holder stopped
        # heartbeating for longer than the entire lease.
        if rows:
            s.execute(
                text(
                    "UPDATE side_effect SET claim_expires_at = NULL "
                    "WHERE run_id = ANY(:ids) AND status = 'started'"
                ),
                {"ids": [r.id for r in rows]},
            )

    reaped = [
        {
            "run_id": str(r.id), "tenant_id": str(r.tenant_id), "workload": r.workload,
            "attempt": r.attempt, "outcome": r.status,
        }
        for r in rows
    ]
    for outcome, count in Counter(r.status for r in rows).items():
        record_run_recovery(outcome, count)
    if reaped:
        # Worth alerting on: a non-zero steady state means workers are dying
        # mid-run, which no amount of successful retries makes acceptable.
        logger.warning("reaped %d expired lease(s): %s", len(reaped), reaped)
    return reaped
