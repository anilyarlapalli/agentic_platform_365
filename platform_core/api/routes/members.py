"""Membership and capability grants — the finer-than-a-role authority."""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text

from platform_core.api.deps import get_context
from platform_core.db.engine import tenant_session
from platform_core.identity.capabilities import Capability, NotAuthorized, grant, revoke
from platform_core.identity.principal import RequestContext
from platform_core.observability import audit

router = APIRouter(prefix="/api", tags=["members"])


class GrantRequest(BaseModel):
    principal_id: uuid.UUID
    capability: Capability
    resource: str = Field(default="*", max_length=200)
    expires_at: datetime | None = None


@router.get("/members")
def list_members(ctx: RequestContext = Depends(get_context)) -> dict:
    with tenant_session(ctx.tenant) as s:
        rows = s.execute(
            text(
                "SELECT id, subject, roles, actor_type, created_at, disabled_at "
                "FROM principal ORDER BY created_at LIMIT 200"
            )
        ).all()
        grants = s.execute(
            text(
                "SELECT id, principal_id, capability, resource, expires_at "
                "FROM capability_grant WHERE revoked_at IS NULL"
            )
        ).all()

    by_principal: dict[str, list[dict]] = {}
    for g in grants:
        by_principal.setdefault(str(g.principal_id), []).append(
            {
                "id": str(g.id),
                "capability": g.capability,
                "resource": g.resource,
                "expires_at": g.expires_at.isoformat() if g.expires_at else None,
            }
        )

    return {
        "members": [
            {
                "id": str(r.id),
                "subject": r.subject,
                "roles": list(r.roles or []),
                "actor_type": r.actor_type,
                "disabled": r.disabled_at is not None,
                "grants": by_principal.get(str(r.id), []),
            }
            for r in rows
        ]
    }


@router.post("/members/grants", status_code=201)
def create_grant(payload: GrantRequest, ctx: RequestContext = Depends(get_context)) -> dict:
    """Grant a capability. The granter must hold it themselves.

    Without that rule, MEMBER_MANAGE is equivalent to OWNER: anyone who can
    manage members could grant themselves everything they lack.
    """
    try:
        grant_id = grant(
            ctx.principal,
            payload.principal_id,
            payload.capability,
            resource=payload.resource,
            expires_at=payload.expires_at,
            audit_context=ctx,
        )
    except NotAuthorized as exc:
        audit.record(
            ctx,
            action="member.capability.grant",
            outcome=audit.Outcome.DENIED,
            resource_type="principal",
            resource_id=str(payload.principal_id),
            detail={"capability": str(payload.capability), "reason": exc.reason},
        )
        raise HTTPException(status_code=403, detail=str(exc)) from None

    return {
        "id": str(grant_id),
        "principal_id": str(payload.principal_id),
        "capability": str(payload.capability),
        "resource": payload.resource,
    }


@router.delete("/members/grants/{grant_id}")
def revoke_grant(grant_id: uuid.UUID, ctx: RequestContext = Depends(get_context)) -> dict:
    try:
        revoked = revoke(ctx.principal, grant_id, audit_context=ctx)
    except NotAuthorized as exc:
        audit.record(
            ctx,
            action="member.capability.revoke",
            outcome=audit.Outcome.DENIED,
            resource_type="capability_grant",
            resource_id=str(grant_id),
            detail={"reason": exc.reason},
        )
        raise HTTPException(status_code=403, detail=str(exc)) from None
    if not revoked:
        raise HTTPException(status_code=404, detail="Capability grant not found.")
    return {"id": str(grant_id), "revoked": True}
