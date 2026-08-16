"""Questions the corpus could not answer, kept so they can be fixed.

An ungrounded chat turn is three things at once: a user who did not get an
answer, a document somebody should probably upload, and an eval item that would
prove it once they had. Nothing captured it. Sessions hold the transcript and
expire in twelve hours, so the record had to be written down or it was not a
record.

## Only the failures

This is deliberately not a transcript. A durable log of everything users ask is a
materially different thing to hold, and "it might be useful later" is not a
reason to hold it. Recording only the turns where retrieval came back empty keeps
the table to what somebody is plainly entitled to read: a list of things the
corpus should cover and does not.

Questions that *were* answered stay in the session window, where they already
live and already expire.

## Recording never breaks the turn

:func:`record` swallows everything. A user waiting on an answer must not see it
fail because a backlog write did — the answer is the product, and this is
bookkeeping about the answer. It logs at warning so a persistently broken write
is visible rather than silent.
"""

from __future__ import annotations

import logging
import re
import uuid
from typing import Any

from sqlalchemy import text

from platform_core.db.engine import relay_session, tenant_session
from platform_core.identity.principal import RequestContext

logger = logging.getLogger("platform.corpus.gaps")

# Trailing punctuation and case carry no meaning for "is this the same
# question", and treating them as significant would split one gap asked forty
# ways into forty rows of one — which is exactly the count that makes the
# backlog readable.
_PUNCT = re.compile(r"[^\w\s-]+")
_SPACE = re.compile(r"\s+")

MAX_QUESTION_CHARS = 2000


def normalise(question: str) -> str:
    """The deduplication key: lowercased, depunctuated, whitespace collapsed."""
    lowered = (question or "").strip().lower()
    return _SPACE.sub(" ", _PUNCT.sub(" ", lowered)).strip()


def record(
    ctx: RequestContext,
    *,
    collection: str,
    question: str,
    mode: str = "dense",
) -> None:
    """Note that the corpus had nothing for this. Never raises.

    Idempotent per question per collection: a repeat increments ``occurrences``
    and moves ``last_asked_at`` rather than adding a row. The count is the
    priority signal — a gap forty people hit is not the same backlog item as one
    somebody hit once.
    """
    key = normalise(question)
    if not key:
        return
    try:
        with tenant_session(ctx.tenant) as s:
            s.execute(
                text(
                    "INSERT INTO unanswered_question "
                    "  (tenant_id, collection, question, question_key, mode, "
                    "   last_asked_by) "
                    "VALUES (:t, :c, :q, :k, :m, :by) "
                    "ON CONFLICT (tenant_id, collection, question_key) DO UPDATE "
                    "  SET occurrences = unanswered_question.occurrences + 1, "
                    "      last_asked_at = now(), "
                    "      last_asked_by = EXCLUDED.last_asked_by, "
                    "      mode = EXCLUDED.mode"
                ),
                {
                    "t": ctx.tenant.id, "c": collection,
                    "q": question.strip()[:MAX_QUESTION_CHARS], "k": key,
                    "m": mode, "by": ctx.principal.id,
                },
            )
    except Exception:
        # Bookkeeping about an answer must never cost the answer.
        logger.warning("could not record an unanswered question", exc_info=True)


def backlog(
    ctx: RequestContext,
    *,
    collection: str | None = None,
    include_seeded: bool = False,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """The gaps, most-asked first.

    Seeded rows are excluded by default. They are already eval items, so leaving
    them in the working list would mean re-deciding the same question every time
    somebody opens it.
    """
    where = ["1 = 1"] if include_seeded else ["seeded_into IS NULL"]
    params: dict[str, Any] = {"limit": limit}
    if collection:
        where.append("collection = :c")
        params["c"] = collection

    with tenant_session(ctx.tenant) as s:
        rows = s.execute(
            text(
                "SELECT id, collection, question, mode, occurrences, "
                "  first_asked_at, last_asked_at, seeded_into, seeded_at "
                f"FROM unanswered_question WHERE {' AND '.join(where)} "
                "ORDER BY occurrences DESC, last_asked_at DESC LIMIT :limit"
            ),
            params,
        ).all()

    return [
        {
            "id": str(r.id),
            "collection": r.collection,
            "question": r.question,
            "mode": r.mode,
            # How many times this gap was hit. The whole reason the table
            # deduplicates rather than appending.
            "occurrences": r.occurrences,
            "first_asked_at": r.first_asked_at.isoformat(),
            "last_asked_at": r.last_asked_at.isoformat(),
            "seeded_into": r.seeded_into,
            "seeded_at": r.seeded_at.isoformat() if r.seeded_at else None,
        }
        for r in rows
    ]


def mark_seeded(ctx: RequestContext, ids: list[uuid.UUID], dataset_name: str) -> int:
    """Record that these gaps became eval items.

    Not a delete. The row is what makes "this was asked forty times before we
    fixed it" sayable, and the count is the only place that number lives.
    """
    if not ids:
        return 0
    with tenant_session(ctx.tenant) as s:
        return s.execute(
            text(
                "UPDATE unanswered_question SET seeded_into = :n, seeded_at = now() "
                "WHERE id = ANY(:ids) AND seeded_into IS NULL"
            ),
            {"n": dataset_name, "ids": ids},
        ).rowcount


def purge(older_than_days: int) -> int:
    """Delete unseeded gaps past their retention. Cross-tenant housekeeping.

    Unseeded only. A row already promoted into an eval set has become part of a
    measurement, and deleting it would silently detach the item from why it
    exists — the retention here is on *unactioned user content*, which is the
    thing that should not accumulate indefinitely.
    """
    with relay_session(reason="retention: purge unanswered questions") as session:
        return int(
            session.execute(
                text("SELECT platform_purge_unanswered_questions(:days)"),
                {"days": int(older_than_days)},
            ).scalar_one()
        )
