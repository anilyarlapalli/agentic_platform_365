"""Liveness and readiness. Public, and therefore carrying no tenant data.

Split deliberately. ``/health`` answers "is this process alive" and must never
depend on a downstream service — a liveness probe that fails when Postgres is
slow causes the orchestrator to restart a healthy process, turning a degraded
dependency into an outage. ``/health/ready`` answers "can this process do useful
work" and is allowed to check dependencies.

The Azure build learned this the hard way in the opposite direction: warming
domains inline at startup left uvicorn at "Waiting for application startup" with
the port closed, so the health probe failed and the revision restart-looped —
and with Storage unreachable the SDK's retry policy stretched that into minutes.
Its fix was to warm on a background thread and serve immediately. Same principle.
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from redis import Redis
from sqlalchemy import text

from platform_core.adapters.local.object_store import get_object_store
from platform_core.adapters.postgres.checkpoint import get_checkpoint_store
from platform_core.db.engine import system_session
from platform_core.governance import continuous_eval
from platform_core.observability.telemetry import collector_reachable, telemetry_status
from platform_core.settings import get_settings

router = APIRouter(tags=["health"])


@router.get("/health")
def health() -> dict:
    """Liveness. No dependency checks, by design."""
    settings = get_settings()
    return {
        "status": "alive",
        "service": settings.service_name,
        "release": settings.release,
        "environment": settings.environment,
    }


@router.get("/health/ready")
def ready() -> JSONResponse:
    """Readiness, with per-dependency detail.

    Reports each dependency separately rather than collapsing to a boolean: "not
    ready" without saying which dependency is down is a page that starts with an
    investigation instead of an action.

    The route is public for orchestrator probes, so it reports only control
    state. Resolved configuration belongs in authenticated operational tooling
    and telemetry, never in this response.
    """
    settings = get_settings()
    checks: dict[str, dict] = {}

    try:
        with system_session(reason="readiness probe") as s:
            s.execute(text("SELECT 1"))
            rls = s.execute(
                text(
                    "SELECT count(*) FROM pg_class "
                    "WHERE relnamespace = 'public'::regnamespace AND relkind = 'r' "
                    "AND relrowsecurity AND relforcerowsecurity"
                )
            ).scalar_one()
            role = s.execute(text("SELECT current_user")).scalar_one()
            bypass = s.execute(
                text("SELECT rolbypassrls FROM pg_roles WHERE rolname = current_user")
            ).scalar_one()
        checks["postgres"] = {
            "ok": bool(rls) and not bypass,
            "protected_tables": rls,
            "role": role,
            # Surfaced because it is a silent correctness problem: policies stay
            # listed in pg_policies while none of them apply.
            "rls_enforced": not bypass,
        }
    except Exception as exc:
        checks["postgres"] = {"ok": False, "error": type(exc).__name__}

    problems = settings.check_coherence()
    checks["configuration"] = {"ok": not problems, "problem_count": len(problems)}

    try:
        Redis.from_url(
            settings.redis_url,
            socket_connect_timeout=0.5,
            socket_timeout=0.5,
        ).ping()
        checks["redis"] = {"ok": True}
    except Exception as exc:
        checks["redis"] = {"ok": False, "error": type(exc).__name__}

    try:
        get_object_store().ready()
        checks["object_store"] = {"ok": True}
    except Exception as exc:
        checks["object_store"] = {"ok": False, "error": type(exc).__name__}

    telemetry = telemetry_status()
    reachable, reachability_error = collector_reachable()
    checks["telemetry"] = {
        "ok": bool(telemetry["enabled"] and telemetry["configured"] and reachable),
        "configured": telemetry["configured"],
        "collector_reachable": reachable,
        "error": telemetry["error"] or reachability_error,
    }

    durability = get_checkpoint_store().durability()
    checks["checkpoint"] = {
        "ok": durability.durable,
        "backend": durability.backend,
        "detail": durability.detail,
    }

    try:
        eval_health = continuous_eval.health()
        checks["continuous_eval"] = {
            "ok": bool(eval_health.get("ok")),
            "missing_policy": bool(eval_health.get("missing_policies")),
            "overdue": bool(eval_health.get("overdue_policies")),
        }
        if settings.environment == "local":
            checks["continuous_eval"]["dataset_names"] = int(
                eval_health.get("dataset_names", 0)
            )
    except Exception as exc:
        checks["continuous_eval"] = {"ok": False, "error": type(exc).__name__}

    is_ready = all(check.get("ok") for check in checks.values())
    content = {
        "status": "ready" if is_ready else "degraded",
        "release": settings.release,
        "checks": checks,
    }

    return JSONResponse(status_code=200 if is_ready else 503, content=content)
