"""Spend reporting and budget ceilings.

Thin in Phase 1 — the ledger lands in Phase 3. What matters now is that the
routes exist with the right authority: reading usage is an operator capability,
changing a ceiling is not. In the Azure build both live behind the same
``admin`` gate, which means anyone who can read spend can also raise the cap.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import text

from platform_core.api.deps import get_context
from platform_core.db.engine import tenant_session
from platform_core.identity.principal import RequestContext
from platform_core.observability import audit
from platform_core.observability.ledger import ledger
from platform_core.settings import get_settings

router = APIRouter(prefix="/api", tags=["usage"])


class CapsUpdate(BaseModel):
    daily_token_cap: int | None = Field(default=None, ge=0)
    monthly_cost_cap_usd: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def at_least_one_cap(self) -> CapsUpdate:
        if self.daily_token_cap is None and self.monthly_cost_cap_usd is None:
            raise ValueError("at least one budget cap must be supplied")
        return self


@router.get("/usage")
def usage(ctx: Annotated[RequestContext, Depends(get_context)]) -> dict:
    """Current spend against the tenant's ceilings."""
    status = ledger.status(ctx)

    return {
        "tenant": ctx.tenant.slug,
        "daily_token_cap": status.daily_token_cap,
        "monthly_cost_cap_usd": status.monthly_cost_cap_usd,
        "tokens_today": status.tokens_today,
        "cost_this_month_usd": status.cost_this_month_usd,
        "reserved_tokens": status.reserved_tokens,
        "reserved_cost_usd": status.reserved_cost_usd,
        "headroom_tokens": status.headroom_tokens,
        "from_cache": status.from_cache,
        "fail_closed": get_settings().budget_fail_closed,
    }


@router.put("/usage/caps")
def set_caps(
    payload: CapsUpdate,
    ctx: Annotated[RequestContext, Depends(get_context)],
) -> dict:
    """Change this tenant's ceilings. Requires BUDGET_MANAGE.

    Uses a narrowly scoped security-definer function rather than loading the
    table-owner credential into the API process. The function independently
    binds the target to the transaction's tenant scope.
    """
    previous = ledger.status(ctx)
    with tenant_session(ctx.tenant) as session:
        updated = session.execute(
            text(
                "SELECT daily_token_cap, monthly_cost_cap_usd "
                "FROM platform_set_tenant_budget_caps(:id, :daily, :monthly)"
            ),
            {
                "id": ctx.tenant.id,
                "daily": payload.daily_token_cap,
                "monthly": payload.monthly_cost_cap_usd,
            },
        ).one()
        audit.append_in_session(
            session,
            ctx,
            action="budget.caps.update",
            outcome=audit.Outcome.SUCCEEDED,
            resource_type="tenant",
            resource_id=str(ctx.tenant.id),
            detail={
                "previous_daily_token_cap": previous.daily_token_cap,
                "previous_monthly_cost_cap_usd": previous.monthly_cost_cap_usd,
                "daily_token_cap": (
                    int(updated.daily_token_cap)
                    if updated.daily_token_cap is not None
                    else previous.daily_token_cap
                ),
                "monthly_cost_cap_usd": (
                    float(updated.monthly_cost_cap_usd)
                    if updated.monthly_cost_cap_usd is not None
                    else previous.monthly_cost_cap_usd
                ),
            },
        )
    ledger.invalidate(ctx.tenant.id)
    current = ledger.status(ctx)
    return {
        "tenant": ctx.tenant.slug,
        "updated": True,
        "daily_token_cap": current.daily_token_cap,
        "monthly_cost_cap_usd": current.monthly_cost_cap_usd,
    }
