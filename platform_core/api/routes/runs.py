"""Runs: the unit of work, and the idempotency anchor.

Note what is absent from every query below: a ``WHERE tenant_id = …`` clause.
That is deliberate and is the point of the architecture. These handlers run
inside ``tenant_session``, so Postgres applies the boundary. A handler that
forgets a filter returns nothing rather than everything — the failure mode of
forgetting is an empty result, not a breach.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import text

from platform_core.api.deps import get_context
from platform_core.db.engine import tenant_session
from platform_core.identity.principal import RequestContext
from platform_core.observability import audit

router = APIRouter(prefix="/api", tags=["runs"])


def _serialize(row) -> dict:
    return {
        "id": str(row.id),
        "workload": row.workload,
        "status": row.status,
        "attempt": row.attempt,
        "available_at": row.available_at.isoformat(),
        "cancel_requested_at": (
            row.cancel_requested_at.isoformat() if row.cancel_requested_at else None
        ),
        "created_at": row.created_at.isoformat(),
        "started_at": row.started_at.isoformat() if row.started_at else None,
        "finished_at": row.finished_at.isoformat() if row.finished_at else None,
        "error": row.error,
        "release": row.release,
    }


@router.get("/runs")
def list_runs(
    request: Request,
    ctx: Annotated[RequestContext, Depends(get_context)],
    limit: int = 50,
) -> dict:
    with tenant_session(ctx.tenant) as s:
        rows = s.execute(
            text(
                "SELECT id, workload, status, attempt, available_at, cancel_requested_at, "
                "created_at, started_at, finished_at, error, release FROM run "
                "ORDER BY created_at DESC LIMIT :limit"
            ),
            {"limit": min(max(limit, 1), 200)},
        ).all()
    return {"runs": [_serialize(r) for r in rows], "tenant": ctx.tenant.slug}


@router.get("/runs/{run_id}")
def get_run(
    run_id: uuid.UUID,
    ctx: Annotated[RequestContext, Depends(get_context)],
) -> dict:
    with tenant_session(ctx.tenant) as s:
        row = s.execute(
            text(
                "SELECT id, workload, status, attempt, available_at, cancel_requested_at, "
                "created_at, started_at, finished_at, error, release FROM run WHERE id = :id"
            ),
            {"id": run_id},
        ).one_or_none()

    if row is None:
        # 404 whether the run is absent or belongs to another tenant. Returning
        # 403 for the latter would confirm that the id exists somewhere, which
        # is an existence oracle across the boundary.
        raise HTTPException(status_code=404, detail="Run not found.")
    return _serialize(row)


@router.post("/runs/{run_id}/cancel")
def cancel_run(
    run_id: uuid.UUID,
    ctx: Annotated[RequestContext, Depends(get_context)],
) -> dict:
    with tenant_session(ctx.tenant) as s:
        # Pending work can terminate immediately. A live lease receives a
        # cancellation request which its holder acknowledges at a safe boundary;
        # changing its status underneath the worker would skip cleanup and
        # confuse a lost lease with a requested stop.
        updated = s.execute(
            text(
                "UPDATE run SET cancel_requested_at = coalesce(cancel_requested_at, now()), "
                "cancel_requested_by = coalesce(cancel_requested_by, :by), "
                "status = CASE WHEN status = 'pending' THEN 'cancelled' ELSE status END, "
                "finished_at = CASE WHEN status = 'pending' THEN now() ELSE finished_at END "
                "WHERE id = :id AND status IN ('pending','leased','cancelled') "
                "RETURNING id, status, cancel_requested_at"
            ),
            {"id": run_id, "by": ctx.principal.id},
        ).one_or_none()

    if updated is None:
        raise HTTPException(status_code=404, detail="Run not found, or already finished.")
    outcome = "cancelled" if updated.status == "cancelled" else "cancellation_requested"
    audit.record(
        ctx,
        action="run.cancel",
        outcome=audit.Outcome.SUCCEEDED,
        resource_type="run",
        resource_id=str(updated.id),
        detail={"status": outcome},
    )
    return {
        "id": str(updated.id),
        "status": outcome,
        "cancel_requested_at": updated.cancel_requested_at.isoformat(),
    }
