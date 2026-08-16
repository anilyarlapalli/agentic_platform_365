"""Execute an eval set and persist the run. Never overwrites a previous one.

## Two metrics, reported separately

``retrieval_recall``
    Of the evidence an item requires, how much came back. Measured over items
    that *have* evidence, with the count reported alongside — averaging in items
    with nothing to retrieve silently dilutes the number.

``answer_pass_rate``
    Whether the answer was right.

Kept apart because a wrong answer *because the evidence was never retrieved* and
a wrong answer *despite having it* are different bugs on different surfaces, and
a combined score tells you neither. The Azure build reaches the same conclusion
and states it in the same terms; the difference here is that both numbers are
retained per run so they can be trended.

## Retrieval is injected

:func:`run` takes a ``retrieve`` callable. The platform is being gated, not a
particular retriever — and the acceptance test needs to inject a deliberately
degraded one to prove the gate blocks. A runner that reached into a specific
pipeline could not be tested without breaking that pipeline.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    # Type-only: importing the judge at module scope would drag the LLM port
    # into every caller that only wants to retrieve, and the acceptance test
    # injects a deliberately degraded retriever with no judge at all.
    from platform_core.gates.judge import Verdict

from sqlalchemy import text

from platform_core.corpus import builds
from platform_core.correctness.cancellation import RunCancelled, cancellation_point
from platform_core.db.engine import tenant_session
from platform_core.gates.datasets import Dataset, EvalItem
from platform_core.identity.principal import RequestContext
from platform_core.settings import get_settings

logger = logging.getLogger("platform.gates.runner")

# A retriever: (ctx, collection, question, top_k) -> list of canonical chunk ids.
Retriever = Callable[[RequestContext, str, str, int], list[str]]

# An answerer: (ctx, collection, item, retrieved_ids) -> the answer text.
#
# Separate from the judge, which they were not. The old ``Judge`` alias was
# annotated ``(item, retrieved_texts)`` while ``run`` passed canonical **ids**,
# and the same callable was expected to both produce the answer and grade it —
# so anything written against the signature would have received ids where it
# expected text and graded whatever it made of them. Producing the answer is the
# system under test; grading it is the measurement, and one model must not be
# doing both.
Answerer = Callable[[RequestContext, str, EvalItem, list[str]], str]

# A judge: (item, actual_answer, retrieved_ids) -> Verdict.
Judge = Callable[[EvalItem, str, list[str]], "Verdict"]


@dataclass(frozen=True, slots=True)
class ItemOutcome:
    item_id: str
    question: str
    must_cite: list[str]
    retrieved: list[str]
    retrieval_recall: float | None
    passed: bool | None
    answer: str = ""
    detail: dict[str, Any] = field(default_factory=dict)
    # Deterministic per-item scores — faithfulness, relevancy, precision,
    # citation accuracy. Computed for every item, unlike the judge, so a
    # regression has somewhere to appear between two verdicts.
    metrics: dict[str, Any] = field(default_factory=dict)
    # Where the judge said to look. None when it did not run.
    fix_surface: str | None = None
    # The judge could not be reached or parsed. Kept apart from ``passed=False``
    # because they demand opposite responses: one is a regression in the
    # platform, the other is a broken measurement, and a run where the judge was
    # down must never read as a run where the answers were bad.
    judge_unavailable: bool = False


@dataclass(frozen=True, slots=True)
class EvalRun:
    id: uuid.UUID
    dataset_sha: str
    code_rev: str
    model_id: str
    cassette_sha: str | None
    answer_pass_rate: float | None
    retrieval_recall: float | None
    items_run: int
    items_scoreable: int
    outcomes: list[ItemOutcome]
    elapsed_s: float
    quality: dict[str, Any] = field(default_factory=dict)
    judge_model: str | None = None
    judge_unavailable: int = 0
    # Items a reviewer flagged as having unusable evidence. Excluded from the
    # run and counted, because a set that quietly shrinks is a set whose scores
    # stop being comparable without anything saying so.
    items_excluded: int = 0

    def summary(self) -> dict[str, Any]:
        return {
            "run_id": str(self.id),
            "dataset_sha": self.dataset_sha[:12],
            "code_rev": self.code_rev,
            "model_id": self.model_id,
            "judge_model": self.judge_model,
            "answer_pass_rate": self.answer_pass_rate,
            "retrieval_recall": self.retrieval_recall,
            "items_run": self.items_run,
            "items_scoreable": self.items_scoreable,
            "items_excluded": self.items_excluded,
            "judge_unavailable": self.judge_unavailable,
            "quality": self.quality,
            "elapsed_s": round(self.elapsed_s, 2),
        }


def run(
    ctx: RequestContext,
    dataset: Dataset,
    *,
    retrieve: Retriever,
    answer: Answerer | None = None,
    judge: Judge | None = None,
    evidence: Callable[[list[str]], dict[str, str]] | None = None,
    labels: dict[str, dict[str, Any]] | None = None,
    top_k: int = 5,
    cassette_sha: str | None = None,
) -> EvalRun:
    """Score a dataset and persist the run.

    Every item is attempted even if one raises: a single malformed question
    should not discard the other twenty-four results, and an item that errored
    is recorded as a failure with its reason rather than vanishing.

    ``retrieve`` alone scores retrieval recall, which is what the gate compared
    on before there was an answerer. Supplying ``answer`` and ``judge`` adds the
    pass rate; supplying ``evidence`` — a lookup from chunk ids to their text —
    adds the deterministic quality metrics. Each is optional and each is
    reported over its own denominator, so a run with no judge reports a null
    pass rate rather than a flattering one.
    """
    settings = get_settings()
    started = time.monotonic()
    labels = labels or {}

    with tenant_session(ctx.tenant) as s:
        run_id = s.execute(
            text(
                "INSERT INTO eval_run (tenant_id, dataset_id, dataset_sha, code_rev, "
                "  model_id, cassette_sha, status) "
                "VALUES (:t, :d, :sha, :rev, :model, :cassette, 'running') RETURNING id"
            ),
            {
                "t": ctx.tenant.id, "d": dataset.id, "sha": dataset.content_sha256,
                "rev": settings.release, "model": settings.llm_model_cheap,
                "cassette": cassette_sha,
            },
        ).scalar_one()

    from platform_core.gates import metrics as quality_metrics

    outcomes: list[ItemOutcome] = []
    excluded = 0
    for item in dataset.items:
        cancellation_point(ctx)
        # A reviewer judged this item's evidence unusable — scrambled parser
        # output, page residue. Running it would score a failure that says
        # nothing about the platform, so it is excluded and counted.
        if (labels.get(item.id, {}).get("unusable_reason") or "").strip():
            excluded += 1
            continue

        try:
            retrieved = retrieve(ctx, dataset.collection, item.question, top_k)
        except RunCancelled:
            raise
        except Exception as exc:
            logger.exception("retrieval failed for item %s", item.id)
            outcomes.append(
                ItemOutcome(
                    item_id=item.id, question=item.question, must_cite=item.must_cite,
                    retrieved=[], retrieval_recall=0.0 if item.must_cite else None,
                    passed=False, detail={"error": f"{type(exc).__name__}: {exc}"},
                )
            )
            continue

        wanted = set(item.must_cite)
        got = set(retrieved)
        recall = (len(wanted & got) / len(wanted)) if wanted else None

        actual = ""
        if answer is not None:
            try:
                actual = answer(ctx, dataset.collection, item, retrieved)
            except RunCancelled:
                raise
            except Exception as exc:
                logger.exception("answering failed for item %s", item.id)
                actual = ""
                # An answerer that raised is a platform failure, and recording it
                # as an empty answer alone would let the judge grade silence.
                detail_error = f"answerer: {type(exc).__name__}: {exc}"
            else:
                detail_error = ""
        else:
            detail_error = ""

        passed: bool | None = None
        fix_surface: str | None = None
        judge_down = False
        reason = ""
        if judge is not None and answer is not None:
            try:
                verdict = judge(item, actual, retrieved)
            except RunCancelled:
                raise
            except Exception as exc:
                logger.exception("judge failed for item %s", item.id)
                passed, judge_down = False, True
                reason = f"judge error: {type(exc).__name__}"
            else:
                passed = verdict.passed
                reason = verdict.reason
                fix_surface = str(verdict.fix_surface)
                judge_down = verdict.judge_unavailable

        item_metrics: dict[str, Any] = {}
        if evidence is not None and answer is not None:
            # Faithfulness is measured against what the answerer was **given**,
            # not against the evidence the item declares. Measuring it against
            # `must_cite` scores an answer as unfaithful for using other chunks
            # that retrieval legitimately returned — which is the same mistake
            # the judge's rubric is careful not to make, one metric lower down.
            # Caught by a live run reporting 0.32 for answers that were entirely
            # grounded.
            texts = evidence(retrieved)
            item_metrics = quality_metrics.score(
                answer=actual,
                question=item.question,
                expected_answer=item.expected_answer,
                retrieved=retrieved,
                evidence_texts=[texts[c] for c in retrieved if c in texts],
                must_cite=item.must_cite,
            )

        outcomes.append(
            ItemOutcome(
                item_id=item.id, question=item.question, must_cite=item.must_cite,
                retrieved=retrieved, retrieval_recall=recall, passed=passed,
                answer=actual,
                detail={
                    "evidence_found": sorted(wanted & got),
                    **({"reason": reason} if reason else {}),
                    **({"error": detail_error} if detail_error else {}),
                },
                metrics=item_metrics,
                fix_surface=fix_surface,
                judge_unavailable=judge_down,
            )
        )

    scoreable = [o for o in outcomes if o.retrieval_recall is not None]
    # A verdict the judge could not produce is not a verdict. Folding those into
    # the pass rate would report a judge outage as a quality collapse — the
    # reference deployment did exactly that when an unsupported response_format
    # turned every item into a failure.
    judged = [o for o in outcomes if o.passed is not None and not o.judge_unavailable]
    unavailable = sum(1 for o in outcomes if o.judge_unavailable)

    recall = (
        round(sum(o.retrieval_recall for o in scoreable) / len(scoreable), 4)
        if scoreable else None
    )
    pass_rate = (
        round(sum(1 for o in judged if o.passed) / len(judged), 4) if judged else None
    )
    quality = quality_metrics.aggregate([o.metrics for o in outcomes if o.metrics])
    elapsed = time.monotonic() - started

    with tenant_session(ctx.tenant) as s:
        for outcome in outcomes:
            s.execute(
                text(
                    "INSERT INTO eval_result (tenant_id, eval_run_id, item_id, question, "
                    "  passed, must_cite, retrieved, retrieval_recall, answer, detail) "
                    "VALUES (:t, :r, :item, :q, :passed, :must, :got, :recall, :ans, :d)"
                ),
                {
                    "t": ctx.tenant.id, "r": run_id, "item": outcome.item_id,
                    "q": outcome.question, "passed": outcome.passed,
                    "must": json.dumps(outcome.must_cite),
                    "got": json.dumps(outcome.retrieved),
                    "recall": outcome.retrieval_recall, "ans": outcome.answer,
                    "d": json.dumps({
                        **outcome.detail,
                        "metrics": outcome.metrics,
                        "fix_surface": outcome.fix_surface,
                        "judge_unavailable": outcome.judge_unavailable,
                    }),
                },
            )

        s.execute(
            text(
                "UPDATE eval_run SET status = 'completed', finished_at = now(), "
                "  answer_pass_rate = :pass, retrieval_recall = :recall, "
                "  items_run = :run, items_scoreable = :scoreable, metrics = :metrics "
                "WHERE id = :id"
            ),
            {
                "pass": pass_rate, "recall": recall, "run": len(outcomes),
                "scoreable": len(scoreable), "id": run_id,
                "metrics": json.dumps({
                    "elapsed_s": round(elapsed, 2),
                    "items_judged": len(judged),
                    "items_excluded": excluded,
                    "judge_unavailable": unavailable,
                    "judge_model": settings.llm_model_judge if judge else None,
                    "quality": quality,
                    "top_k": top_k,
                }),
            },
        )

    logger.info(
        "eval run %s: %d items, recall=%s, pass_rate=%s, judge_down=%d, "
        "excluded=%d, %.1fs",
        run_id, len(outcomes), recall, pass_rate, unavailable, excluded, elapsed,
    )
    if unavailable:
        # Loud, because the alternative is a pass rate computed over a subset
        # nobody was told about.
        logger.warning(
            "eval run %s: the judge was unavailable for %d of %d items — those are "
            "excluded from the pass rate rather than counted as failures",
            run_id, unavailable, len(outcomes),
        )
    return EvalRun(
        id=run_id, dataset_sha=dataset.content_sha256, code_rev=settings.release,
        model_id=settings.llm_model_cheap, cassette_sha=cassette_sha,
        answer_pass_rate=pass_rate, retrieval_recall=recall,
        items_run=len(outcomes), items_scoreable=len(scoreable),
        outcomes=outcomes, elapsed_s=elapsed,
        quality=quality,
        judge_model=settings.llm_model_judge if judge else None,
        judge_unavailable=unavailable, items_excluded=excluded,
    )


def load(ctx: RequestContext, run_id: uuid.UUID, *, with_outcomes: bool = True) -> EvalRun | None:
    """Rebuild a persisted run.

    :func:`platform_core.gates.promotion.promote` takes an :class:`EvalRun`, not
    an id — deliberately, so a caller cannot promote a run it never looked at.
    That works when the run was just computed in the same process and not at all
    when a person decides hours later, which is when a promotion actually
    happens. This closes that gap without loosening the signature.

    ``with_outcomes=False`` skips the per-item rows: promotion reads only the
    aggregates, and a five-hundred-item run has no reason to be materialised to
    move a pointer.
    """
    with tenant_session(ctx.tenant) as s:
        row = s.execute(
            text(
                "SELECT id, dataset_sha, code_rev, model_id, cassette_sha, "
                "  answer_pass_rate, retrieval_recall, items_run, items_scoreable, "
                "  metrics, status "
                "FROM eval_run WHERE id = :id"
            ),
            {"id": run_id},
        ).one_or_none()
        if row is None:
            return None

        outcomes: list[ItemOutcome] = []
        if with_outcomes:
            results = s.execute(
                text(
                    "SELECT item_id, question, passed, must_cite, retrieved, "
                    "  retrieval_recall, answer, detail "
                    "FROM eval_result WHERE eval_run_id = :r ORDER BY item_id"
                ),
                {"r": run_id},
            ).all()
            outcomes = [
                ItemOutcome(
                    item_id=r.item_id, question=r.question,
                    must_cite=list(r.must_cite or []), retrieved=list(r.retrieved or []),
                    retrieval_recall=r.retrieval_recall, passed=r.passed,
                    answer=r.answer or "", detail=dict(r.detail or {}),
                )
                for r in results
            ]

    return EvalRun(
        id=row.id, dataset_sha=row.dataset_sha, code_rev=row.code_rev,
        model_id=row.model_id, cassette_sha=row.cassette_sha,
        answer_pass_rate=row.answer_pass_rate, retrieval_recall=row.retrieval_recall,
        items_run=row.items_run or 0, items_scoreable=row.items_scoreable or 0,
        outcomes=outcomes,
        elapsed_s=float((row.metrics or {}).get("elapsed_s") or 0.0),
    )


def pgvector_retriever(embed: Callable[[list[str]], list[list[float]]]) -> Retriever:
    """Retrieval over pgvector, scoped by the tenant session.

    Returns **canonical ids only**. Ordinals exist inside the query and never
    cross this boundary — which is the whole discipline that keeps a stored eval
    result meaningful after the next rebuild renumbers everything.
    """

    def retrieve(ctx: RequestContext, collection: str, question: str,
                 top_k: int) -> list[str]:
        vector = embed([question])[0]
        with tenant_session(ctx.tenant) as s:
            # The eval gate must grade the corpus that is actually being
            # served. Reading across builds would let a half-written rebuild
            # change a gate's verdict, which is the one thing a gate must not
            # be susceptible to.
            build_version = builds.live_version_or_none(ctx, collection, session=s)
            if build_version is None:
                return []
            rows = s.execute(
                text(
                    "SELECT canonical_id FROM chunk "
                    "WHERE collection = :c AND build_version = :b "
                    "  AND embedding IS NOT NULL "
                    "ORDER BY embedding <=> CAST(:v AS vector) LIMIT :k"
                ),
                {"c": collection, "v": str(vector), "k": top_k, "b": build_version},
            ).scalars().all()
        return list(rows)

    return retrieve
