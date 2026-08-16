"""Mandatory continuous evaluation without a cross-tenant data credential.

The relay can execute one narrow database function that selects due policies
and atomically creates durable run/outbox rows. It cannot read evaluation items,
documents, principals, or policy rows directly.
"""

from __future__ import annotations

from sqlalchemy import text

from platform_core.db.engine import relay_session, system_session, tenant_session
from platform_core.identity.principal import RequestContext
from platform_core.observability import audit
from platform_core.observability.telemetry import record_eval_scheduler
from platform_core.settings import get_settings


def schedule_due(*, limit: int = 100) -> int:
    settings = get_settings()
    try:
        with relay_session(reason="governance: schedule due continuous evaluations") as session:
            scheduled = int(
                session.execute(
                    text("SELECT platform_schedule_due_continuous_evals(:limit, :release)"),
                    {"limit": min(max(limit, 1), 500), "release": settings.release},
                ).scalar_one()
            )
    except Exception:
        record_eval_scheduler("failed")
        raise
    record_eval_scheduler("succeeded", scheduled=scheduled)
    return scheduled


def health() -> dict:
    """Aggregate-only status safe for the public readiness response."""
    with system_session(reason="readiness: continuous evaluation schedules") as session:
        result = session.execute(
            text("SELECT platform_continuous_eval_health()")
        ).scalar_one()
    return dict(result)


def list_policies(ctx: RequestContext) -> list[dict]:
    with tenant_session(ctx.tenant) as session:
        rows = session.execute(
            text(
                "SELECT dataset_name, interval_seconds, top_k, next_run_at, "
                "last_scheduled_at, last_run_id, updated_at "
                "FROM continuous_eval_policy ORDER BY dataset_name"
            )
        ).all()
    return [
        {
            "dataset": row.dataset_name,
            "interval_seconds": row.interval_seconds,
            "top_k": row.top_k,
            "next_run_at": row.next_run_at.isoformat(),
            "last_scheduled_at": (
                row.last_scheduled_at.isoformat() if row.last_scheduled_at else None
            ),
            "last_run_id": str(row.last_run_id) if row.last_run_id else None,
            "updated_at": row.updated_at.isoformat(),
            "mandatory": True,
        }
        for row in rows
    ]


def update_policy(
    ctx: RequestContext,
    *,
    dataset_name: str,
    interval_seconds: int,
    top_k: int,
) -> dict | None:
    with tenant_session(ctx.tenant) as session:
        row = session.execute(
            text(
                "UPDATE continuous_eval_policy "
                "SET interval_seconds = :interval, top_k = :top_k, "
                "updated_by = :by, updated_at = now(), "
                "next_run_at = LEAST(next_run_at, now() + make_interval(secs => :interval)) "
                "WHERE dataset_name = :name "
                "RETURNING dataset_name, interval_seconds, top_k, next_run_at"
            ),
            {
                "interval": interval_seconds,
                "top_k": top_k,
                "by": ctx.principal.id,
                "name": dataset_name,
            },
        ).one_or_none()
        if row is None:
            return None
        audit.append_in_session(
            session,
            ctx,
            action="eval.continuous.policy.updated",
            outcome=audit.Outcome.SUCCEEDED,
            resource_type="eval_dataset",
            resource_id=dataset_name,
            detail={"interval_seconds": interval_seconds, "top_k": top_k},
        )
    return {
        "dataset": row.dataset_name,
        "interval_seconds": row.interval_seconds,
        "top_k": row.top_k,
        "next_run_at": row.next_run_at.isoformat(),
        "mandatory": True,
    }
