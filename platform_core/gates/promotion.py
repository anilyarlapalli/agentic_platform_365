"""The gate: compare a candidate run against the baseline, and refuse a regression.

This is the difference between measuring and gating. The Azure build computes
`answer_pass_rate` and `retrieval_recall` correctly, and then writes them to a
single blob per domain that the next run overwrites — so there is no baseline,
no history, and nothing that can refuse anything. A number with no comparison is
a reading.

## What the gate refuses, and why each one is separate

**Regression** beyond a threshold on either metric. Two thresholds, not one
combined score, because the two failures live on different surfaces.

**An incomparable baseline.** A candidate scored over dataset A cannot be
compared to a baseline scored over dataset B. Both numbers are real and correctly
computed; putting them side by side produces a confident wrong answer. Refusing
is the only correct behaviour, so `dataset_sha` equality is checked before
anything else.

**A shrunken sample.** A candidate that scored 5 items against a baseline of 25
can beat it on average while being much worse. Recall computed over a different
number of scoreable items is a different measurement.

**An unscoreable run.** A run where nothing could be scored is not a pass. This
is the vacuity failure this codebase has hit three times in other forms — an
assertion whose "pass" state is indistinguishable from its "nothing to check"
state — so the gate rejects a null metric rather than treating it as "no
regression detected".

## First promotion

With no baseline, there is nothing to regress against, so the first run is
promoted if it is *scoreable*. Stated explicitly because "no baseline" must not
silently mean "gate passed" for every subsequent run too.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text

from platform_core.db.engine import tenant_session
from platform_core.gates.runner import EvalRun
from platform_core.identity.capabilities import Capability, require
from platform_core.identity.principal import RequestContext
from platform_core.observability import audit

logger = logging.getLogger("platform.gates.promotion")

# How far a metric may fall before promotion is refused. Not zero: re-running an
# identical candidate can move a rate by one item in twenty-five (0.04), and a
# gate that fires on noise gets switched off, which is worse than no gate.
DEFAULT_RECALL_TOLERANCE = 0.02
DEFAULT_PASS_RATE_TOLERANCE = 0.02


@dataclass(frozen=True, slots=True)
class GateDecision:
    promoted: bool
    reasons: list[str] = field(default_factory=list)
    candidate: dict[str, Any] = field(default_factory=dict)
    baseline: dict[str, Any] | None = None
    deltas: dict[str, float] = field(default_factory=dict)

    def explain(self) -> str:
        verdict = "PROMOTED" if self.promoted else "BLOCKED"
        lines = [f"{verdict}: {self.candidate.get('run_id', '?')}"]
        if self.baseline:
            lines.append(f"  baseline: {self.baseline.get('run_id')}")
        for metric, delta in self.deltas.items():
            lines.append(f"  {metric}: {delta:+.4f}")
        lines.extend(f"  · {r}" for r in self.reasons)
        return "\n".join(lines)


def _baseline_row(ctx: RequestContext, dataset_name: str) -> dict | None:
    with tenant_session(ctx.tenant) as s:
        row = s.execute(
            text(
                "SELECT r.id, r.dataset_sha, r.code_rev, r.model_id, "
                "  r.answer_pass_rate, r.retrieval_recall, r.items_run, r.items_scoreable "
                "FROM eval_baseline b JOIN eval_run r ON r.id = b.eval_run_id "
                "WHERE b.dataset_name = :n"
            ),
            {"n": dataset_name},
        ).one_or_none()
    if row is None:
        return None
    return {
        "run_id": str(row.id), "dataset_sha": row.dataset_sha,
        "code_rev": row.code_rev, "model_id": row.model_id,
        "answer_pass_rate": row.answer_pass_rate,
        "retrieval_recall": row.retrieval_recall,
        "items_run": row.items_run, "items_scoreable": row.items_scoreable,
    }


def evaluate(
    ctx: RequestContext,
    candidate: EvalRun,
    *,
    dataset_name: str,
    recall_tolerance: float = DEFAULT_RECALL_TOLERANCE,
    pass_rate_tolerance: float = DEFAULT_PASS_RATE_TOLERANCE,
) -> GateDecision:
    """Decide whether the candidate may become the baseline. Does not move it."""
    summary = candidate.summary()
    baseline = _baseline_row(ctx, dataset_name)
    reasons: list[str] = []
    deltas: dict[str, float] = {}

    # A run where nothing could be scored is not a pass. Checked first, because
    # every comparison below would otherwise be against None and read as "no
    # regression".
    if candidate.items_run == 0:
        reasons.append("the candidate scored no items at all")
    if candidate.retrieval_recall is None and candidate.answer_pass_rate is None:
        reasons.append(
            "the candidate produced no scoreable metric — an unscoreable run is not "
            "a passing run"
        )

    if baseline is None:
        if not reasons:
            reasons.append("no baseline for this dataset; promoting the first scoreable run")
        return GateDecision(
            promoted=not reasons or reasons[-1].startswith("no baseline"),
            reasons=reasons, candidate=summary, baseline=None, deltas={},
        )

    # Incomparable datasets. Both numbers are real; the comparison is not.
    if baseline["dataset_sha"] != candidate.dataset_sha:
        reasons.append(
            f"baseline was scored on dataset {baseline['dataset_sha'][:12]}… and the "
            f"candidate on {candidate.dataset_sha[:12]}… — these numbers are not "
            f"comparable, so no regression can be ruled out"
        )
        return GateDecision(promoted=False, reasons=reasons, candidate=summary,
                            baseline=baseline, deltas={})

    if candidate.items_scoreable < baseline["items_scoreable"]:
        reasons.append(
            f"the candidate scored {candidate.items_scoreable} items against the "
            f"baseline's {baseline['items_scoreable']} — a smaller sample can beat a "
            f"larger one on average while being worse"
        )

    for metric, tolerance in (
        ("retrieval_recall", recall_tolerance),
        ("answer_pass_rate", pass_rate_tolerance),
    ):
        base_value = baseline[metric]
        cand_value = getattr(candidate, metric)
        if base_value is None:
            continue
        if cand_value is None:
            reasons.append(
                f"the baseline has a {metric} of {base_value:.4f} and the candidate has "
                f"none — a metric that stopped being measurable is a regression, not a pass"
            )
            continue
        delta = cand_value - base_value
        deltas[metric] = round(delta, 4)
        if delta < -tolerance:
            reasons.append(
                f"{metric} fell {abs(delta):.4f} ({base_value:.4f} → {cand_value:.4f}), "
                f"beyond the {tolerance:.4f} tolerance"
            )

    return GateDecision(
        promoted=not reasons, reasons=reasons or ["no regression beyond tolerance"],
        candidate=summary, baseline=baseline, deltas=deltas,
    )


def promote(
    ctx: RequestContext,
    candidate: EvalRun,
    *,
    dataset_name: str,
    note: str | None = None,
    force: bool = False,
    **thresholds,
) -> GateDecision:
    """Evaluate and, if the gate passes, move the baseline pointer.

    ``force`` overrides a block. It exists because a deliberate, reviewed
    regression is a real thing — accepting slightly worse recall for much lower
    cost, say — and a gate with no override gets bypassed by deleting the
    baseline, which loses the history. Every forced promotion is audited with the
    reasons it overrode, so the decision stays visible.
    """
    require(ctx.principal, Capability.RELEASE_PROMOTE)
    decision = evaluate(ctx, candidate, dataset_name=dataset_name, **thresholds)

    if not decision.promoted and not force:
        audit.record(
            ctx, action="eval.promotion.blocked", outcome=audit.Outcome.DENIED,
            resource_type="eval_dataset", resource_id=dataset_name,
            detail={"reasons": decision.reasons, "deltas": decision.deltas,
                    "candidate": decision.candidate},
        )
        logger.warning("promotion blocked for %s:\n%s", dataset_name, decision.explain())
        return decision

    with tenant_session(ctx.tenant) as s:
        s.execute(
            text(
                "INSERT INTO eval_baseline (tenant_id, dataset_name, eval_run_id, "
                "  promoted_by, note) VALUES (:t, :n, :r, :by, :note) "
                "ON CONFLICT (tenant_id, dataset_name) DO UPDATE "
                "  SET eval_run_id = EXCLUDED.eval_run_id, "
                "      promoted_by = EXCLUDED.promoted_by, "
                "      promoted_at = now(), note = EXCLUDED.note"
            ),
            {
                "t": ctx.tenant.id, "n": dataset_name, "r": candidate.id,
                "by": ctx.principal.id,
                "note": note or ("forced" if force else None),
            },
        )
        audit.append_in_session(
            s,
            ctx,
            action="eval.promotion.forced" if (force and not decision.promoted)
            else "eval.promotion.accepted",
            outcome=audit.Outcome.SUCCEEDED,
            resource_type="eval_dataset",
            resource_id=dataset_name,
            detail={
                "run_id": str(candidate.id),
                "deltas": decision.deltas,
                "overridden_reasons": (
                    decision.reasons if force and not decision.promoted else []
                ),
                "note": note,
            },
        )
    logger.info("baseline for %s is now %s", dataset_name, candidate.id)
    return GateDecision(
        promoted=True,
        reasons=decision.reasons if not force else [*decision.reasons, "forced"],
        candidate=decision.candidate, baseline=decision.baseline, deltas=decision.deltas,
    )


def history(ctx: RequestContext, dataset_name: str, *, limit: int = 20) -> list[dict]:
    """Every run for a dataset, newest first. The thing overwriting destroys."""
    with tenant_session(ctx.tenant) as s:
        rows = s.execute(
            text(
                "SELECT r.id, r.dataset_sha, r.code_rev, r.model_id, r.status, "
                "  r.answer_pass_rate, r.retrieval_recall, r.items_run, "
                "  r.items_scoreable, r.started_at, "
                "  (b.eval_run_id = r.id) AS is_baseline "
                "FROM eval_run r "
                "JOIN eval_dataset d ON d.id = r.dataset_id "
                "LEFT JOIN eval_baseline b ON b.dataset_name = d.name "
                "  AND b.tenant_id = r.tenant_id "
                "WHERE d.name = :n ORDER BY r.started_at DESC LIMIT :k"
            ),
            {"n": dataset_name, "k": limit},
        ).all()
    return [
        {
            "run_id": str(r.id), "dataset_sha": r.dataset_sha[:12],
            "code_rev": r.code_rev, "model_id": r.model_id, "status": r.status,
            "answer_pass_rate": r.answer_pass_rate,
            "retrieval_recall": r.retrieval_recall,
            "items_run": r.items_run, "items_scoreable": r.items_scoreable,
            "started_at": r.started_at.isoformat(),
            "is_baseline": bool(r.is_baseline),
        }
        for r in rows
    ]
