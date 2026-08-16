"""The tool approval gate — a live one.

The Azure build's tool registry declares ``side_effect`` and
``requires_approval`` correctly, but HITL was retired on 2026-06-27, so the flag
routes to a disabled gate. Any write-side-effect tool registered there today
executes with no human in the loop. It is latent rather than live only because
no write tools are currently registered — and the durable Postgres checkpointer
built specifically to let that gate survive replica loss is guarding nothing.

Three properties this implementation holds that a flag alone does not:

**Maker cannot be checker.** Enforced by a CHECK constraint in migration 0002,
so no code path can bypass it, plus a capability check that refuses
self-approval independently.

**An approval is bound to exact arguments.** ``arguments_sha256`` is recorded at
request time and re-verified at execution. Approving "call the tool" and
approving "call the tool *with these arguments*" are different acts, and only
the second is safe: without the binding, an approval can be replayed against
arguments the reviewer never saw.

**An approval is single-use and expires.** ``consumed_at`` is set when the call
runs, and ``expires_at`` bounds how long a pending decision can pin a run.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text

from platform_core.api.deps import get_context
from platform_core.db.engine import tenant_session
from platform_core.identity.capabilities import Capability, NotAuthorized, require
from platform_core.identity.principal import RequestContext
from platform_core.observability import audit

router = APIRouter(prefix="/api", tags=["approvals"])


class Decision(BaseModel):
    approved: bool
    note: str | None = Field(default=None, max_length=2000)


@router.get("/approvals")
def list_approvals(
    status: str = "pending", ctx: RequestContext = Depends(get_context)
) -> dict:
    with tenant_session(ctx.tenant) as s:
        rows = s.execute(
            text(
                "SELECT id, run_id, tool_name, side_effect, arguments, status, "
                "requested_by, requested_at, expires_at "
                "FROM tool_approval WHERE status = :st "
                "ORDER BY requested_at DESC LIMIT 100"
            ),
            {"st": status},
        ).all()

    return {
        "approvals": [
            {
                "id": str(r.id),
                "run_id": str(r.run_id),
                "tool_name": r.tool_name,
                "side_effect": r.side_effect,
                "arguments": r.arguments,
                "status": r.status,
                "requested_by": str(r.requested_by),
                "requested_at": r.requested_at.isoformat(),
                "expires_at": r.expires_at.isoformat(),
                # So a reviewer can see, before deciding, that this is their own
                # request and will be refused.
                "is_own_request": r.requested_by == ctx.principal.id,
            }
            for r in rows
        ]
    }


@router.post("/approvals/{approval_id}/decide")
def decide(
    approval_id: uuid.UUID, decision: Decision, ctx: RequestContext = Depends(get_context)
) -> dict:
    """Approve or reject. Refuses self-approval and expired requests."""
    with tenant_session(ctx.tenant) as s:
        row = s.execute(
            text(
                "SELECT id, requested_by, status, expires_at FROM tool_approval "
                "WHERE id = :id"
            ),
            {"id": approval_id},
        ).one_or_none()

        if row is None:
            raise HTTPException(status_code=404, detail="Approval not found.")
        if row.status != "pending":
            raise HTTPException(
                status_code=409, detail=f"Already {row.status}; decisions are final."
            )

        # Checked in the application as well as in the database. The constraint
        # is the guarantee; this is what turns a constraint violation into an
        # intelligible 403 rather than a 500.
        try:
            require(
                ctx.principal,
                Capability.TOOL_APPROVE,
                subject_principal_id=row.requested_by,
            )
        except NotAuthorized as exc:
            audit.record(
                ctx,
                action="tool.approval.decide",
                outcome=audit.Outcome.DENIED,
                resource_type="tool_approval",
                resource_id=str(approval_id),
                detail={"reason": exc.reason},
            )
            raise HTTPException(status_code=403, detail=str(exc)) from None

        updated = s.execute(
            text(
                "UPDATE tool_approval SET status = :st, decided_by = :by, "
                "decided_at = now(), decision_note = :note "
                "WHERE id = :id AND status = 'pending' AND expires_at > now() "
                "RETURNING id, status"
            ),
            {
                "st": "approved" if decision.approved else "rejected",
                "by": ctx.principal.id,
                "note": decision.note,
                "id": approval_id,
            },
        ).one_or_none()

        if updated is not None:
            audit.append_in_session(
                s,
                ctx,
                action="tool.approval.decide",
                outcome=audit.Outcome.SUCCEEDED,
                resource_type="tool_approval",
                resource_id=str(approval_id),
                detail={"decision": updated.status},
            )

    if updated is None:
        # The conditional UPDATE also covers expiry, so a request that lapsed
        # between the read and the write is refused rather than silently decided.
        raise HTTPException(status_code=409, detail="Approval expired or already decided.")

    return {"id": str(updated.id), "status": updated.status}
