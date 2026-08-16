"""Cooperative cancellation for durable runs.

Cancellation is a request while work is leased, not an out-of-band mutation of
the final status. Workloads call :func:`cancellation_point` at effect, model,
and batch boundaries; the lease holder is the only process allowed to
acknowledge the request as ``cancelled``.
"""

from __future__ import annotations

import time

from sqlalchemy import text

from platform_core.db.engine import tenant_session
from platform_core.identity.principal import RequestContext


class RunCancelled(RuntimeError):
    """The current durable run has a cancellation request."""


def cancellation_point(ctx: RequestContext) -> None:
    """Raise when the current run was cancelled; no-op for synchronous requests."""
    if ctx.run_id is None:
        return
    with tenant_session(ctx.tenant) as session:
        row = session.execute(
            text("SELECT status, cancel_requested_at FROM run WHERE id = :id"),
            {"id": ctx.run_id},
        ).one_or_none()
    if row is not None and (row.status == "cancelled" or row.cancel_requested_at is not None):
        raise RunCancelled(f"run {ctx.run_id} was cancelled")


def interruptible_sleep(ctx: RequestContext, seconds: float, *, quantum: float = 1.0) -> None:
    """Sleep for retry backoff while observing cancellation at least once a second."""
    deadline = time.monotonic() + max(0.0, seconds)
    while True:
        cancellation_point(ctx)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(max(0.05, quantum), remaining))
