"""The eval gate as a reachable surface: who may run it, and what running it does.

``tests/properties/test_eval_gates.py`` proves the gate *decides* correctly —
that a degraded retriever is blocked, that an incomparable dataset is refused,
that an unscoreable run is not a pass. None of that was reachable outside pytest:
``platform_core/gates/`` had no caller anywhere in the tree.

These properties are about the exposure, not the decision, and deliberately do
not restate anything that file already covers:

* the three acts — measuring, reading and deciding — are three authorities;
* a queued eval scores the dataset version the caller asked for, not whatever
  the name points at when the worker gets to it;
* the workload reports the verdict and does not act on it;
* a stored run can be rebuilt well enough to promote, hours later, in another
  process.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from platform_core.db.engine import tenant_session
from platform_core.gates import datasets, promotion, runner
from platform_core.identity.capabilities import (
    ROLE_CAPABILITIES,
    Capability,
)
from platform_core.identity.principal import ActorType, Principal, RequestContext, Role
from workloads.eval import workload as eval_workload

DATASET = "exposure-set"
COLLECTION = "maintenance"

ITEMS = [
    {
        "id": f"q{i}",
        "question": f"What is the torque specification for spindle {i}?",
        "expected_answer": f"Spec {i}",
        "must_cite": [f"c_{i:016x}"],
    }
    for i in range(5)
]


def _principal(tenant, subject: str, role: Role) -> Principal:
    with tenant_session(tenant) as s:
        pid = s.execute(
            text(
                "INSERT INTO principal (tenant_id, subject, actor_type, roles) "
                "VALUES (:t, :s, 'human', :r) "
                "ON CONFLICT (tenant_id, subject) DO UPDATE SET roles = EXCLUDED.roles "
                "RETURNING id"
            ),
            {"t": tenant.id, "s": subject, "r": [str(role)]},
        ).scalar_one()
    return Principal(
        id=pid, tenant=tenant, subject=subject,
        roles=frozenset({role}), actor_type=ActorType.HUMAN,
    )


@pytest.fixture
def owner_ctx(tenant_a) -> RequestContext:
    return RequestContext(
        principal=_principal(tenant_a, "release@acme.example", Role.OWNER),
        labels={"task": "eval"},
    )


@pytest.fixture
def dataset(owner_ctx):
    return datasets.save(owner_ctx, name=DATASET, collection=COLLECTION, items=ITEMS)


def perfect_retriever(ctx, collection, question, top_k):
    index = int(question.split("spindle ")[1].rstrip("?"))
    return [f"c_{index:016x}"]


# ── the three authorities ─────────────────────────────────────────────────


def test_measuring_reading_and_deciding_are_three_authorities(
    tenant_a, record_evidence
):
    """An operator may score a set; only a promoter may change what "good" means.

    Asserted against the capability sets rather than through HTTP because the
    policy table maps routes onto exactly these, and a role that lacks the
    capability cannot reach the route by any path. The literal role names are
    checked too — a future edit that quietly granted ``release:promote`` to
    operators would otherwise leave this test green.
    """
    operator = ROLE_CAPABILITIES[Role.OPERATOR]
    reviewer = ROLE_CAPABILITIES[Role.REVIEWER]
    owner = ROLE_CAPABILITIES[Role.OWNER]

    # Measuring is not deciding: making people ask permission to measure is how
    # measurement stops happening.
    assert Capability.EVAL_RUN in operator
    assert Capability.EVAL_READ in operator and Capability.EVAL_READ in reviewer

    # But rewriting the golden set or moving the baseline is a release decision.
    assert Capability.RELEASE_PROMOTE not in operator
    assert Capability.RELEASE_PROMOTE not in reviewer
    assert Capability.RELEASE_PROMOTE in owner

    record_evidence(
        "eval_authorities_are_separate", holds=True,
        detail="operators may run and read; only release:promote writes sets or baselines",
    )


def test_writing_a_dataset_needs_the_promotion_capability(tenant_a, record_evidence):
    """Because whoever rewrites the questions can make any regression pass.

    This is the route policy's claim, checked against the policy table itself so
    a change to it is visible here rather than only in an integration test.
    """
    from platform_core.api.policy import ROUTE_CAPABILITIES

    assert ROUTE_CAPABILITIES[("PUT", "/api/eval/datasets/{name}")].capability is (
        Capability.RELEASE_PROMOTE
    )
    assert ROUTE_CAPABILITIES[("POST", "/api/eval/runs/{run_id}/promote")].capability is (
        Capability.RELEASE_PROMOTE
    )
    # And the cheaper acts are genuinely cheaper — otherwise the split above is
    # decorative and everything is really one authority.
    assert ROUTE_CAPABILITIES[("POST", "/api/eval/run")].capability is Capability.EVAL_RUN
    assert ROUTE_CAPABILITIES[("GET", "/api/eval")].capability is Capability.EVAL_READ

    record_evidence(
        "eval_dataset_writes_are_release_authority", holds=True,
        detail="authoring the golden set carries the same capability as moving the baseline",
    )


# ── what running does, and does not do ────────────────────────────────────


def test_the_workload_reports_the_verdict_without_acting_on_it(
    owner_ctx, dataset, monkeypatch, record_evidence
):
    """A worker must not promote its own candidate.

    The release equivalent of approving your own schema, and the same shape as an
    ingest path that decides a build is good by having finished. The verdict is
    computed and returned; the baseline pointer is left where it was.
    """
    monkeypatch.setattr(
        eval_workload.runner, "pgvector_retriever", lambda embed: perfect_retriever
    )
    monkeypatch.setattr(
        "platform_core.observability.llm.build_client",
        lambda **_: type("L", (), {"embed": lambda self, ctx, texts: [[0.1] * 1536]})(),
    )

    baseline_before = promotion._baseline_row(owner_ctx, DATASET)
    assert baseline_before is None, "precondition: this dataset has no baseline yet"

    result = eval_workload.run(
        owner_ctx, {"dataset": DATASET, "content_sha256": dataset.content_sha256}
    )

    assert result["retrieval_recall"] == 1.0
    assert result["gate"]["would_promote"] is True, result["gate"]["reasons"]

    # The verdict said yes and the pointer did not move. That gap is the property.
    assert promotion._baseline_row(owner_ctx, DATASET) is None

    record_evidence(
        "eval_workload_decides_without_promoting", holds=True,
        detail="the run reports would_promote and leaves the baseline untouched",
    )


def test_a_missing_dataset_fails_the_run_rather_than_scoring_nothing(owner_ctx):
    """Zero items scored must not read as a clean run.

    ``promotion.evaluate`` already refuses an unscoreable candidate; this refuses
    it a step earlier, so the queue does not carry a run that can only ever fail.
    """
    with pytest.raises(ValueError):
        eval_workload.run(owner_ctx, {"dataset": "no-such-dataset"})


def test_a_queued_eval_scores_the_version_that_was_asked_for(
    owner_ctx, dataset, monkeypatch, record_evidence
):
    """Pinning the hash is what makes the later comparison legitimate.

    Without it, editing the set between queueing and execution silently changes
    what is scored — and the run records the *new* hash, so the mismatch that
    ``promotion.evaluate`` exists to catch never even appears.
    """
    monkeypatch.setattr(
        eval_workload.runner, "pgvector_retriever", lambda embed: perfect_retriever
    )
    monkeypatch.setattr(
        "platform_core.observability.llm.build_client",
        lambda **_: type("L", (), {"embed": lambda self, ctx, texts: [[0.1] * 1536]})(),
    )
    pinned = dataset.content_sha256

    # The set moves on after the run was queued.
    revised = datasets.save(
        owner_ctx, name=DATASET, collection=COLLECTION,
        items=[*ITEMS, {"id": "q9", "question": "A later addition?",
                        "expected_answer": "", "must_cite": ["c_00000000000000ff"]}],
    )
    assert revised.content_sha256 != pinned, "precondition: the set must have changed"

    result = eval_workload.run(
        owner_ctx, {"dataset": DATASET, "content_sha256": pinned}
    )
    assert result["items_run"] == len(ITEMS)
    assert result["dataset_sha"] == pinned[:12]

    record_evidence(
        "eval_run_is_pinned_to_a_dataset_version", holds=True,
        detail="a set edited after queueing does not change what the run scored",
    )


# ── a run outlives the process that produced it ───────────────────────────


def test_a_stored_run_can_be_rebuilt_and_promoted(
    owner_ctx, dataset, monkeypatch, record_evidence
):
    """Promotion happens hours later, by a person, in another process.

    ``promote`` takes an ``EvalRun`` rather than an id on purpose — a caller
    should not be able to promote a run it never looked at. That is only workable
    if a stored run can be reconstituted, which nothing could do before.
    """
    monkeypatch.setattr(
        eval_workload.runner, "pgvector_retriever", lambda embed: perfect_retriever
    )
    monkeypatch.setattr(
        "platform_core.observability.llm.build_client",
        lambda **_: type("L", (), {"embed": lambda self, ctx, texts: [[0.1] * 1536]})(),
    )
    result = eval_workload.run(
        owner_ctx, {"dataset": DATASET, "content_sha256": dataset.content_sha256}
    )
    run_id = uuid.UUID(result["run_id"])

    reloaded = runner.load(owner_ctx, run_id)
    assert reloaded is not None
    assert reloaded.retrieval_recall == result["retrieval_recall"]
    assert reloaded.items_run == result["items_run"]
    assert reloaded.dataset_sha == dataset.content_sha256
    assert len(reloaded.outcomes) == len(ITEMS), "the per-item detail must survive too"

    decision = promotion.promote(owner_ctx, reloaded, dataset_name=DATASET)
    assert decision.promoted is True
    assert (promotion._baseline_row(owner_ctx, DATASET) or {})["run_id"] == str(run_id)

    # Aggregates only, for the path that just moves a pointer.
    lean = runner.load(owner_ctx, run_id, with_outcomes=False)
    assert lean is not None and lean.outcomes == []
    assert lean.retrieval_recall == reloaded.retrieval_recall

    record_evidence(
        "a_stored_eval_run_is_promotable", holds=True,
        detail="a persisted run reconstitutes with its metrics and per-item outcomes",
    )


def test_an_unknown_run_is_absent_rather_than_an_error(owner_ctx):
    assert runner.load(owner_ctx, uuid.uuid4()) is None
