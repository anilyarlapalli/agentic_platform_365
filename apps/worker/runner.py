"""The worker loop: lease a run, execute it, release the lease.

The whole point of this file is what happens when it *stops* mid-way, so it is
written to be killed. Every decision below is about what state a SIGKILL at that
line leaves behind.

Ordering, and why:

1. **Lease before working.** A crash before the lease leaves the run ``pending``
   — nothing to recover, the next worker takes it.
2. **Heartbeat while working.** A crash stops the heartbeat, the lease expires,
   the reaper returns the run to ``pending``. Recovery is the absence of an
   action, which is the only kind that survives a process that stops existing.
3. **Complete under a fence.** ``complete()`` updates only if this worker still
   holds the lease, so a stalled worker waking after its run was reaped cannot
   overwrite the new holder's outcome.

Contrast with the Azure worker, which publishes artifacts and then marks the job
finished: a crash between those leaves the artifacts live and the job ``running``
forever, with the redelivered message deleted because ``claim()`` returns False.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import signal
import socket
import threading
import time
import uuid
from collections.abc import Callable
from datetime import timedelta
from typing import Any

from platform_core.correctness import leases
from platform_core.correctness.cancellation import RunCancelled, cancellation_point
from platform_core.correctness.crash_points import maybe_crash
from platform_core.correctness.side_effects import NeedsReconciliation
from platform_core.identity.principal import (
    ActorType,
    Principal,
    RequestContext,
    Role,
    Tenant,
)
from platform_core.observability.telemetry import (
    bind_request_context,
    configure_telemetry,
    pseudonym,
    record_run_outcome,
    shutdown_telemetry,
    start_span,
)
from platform_core.ports.errors import TransientError
from platform_core.settings import require_coherent_settings

logger = logging.getLogger("platform.worker")

WORKLOADS: dict[str, Callable[[RequestContext, dict], dict]] = {}


def register(name: str, handler: Callable[[RequestContext, dict], dict]) -> None:
    WORKLOADS[name] = handler


def _load_workloads() -> None:
    from workloads.echo import workload as echo
    from workloads.eval import workload as evaluation
    from workloads.onboarding import workload as onboarding
    from workloads.reindex import workload as reindex

    register(echo.WORKLOAD, echo.run)
    register(evaluation.WORKLOAD, evaluation.run)
    register(onboarding.WORKLOAD, onboarding.run)
    register(reindex.WORKLOAD, reindex.run)


def service_principal(tenant: Tenant) -> Principal:
    """The tenant's worker identity — a real row, not a synthetic id.

    A worker acts **for** a tenant, not **as** the human who queued the work:
    "alice ingested" and "the worker ingested for alice's tenant" are different
    events, and only one of them is something alice did. So the worker needs its
    own principal.

    It has to be a real row rather than a placeholder uuid. The first version
    used ``UUID(int=0)``, which violated the foreign key on
    ``document.uploaded_by`` the moment a workload wrote anything attributed to
    it — a synthetic identity is fine right up until something tries to
    reference it, which is the whole purpose of an identity.

    One service principal per tenant, created on first use and reused
    thereafter, so audit rows join to something that exists.
    """
    from sqlalchemy import text as sa_text

    from platform_core.db.engine import tenant_session as scoped

    with scoped(tenant) as s:
        principal_id = s.execute(
            sa_text(
                "INSERT INTO principal (tenant_id, subject, actor_type, roles) "
                "VALUES (:t, 'service:worker', 'service', ARRAY['service']) "
                "ON CONFLICT (tenant_id, subject) DO UPDATE "
                "  SET actor_type = 'service' "
                "RETURNING id"
            ),
            {"t": tenant.id},
        ).scalar_one()

    return Principal(
        id=principal_id, tenant=tenant, subject="service:worker",
        roles=frozenset({Role.SERVICE}), actor_type=ActorType.SERVICE,
    )


def worker_id() -> str:
    """Identifies the lease holder. Unique per process, not per host.

    Two workers on one host must not share an identity, or one could complete
    the other's lease — the fence checks ``leased_by``, so an identity collision
    silently defeats it.
    """
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


class Heartbeater:
    """Extends the lease on a background thread while the run executes.

    A daemon thread, so it cannot keep a dying process alive. It sets
    :attr:`lost` when the lease is gone; the workload loop is expected to check
    it, and :meth:`raise_if_lost` makes that a one-liner at each step boundary.
    """

    def __init__(self, run: leases.LeasedRun, *, lease: timedelta) -> None:
        self._run = run
        self._lease = lease
        self._stop = threading.Event()
        self.lost = False
        self._thread = threading.Thread(
            target=self._loop, name=f"heartbeat-{run.run_id}", daemon=True
        )

    def _loop(self) -> None:
        interval = max(1.0, self._lease.total_seconds() * leases.HEARTBEAT_RATIO)
        while not self._stop.wait(interval):
            if not leases.heartbeat(self._run, extend=self._lease):
                self.lost = True
                return

    def __enter__(self) -> Heartbeater:
        self._thread.start()
        return self

    def __exit__(self, *_exc) -> None:
        self._stop.set()

    def raise_if_lost(self) -> None:
        if self.lost:
            raise TransientError(
                f"lease on run {self._run.run_id} was lost mid-execution; "
                f"another worker may now own it"
            )


def execute_one(
    tenant: Tenant,
    *,
    wid: str | None = None,
    lease: timedelta = leases.DEFAULT_LEASE,
) -> dict[str, Any] | None:
    """Lease and execute the next available run. None when there is nothing.

    The polling entry point. The Celery path uses
    :func:`~apps.worker.tasks.execute_run`, which leases a *named* run and then
    calls :func:`execute_leased`. Both share the body below, because two copies
    of "what to do with a leased run" would drift and only one of them would
    keep the fencing right.
    """
    wid = wid or worker_id()
    run = leases.acquire(tenant, worker_id=wid, lease=lease)
    if run is None:
        return None
    return execute_leased(run, wid=wid, lease=lease)


def execute_leased(
    run: leases.LeasedRun,
    *,
    wid: str,
    lease: timedelta = leases.DEFAULT_LEASE,
) -> dict[str, Any]:
    """Execute a run this worker already holds the lease on.

    Returns a summary rather than raising on workload failure: a failed run is a
    recorded outcome, not an exception for the caller to handle. Raising here
    would also make Celery redeliver a message whose run is already recorded
    failed, which is a retry loop with nothing to gain.
    """
    with start_span(
        "platform.run.execute",
        attributes={
            "platform.run.id": str(run.run_id),
            "platform.tenant.id": pseudonym(run.tenant.id),
            "platform.workload": run.workload,
            "platform.run.attempt": run.attempt,
        },
    ) as span:
        result = _execute_leased_body(run, wid=wid, lease=lease)
        outcome = str(result.get("outcome", "unknown"))
        span.set_attribute("platform.outcome", outcome)
        record_run_outcome(run.workload, outcome)
        return result


def _execute_leased_body(
    run: leases.LeasedRun,
    *,
    wid: str,
    lease: timedelta,
) -> dict[str, Any]:
    tenant = run.tenant
    logger.info("leased run %s (%s, attempt %d/%d)",
                run.run_id, run.workload, run.attempt, run.max_attempts)
    maybe_crash("after:lease")

    handler = WORKLOADS.get(run.workload)
    if handler is None:
        leases.fail(run, error=f"no handler for workload {run.workload!r}", retryable=False)
        return {"run_id": str(run.run_id), "outcome": "no_handler"}

    ctx = RequestContext(
        principal=service_principal(tenant),
        run_id=run.run_id,
        labels={"workload": run.workload, "task": run.workload},
    )
    bind_request_context(ctx)
    from platform_core.adapters.postgres.checkpoint import get_checkpoint_store

    checkpoints = get_checkpoint_store()
    thread_id = f"run:{run.run_id}"
    checkpoints.append(
        ctx,
        thread_id,
        {
            "phase": "run_started",
            "workload": run.workload,
            "attempt": run.attempt,
        },
    )

    with Heartbeater(run, lease=lease) as beat:
        try:
            cancellation_point(ctx)
            result = handler(ctx, run.input)
            # Close the race where cancellation arrives after the handler's
            # final internal checkpoint but before its result is committed.
            cancellation_point(ctx)
            beat.raise_if_lost()
        except RunCancelled:
            logger.info("run %s acknowledged cancellation", run.run_id)
            try:
                checkpoints.append(ctx, thread_id, {"phase": "cancelled"})
            finally:
                leases.acknowledge_cancellation(run)
            return {"run_id": str(run.run_id), "outcome": "cancelled"}
        except NeedsReconciliation as exc:
            # Terminal on purpose. A step that is not safe to repeat and did not
            # complete needs a human or a reconciler, and retrying would be the
            # exact double-application the policy exists to prevent.
            logger.error("run %s needs reconciliation: %s", run.run_id, exc)
            try:
                checkpoints.append(
                    ctx,
                    thread_id,
                    {"phase": "needs_reconciliation", "step": exc.step},
                    awaiting="reconciliation",
                )
            finally:
                leases.fail(run, error=str(exc), retryable=False)
            return {"run_id": str(run.run_id), "outcome": "needs_reconciliation",
                    "step": exc.step}
        except TransientError as exc:
            logger.warning("run %s transient failure: %s", run.run_id, exc)
            try:
                checkpoints.append(
                    ctx,
                    thread_id,
                    {"phase": "retry_pending", "error_type": type(exc).__name__},
                    awaiting="retry",
                )
            finally:
                retry_after = (
                    timedelta(seconds=exc.retry_after_s)
                    if exc.retry_after_s is not None
                    else None
                )
                leases.fail(
                    run,
                    error=str(exc),
                    retryable=True,
                    retry_after=retry_after,
                )
            return {"run_id": str(run.run_id), "outcome": "retry"}
        except Exception as exc:
            # Deterministic failure. Retrying a malformed input just burns the
            # attempt budget, so this is terminal — the distinction the Azure
            # worker collapses by catching everything and never re-raising.
            logger.exception("run %s failed", run.run_id)
            try:
                checkpoints.append(
                    ctx,
                    thread_id,
                    {"phase": "failed", "error_type": type(exc).__name__},
                )
            finally:
                leases.fail(run, error=f"{type(exc).__name__}: {exc}", retryable=False)
            return {"run_id": str(run.run_id), "outcome": "failed"}

    maybe_crash("before:complete")
    checkpoints.append(
        ctx,
        thread_id,
        {
            "phase": "handler_completed",
            "result_sha256": hashlib.sha256(
                json.dumps(result, sort_keys=True, default=str).encode("utf-8")
            ).hexdigest(),
        },
    )
    if not leases.complete(run, result=result):
        # The fence rejected the write: this worker no longer held the lease.
        # The run belongs to someone else now and its outcome is theirs.
        return {"run_id": str(run.run_id), "outcome": "lease_lost"}
    checkpoints.append(ctx, thread_id, {"phase": "run_succeeded"})
    maybe_crash("after:complete")

    return {"run_id": str(run.run_id), "outcome": "succeeded", "result": result}


def main() -> int:
    """Poll loop for a long-running worker process."""
    settings = require_coherent_settings()
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    configure_telemetry(settings)
    _load_workloads()

    stopping = threading.Event()

    def _sigterm(*_a):
        logger.info("SIGTERM — finishing the current run then exiting")
        stopping.set()

    signal.signal(signal.SIGTERM, _sigterm)

    from sqlalchemy import text

    from platform_core.db.engine import relay_session

    wid = worker_id()
    logger.info("worker %s started, workloads=%s", wid, sorted(WORKLOADS))

    while not stopping.is_set():
        # Tenants with pending work. Read under the relay credential because a
        # worker serves every tenant; it then does the work inside each tenant's
        # own scoped session.
        with relay_session(reason="worker: find tenants with pending runs") as s:
            tenant_rows = s.execute(
                text(
                    "SELECT DISTINCT r.tenant_id, t.slug FROM run r "
                    "JOIN tenant t ON t.id = r.tenant_id "
                    "WHERE r.status = 'pending' AND r.attempt < r.max_attempts "
                    "  AND r.available_at <= now() AND r.cancel_requested_at IS NULL "
                    "ORDER BY r.tenant_id "
                    "LIMIT 50"
                )
            ).all()

        did_work = False
        for row in tenant_rows:
            if stopping.is_set():
                break
            tenant = Tenant(id=row.tenant_id, slug=row.slug)
            # One run per tenant per pass. Draining tenant A completely before
            # touching tenant B lets one bulk import starve every interactive
            # tenant behind it.
            if execute_one(tenant, wid=wid) is not None:
                did_work = True

        if not did_work:
            time.sleep(settings.worker_poll_interval_seconds)

    logger.info("worker %s stopped", wid)
    shutdown_telemetry()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
