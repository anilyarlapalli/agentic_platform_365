"""Draft the expected answers a judge will grade against.

## The annotator reads the evidence, never the retriever's output

This is the property the whole exercise rests on. If the reference answer were
written from what retrieval returned, it could only ever contain what retrieval
returned — and a retrieval miss would be **structurally undetectable**, because
the yardstick would have moved to wherever the system happened to be pointing.
So the annotator is given the chunks the item cites, by id, read straight from
the corpus, and nothing else.

The same reasoning is why ``llm_model_annotator`` must differ from the answering
model: a yardstick written by the thing being measured measures self-consistency.
:meth:`Settings.check_coherence` refuses that configuration at startup.

## Drafting produces a new dataset version, and that is correct

``expected_answer`` is content: it is inside ``content_sha256``, so filling one
in mints a new dataset version. That is not an accident to work around. A
baseline scored against blank references and a candidate scored against drafted
ones are not comparable, and the promotion gate refuses to compare them — which
is exactly what should happen.

Review *labels* (who wrote it, whether anyone read it) live in
``eval_item_label``, keyed by dataset **name**, so they survive that
re-versioning. A reviewer does not re-confirm forty items because one answer was
drafted.

## Never overwrite a human

An item whose ``answer_source`` is ``sme_edited`` or ``sme_authored`` is skipped,
so re-running drafting is safe and cannot quietly replace reviewed ground truth
with a model's guess. Items are also skipped when they already carry a
non-empty answer, so a second pass fills gaps rather than churning.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from sqlalchemy import text

from platform_core.corpus import builds
from platform_core.correctness.cancellation import RunCancelled, cancellation_point
from platform_core.db.engine import tenant_session
from platform_core.gates.datasets import Dataset
from platform_core.identity.principal import RequestContext
from platform_core.ports.llm import ChatRequest
from platform_core.settings import get_settings

logger = logging.getLogger("platform.gates.annotator")

# Items per call. Small because each one carries the full text of every evidence
# chunk it must quote from, and a batch that overflows the context window is
# retried whole.
BATCH = 4

# A ceiling on one drafting request. The point is a labelled subset a reviewer
# will actually read, not a set nobody has looked at that is nonetheless
# described as ground truth.
MAX_ITEMS = 50

_HUMAN_AUTHORED = frozenset({"sme_edited", "sme_authored"})

_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$")

_SYSTEM = """\
You write reference answers for evaluating a retrieval system.

For each question you are given the exact source passages the question was drawn \
from. Answer ONLY from those passages.

Rules:
· Quote or closely paraphrase the passages. Do not add outside knowledge.
· Be specific — include figures, identifiers and units exactly as they appear.
· Two or three sentences. No preamble, no citation markers.
· If the passages genuinely do not answer the question, return an empty string \
for that item rather than inventing one.

Respond with only a json object mapping each item id to its answer:
{"<id>": "<answer>", ...}
"""


def _evidence_texts(ctx: RequestContext, collection: str,
                    chunk_ids: list[str]) -> dict[str, str]:
    """Chunk text by canonical id, from the build that is actually being served.

    Scoped to the live build like every other read. An annotator quoting a
    superseded build would write a reference the current corpus cannot support,
    and every item drawn from it would fail for a reason that is not a defect.
    """
    if not chunk_ids:
        return {}
    with tenant_session(ctx.tenant) as s:
        build_version = builds.live_version_or_none(ctx, collection, session=s)
        if build_version is None:
            return {}
        rows = s.execute(
            text(
                "SELECT canonical_id, text FROM chunk "
                "WHERE collection = :c AND build_version = :v "
                "  AND canonical_id = ANY(:ids)"
            ),
            {"c": collection, "v": build_version, "ids": list(set(chunk_ids))},
        ).all()
    return {r.canonical_id: r.text for r in rows}


def _parse(raw: str) -> dict[str, str]:
    text_out = _FENCE.sub("", raw or "").strip()
    if not text_out:
        return {}
    try:
        data = json.loads(text_out)
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v or "").strip() for k, v in data.items()}


def draft(
    ctx: RequestContext,
    dataset: Dataset,
    *,
    llm,
    labels: dict[str, dict[str, Any]] | None = None,
    limit: int = 15,
    model: str | None = None,
) -> dict[str, Any]:
    """Draft missing expected answers. Returns the proposals and a report.

    Does **not** write anything. The caller saves a new dataset version from the
    proposals and records the labels, because those are two different stores with
    two different lifetimes — see the module docstring.
    """
    settings = get_settings()
    chosen = model or settings.llm_model_annotator
    labels = labels or {}
    limit = max(1, min(int(limit or 15), MAX_ITEMS))

    pending = [
        item for item in dataset.items
        if not (item.expected_answer or "").strip()
        and labels.get(item.id, {}).get("answer_source") not in _HUMAN_AUTHORED
        and not (labels.get(item.id, {}).get("unusable_reason") or "")
    ][:limit]

    if not pending:
        return {"drafted": {}, "model": chosen, "attempted": 0,
                "skipped_no_evidence": 0, "note": "nothing pending"}

    evidence = _evidence_texts(
        ctx, dataset.collection, [cid for item in pending for cid in item.must_cite]
    )

    drafted: dict[str, str] = {}
    skipped_no_evidence = 0
    attempted = 0

    for start in range(0, len(pending), BATCH):
        cancellation_point(ctx)
        batch = pending[start : start + BATCH]
        blocks: list[str] = []
        for item in batch:
            passages = [evidence[cid] for cid in item.must_cite if cid in evidence]
            if not passages:
                # Reported rather than drafted around. An answer written without
                # the evidence the item cites is not a reference for anything,
                # and inventing one here is how an eval set fills with plausible
                # fiction.
                skipped_no_evidence += 1
                continue
            joined = "\n\n---\n\n".join(passages)
            blocks.append(f"ITEM {item.id}\nQUESTION: {item.question}\nPASSAGES:\n{joined}")

        if not blocks:
            continue

        attempted += len(blocks)
        try:
            response = llm.chat(
                ctx,
                ChatRequest(
                    model=chosen,
                    messages=[
                        {"role": "system", "content": _SYSTEM},
                        {"role": "user", "content": "\n\n====\n\n".join(blocks)},
                    ],
                    temperature=0,
                    max_tokens=1200,
                    response_format={"type": "json_object"},
                ),
            )
        except RunCancelled:
            raise
        except Exception:
            logger.exception("annotator batch failed; continuing with the rest")
            continue

        for item_id, answer in _parse(response.content).items():
            if answer:
                drafted[item_id] = answer

    logger.info("annotator %s drafted %d of %d attempted (%d had no evidence)",
                chosen, len(drafted), attempted, skipped_no_evidence)
    return {
        "drafted": drafted,
        "model": chosen,
        "attempted": attempted,
        "skipped_no_evidence": skipped_no_evidence,
        "pending_after": max(0, len(pending) - len(drafted)),
    }


def apply(dataset: Dataset, drafted: dict[str, str]) -> list[dict[str, Any]]:
    """The dataset's items with drafted answers merged in, ready to save.

    Only fills blanks. An item that already has an answer keeps it even if the
    caller passed a proposal for it, so a stale report cannot overwrite work done
    in between.
    """
    out: list[dict[str, Any]] = []
    for item in dataset.items:
        payload = item.to_dict()
        if not (item.expected_answer or "").strip() and drafted.get(item.id):
            payload["expected_answer"] = drafted[item.id]
        out.append(payload)
    return out
