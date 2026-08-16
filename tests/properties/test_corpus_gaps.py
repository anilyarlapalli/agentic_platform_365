"""Questions the corpus could not answer: recorded, counted, and eventually forgotten.

An ungrounded chat turn is the most valuable eval item available — a real person
wanted an answer and the platform had none — and until now it vanished with the
session twelve hours later.

Four properties make the record worth keeping:

* **it is only the failures.** A durable log of everything users ask is a
  different thing to hold, justified by nothing stronger than "it might be
  useful". This table is a list of things the corpus should cover and does not;
* **it never costs an answer.** Bookkeeping about a reply must not be able to
  break the reply;
* **it counts.** A gap forty people hit and one somebody hit once are different
  backlog items, and only the deduplicated count says which is which;
* **it expires.** Unactioned user content does not accumulate for ever, and the
  retention is enforced by a sweep rather than described in a docstring.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from platform_core.corpus import gaps
from platform_core.db.engine import owner_session, tenant_session
from platform_core.identity.principal import ActorType, Principal, RequestContext, Role

COLLECTION = "gap-properties"


def _ctx(tenant) -> RequestContext:
    with tenant_session(tenant) as s:
        pid = s.execute(
            text(
                "INSERT INTO principal (tenant_id, subject, actor_type, roles) "
                "VALUES (:t, 'asker@acme.example', 'human', ARRAY['operator']) "
                "ON CONFLICT (tenant_id, subject) DO UPDATE SET roles = EXCLUDED.roles "
                "RETURNING id"
            ),
            {"t": tenant.id},
        ).scalar_one()
    return RequestContext(
        principal=Principal(id=pid, tenant=tenant, subject="asker@acme.example",
                            roles=frozenset({Role.OPERATOR}),
                            actor_type=ActorType.HUMAN),
        labels={"workload": "chat", "task": "chat"},
    )


@pytest.fixture
def ctx(tenant_a):
    return _ctx(tenant_a)


@pytest.fixture(autouse=True)
def _sweep(tenant_a):
    yield
    with owner_session() as s:
        s.execute(text("DELETE FROM unanswered_question WHERE collection = :c"),
                  {"c": COLLECTION})


class _FakeLLM:
    """Enough to reach the ungrounded branch without spending anything."""

    def embed(self, ctx, texts, *, model=None):
        return [[0.1] * 1536 for _ in texts]

    def chat(self, ctx, request):  # pragma: no cover — must never be reached
        raise AssertionError("an ungrounded turn must not call the model")


# ── the record ────────────────────────────────────────────────────────────


def test_an_unanswerable_question_is_recorded_from_the_chat_path(
    ctx, record_evidence
):
    """Recorded where the refusal happens, not mined from the session.

    Sessions expire in twelve hours. A record that only survives that long would
    mean anything asked on a Friday evening is gone by Saturday, which makes the
    backlog an artefact of when somebody happened to look.
    """
    from workloads.chat import service as chat

    answer = chat.answer(
        ctx, question="What is the warranty period for the SA-400?",
        collection="a-collection-with-no-build", llm=_FakeLLM(),
    )
    assert answer.grounded is False, "precondition: this must be the refusal path"

    backlog = gaps.backlog(ctx, collection="a-collection-with-no-build")
    assert [g["question"] for g in backlog] == [
        "What is the warranty period for the SA-400?"
    ]
    assert backlog[0]["mode"] == "dense"

    with owner_session() as s:
        s.execute(
            text("DELETE FROM unanswered_question WHERE collection = :c"),
            {"c": "a-collection-with-no-build"},
        )

    record_evidence(
        "unanswered_questions_are_recorded", holds=True,
        detail="an ungrounded chat turn writes a durable backlog row",
    )


def test_a_grounded_answer_records_nothing(ctx, seeded_corpus):
    """Only the failures. This is a backlog, not a transcript.

    The distinction is the whole reason the table is something a reviewer can be
    shown: it holds what the corpus does not cover, not what people asked it.
    """
    from workloads.chat import service as chat

    with tenant_session(ctx.tenant) as s:
        s.execute(
            text(
                "INSERT INTO collection_build (tenant_id, collection, build_version, "
                "  status, chunk_count, promoted_at) "
                "VALUES (:t, 'maintenance', 1, 'live', 1, now()) "
                "ON CONFLICT (tenant_id, collection, build_version) DO NOTHING"
            ),
            {"t": ctx.tenant.id},
        )

    class _Answering(_FakeLLM):
        def chat(self, ctx, request):
            return type("R", (), {
                "content": "The torque is 145 Nm [c_acme0000000000].",
                "cache_hit": False, "cost_usd": 0.0,
                "usage": type("U", (), {"input_tokens": 1, "output_tokens": 1})(),
                "finish_reason": "stop", "latency_ms": 1.0,
            })()

    answer = chat.answer(ctx, question="What is the torque?",
                         collection="maintenance", llm=_Answering())
    assert answer.grounded is True, "precondition: this must be the grounded path"
    assert gaps.backlog(ctx, collection="maintenance") == []


def test_recording_never_breaks_the_turn(ctx, monkeypatch, record_evidence):
    """The answer is the product; this is bookkeeping about the answer."""
    def explode(*_a, **_k):
        raise RuntimeError("the database is on fire")

    monkeypatch.setattr(gaps, "tenant_session", explode)
    # No exception, and no silence either — it logs at warning.
    gaps.record(ctx, collection=COLLECTION, question="does this raise?")

    record_evidence(
        "gap_recording_cannot_break_a_reply", holds=True,
        detail="a failed backlog write is swallowed and logged, never surfaced to the user",
    )


# ── the count is the priority ─────────────────────────────────────────────


def test_the_same_question_asked_again_is_one_row_with_a_count(ctx, record_evidence):
    """Case and punctuation carry no meaning for "is this the same question".

    Treating them as significant would split one gap asked forty ways into forty
    rows of one — and the count is the only thing that distinguishes a gap worth
    fixing from a one-off.
    """
    for phrasing in (
        "What is the SA-400 warranty period?",
        "what is the sa-400 warranty period",
        "What is the SA-400 warranty period??  ",
    ):
        gaps.record(ctx, collection=COLLECTION, question=phrasing)
    gaps.record(ctx, collection=COLLECTION, question="Something else entirely?")

    backlog = gaps.backlog(ctx, collection=COLLECTION)
    assert len(backlog) == 2, "the three phrasings must be one row"
    assert backlog[0]["occurrences"] == 3, "most-asked first"
    assert backlog[1]["occurrences"] == 1
    # The first phrasing is kept verbatim; the normalisation is a key, not a
    # rewrite of what somebody actually typed.
    assert backlog[0]["question"] == "What is the SA-400 warranty period?"

    record_evidence(
        "repeated_gaps_are_counted_not_duplicated", holds=True,
        detail="three phrasings of one question form a single row with occurrences=3",
    )


def test_an_empty_question_is_not_recorded(ctx):
    """A key that normalises to nothing would collide with every other blank."""
    gaps.record(ctx, collection=COLLECTION, question="   ???   ")
    assert gaps.backlog(ctx, collection=COLLECTION) == []


# ── lifecycle ─────────────────────────────────────────────────────────────


def test_a_seeded_gap_leaves_the_backlog_without_leaving_the_table(
    ctx, record_evidence
):
    """Not a delete.

    The row is what makes "this was asked forty times before we fixed it"
    sayable, and the count lives nowhere else. It leaves the *working list*
    because re-deciding the same question every time somebody opens the backlog
    is how a backlog stops being read.
    """
    gaps.record(ctx, collection=COLLECTION, question="A gap that becomes an item?")
    gap = gaps.backlog(ctx, collection=COLLECTION)[0]

    import uuid as _uuid

    assert gaps.mark_seeded(ctx, [_uuid.UUID(gap["id"])], "some-dataset") == 1

    assert gaps.backlog(ctx, collection=COLLECTION) == []
    still_there = gaps.backlog(ctx, collection=COLLECTION, include_seeded=True)
    assert len(still_there) == 1
    assert still_there[0]["seeded_into"] == "some-dataset"
    assert still_there[0]["occurrences"] == 1

    record_evidence(
        "seeded_gaps_are_retained_with_their_count", holds=True,
        detail="marking a gap seeded removes it from the working list, not the record",
    )


def test_retention_deletes_unactioned_gaps_and_spares_seeded_ones(
    ctx, record_evidence
):
    """User content does not accumulate for ever, and the rule actually runs.

    ``sessions.purge_expired`` was written in Phase 5 and called by nothing, so
    expired conversations were invisible to ``load`` and retained indefinitely.
    This purge is invoked from the worker's ``sweep`` — the only cross-tenant
    periodic task there is — and so is that one, now.
    """
    import uuid as _uuid

    gaps.record(ctx, collection=COLLECTION, question="An old unactioned gap?")
    gaps.record(ctx, collection=COLLECTION, question="An old gap that was seeded?")
    seeded = [
        g for g in gaps.backlog(ctx, collection=COLLECTION)
        if "seeded" in g["question"]
    ][0]
    gaps.mark_seeded(ctx, [_uuid.UUID(seeded["id"])], "some-dataset")

    with owner_session() as s:
        s.execute(
            text(
                "UPDATE unanswered_question SET last_asked_at = now() - interval '90 days' "
                "WHERE collection = :c"
            ),
            {"c": COLLECTION},
        )

    before = gaps.backlog(ctx, collection=COLLECTION, include_seeded=True)
    assert len(before) == 2, "precondition: both rows must exist and be old"

    assert gaps.purge(older_than_days=30) >= 1

    after = gaps.backlog(ctx, collection=COLLECTION, include_seeded=True)
    assert [g["seeded_into"] for g in after] == ["some-dataset"], (
        "an actioned gap is part of a measurement and must survive retention"
    )

    record_evidence(
        "gap_retention_is_enforced", holds=True,
        detail="unactioned gaps past retention are deleted; seeded ones are kept",
    )


def test_the_sweep_runs_both_purges(record_evidence):
    """The mechanism, not just the function.

    A retention rule nothing invokes is not a retention rule — which is what the
    session purge was for eight phases. Asserted against the sweep's own report
    so a future refactor that drops the call is visible here.
    """
    from apps.worker import tasks

    result = tasks.sweep()
    assert "purged_sessions" in result
    assert "purged_gaps" in result

    record_evidence(
        "retention_is_invoked_by_the_sweep", holds=True,
        detail="sweep() reports both purges, so neither is a documented intention",
    )
