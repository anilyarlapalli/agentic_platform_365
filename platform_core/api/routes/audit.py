"""Tenant audit inspection and chain verification."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from platform_core.api.deps import get_context
from platform_core.identity.principal import RequestContext
from platform_core.observability import audit as audit_log

router = APIRouter(prefix="/api", tags=["audit"])


@router.get("/audit")
def audit_events(
    ctx: Annotated[RequestContext, Depends(get_context)],
    limit: int = Query(default=100, ge=1, le=500),
) -> dict:
    """Recent tenant events plus a fresh verification of the retained chain."""
    events = audit_log.recent(ctx, limit=limit)
    verification = audit_log.verify_chain(ctx.tenant.id, ctx.tenant.slug)
    return {
        "verification": {
            "intact": verification.intact,
            "events_checked": verification.events_checked,
            "first_break_id": verification.first_break_id,
            "reason": verification.reason,
            "anchor_event_id": verification.anchor_event_id,
            "events_anchored": verification.events_anchored,
        },
        "events": [
            {
                "id": event.id,
                "at": event.at.isoformat(),
                "actor_subject": event.actor_subject,
                "action": event.action,
                "resource_type": event.resource_type,
                "resource_id": event.resource_id,
                "outcome": event.outcome,
                "detail": event.detail,
                "prev_hash": event.prev_hash,
                "hash": event.hash,
            }
            for event in events
        ],
    }
