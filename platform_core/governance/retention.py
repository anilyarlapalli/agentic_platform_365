"""Enforce bounded data lifecycle through one narrow relay operation."""

from __future__ import annotations

from sqlalchemy import text

from platform_core.db.engine import relay_session
from platform_core.observability.telemetry import record_retention_pass, record_retention_rows
from platform_core.settings import get_settings


def enforce() -> dict[str, int]:
    settings = get_settings()
    try:
        with relay_session(reason="governance: enforce retention policy") as session:
            result = session.execute(
                text(
                    "SELECT platform_enforce_retention("
                    ":gap, :runs, :evals, :usage, :audit, :audit_batch)"
                ),
                {
                    "gap": settings.unanswered_question_retention_days,
                    "runs": settings.terminal_run_retention_days,
                    "evals": settings.eval_history_retention_days,
                    "usage": settings.usage_ledger_retention_days,
                    "audit": settings.audit_retention_days,
                    "audit_batch": 100,
                },
            ).scalar_one()
    except Exception:
        record_retention_pass("failed")
        raise
    counts = {str(key): int(value) for key, value in dict(result).items()}
    for category, count in counts.items():
        record_retention_rows(category, count)
    record_retention_pass("succeeded")
    return counts
