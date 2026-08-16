"""The ``JobQueue`` port over Celery/Redis + Postgres leases.

One adapter, two mechanisms, because the port describes two different things
that callers want together:

* **Delivery** — Celery. ``publish`` sends a pointer.
* **Holding** — Postgres. ``acquire``/``heartbeat``/``complete``/``fail``/
  ``reap_expired`` are lease operations, delegated to
  :mod:`platform_core.correctness.leases`.

Splitting them across two ports would push the composition into every caller,
and every caller would compose it slightly differently. Keeping the split
*inside* the adapter is what lets the broker be swapped — for Azure Service Bus,
for a Postgres-only queue — without any caller learning that leases and delivery
were ever separate concerns.

``publish`` is relay-only by contract. Application code writes an outbox row in
its own transaction and never touches this.
"""

from __future__ import annotations

import logging
import uuid
from datetime import timedelta

from platform_core.adapters.local.celery_app import TASK_RUN, celery_app
from platform_core.correctness import leases
from platform_core.identity.principal import Tenant
from platform_core.ports.errors import TransientError
from platform_core.ports.job_queue import QueueMessage

logger = logging.getLogger("platform.adapters.celery_queue")


class CeleryJobQueue:
    """Implements :class:`platform_core.ports.job_queue.JobQueue`."""

    def __init__(self, *, worker_id: str | None = None) -> None:
        self._worker_id = worker_id

    # ── delivery ──────────────────────────────────────────────────────────

    def publish(self, message: QueueMessage) -> None:
        """Send a pointer. Relay-only.

        Raises :class:`TransientError` on a broker failure so the relay leaves
        the outbox row unpublished and retries it — the row is the record of
        intent, and a broker that is briefly down must not be able to destroy
        that record.
        """
        try:
            celery_app.send_task(
                TASK_RUN,
                kwargs={
                    "tenant_id": str(message.tenant_id),
                    "run_id": str(message.run_id),
                    "workload": message.workload,
                    "trace_context": message.trace_context,
                },
                # Deduplicate at the broker where it is cheap. Not relied upon:
                # the lease is what actually prevents double execution, and this
                # only reduces pointless deliveries.
                task_id=f"run-{message.run_id}",
                queue="runs",
            )
        except Exception as exc:
            raise TransientError(
                f"could not publish run {message.run_id} to the broker", cause=exc
            ) from exc

    # ── holding ───────────────────────────────────────────────────────────

    def acquire(
        self, *, worker_id: str, lease_duration: timedelta, workloads: list[str] | None = None
    ):
        raise NotImplementedError(
            "acquire() without a tenant is not meaningful here: leases are "
            "tenant-scoped rows and a cross-tenant scan would need the relay "
            "credential. Use acquire_for_tenant()."
        )

    def acquire_for_tenant(
        self, tenant: Tenant, *, worker_id: str, lease_duration: timedelta,
        run_id: uuid.UUID | None = None,
    ) -> leases.LeasedRun | None:
        if run_id is not None:
            return leases.acquire_specific(
                tenant, run_id, worker_id=worker_id, lease=lease_duration
            )
        return leases.acquire(tenant, worker_id=worker_id, lease=lease_duration)

    def heartbeat(self, message, *, worker_id: str, extend_by: timedelta) -> bool:
        return leases.heartbeat(message, extend=extend_by)

    def complete(self, message, *, worker_id: str) -> None:
        leases.complete(message)

    def fail(self, message, *, worker_id: str, error: str, retryable: bool,
             retry_after: timedelta | None = None) -> None:
        leases.fail(
            message,
            error=error,
            retryable=retryable,
            retry_after=retry_after,
        )

    def reap_expired(self, *, older_than: timedelta = timedelta(0)) -> int:
        return len(leases.reap_expired(grace=older_than))

    # ── observation ───────────────────────────────────────────────────────

    def depth(self, workload: str | None = None) -> tuple[int, float]:
        """Pending count and the age of the oldest pending run, from Postgres.

        Read from the run table rather than from the broker on purpose. The
        broker's queue length says how many *pointers* are undelivered, which is
        not the same as how much work is outstanding — a delivered-but-unleased
        run is invisible to it. The run table is the truth.
        """
        from sqlalchemy import text

        from platform_core.db.engine import relay_session

        with relay_session(reason="queue depth probe") as s:
            row = s.execute(
                text(
                    "SELECT count(*) AS n, "
                    "  coalesce(extract(epoch from (now() - min(created_at))), 0) AS age "
                    "FROM run WHERE status = 'pending' AND available_at <= now() "
                    "  AND cancel_requested_at IS NULL "
                    "  AND (CAST(:w AS text) IS NULL OR workload = :w)"
                ),
                {"w": workload},
            ).one()
        return int(row.n), float(row.age)
