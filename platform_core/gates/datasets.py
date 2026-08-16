"""Golden sets, versioned by the hash of their contents.

A dataset is `(name, content_sha256)`. Editing a question does not mutate a row;
it produces a new version, so every historical run keeps pointing at the exact
questions it was scored on.

That is what makes a comparison legitimate. Comparing a candidate against a
baseline computed over a *different* set of questions is the subtlest way to
produce a confident wrong answer — the numbers are both real, both correctly
computed, and mean nothing next to each other. The promotion gate refuses it.

## Evidence ids are canonical, always

`must_cite` holds `c_<sha1:16>` content-addressed chunk ids and nothing else.
The Azure eval set carries a mixture: canonical ids, and a synthetic `page:…`
handle written by a drafting fallback that the retriever can never return —
which scores a permanent miss and halves the recall of every item that has one.
:func:`build_dataset` rejects a non-canonical id rather than storing something
that can only ever be wrong.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text

from platform_core.db.engine import tenant_session
from platform_core.identity.principal import RequestContext
from platform_core.observability import audit
from platform_core.settings import get_settings

CANONICAL_ID = re.compile(r"^c_[0-9a-f]{6,32}$")


class InvalidDataset(ValueError):
    """The dataset would produce meaningless scores. Rejected at construction."""


@dataclass(frozen=True, slots=True)
class EvalItem:
    id: str
    question: str
    expected_answer: str
    must_cite: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "question": self.question,
            "expected_answer": self.expected_answer,
            "must_cite": list(self.must_cite),
        }


@dataclass(frozen=True, slots=True)
class Dataset:
    id: uuid.UUID
    name: str
    collection: str
    content_sha256: str
    items: list[EvalItem]

    @property
    def scoreable_items(self) -> list[EvalItem]:
        """Items that can contribute to retrieval recall.

        An item with no evidence can still score answer quality, but including
        it in a recall average would silently dilute the metric — so recall is
        reported over this subset and the count is reported alongside it.
        """
        return [item for item in self.items if item.must_cite]


def content_sha(items: list[EvalItem]) -> str:
    """Hash of the questions. Order-independent, so a reordering is not a new version."""
    payload = json.dumps(
        sorted((item.to_dict() for item in items), key=lambda d: d["id"]),
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def build_dataset(items: list[dict[str, Any]]) -> list[EvalItem]:
    """Validate and normalise. Raises rather than storing something unscoreable."""
    if not items:
        raise InvalidDataset("a dataset with no items cannot gate anything")

    seen: set[str] = set()
    built: list[EvalItem] = []
    for raw in items:
        item_id = str(raw.get("id") or "").strip()
        if not item_id:
            raise InvalidDataset("every item needs a stable id to be comparable across runs")
        if item_id in seen:
            raise InvalidDataset(f"duplicate item id {item_id!r}")
        seen.add(item_id)

        question = str(raw.get("question") or "").strip()
        if not question:
            raise InvalidDataset(f"item {item_id!r} has no question")

        must_cite = [str(c) for c in (raw.get("must_cite") or [])]
        bad = [c for c in must_cite if not CANONICAL_ID.match(c)]
        if bad:
            # The Azure failure this prevents: a synthetic handle the retriever
            # can never return scores a permanent miss and halves the item's
            # recall, indistinguishably from a real retrieval failure.
            raise InvalidDataset(
                f"item {item_id!r} cites {bad!r}, which are not canonical chunk ids "
                f"(c_<hex>). A citation the retriever cannot emit scores a permanent "
                f"miss and makes the metric mean something other than it appears to."
            )

        built.append(
            EvalItem(
                id=item_id,
                question=question,
                expected_answer=str(raw.get("expected_answer") or "").strip(),
                must_cite=must_cite,
            )
        )
    return built


def save(ctx: RequestContext, *, name: str, collection: str,
         items: list[dict[str, Any]]) -> Dataset:
    """Store a dataset version. Idempotent on (name, content hash)."""
    built = build_dataset(items)
    sha = content_sha(built)

    with tenant_session(ctx.tenant) as s:
        dataset_id = s.execute(
            text(
                "INSERT INTO eval_dataset (tenant_id, name, collection, content_sha256, "
                "  items, item_count, created_by) "
                "VALUES (:t, :n, :c, :sha, :items, :count, :by) "
                "ON CONFLICT (tenant_id, name, content_sha256) DO UPDATE "
                "  SET collection = EXCLUDED.collection "
                "RETURNING id"
            ),
            {
                "t": ctx.tenant.id, "n": name, "c": collection, "sha": sha,
                "items": json.dumps([i.to_dict() for i in built]),
                "count": len(built), "by": ctx.principal.id,
            },
        ).scalar_one()

        settings = get_settings()
        s.execute(
            text(
                "INSERT INTO continuous_eval_policy "
                "(tenant_id, dataset_name, interval_seconds, top_k, created_by, updated_by) "
                "VALUES (:t, :n, :interval, :top_k, :by, :by) "
                "ON CONFLICT (tenant_id, dataset_name) DO UPDATE "
                "SET updated_by = EXCLUDED.updated_by, updated_at = now()"
            ),
            {
                "t": ctx.tenant.id,
                "n": name,
                "interval": settings.continuous_eval_interval_seconds,
                "top_k": settings.continuous_eval_default_top_k,
                "by": ctx.principal.id,
            },
        )
        audit.append_in_session(
            s,
            ctx,
            action="eval.dataset.version.saved",
            outcome=audit.Outcome.SUCCEEDED,
            resource_type="eval_dataset",
            resource_id=name,
            detail={
                "content_sha256": sha,
                "collection": collection,
                "item_count": len(built),
            },
        )

    return Dataset(id=dataset_id, name=name, collection=collection,
                   content_sha256=sha, items=built)


def load(ctx: RequestContext, *, name: str, content_sha256: str | None = None) -> Dataset | None:
    """Load a dataset version. Latest for the name when no hash is given."""
    with tenant_session(ctx.tenant) as s:
        if content_sha256:
            row = s.execute(
                text(
                    "SELECT id, name, collection, content_sha256, items FROM eval_dataset "
                    "WHERE name = :n AND content_sha256 = :sha"
                ),
                {"n": name, "sha": content_sha256},
            ).one_or_none()
        else:
            row = s.execute(
                text(
                    "SELECT id, name, collection, content_sha256, items FROM eval_dataset "
                    "WHERE name = :n ORDER BY created_at DESC LIMIT 1"
                ),
                {"n": name},
            ).one_or_none()

    if row is None:
        return None
    return Dataset(
        id=row.id, name=row.name, collection=row.collection,
        content_sha256=row.content_sha256,
        items=[EvalItem(**item) for item in row.items],
    )
