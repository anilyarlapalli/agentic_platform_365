"""Review state for eval items, held beside the dataset rather than inside it.

A dataset is ``(name, content_sha256)`` and the hash covers the items. Review
state must therefore live outside it, or the first click of the review workflow
would mint a new dataset version and orphan the baseline — the reviewer punished
for reviewing. See migration 0018.

Keyed by dataset **name**, so a label survives re-versioning of the questions. A
reviewer does not re-confirm forty items because one answer was drafted.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import text

from platform_core.db.engine import tenant_session
from platform_core.identity.principal import RequestContext

logger = logging.getLogger("platform.gates.labels")

ANSWER_SOURCES = ("empty", "llm_drafted", "sme_edited", "sme_authored")
HUMAN_AUTHORED = frozenset({"sme_edited", "sme_authored"})

_SELECT = (
    "SELECT item_id, answer_source, annotator_model, annotated_at, confirmed, "
    "  confirmed_by, confirmed_at, requires_kg_hop, unusable_reason, origin "
    "FROM eval_item_label WHERE dataset_name = :n"
)


def _row(r) -> dict[str, Any]:
    return {
        "item_id": r.item_id,
        "answer_source": r.answer_source,
        "annotator_model": r.annotator_model,
        "annotated_at": r.annotated_at.isoformat() if r.annotated_at else None,
        "confirmed": r.confirmed,
        "confirmed_by": str(r.confirmed_by) if r.confirmed_by else None,
        "confirmed_at": r.confirmed_at.isoformat() if r.confirmed_at else None,
        "requires_kg_hop": r.requires_kg_hop,
        "unusable_reason": r.unusable_reason or "",
        "origin": r.origin,
    }


def for_dataset(ctx: RequestContext, dataset_name: str) -> dict[str, dict[str, Any]]:
    """Every label for a dataset name, keyed by item id."""
    with tenant_session(ctx.tenant) as s:
        rows = s.execute(text(_SELECT), {"n": dataset_name}).all()
    return {r.item_id: _row(r) for r in rows}


def record_drafted(ctx: RequestContext, dataset_name: str,
                   item_ids: list[str], *, model: str) -> int:
    """Mark items as machine-drafted. Never downgrades a human-authored one.

    The guard is here rather than only at the call site because this is the
    write that would destroy the distinction: overwriting ``sme_edited`` with
    ``llm_drafted`` would make a reviewed answer look like an unread one, and the
    rubber-stamp count would then be wrong in the direction that flatters.
    """
    if not item_ids:
        return 0
    with tenant_session(ctx.tenant) as s:
        result = s.execute(
            text(
                "INSERT INTO eval_item_label "
                "  (tenant_id, dataset_name, item_id, answer_source, "
                "   annotator_model, annotated_at) "
                "SELECT :t, :n, unnest(CAST(:ids AS text[])), 'llm_drafted', :m, now() "
                "ON CONFLICT (tenant_id, dataset_name, item_id) DO UPDATE "
                "  SET answer_source = 'llm_drafted', annotator_model = :m, "
                "      annotated_at = now(), updated_at = now() "
                "WHERE eval_item_label.answer_source NOT IN ('sme_edited', 'sme_authored')"
            ),
            {"t": ctx.tenant.id, "n": dataset_name, "ids": list(item_ids), "m": model},
        )
    return result.rowcount


def set_origin(ctx: RequestContext, dataset_name: str,
               item_ids: list[str], origin: str) -> int:
    """Record where a batch of items came from.

    A set built from real failures and one built from proposals about the corpus
    are different instruments — the first says "we could not answer this", the
    second says "we think this matters" — and a reader cannot tell them apart
    from the questions alone.
    """
    if not item_ids:
        return 0
    with tenant_session(ctx.tenant) as s:
        result = s.execute(
            text(
                "INSERT INTO eval_item_label (tenant_id, dataset_name, item_id, origin) "
                "SELECT :t, :n, unnest(CAST(:ids AS text[])), :o "
                "ON CONFLICT (tenant_id, dataset_name, item_id) DO UPDATE "
                "  SET origin = EXCLUDED.origin, updated_at = now()"
            ),
            {"t": ctx.tenant.id, "n": dataset_name, "ids": list(item_ids), "o": origin},
        )
    return result.rowcount


def set_label(
    ctx: RequestContext,
    dataset_name: str,
    item_id: str,
    *,
    answer_edited: bool = False,
    confirmed: bool | None = None,
    requires_kg_hop: bool | None = None,
    unusable_reason: str | None = None,
) -> dict[str, Any]:
    """Record a reviewer's verdict on one item.

    ``answer_edited`` promotes ``answer_source`` to ``sme_edited``. That is the
    single fact separating reviewed ground truth from an answer nobody read, and
    it is set by the route that actually changed the text rather than inferred
    later from a timestamp.

    Confirmation carries its confirmer, enforced by a CHECK constraint as well as
    here — a half-written verdict is not a verdict.
    """
    sets = ["updated_at = now()"]
    params: dict[str, Any] = {
        "t": ctx.tenant.id, "n": dataset_name, "i": item_id,
    }
    if answer_edited:
        sets.append("answer_source = 'sme_edited'")
    if confirmed is not None:
        sets.append("confirmed = :c")
        params["c"] = confirmed
        # Cleared together with the flag, so an unconfirmed item does not keep
        # the name of whoever confirmed it last.
        sets.append("confirmed_by = CASE WHEN :c THEN CAST(:p AS uuid) ELSE NULL END")
        sets.append("confirmed_at = CASE WHEN :c THEN now() ELSE NULL END")
        params["p"] = ctx.principal.id
    if requires_kg_hop is not None:
        sets.append("requires_kg_hop = :h")
        params["h"] = requires_kg_hop
    if unusable_reason is not None:
        sets.append("unusable_reason = NULLIF(:u, '')")
        params["u"] = unusable_reason

    with tenant_session(ctx.tenant) as s:
        s.execute(
            text(
                "INSERT INTO eval_item_label (tenant_id, dataset_name, item_id) "
                "VALUES (:t, :n, :i) "
                "ON CONFLICT (tenant_id, dataset_name, item_id) DO NOTHING"
            ),
            params if not confirmed else {"t": params["t"], "n": params["n"], "i": params["i"]},
        )
        row = s.execute(
            text(
                f"UPDATE eval_item_label SET {', '.join(sets)} "
                "WHERE dataset_name = :n AND item_id = :i "
                "RETURNING item_id, answer_source, annotator_model, annotated_at, "
                "  confirmed, confirmed_by, confirmed_at, requires_kg_hop, "
                "  unusable_reason, origin"
            ),
            params,
        ).one()
    return _row(row)


def summarise(items: list[Any], labels: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """The counts a reviewer needs, including the rubber-stamp signal.

    ``accepted_unedited`` is reported deliberately: a set where every drafted
    answer was confirmed without a single edit is **not** SME-attested ground
    truth, and the number should be visible rather than inferred. Without it,
    "reviewed" and "clicked through" are the same word.
    """
    total = len(items)
    with_answer = sum(1 for i in items if (i.expected_answer or "").strip())
    with_evidence = sum(1 for i in items if i.must_cite)

    confirmed = [
        i for i in items if labels.get(i.id, {}).get("confirmed")
    ]
    accepted_unedited = sum(
        1 for i in confirmed
        if labels.get(i.id, {}).get("answer_source") == "llm_drafted"
    )
    return {
        "total": total,
        "with_expected_answer": with_answer,
        "with_evidence": with_evidence,
        "drafted": sum(
            1 for i in items
            if labels.get(i.id, {}).get("answer_source") == "llm_drafted"
        ),
        "sme_authored": sum(
            1 for i in items
            if labels.get(i.id, {}).get("answer_source") in HUMAN_AUTHORED
        ),
        "confirmed": len(confirmed),
        "accepted_unedited": accepted_unedited,
        "requires_kg_hop": sum(
            1 for i in items if labels.get(i.id, {}).get("requires_kg_hop")
        ),
        "unusable": sum(
            1 for i in items if (labels.get(i.id, {}).get("unusable_reason") or "")
        ),
        "from_real_failures": sum(
            1 for i in items if labels.get(i.id, {}).get("origin") == "mined"
        ),
        "annotator_models": sorted({
            m for m in (
                labels.get(i.id, {}).get("annotator_model") for i in items
            ) if m
        }),
    }
