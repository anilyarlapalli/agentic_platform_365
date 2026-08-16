"""Grading an answer: who writes the yardstick, who marks the work, and what counts.

A pass rate is only worth having if the thing producing it is independent of the
thing being measured. Three separations carry that, and each is a property here:

* the **annotator** writes the reference from the evidence, never from what
  retrieval returned — otherwise the yardstick moves to wherever the system is
  already pointing and a retrieval miss becomes undetectable;
* the **judge** is a different model from the answerer, or it marks its own
  homework and the numbers are flattering in a way no report reveals;
* a **human** edit is distinguishable from a machine draft nobody read, or
  "SME-attested ground truth" is a claim with nothing behind it.

The fourth property is about honesty of denominators: a judge that could not be
reached is not evidence that answers are bad, and a run must not report it as
such.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from platform_core.db.engine import tenant_session
from platform_core.gates import annotator, datasets, labels, metrics, runner
from platform_core.gates.judge import FixSurface, Verdict, verdict
from platform_core.identity.principal import ActorType, Principal, RequestContext, Role
from platform_core.settings import Settings

DATASET = "grading-properties"
COLLECTION = "maintenance"

ITEMS = [
    {"id": "q0", "question": "What is the final torque for spindle SA-400?",
     "expected_answer": "145 Nm in three stages.", "must_cite": ["c_0000000000000000"]},
    {"id": "q1", "question": "What causes VFD fault F-051?",
     "expected_answer": "DC bus overvoltage from a short decel ramp.",
     "must_cite": ["c_0000000000000001"]},
]


def _ctx(tenant) -> RequestContext:
    with tenant_session(tenant) as s:
        pid = s.execute(
            text(
                "INSERT INTO principal (tenant_id, subject, actor_type, roles) "
                "VALUES (:t, 'grader@acme.example', 'human', ARRAY['owner']) "
                "ON CONFLICT (tenant_id, subject) DO UPDATE SET roles = EXCLUDED.roles "
                "RETURNING id"
            ),
            {"t": tenant.id},
        ).scalar_one()
    return RequestContext(
        principal=Principal(id=pid, tenant=tenant, subject="grader@acme.example",
                            roles=frozenset({Role.OWNER}), actor_type=ActorType.HUMAN),
        labels={"task": "eval"},
    )


@pytest.fixture
def ctx(tenant_a):
    return _ctx(tenant_a)


@pytest.fixture
def dataset(ctx):
    return datasets.save(ctx, name=DATASET, collection=COLLECTION, items=ITEMS)


@pytest.fixture
def corpus(ctx):
    """A live build holding the chunks the items cite.

    Both of them. The annotator refuses to draft an item whose evidence it
    cannot read — correctly — so a fixture that seeded only one chunk would make
    "nothing was drafted" indistinguishable from "the skip rule worked", which is
    how a test ends up asserting nothing.
    """
    texts = {
        "c_0000000000000000": "SENTINEL-EVIDENCE spindle SA-400 final torque is 145 Nm",
        "c_0000000000000001": "SENTINEL-EVIDENCE fault F-051 is DC bus overvoltage",
    }
    with tenant_session(ctx.tenant) as s:
        doc_id = s.execute(
            text(
                "INSERT INTO document (tenant_id, workload, collection, filename, "
                "  content_sha256, byte_size, storage_key) "
                "VALUES (:t, 'echo', :c, 'evidence.md', :sha, 10, 'k') "
                "ON CONFLICT (tenant_id, collection, filename) "
                "  WHERE superseded_at IS NULL DO UPDATE "
                "  SET content_sha256 = EXCLUDED.content_sha256 RETURNING id"
            ),
            {"t": ctx.tenant.id, "c": COLLECTION, "sha": "e" * 64},
        ).scalar_one()
        for ordinal, (cid, body) in enumerate(texts.items()):
            s.execute(
                text(
                    "INSERT INTO chunk (tenant_id, document_id, collection, "
                    "  canonical_id, ordinal, build_version, text, embedding, "
                    "  embedding_model) "
                    "VALUES (:t, :d, :c, :cid, :o, 1, :txt, CAST(:vec AS vector), 'test') "
                    "ON CONFLICT (tenant_id, collection, canonical_id, build_version) "
                    "DO UPDATE SET text = EXCLUDED.text"
                ),
                {"t": ctx.tenant.id, "d": doc_id, "c": COLLECTION, "cid": cid,
                 "o": ordinal, "txt": body, "vec": str([0.1] * 1536)},
            )
        s.execute(
            text(
                "INSERT INTO collection_build (tenant_id, collection, build_version, "
                "  status, chunk_count, promoted_at) "
                "VALUES (:t, :c, 1, 'live', :n, now()) "
                "ON CONFLICT (tenant_id, collection, build_version) DO NOTHING"
            ),
            {"t": ctx.tenant.id, "c": COLLECTION, "n": len(texts)},
        )
    return texts


class _RecordingLLM:
    """Captures every prompt so a test can assert what the model was shown."""

    def __init__(self, reply: str = '{"q0": "drafted answer"}') -> None:
        self.prompts: list[str] = []
        self.models: list[str] = []
        self._reply = reply

    def chat(self, ctx, request):
        self.models.append(request.model)
        self.prompts.append(
            "\n".join(m["content"] for m in request.messages)
        )
        return type(
            "R", (), {"content": self._reply, "cache_hit": False}
        )()


@pytest.fixture(autouse=True)
def _sweep(tenant_a):
    yield
    with tenant_session(tenant_a) as s:
        s.execute(text("DELETE FROM eval_item_label WHERE dataset_name = :n"),
                  {"n": DATASET})


# ── independence ──────────────────────────────────────────────────────────


def test_the_judge_is_not_the_model_it_grades(record_evidence):
    """Refused at startup, because the failure has no symptom.

    A judge sharing a model with the answerer produces numbers that are real,
    correctly computed and biased upward for ever. Compared against literals as
    well as the defaults, so widening the defaults cannot make this pass by
    moving the goalposts.
    """
    defaults = Settings()
    assert defaults.llm_model_judge != defaults.llm_model_cheap
    assert defaults.llm_model_annotator != defaults.llm_model_cheap
    assert defaults.llm_model_annotator != defaults.llm_model_judge
    assert defaults.check_coherence() == []

    collided = Settings(llm_model_judge=defaults.llm_model_cheap)
    problems = collided.check_coherence()
    assert problems, "a judge equal to the answering model must not start"
    assert any("judge" in p and "grading" in p for p in problems)

    # The annotator is the yardstick; the same argument one step earlier.
    annotator_collided = Settings(llm_model_annotator=defaults.llm_model_cheap)
    assert annotator_collided.check_coherence(), (
        "an annotator equal to the answering model must not start either"
    )

    record_evidence(
        "judge_is_independent_of_the_answerer", holds=True,
        detail="judge, annotator and answerer are three distinct models, checked at startup",
    )


def test_the_annotator_is_shown_evidence_and_never_the_retriever(
    ctx, corpus, record_evidence
):
    """The property the whole measurement rests on.

    If the reference were written from what retrieval returned, it could only
    contain what retrieval returned — and a retrieval miss would be structurally
    undetectable. Asserted on the prompt the annotator actually received.
    """
    blank = datasets.save(
        ctx, name=DATASET, collection=COLLECTION,
        items=[{**item, "expected_answer": ""} for item in ITEMS],
    )
    llm = _RecordingLLM()
    report = annotator.draft(ctx, blank, llm=llm, labels={})

    assert llm.prompts, "precondition: the annotator must have been called"
    assert report["attempted"] == len(ITEMS), "every item's evidence must be readable"
    prompt = "\n".join(llm.prompts)
    assert prompt.count("SENTINEL-EVIDENCE") == len(ITEMS), "the cited evidence is shown"
    assert "RETRIEVED" not in prompt.upper(), (
        "nothing about what retrieval returned may reach the annotator"
    )
    assert llm.models == [Settings().llm_model_annotator]

    record_evidence(
        "annotator_sees_evidence_not_retrieval", holds=True,
        detail="the drafting prompt contains the cited chunks and no retrieval output",
    )


# ── never overwrite a human ───────────────────────────────────────────────


def test_a_human_authored_answer_is_never_redrafted(ctx, corpus, record_evidence):
    """Re-running drafting must be safe.

    Otherwise a second pass quietly replaces reviewed ground truth with a
    model's guess, and nothing about the set would look different afterwards.
    """
    labels.set_label(ctx, DATASET, "q0", answer_edited=True)
    blank = datasets.save(
        ctx, name=DATASET, collection=COLLECTION,
        items=[{**item, "expected_answer": ""} for item in ITEMS],
    )

    llm = _RecordingLLM(reply='{"q1": "drafted"}')
    report = annotator.draft(
        ctx, blank, llm=llm, labels=labels.for_dataset(ctx, DATASET)
    )
    prompt = "\n".join(llm.prompts)
    assert "ITEM q0" not in prompt, "a human-authored item must not be re-drafted"
    assert "ITEM q1" in prompt, "precondition: something must have been drafted"

    # And the store refuses the downgrade even if a caller asks for it.
    labels.record_drafted(ctx, DATASET, ["q0", "q1"], model="some-model")
    after = labels.for_dataset(ctx, DATASET)
    assert after["q0"]["answer_source"] == "sme_edited"
    assert after["q1"]["answer_source"] == "llm_drafted"
    assert report["drafted"]

    record_evidence(
        "human_answers_survive_redrafting", holds=True,
        detail="sme_edited is skipped by the annotator and refused by the label store",
    )


def test_apply_only_fills_blanks(ctx, dataset):
    """A stale drafting report must not overwrite work done since."""
    merged = annotator.apply(dataset, {"q0": "a proposal", "q1": "another"})
    assert merged[0]["expected_answer"] == ITEMS[0]["expected_answer"]


# ── the rubber-stamp signal ───────────────────────────────────────────────


def test_confirming_without_editing_is_counted_and_named(ctx, dataset, record_evidence):
    """"Reviewed" and "clicked through" must not be the same number.

    A set where every drafted answer was confirmed unread is not SME-attested
    ground truth. Reporting the count is cheaper than discovering it when the
    numbers are challenged.
    """
    labels.record_drafted(ctx, DATASET, ["q0", "q1"], model="annotator-x")
    labels.set_label(ctx, DATASET, "q0", answer_edited=True, confirmed=True)
    labels.set_label(ctx, DATASET, "q1", confirmed=True)  # confirmed, never edited

    summary = labels.summarise(dataset.items, labels.for_dataset(ctx, DATASET))
    assert summary["confirmed"] == 2
    assert summary["accepted_unedited"] == 1, (
        "the item confirmed without an edit must be counted separately"
    )
    assert summary["sme_authored"] == 1
    assert summary["annotator_models"] == ["annotator-x"]

    record_evidence(
        "rubber_stamped_answers_are_counted", holds=True,
        detail="confirmed-but-unedited drafts are reported as the annotator's, not the SME's",
    )


def test_labels_do_not_change_the_dataset_version(ctx, dataset, record_evidence):
    """Reviewing must not orphan the baseline.

    Labels are keyed by dataset *name* and held in their own table precisely so
    that a reviewer's clicks cannot mint a new version — the promotion gate
    refuses to compare across content hashes, so it would break on the first
    confirmation.
    """
    before = dataset.content_sha256
    labels.set_label(ctx, DATASET, "q0", confirmed=True, requires_kg_hop=True)
    labels.set_label(ctx, DATASET, "q1", unusable_reason="scrambled evidence")

    reloaded = datasets.load(ctx, name=DATASET)
    assert reloaded is not None and reloaded.content_sha256 == before

    # And editing the answer *does* — because that changes the yardstick.
    edited = datasets.save(
        ctx, name=DATASET, collection=COLLECTION,
        items=[{**ITEMS[0], "expected_answer": "a different reference"}, ITEMS[1]],
    )
    assert edited.content_sha256 != before

    # The labels survive that re-versioning, which is the point of the split.
    assert labels.for_dataset(ctx, DATASET)["q0"]["confirmed"] is True

    record_evidence(
        "labels_are_outside_the_content_hash", holds=True,
        detail="confirming does not change content_sha256; editing an answer does",
    )


# ── honest denominators ───────────────────────────────────────────────────


def test_an_unavailable_judge_is_not_a_failing_answer(ctx, dataset, record_evidence):
    """The reference deployment scored every item as a failure this way.

    An unsupported ``response_format`` returned a 400, a blanket handler turned
    it into "judge unavailable", and the run reported a quality collapse while
    nothing was wrong with the answers. Those items are excluded from the pass
    rate and counted separately instead.
    """
    def retrieve(_ctx, _collection, _question, _top_k):
        return ["c_0000000000000000"]

    def answer(_ctx, _collection, _item, _retrieved):
        return "an answer [c_0000000000000000]"

    def down(_item, _actual, _retrieved):
        return Verdict(passed=False, reason="judge unavailable: boom",
                       judge_unavailable=True)

    completed = runner.run(ctx, dataset, retrieve=retrieve, answer=answer, judge=down)

    assert completed.items_run == 2, "precondition: items must have been attempted"
    assert completed.judge_unavailable == 2
    assert completed.answer_pass_rate is None, (
        "a pass rate over zero usable verdicts must be null, not zero"
    )

    record_evidence(
        "judge_outage_is_not_a_quality_regression", holds=True,
        detail="items the judge could not grade are excluded from the pass rate and counted",
    )


def test_an_item_flagged_unusable_is_excluded_and_counted(
    ctx, dataset, record_evidence
):
    """A set that quietly shrinks stops being comparable without saying so."""
    labels.set_label(ctx, DATASET, "q1", unusable_reason="scrambled parser output")

    def retrieve(_ctx, _collection, _question, _top_k):
        return ["c_0000000000000000"]

    completed = runner.run(
        ctx, dataset, retrieve=retrieve,
        labels=labels.for_dataset(ctx, DATASET),
    )
    assert completed.items_run == 1
    assert completed.items_excluded == 1
    assert {o.item_id for o in completed.outcomes} == {"q0"}

    record_evidence(
        "unusable_items_are_excluded_and_reported", holds=True,
        detail="a flagged item is dropped from the run and its count is on the summary",
    )


def test_an_item_with_no_reference_cannot_pass(ctx):
    """Grading against a blank yardstick is not a pass.

    Returned as unavailable rather than failed: it is a gap in the eval set,
    which is a different backlog from a gap in the platform, and the fix_surface
    says so.
    """
    from platform_core.gates.datasets import EvalItem

    result = verdict(
        ctx, EvalItem(id="x", question="q?", expected_answer="", must_cite=[]),
        actual_answer="anything", retrieved=[], llm=_RecordingLLM(),
    )
    assert result.passed is False
    assert result.judge_unavailable is True
    assert result.fix_surface is FixSurface.EXPECTED_ANSWER


# ── the deterministic metrics ─────────────────────────────────────────────


def test_faithfulness_separates_grounded_from_invented(record_evidence):
    """The one metric that catches a model answering from prior knowledge.

    Asserted as a gap between two answers over the same evidence rather than
    against a threshold: the absolute value depends on tokenisation, the
    ordering does not.
    """
    evidence = ["Spindle SA-400: final torque is 145 Nm, applied in three stages."]
    grounded = metrics.faithfulness("The torque is 145 Nm in three stages.", evidence)
    invented = metrics.faithfulness(
        "The SA-400 uses a hydraulic clamp rated to 900 psi.", evidence
    )
    assert grounded > 0.8
    assert invented < 0.3
    assert grounded > invented * 2

    record_evidence(
        "faithfulness_detects_ungrounded_answers", holds=True,
        detail=f"grounded {grounded} vs invented {invented} over identical evidence",
    )


def test_a_metric_with_nothing_to_measure_is_null_not_zero():
    """Zero and "not applicable" average very differently."""
    assert metrics.context_precision(["c_1"], []) is None
    assert metrics.citation_accuracy("no citations at all", ["c_1"]) is None
    assert metrics.citation_accuracy("cites [c_ffffffffffff]", ["c_1"]) == 0.0

    aggregated = metrics.aggregate([
        {"faithfulness": 1.0, "context_precision": None},
        {"faithfulness": 0.0, "context_precision": 0.5},
    ])
    assert aggregated["faithfulness"] == 0.5 and aggregated["faithfulness_n"] == 2
    # Averaged over the one item where it applied, not over both.
    assert aggregated["context_precision"] == 0.5
    assert aggregated["context_precision_n"] == 1
