"""A regression must block promotion, and the gate must refuse to guess.

The acceptance case is the first one: inject a deliberately degraded retriever
and require the gate to block. Everything else here covers the ways a gate can
*look* like it is working while permitting a regression through — which, on this
codebase's record, is the more likely failure.

Where the Azure build stands for comparison: it computes both metrics correctly
and writes them to a single blob per domain that the next run overwrites. There
is no baseline, no history, and nothing capable of refusing anything.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from platform_core.db.engine import tenant_session
from platform_core.gates import datasets, promotion
from platform_core.gates.datasets import InvalidDataset
from platform_core.gates.runner import run as run_eval
from platform_core.identity.capabilities import Capability, NotAuthorized
from platform_core.identity.principal import Principal, RequestContext, Role

pytestmark = pytest.mark.property

DATASET = "maintenance-golden"
COLLECTION = "maintenance"

# Five questions, each citing one chunk. Small enough to reason about, large
# enough that a 40% degradation is not one item's worth of noise.
ITEMS = [
    {
        "id": f"q{i}",
        "question": f"What is the torque specification for spindle {i}?",
        "expected_answer": f"Spec {i}",
        "must_cite": [f"c_{i:016x}"],
    }
    for i in range(5)
]


@pytest.fixture
def promoter(tenant_a, principal_a) -> Principal:
    """A principal that may promote. RELEASE_PROMOTE is an owner capability."""
    with tenant_session(tenant_a) as s:
        pid = s.execute(
            text(
                "INSERT INTO principal (tenant_id, subject, roles) "
                "VALUES (:t, 'release@acme.example', ARRAY['owner']) "
                "ON CONFLICT (tenant_id, subject) DO UPDATE SET roles = EXCLUDED.roles "
                "RETURNING id"
            ),
            {"t": tenant_a.id},
        ).scalar_one()
    return Principal(
        id=pid, tenant=tenant_a, subject="release@acme.example",
        roles=frozenset({Role.OWNER}),
    )


@pytest.fixture
def ctx(promoter) -> RequestContext:
    return RequestContext(principal=promoter, labels={"task": "eval"})


@pytest.fixture
def dataset(ctx):
    return datasets.save(ctx, name=DATASET, collection=COLLECTION, items=ITEMS)


def perfect_retriever(ctx, collection, question, top_k):
    """Returns the right evidence for every question. Recall 1.0."""
    index = int(question.split("spindle ")[1].rstrip("?"))
    return [f"c_{index:016x}"]


def degraded_retriever(ctx, collection, question, top_k):
    """Returns the right evidence for only the first two questions. Recall 0.4.

    Deliberately *partial* rather than broken. A retriever returning nothing at
    all would be caught by any check; one that quietly gets 40% right is the
    realistic regression, and it is what the tolerance has to be sensitive
    enough to catch.
    """
    index = int(question.split("spindle ")[1].rstrip("?"))
    return [f"c_{index:016x}"] if index < 2 else ["c_deadbeefdeadbeef"]


# ── the acceptance case ──────────────────────────────────────────────────


def test_a_degraded_retriever_blocks_promotion(ctx, dataset, record_evidence):
    """Phase 4's acceptance test."""
    good = run_eval(ctx, dataset, retrieve=perfect_retriever)
    assert good.retrieval_recall == 1.0

    accepted = promotion.promote(ctx, good, dataset_name=DATASET, note="initial baseline")
    assert accepted.promoted

    bad = run_eval(ctx, dataset, retrieve=degraded_retriever)
    assert bad.retrieval_recall == pytest.approx(0.4)

    decision = promotion.promote(ctx, bad, dataset_name=DATASET)
    assert not decision.promoted, (
        f"a retriever that lost 60% of its recall was promoted:\n{decision.explain()}"
    )
    assert any("retrieval_recall fell" in r for r in decision.reasons), decision.reasons
    assert decision.deltas["retrieval_recall"] == pytest.approx(-0.6)

    # And the baseline did not move.
    current = promotion._baseline_row(ctx, DATASET)
    assert current["run_id"] == str(good.id), "a blocked candidate still became the baseline"

    record_evidence(
        "eval_gate_blocks_regression", holds=True,
        baseline_recall=good.retrieval_recall, candidate_recall=bad.retrieval_recall,
        delta=decision.deltas["retrieval_recall"], reasons=decision.reasons,
        detail="a 60% recall regression is refused and the baseline pointer is unmoved",
    )


def test_history_is_retained_across_runs(ctx, dataset, record_evidence):
    """Runs accumulate. This is what overwriting a single blob destroys."""
    first = run_eval(ctx, dataset, retrieve=perfect_retriever)
    promotion.promote(ctx, first, dataset_name=DATASET)
    second = run_eval(ctx, dataset, retrieve=degraded_retriever)
    promotion.promote(ctx, second, dataset_name=DATASET)
    third = run_eval(ctx, dataset, retrieve=perfect_retriever)

    entries = promotion.history(ctx, DATASET)
    assert len(entries) == 3, f"expected 3 retained runs, saw {len(entries)}"
    assert [e["run_id"] for e in entries] == [str(third.id), str(second.id), str(first.id)]
    assert sum(1 for e in entries if e["is_baseline"]) == 1
    # The blocked run is still there — a regression must remain inspectable.
    assert any(e["run_id"] == str(second.id) for e in entries)

    record_evidence(
        "eval_history_retained", holds=True, runs=len(entries),
        detail="every run is retained, including blocked ones; baseline is a pointer",
    )


# ── the ways a gate can silently stop gating ─────────────────────────────


def test_an_unscoreable_run_is_not_a_pass(ctx, record_evidence):
    """A run with nothing to score must not read as "no regression detected".

    This is the vacuity failure this codebase has hit three times in other
    forms — a check whose pass state is indistinguishable from its
    nothing-to-check state.
    """
    empty = datasets.save(
        ctx, name="no-evidence", collection=COLLECTION,
        items=[{"id": "q0", "question": "What is spindle 0?", "expected_answer": "x"}],
    )
    result = run_eval(ctx, empty, retrieve=perfect_retriever)
    assert result.retrieval_recall is None
    assert result.answer_pass_rate is None

    decision = promotion.evaluate(ctx, result, dataset_name="no-evidence")
    assert not decision.promoted, "an unscoreable run was treated as a pass"
    assert any("unscoreable" in r for r in decision.reasons)

    record_evidence(
        "eval_gate_rejects_unscoreable", holds=True,
        detail="a run producing no metric is refused rather than read as no-regression",
    )


def test_an_incomparable_dataset_blocks(ctx, dataset, record_evidence):
    """Scores over different questions are not comparable, however good they look."""
    good = run_eval(ctx, dataset, retrieve=perfect_retriever)
    promotion.promote(ctx, good, dataset_name=DATASET)

    # A new version of the same dataset: one question changed.
    revised = datasets.save(
        ctx, name=DATASET, collection=COLLECTION,
        items=[*ITEMS[:-1], {**ITEMS[-1], "question": "What is the revised spec?",
                             "must_cite": ["c_00000000000000ff"]}],
    )
    assert revised.content_sha256 != dataset.content_sha256

    def retriever(ctx, collection, question, top_k):
        if "revised" in question:
            return ["c_00000000000000ff"]
        return perfect_retriever(ctx, collection, question, top_k)

    candidate = run_eval(ctx, revised, retrieve=retriever)
    assert candidate.retrieval_recall == 1.0  # a perfect score

    decision = promotion.evaluate(ctx, candidate, dataset_name=DATASET)
    assert not decision.promoted, (
        "a perfect score on a different dataset was accepted as evidence of no regression"
    )
    assert any("not comparable" in r for r in decision.reasons)

    record_evidence(
        "eval_gate_refuses_incomparable_baseline", holds=True,
        detail="a 1.0 recall on a different dataset_sha is refused, not accepted",
    )


def test_a_smaller_sample_does_not_beat_a_larger_one(ctx, dataset, record_evidence):
    """5 scoreable items must not silently replace a baseline of 25."""
    good = run_eval(ctx, dataset, retrieve=perfect_retriever)
    promotion.promote(ctx, good, dataset_name=DATASET)

    # Same dataset hash, but retrieval errors on three items so only two are
    # scoreable. Recall over those two is perfect.
    def flaky(ctx, collection, question, top_k):
        index = int(question.split("spindle ")[1].rstrip("?"))
        if index >= 2:
            raise RuntimeError("retriever unavailable")
        return [f"c_{index:016x}"]

    candidate = run_eval(ctx, dataset, retrieve=flaky)
    decision = promotion.evaluate(ctx, candidate, dataset_name=DATASET)
    assert not decision.promoted, decision.explain()

    record_evidence(
        "eval_gate_sample_size_guarded", holds=True,
        baseline_items=good.items_scoreable, candidate_items=candidate.items_scoreable,
        detail="errors that shrink the scoreable set are treated as a regression",
    )


def test_a_metric_that_stopped_being_measurable_is_a_regression(ctx, dataset,
                                                                record_evidence):
    """Losing the ability to measure is not the same as measuring no change."""
    good = run_eval(ctx, dataset, retrieve=perfect_retriever)
    promotion.promote(ctx, good, dataset_name=DATASET)

    stripped = datasets.save(
        ctx, name="stripped", collection=COLLECTION,
        items=[{**i, "must_cite": []} for i in ITEMS],
    )
    candidate = run_eval(ctx, stripped, retrieve=perfect_retriever)
    assert candidate.retrieval_recall is None

    decision = promotion.evaluate(ctx, candidate, dataset_name=DATASET)
    assert not decision.promoted

    record_evidence(
        "eval_gate_missing_metric_is_regression", holds=True,
        detail="a candidate with no recall against a baseline that had one is refused",
    )


# ── dataset integrity ────────────────────────────────────────────────────


def test_non_canonical_citations_are_rejected(ctx, record_evidence):
    """A citation the retriever can never emit scores a permanent miss.

    The Azure eval sets carry a synthetic `page:…` handle written by a drafting
    fallback; it halves the recall of every item that has one, indistinguishably
    from a real retrieval failure.
    """
    with pytest.raises(InvalidDataset, match="canonical"):
        datasets.save(
            ctx, name="bad-citations", collection=COLLECTION,
            items=[{"id": "q0", "question": "?", "expected_answer": "x",
                    "must_cite": ["page:12"]}],
        )

    record_evidence(
        "eval_dataset_rejects_unreachable_citations", holds=True,
        detail="a non-canonical must_cite is refused at construction",
    )


def test_editing_a_question_creates_a_new_version(ctx, record_evidence):
    """Datasets are content-addressed, so history keeps pointing at real questions."""
    first = datasets.save(ctx, name="versioned", collection=COLLECTION, items=ITEMS)
    edited = datasets.save(
        ctx, name="versioned", collection=COLLECTION,
        items=[{**ITEMS[0], "question": "changed?"}, *ITEMS[1:]],
    )
    assert first.content_sha256 != edited.content_sha256
    assert first.id != edited.id

    # The original version is still loadable by its hash.
    recovered = datasets.load(ctx, name="versioned", content_sha256=first.content_sha256)
    assert recovered is not None
    assert recovered.items[0].question == ITEMS[0]["question"]

    # Reordering is not a new version — identity is the set, not the sequence.
    reordered = datasets.save(
        ctx, name="versioned", collection=COLLECTION, items=list(reversed(ITEMS))
    )
    assert reordered.content_sha256 == first.content_sha256

    record_evidence(
        "eval_dataset_content_addressed", holds=True,
        detail="editing forks a version; reordering does not",
    )


# ── authority ────────────────────────────────────────────────────────────


def test_promotion_requires_the_release_capability(tenant_a, principal_a, dataset,
                                                   ctx, record_evidence):
    """Moving a baseline is a release action, not an eval action.

    Running an eval and deciding it is good enough to become the reference are
    different authorities. `principal_a` is an operator: it may run evals.
    """
    candidate = run_eval(ctx, dataset, retrieve=perfect_retriever)
    operator_ctx = RequestContext(principal=principal_a, labels={"task": "eval"})

    with pytest.raises(NotAuthorized) as err:
        promotion.promote(operator_ctx, candidate, dataset_name=DATASET)
    assert Capability.RELEASE_PROMOTE in str(err.value)

    record_evidence(
        "eval_promotion_requires_capability", holds=True,
        detail="an operator may run an eval but not move the baseline",
    )


def test_a_forced_promotion_is_audited_with_what_it_overrode(ctx, dataset,
                                                             record_evidence):
    """An override is legitimate; an invisible override is not.

    A gate with no override gets bypassed by deleting the baseline, which loses
    the history. So force exists — and records exactly what it ignored.
    """
    from platform_core.observability import audit

    good = run_eval(ctx, dataset, retrieve=perfect_retriever)
    promotion.promote(ctx, good, dataset_name=DATASET)

    bad = run_eval(ctx, dataset, retrieve=degraded_retriever)
    decision = promotion.promote(
        ctx, bad, dataset_name=DATASET, force=True,
        note="accepting lower recall for a 10x cost reduction",
    )
    assert decision.promoted

    events = audit.recent(ctx, limit=10)
    forced = [e for e in events if e.action == "eval.promotion.forced"]
    assert forced, [e.action for e in events]
    assert forced[0].detail["overridden_reasons"], "the override recorded nothing"
    assert any("retrieval_recall fell" in r for r in forced[0].detail["overridden_reasons"])
    assert forced[0].detail["note"]

    record_evidence(
        "eval_forced_promotion_audited", holds=True,
        overridden=forced[0].detail["overridden_reasons"],
        detail="a forced promotion records the reasons it overrode and the stated note",
    )
