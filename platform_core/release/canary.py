"""Canary: ship to a slice, watch, and roll back automatically on breach.

## What "rollback" has to mean

Rolling back is an **action taken on running infrastructure**, not a redeploy.
In the Azure build it is the latter: `deploy.sh` runs
`az containerapp update --image` in single-revision mode, so recovering from a
bad release means rebuilding the previous tag and waiting for it to roll out —
during which the bad revision is still serving. Its own Traps section records
what that costs: two deploy cycles spent chasing a bug that was already fixed,
because a draining old replica served the run.

Here a rollback is shifting a weight back to 100/0 and marking the candidate
`rolled_back`. It takes effect at the router, immediately, and the previous
revision never stopped running.

## Comparison, not a fixed threshold

A canary is judged **against the revision it is replacing**, over the same
window, not against an absolute number. A service whose steady-state error rate
is 2% would trip a fixed 1% threshold on every deploy; one whose steady state is
0.01% would sail past a 1% threshold while regressing a hundredfold. Both are
wrong for the same reason: the number that matters is the change.

## Minimum sample

A canary at 10% traffic accumulates observations slowly, and a 1-in-3 error rate
over three requests is noise. The gate refuses to *pass* on an insufficient
sample — it holds the canary rather than promoting it. This is the same vacuity
rule as everywhere else here: "no breach detected" and "not enough data to
detect a breach" are different states and must not share an outcome.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Protocol

from sqlalchemy import text

from platform_core.db.engine import owner_session
from platform_core.observability import audit

logger = logging.getLogger("platform.release.canary")

# Defaults, all overridable per deployment.
DEFAULT_ERROR_RATE_TOLERANCE = 0.05    # 5 percentage points worse than baseline
DEFAULT_LATENCY_TOLERANCE = 1.5        # p95 may be 1.5x the baseline's
DEFAULT_MIN_OBSERVATIONS = 20


@dataclass(frozen=True, slots=True)
class RevisionStats:
    revision: str
    observations: int
    error_rate: float
    p95_latency_ms: float

    @property
    def sufficient(self) -> bool:
        return self.observations >= DEFAULT_MIN_OBSERVATIONS


class MetricsSource(Protocol):
    """Where per-revision outcomes come from.

    A protocol so the comparison logic is independent of the store. Postgres
    here because it can be driven transactionally in a test; production would
    supply a Prometheus-backed implementation and nothing in
    :func:`evaluate_canary` would change.
    """

    def stats(self, revision: str, *, window: timedelta) -> RevisionStats: ...


class PostgresMetrics:
    def stats(self, revision: str, *, window: timedelta) -> RevisionStats:
        with owner_session() as s:
            row = s.execute(
                text(
                    "SELECT count(*) AS n, "
                    "  coalesce(avg(CASE WHEN outcome = 'error' THEN 1.0 ELSE 0.0 END), 0) "
                    "    AS error_rate, "
                    "  coalesce(percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms), 0) "
                    "    AS p95 "
                    "FROM release_observation "
                    "WHERE revision = :rev AND at >= now() - :window"
                ),
                {"rev": revision, "window": window},
            ).one()
        return RevisionStats(
            revision=revision, observations=int(row.n),
            error_rate=float(row.error_rate), p95_latency_ms=float(row.p95),
        )


@dataclass(frozen=True, slots=True)
class CanaryVerdict:
    action: str                       # "promote" | "hold" | "rollback"
    reasons: list[str] = field(default_factory=list)
    candidate: RevisionStats | None = None
    baseline: RevisionStats | None = None

    @property
    def breached(self) -> bool:
        return self.action == "rollback"

    def explain(self) -> str:
        lines = [f"{self.action.upper()}"]
        for label, stats in (("candidate", self.candidate), ("baseline", self.baseline)):
            if stats:
                lines.append(
                    f"  {label}: n={stats.observations} "
                    f"errors={stats.error_rate:.3%} p95={stats.p95_latency_ms:.0f}ms"
                )
        lines.extend(f"  · {r}" for r in self.reasons)
        return "\n".join(lines)


def observe(revision: str, *, route: str, outcome: str, latency_ms: float,
            status_code: int | None = None) -> None:
    """Record one request outcome against a revision. Never raises."""
    try:
        with owner_session() as s:
            s.execute(
                text(
                    "INSERT INTO release_observation (revision, route, outcome, "
                    "  status_code, latency_ms) VALUES (:rev, :route, :outcome, :code, :ms)"
                ),
                {"rev": revision, "route": route, "outcome": outcome,
                 "code": status_code, "ms": latency_ms},
            )
    except Exception:
        logger.debug("could not record release observation", exc_info=True)


def evaluate_canary(
    candidate_revision: str,
    baseline_revision: str,
    *,
    metrics: MetricsSource | None = None,
    window: timedelta = timedelta(minutes=10),
    error_rate_tolerance: float = DEFAULT_ERROR_RATE_TOLERANCE,
    latency_tolerance: float = DEFAULT_LATENCY_TOLERANCE,
    min_observations: int = DEFAULT_MIN_OBSERVATIONS,
) -> CanaryVerdict:
    """Compare a canary against what it is replacing, over the same window."""
    source = metrics or PostgresMetrics()
    candidate = source.stats(candidate_revision, window=window)
    baseline = source.stats(baseline_revision, window=window)
    reasons: list[str] = []

    if candidate.observations < min_observations:
        # Held, not promoted. "Not enough data to detect a breach" is not the
        # same state as "no breach", and giving them one outcome is how a canary
        # promotes itself before it has been observed at all.
        return CanaryVerdict(
            action="hold",
            reasons=[
                f"only {candidate.observations} observations for the candidate "
                f"(need {min_observations}) — insufficient sample to judge, holding"
            ],
            candidate=candidate, baseline=baseline,
        )

    error_delta = candidate.error_rate - baseline.error_rate
    if error_delta > error_rate_tolerance:
        reasons.append(
            f"error rate rose {error_delta:.2%} "
            f"({baseline.error_rate:.2%} → {candidate.error_rate:.2%}), beyond the "
            f"{error_rate_tolerance:.2%} tolerance"
        )

    if baseline.p95_latency_ms > 0:
        ratio = candidate.p95_latency_ms / baseline.p95_latency_ms
        if ratio > latency_tolerance:
            reasons.append(
                f"p95 latency is {ratio:.2f}x the baseline "
                f"({baseline.p95_latency_ms:.0f}ms → {candidate.p95_latency_ms:.0f}ms), "
                f"beyond {latency_tolerance:.2f}x"
            )

    if reasons:
        return CanaryVerdict(action="rollback", reasons=reasons,
                             candidate=candidate, baseline=baseline)
    return CanaryVerdict(
        action="promote",
        reasons=[f"no SLO breach over {candidate.observations} observations"],
        candidate=candidate, baseline=baseline,
    )


# ── the actions ──────────────────────────────────────────────────────────


def register(revision: str, *, image_tag: str, schema_version: str) -> None:
    with owner_session() as s:
        s.execute(
            text(
                "INSERT INTO release (revision, image_tag, schema_version, status, "
                "  traffic_weight) VALUES (:rev, :tag, :schema, 'candidate', 0) "
                "ON CONFLICT (revision) DO NOTHING"
            ),
            {"rev": revision, "tag": image_tag, "schema": schema_version},
        )


def start_canary(candidate: str, *, weight: int = 10) -> None:
    """Shift a slice of traffic to the candidate.

    The active revision keeps the remainder — it is never stopped, which is what
    makes rollback instant rather than a redeploy.
    """
    with owner_session() as s:
        active = s.execute(
            text("SELECT revision FROM release WHERE status = 'active'")
        ).scalar_one_or_none()
        if active is None:
            raise RuntimeError("no active revision to canary against")
        s.execute(
            text(
                "UPDATE release SET status = 'canary', traffic_weight = :w "
                "WHERE revision = :rev"
            ),
            {"w": weight, "rev": candidate},
        )
        s.execute(
            text("UPDATE release SET traffic_weight = :w WHERE revision = :rev"),
            {"w": 100 - weight, "rev": active},
        )
    logger.info("canary %s at %d%% against active %s", candidate, weight, active)


def rollback(candidate: str, *, reason: str, ctx=None) -> dict:
    """Return all traffic to the active revision. Immediate.

    Also reports whether the schema has to move: a rollback of the *image* does
    not roll back the *database*, and a candidate that ran a migration the
    previous revision cannot read is a rollback that does not actually recover.
    Expand/contract exists so the answer is normally "no" — this reports it
    rather than assuming.
    """
    with owner_session() as s:
        active = s.execute(
            text("SELECT revision, schema_version FROM release WHERE status = 'active'")
        ).one_or_none()
        candidate_row = s.execute(
            text("SELECT schema_version FROM release WHERE revision = :rev"),
            {"rev": candidate},
        ).one_or_none()

        s.execute(
            text(
                "UPDATE release SET status = 'rolled_back', traffic_weight = 0, "
                "  rolled_back_at = now(), rollback_reason = :why WHERE revision = :rev"
            ),
            {"why": reason[:2000], "rev": candidate},
        )
        if active:
            s.execute(
                text("UPDATE release SET traffic_weight = 100 WHERE revision = :rev"),
                {"rev": active.revision},
            )

    schema_moved = bool(
        active and candidate_row and candidate_row.schema_version != active.schema_version
    )
    result = {
        "rolled_back": candidate,
        "traffic_restored_to": active.revision if active else None,
        "reason": reason,
        "schema_rollback_required": schema_moved,
        "candidate_schema": candidate_row.schema_version if candidate_row else None,
        "active_schema": active.schema_version if active else None,
    }

    if schema_moved:
        # Loud, because this is the case expand/contract is designed to avoid and
        # its presence means the deploy was not expand-only.
        logger.error(
            "rollback of %s requires a schema downgrade %s → %s — the candidate was "
            "not expand-only, so restoring the image alone does not restore service",
            candidate, candidate_row.schema_version, active.schema_version,
        )

    if ctx is not None:
        audit.record(
            ctx, action="release.rollback", outcome=audit.Outcome.SUCCEEDED,
            resource_type="release", resource_id=candidate, detail=result,
        )
    logger.warning("rolled back %s: %s", candidate, reason)
    return result


def promote(candidate: str, *, ctx=None) -> dict:
    """Give the candidate all traffic and retire the previous active revision."""
    with owner_session() as s:
        previous = s.execute(
            text("SELECT revision FROM release WHERE status = 'active'")
        ).scalar_one_or_none()
        if previous:
            s.execute(
                text(
                    "UPDATE release SET status = 'retired', traffic_weight = 0 "
                    "WHERE revision = :rev"
                ),
                {"rev": previous},
            )
        s.execute(
            text(
                "UPDATE release SET status = 'active', traffic_weight = 100, "
                "  promoted_at = now() WHERE revision = :rev"
            ),
            {"rev": candidate},
        )

    result = {"promoted": candidate, "retired": previous}
    if ctx is not None:
        audit.record(
            ctx, action="release.promoted", outcome=audit.Outcome.SUCCEEDED,
            resource_type="release", resource_id=candidate, detail=result,
        )
    logger.info("promoted %s to active (retired %s)", candidate, previous)
    return result


def traffic_split() -> dict[str, int]:
    """What the router should be doing right now."""
    with owner_session() as s:
        rows = s.execute(
            text(
                "SELECT revision, traffic_weight FROM release "
                "WHERE status IN ('active','canary') AND traffic_weight > 0"
            )
        ).all()
    return {r.revision: r.traffic_weight for r in rows}


def supervise(
    candidate: str,
    baseline: str,
    *,
    ctx=None,
    metrics: MetricsSource | None = None,
    **thresholds,
) -> CanaryVerdict:
    """Evaluate and act. The loop a deploy pipeline runs on a schedule.

    Acting automatically is the point. A canary that requires a human to notice
    the breach has the same mean-time-to-recovery as no canary at all — it just
    fails on a smaller fraction of traffic while somebody reads a dashboard.
    """
    verdict = evaluate_canary(candidate, baseline, metrics=metrics, **thresholds)
    if verdict.action == "rollback":
        rollback(candidate, reason="; ".join(verdict.reasons), ctx=ctx)
    elif verdict.action == "promote":
        promote(candidate, ctx=ctx)
    return verdict
