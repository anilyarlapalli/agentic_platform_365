"""Seeding a golden set from questions a reviewer approved.

Eval sets were authored by hand until now, which meant writing the evidence
chunk ids by hand too — and an id the retriever cannot emit scores a permanent
miss indistinguishable from a real retrieval failure. That is the defect
``build_dataset`` rejects non-canonical citations to prevent, and the reason the
reference deployment's seeded sets cannot score recall at all: its drafter chunks
the corpus independently of ingestion, so its ``evidence_chunk_ids`` name a
private namespace.

Here the drafter reads through the same live build the retriever queries, so the
ids are canonical by construction. These properties hold that line:

* what gets seeded cites chunks the retriever really returns, and the validation
  refuses anything else rather than storing a set that can only ever score zero;
* only questions somebody approved are seeded, or the review step is decorative;
* an edit is recorded separately from an approval, because a question a human
  rewrote and one they waved through are different evidence.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import text

from platform_core.db.engine import tenant_session
from platform_core.gates import datasets
from platform_core.identity.principal import ActorType, Principal, RequestContext, Role
from workloads.onboarding import store

DOMAIN = "query-properties"
COLLECTION = "query-properties"

# Canonical ids, the shape `load_documents` returns and `build_dataset` requires.
CID_A = "c_00000000000000aa"
CID_B = "c_00000000000000bb"

PROPOSED = {
    "queries": [
        {"id": "q0", "text": "What is the final torque for spindle SA-400?",
         "evidence_chunk_ids": [CID_A], "source_file": "spec.md", "page": 0,
         "entity_hints": ["spindle"], "approved": False, "edited": False},
        {"id": "q1", "text": "What causes VFD fault F-051?",
         "evidence_chunk_ids": [CID_B], "source_file": "faults.md", "page": 0,
         "entity_hints": ["VFD"], "approved": False, "edited": False},
        {"id": "q2", "text": "What is the lubrication interval?",
         "evidence_chunk_ids": [], "source_file": "lube.md", "page": 0,
         "entity_hints": [], "approved": False, "edited": False},
    ]
}


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
    return Principal(id=pid, tenant=tenant, subject=subject,
                     roles=frozenset({role}), actor_type=ActorType.HUMAN)


@pytest.fixture
def ctx(tenant_a) -> RequestContext:
    return RequestContext(
        principal=_principal(tenant_a, "author@acme.example", Role.OWNER)
    )


@pytest.fixture
def session_id(ctx) -> uuid.UUID:
    with tenant_session(ctx.tenant) as s:
        sid = store.create(s, ctx, domain=DOMAIN, collection=COLLECTION)
        s.execute(
            text("UPDATE onboarding_session SET status = 'draft_ready' WHERE id = :i"),
            {"i": sid},
        )
    store.put_artifact(ctx, sid, "candidate_queries", "candidate_queries", PROPOSED)
    return sid


@pytest.fixture(autouse=True)
def _sweep(tenant_a):
    yield
    with tenant_session(tenant_a) as s:
        s.execute(text("DELETE FROM onboarding_session WHERE domain = :d"), {"d": DOMAIN})
        s.execute(text("DELETE FROM eval_dataset WHERE name = :n"), {"n": DOMAIN})


def _seed(ctx, session_id):
    """Seed through the real route, not a copy of what it does.

    Calling the handler rather than re-implementing its filter is the difference
    between testing the seeding rule and testing a second version of it that can
    drift. The HTTP layer adds nothing here — the capability split is enforced by
    the policy table and covered in `test_eval_exposure`.
    """
    from platform_core.api.routes.onboarding import SeedEval, seed_eval_set

    result = seed_eval_set(session_id, SeedEval(), ctx)
    loaded = datasets.load(ctx, name=result["dataset"],
                           content_sha256=result["content_sha256"])
    assert loaded is not None
    return loaded


# ── the ids are the point ─────────────────────────────────────────────────


def test_seeded_questions_cite_ids_the_retriever_can_emit(ctx, session_id,
                                                          record_evidence):
    """The property the reference deployment could not hold.

    ``build_dataset`` refuses a citation that is not a canonical
    ``c_<hex>`` id, precisely because one the retriever never returns scores a
    permanent miss that looks identical to a genuine retrieval failure. A seeded
    set has to survive that validation without any loosening.
    """
    store.curate_query(ctx, session_id, "q0", approved=True)
    store.curate_query(ctx, session_id, "q1", approved=True)

    dataset = _seed(ctx, session_id)

    assert len(dataset.items) == 2
    assert {c for item in dataset.items for c in item.must_cite} == {CID_A, CID_B}
    # Scoreable means recall can be computed at all — the whole reason the ids
    # have to be canonical.
    assert len(dataset.scoreable_items) == 2

    record_evidence(
        "seeded_evidence_ids_are_canonical", holds=True,
        detail="approved questions seed a dataset whose citations pass build_dataset",
    )


def test_a_non_canonical_id_is_refused_rather_than_stored(ctx, session_id,
                                                          record_evidence):
    """Refused at seed time, not discovered as a zero at score time.

    The generator drops queries grounded in ids it does not recognise and the
    workload filters again against the corpus, so reaching this is a bug
    upstream. The remedy is to re-draft, never to relax the validation that makes
    recall mean something.
    """
    store.put_artifact(
        ctx, session_id, "candidate_queries", "candidate_queries",
        {"queries": [{**PROPOSED["queries"][0], "approved": True,
                      "evidence_chunk_ids": ["spec.md#p3#a1b2c3d"]}]},
    )
    # A 409 from the route, carrying the reason — not a stored set that scores
    # zero on every run with nothing saying why.
    with pytest.raises(HTTPException) as caught:
        _seed(ctx, session_id)
    assert caught.value.status_code == 409
    assert "scoreable" in str(caught.value.detail)

    record_evidence(
        "non_canonical_citations_never_seed", holds=True,
        detail="a synthesised chunk handle is refused before it can score a false miss",
    )


def test_a_question_with_no_evidence_is_seedable_but_not_scoreable(ctx, session_id):
    """Both facts matter, and they are different facts.

    An item with no citations can still score answer quality, and folding it into
    the recall average would dilute the metric silently — which is why the runner
    reports ``items_scoreable`` beside ``items_run``.
    """
    store.curate_query(ctx, session_id, "q2", approved=True)
    dataset = _seed(ctx, session_id)

    assert len(dataset.items) == 1
    assert len(dataset.scoreable_items) == 0


# ── review is not decorative ──────────────────────────────────────────────


def test_seeding_with_nothing_approved_is_refused(ctx, session_id):
    """A set of everything that was proposed is not a reviewed set."""
    with pytest.raises(HTTPException) as caught:
        _seed(ctx, session_id)
    assert caught.value.status_code == 409


def test_only_approved_questions_are_seeded(ctx, session_id, record_evidence):
    """Seeding everything proposed would make the review step ornamental."""
    store.curate_query(ctx, session_id, "q1", approved=True)

    dataset = _seed(ctx, session_id)
    assert [item.id for item in dataset.items] == ["q1"]

    # And un-approving removes it again — the state is read at seed time rather
    # than latched when the box was ticked.
    store.curate_query(ctx, session_id, "q1", approved=False)
    assert not [q for q in store.candidate_queries(ctx, session_id) if q["approved"]]

    record_evidence(
        "only_reviewed_questions_become_ground_truth", holds=True,
        detail="unapproved proposals are excluded from the seeded set",
    )


def test_an_edit_is_recorded_separately_from_an_approval(ctx, session_id,
                                                         record_evidence):
    """The same distinction the eval set draws between sme_edited and confirmed.

    A question a human rewrote and one they waved through are different evidence
    that anybody read it, and collapsing them is how "reviewed" stops meaning
    anything.
    """
    before = store.candidate_queries(ctx, session_id)[0]
    assert before["edited"] is False, "precondition: nothing is edited yet"

    approved_only = store.curate_query(ctx, session_id, "q0", approved=True)
    assert approved_only["approved"] is True
    assert approved_only["edited"] is False, "approving is not editing"

    edited = store.curate_query(
        ctx, session_id, "q0", query_text="What is the SA-400 final torque, in Nm?"
    )
    assert edited["edited"] is True
    assert edited["text"].endswith("in Nm?")

    # Re-submitting identical text is not an edit.
    again = store.curate_query(ctx, session_id, "q1", query_text=PROPOSED["queries"][1]["text"])
    assert again["edited"] is False

    record_evidence(
        "query_edits_are_distinguishable_from_approvals", holds=True,
        detail="editing sets `edited`; approving alone does not",
    )


def test_the_seeded_question_is_the_edited_one(ctx, session_id):
    """An edit that did not reach the dataset would be worse than no edit."""
    store.curate_query(ctx, session_id, "q0", query_text="A rewritten question?",
                       approved=True)
    dataset = _seed(ctx, session_id)
    assert dataset.items[0].question == "A rewritten question?"


# ── lifecycle ─────────────────────────────────────────────────────────────


def test_a_published_sessions_questions_are_fixed(ctx, session_id):
    """Otherwise a published domain's questions could change with no approval."""
    with tenant_session(ctx.tenant) as s:
        s.execute(
            text("UPDATE onboarding_session SET status = 'published' WHERE id = :i"),
            {"i": session_id},
        )
    with pytest.raises(ValueError):
        store.curate_query(ctx, session_id, "q0", approved=True)


def test_seeding_the_same_approvals_twice_is_one_version(ctx, session_id):
    """Datasets are content-addressed, so re-seeding is idempotent.

    It matters because the obvious operator response to an unclear result is to
    do it again, and a second identical version would orphan the baseline for no
    reason.
    """
    store.curate_query(ctx, session_id, "q0", approved=True)
    first = _seed(ctx, session_id)
    second = _seed(ctx, session_id)
    assert first.content_sha256 == second.content_sha256


def test_an_empty_proposal_reads_as_zero_not_as_absence(ctx):
    """A corpus of short chunks proposes nothing, and that must be visible.

    ``_stratified_sample`` drops chunks under 300 characters, so a corpus like
    the six-chunk demo set yields no questions at all. Reported as a count on the
    session, for the same reason ``relations_available`` is: zero and "not
    attempted" look identical from the outside and need different responses.
    """
    from workloads.onboarding.workload import _propose_queries

    result = _propose_queries([], n_queries=5)
    assert result["queries"] == []
    assert "error" not in result, "an empty corpus is not an error, it is a count"


def test_the_adapter_carries_the_canonical_id_into_the_generator():
    """The single line the whole seeding path rests on.

    ``_chunk_identifier`` prefers an explicit ``chunk_id`` and otherwise
    synthesises ``<file>#p<page>#<sha1:7>`` — a private namespace in which, as
    its own docstring says, "every recall / citation metric computed against
    them reads 0.0". The reference deployment ships in that state. Carrying the
    canonical id through the adapter is what avoids it.
    """
    from workloads.onboarding.workload import _chunk_views

    views = _chunk_views([
        {"chunk_id": CID_A, "text": "body text", "source": "spec.md",
         "metadata": {"page": 4}},
        {"chunk_id": CID_B, "text": "other", "source": "faults.md", "metadata": {}},
    ])
    assert [v.chunk_id for v in views] == [CID_A, CID_B]
    assert views[0].content == "body text"
    assert views[0].source_file == "spec.md"
    assert views[0].page == 4
    # Absent page is 0, which disables the front/back-matter skip rather than
    # excluding the chunk — the sampler filters falsy pages out first.
    assert views[1].page == 0
