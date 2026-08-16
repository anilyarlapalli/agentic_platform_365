"""The evaluation gate, made reachable.

``GET /api/eval`` and ``POST /api/eval/run`` were declared in the policy table
with no handler behind them — the same forward declaration ``/api/query`` carried
for several phases. Default-deny made that safe and also meant the gate could
only ever be exercised from pytest. Everything underneath already existed:
:mod:`platform_core.gates.datasets`, :mod:`~platform_core.gates.runner` and
:mod:`~platform_core.gates.promotion`, with the four tables from migration 0010.
Nothing here re-implements any of it.

## Three authorities, matching the three acts

Reading a score, running one, and deciding what the score *means* are different
privileges, and collapsing them is how a gate stops being a gate:

``eval:read``
    Look at datasets, runs and baselines. Every role above viewer holds it.

``eval:run``
    Spend budget scoring a set. An operator holds this: measuring is not
    deciding, and making people ask permission to measure is how measurement
    stops happening.

``release:promote``
    Move the baseline — **and** write a dataset version. Those are the same
    authority wearing two hats: whoever can rewrite the golden set can make any
    regression pass, so gating dataset authorship any lower would leave the
    promotion capability guarding a door with no wall attached. Reusing the
    existing capability rather than inventing an ``eval:author`` keeps that
    equivalence explicit instead of leaving it to a reader to notice.

## Running is queued, not synchronous

An eval is a hundred embedding calls against the live corpus. It goes through the
outbox exactly like onboarding and reindex, so it is leased, retried, swept and
visible in ``/api/runs`` — rather than holding an HTTP connection open for
minutes and losing the work if the client hangs up.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text

from platform_core.api.deps import get_context
from platform_core.corpus import gaps
from platform_core.correctness.outbox import enqueue_run
from platform_core.db.engine import tenant_session
from platform_core.gates import datasets, labels, promotion, runner
from platform_core.gates.datasets import InvalidDataset
from platform_core.governance import continuous_eval
from platform_core.identity.principal import RequestContext
from platform_core.observability import audit
from workloads.eval.workload import DEFAULT_TOP_K, WORKLOAD

logger = logging.getLogger("platform.api.eval")
router = APIRouter(prefix="/api/eval", tags=["eval"])


class DatasetWrite(BaseModel):
    collection: str = Field(min_length=1, max_length=128)
    items: list[dict[str, Any]] = Field(min_length=1)


class RunRequest(BaseModel):
    dataset: str = Field(min_length=1, max_length=128)
    # Pinning a version is the difference between "score the set I reviewed" and
    # "score whatever the set is now". Both are legitimate; only one of them is
    # safe to compare against a baseline, and `promotion.evaluate` refuses the
    # other by hash rather than trusting the caller to have meant it.
    content_sha256: str | None = Field(default=None, max_length=64)
    top_k: int = Field(default=DEFAULT_TOP_K, ge=1, le=50)


class PromoteRequest(BaseModel):
    dataset: str = Field(min_length=1, max_length=128)
    note: str | None = Field(default=None, max_length=1000)
    force: bool = False


class ContinuousPolicyUpdate(BaseModel):
    interval_seconds: int = Field(ge=900, le=604_800)
    top_k: int = Field(default=DEFAULT_TOP_K, ge=1, le=50)


@router.get("/continuous")
def continuous_policies(ctx: RequestContext = Depends(get_context)) -> dict:
    """The mandatory schedule for every named golden set."""
    return {"policies": continuous_eval.list_policies(ctx), "mandatory": True}


@router.put("/continuous/{name}")
def configure_continuous_policy(
    name: str,
    payload: ContinuousPolicyUpdate,
    ctx: RequestContext = Depends(get_context),
) -> dict:
    """Adjust cadence, never disable measurement."""
    policy = continuous_eval.update_policy(
        ctx,
        dataset_name=name,
        interval_seconds=payload.interval_seconds,
        top_k=payload.top_k,
    )
    if policy is None:
        raise HTTPException(status_code=404, detail="Dataset schedule not found.")
    return policy


@router.get("")
def list_datasets(ctx: RequestContext = Depends(get_context)) -> dict:
    """Every dataset, its latest version, its baseline and its last run.

    One query rather than a list plus N detail fetches, because the console's
    first question is always "is anything red", and answering it with a fan-out
    makes the page slower the worse the news is.
    """
    with tenant_session(ctx.tenant) as s:
        rows = s.execute(
            text(
                "SELECT DISTINCT ON (d.name) "
                "  d.name, d.collection, d.content_sha256, d.item_count, d.created_at, "
                "  b.eval_run_id AS baseline_run_id, b.promoted_at, b.note "
                "FROM eval_dataset d "
                "LEFT JOIN eval_baseline b ON b.dataset_name = d.name "
                "ORDER BY d.name, d.created_at DESC"
            )
        ).all()

        latest_runs = s.execute(
            text(
                "SELECT DISTINCT ON (d.name) d.name, r.id, r.status, r.started_at, "
                "  r.answer_pass_rate, r.retrieval_recall, r.items_run, r.items_scoreable "
                "FROM eval_run r JOIN eval_dataset d ON d.id = r.dataset_id "
                "ORDER BY d.name, r.started_at DESC"
            )
        ).all()

    by_name = {r.name: r for r in latest_runs}
    out = []
    for r in rows:
        latest = by_name.get(r.name)
        out.append({
            "name": r.name,
            "collection": r.collection,
            "content_sha256": r.content_sha256,
            "item_count": r.item_count,
            "created_at": r.created_at.isoformat(),
            "baseline_run_id": str(r.baseline_run_id) if r.baseline_run_id else None,
            "baseline_promoted_at": r.promoted_at.isoformat() if r.promoted_at else None,
            "baseline_note": r.note,
            "latest_run": {
                "run_id": str(latest.id),
                "status": latest.status,
                "started_at": latest.started_at.isoformat(),
                "answer_pass_rate": latest.answer_pass_rate,
                "retrieval_recall": latest.retrieval_recall,
                "items_run": latest.items_run,
                "items_scoreable": latest.items_scoreable,
                # A run that is the baseline is stated, not left to be inferred
                # from two ids the reader has to compare by eye.
                "is_baseline": str(latest.id) == str(r.baseline_run_id or ""),
            } if latest else None,
        })
    return {"datasets": out}


@router.get("/datasets/{name}")
def get_dataset(name: str, ctx: RequestContext = Depends(get_context)) -> dict:
    """The current version's items, plus every run scored against the name."""
    dataset = datasets.load(ctx, name=name)
    if dataset is None:
        raise HTTPException(status_code=404, detail="Dataset not found.")
    return {
        "name": dataset.name,
        "collection": dataset.collection,
        "content_sha256": dataset.content_sha256,
        "items": [item.to_dict() for item in dataset.items],
        # Reported because recall is averaged over this subset only. An item with
        # no evidence still scores an answer, and folding it into the recall
        # average would dilute the metric silently.
        "items_scoreable": len(dataset.scoreable_items),
        # Labels alongside the items, not behind a second request: the review
        # screen's first question is "what still needs reading", and answering it
        # with a fan-out makes the page slower the more work there is.
        "labels": labels.for_dataset(ctx, name),
        "review": labels.summarise(dataset.items, labels.for_dataset(ctx, name)),
        "history": promotion.history(ctx, name),
    }


@router.put("/datasets/{name}", status_code=201)
def put_dataset(name: str, payload: DatasetWrite,
                ctx: RequestContext = Depends(get_context)) -> dict:
    """Store a dataset version. Idempotent on content.

    Editing an item does not mutate a row — it produces a new version keyed by
    content hash, so every historical run keeps pointing at the exact questions
    it was scored on. That is what makes the comparison in
    ``promotion.evaluate`` legitimate rather than merely arithmetic.
    """
    try:
        dataset = datasets.save(
            ctx, name=name, collection=payload.collection, items=payload.items
        )
    except InvalidDataset as exc:
        # Rejected at the door rather than stored and discovered at scoring time:
        # a non-canonical citation scores a permanent miss and halves an item's
        # recall indistinguishably from a real retrieval failure.
        raise HTTPException(status_code=400, detail=str(exc)) from None

    return {
        "name": dataset.name,
        "collection": dataset.collection,
        "content_sha256": dataset.content_sha256,
        "item_count": len(dataset.items),
        "items_scoreable": len(dataset.scoreable_items),
    }


class DraftRequest(BaseModel):
    limit: int = Field(default=15, ge=1, le=50)


class ItemReview(BaseModel):
    """A reviewer's verdict on one item. Every field optional; omitted means unchanged."""

    expected_answer: str | None = Field(default=None, max_length=20_000)
    confirmed: bool | None = None
    requires_kg_hop: bool | None = None
    unusable_reason: str | None = Field(default=None, max_length=2000)


@router.post("/datasets/{name}/draft")
def draft_answers(name: str, payload: DraftRequest,
                  ctx: RequestContext = Depends(get_context)) -> dict:
    """Draft missing expected answers from the evidence, and save a new version.

    Spends budget: one annotator call per batch of items. The annotator reads the
    chunks each item cites and **nothing about what retrieval returned** — a
    reference written from the retriever's output could only ever contain what
    the retriever found, and a retrieval miss would then be undetectable by
    construction.

    Saving a new dataset version is correct, not a side effect to avoid.
    ``expected_answer`` is inside ``content_sha256``, so a set with drafted
    references is genuinely not the set the baseline was scored against, and the
    promotion gate refuses to compare them. Review *labels* are keyed by dataset
    name and survive the re-versioning.
    """
    dataset = datasets.load(ctx, name=name)
    if dataset is None:
        raise HTTPException(status_code=404, detail="Dataset not found.")

    from platform_core.gates import annotator
    from platform_core.observability.llm import build_client

    existing = labels.for_dataset(ctx, name)
    # task="eval": a background path, so an unreadable ledger fails this closed
    # rather than spending uncapped and unrecorded.
    scoped = ctx.child(step="annotate", task="eval")
    report = annotator.draft(
        scoped, dataset, llm=build_client(), labels=existing, limit=payload.limit
    )

    if not report["drafted"]:
        return {"dataset": name, "drafted": 0, **report}

    saved = datasets.save(
        ctx, name=name, collection=dataset.collection,
        items=annotator.apply(dataset, report["drafted"]),
    )
    labels.record_drafted(
        ctx, name, list(report["drafted"]), model=report["model"]
    )

    audit.record(
        ctx, action="eval.answers.drafted", outcome=audit.Outcome.SUCCEEDED,
        resource_type="eval_dataset", resource_id=name,
        detail={"items": list(report["drafted"]), "model": report["model"],
                "content_sha256": saved.content_sha256},
    )
    return {
        "dataset": name,
        "drafted": len(report["drafted"]),
        "model": report["model"],
        "skipped_no_evidence": report["skipped_no_evidence"],
        "content_sha256": saved.content_sha256,
        "note": (
            "These are the annotator's answers, not reviewed ground truth. "
            "Read and edit them before confirming."
        ),
    }


@router.put("/datasets/{name}/items/{item_id}")
def review_item(name: str, item_id: str, payload: ItemReview,
                ctx: RequestContext = Depends(get_context)) -> dict:
    """Edit an item's expected answer, and record the reviewer's labels.

    One endpoint at ``release:promote`` for both, because both are release-grade
    decisions wearing different verbs: the expected answer *is* the yardstick,
    and ``unusable_reason`` changes which items are scored at all. Confirming is
    the attestation that makes the set ground truth, and an attestation anyone
    can make is not one.

    Editing the answer mints a new dataset version; the labels do not. That
    asymmetry is the whole reason the two live in different stores.
    """
    dataset = datasets.load(ctx, name=name)
    if dataset is None:
        raise HTTPException(status_code=404, detail="Dataset not found.")
    if not any(item.id == item_id for item in dataset.items):
        raise HTTPException(status_code=404, detail="Item not found in this dataset.")

    out: dict[str, Any] = {"dataset": name, "item_id": item_id}

    if payload.expected_answer is not None:
        items = [
            {**item.to_dict(), "expected_answer": payload.expected_answer}
            if item.id == item_id else item.to_dict()
            for item in dataset.items
        ]
        try:
            saved = datasets.save(ctx, name=name, collection=dataset.collection,
                                  items=items)
        except InvalidDataset as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        out["content_sha256"] = saved.content_sha256
        out["version_changed"] = saved.content_sha256 != dataset.content_sha256

    out["label"] = labels.set_label(
        ctx, name, item_id,
        # Only an actual edit promotes the provenance. Confirming alone must
        # leave it as `llm_drafted`, which is what makes the rubber-stamp count
        # mean anything.
        answer_edited=payload.expected_answer is not None,
        confirmed=payload.confirmed,
        requires_kg_hop=payload.requires_kg_hop,
        unusable_reason=payload.unusable_reason,
    )
    return out


class MineRequest(BaseModel):
    collection: str = Field(min_length=1, max_length=128)
    gap_ids: list[uuid.UUID] = Field(min_length=1, max_length=200)


@router.get("/gaps")
def list_gaps(collection: str | None = None, include_seeded: bool = False,
              ctx: RequestContext = Depends(get_context)) -> dict:
    """Questions the corpus could not answer, most-asked first.

    A backlog, an eval-set candidate list and a retrieval bug report in one
    table. Only ungrounded turns are recorded — this is not a transcript, and the
    narrowness is what makes it something anyone with ``eval:read`` can be shown.
    """
    return {
        "gaps": gaps.backlog(
            ctx, collection=collection, include_seeded=include_seeded
        )
    }


@router.post("/datasets/{name}/mine", status_code=201)
def mine_gaps(name: str, payload: MineRequest,
              ctx: RequestContext = Depends(get_context)) -> dict:
    """Add chosen real-user failures to a golden set. No LLM cost.

    These are the highest-value eval items available: somebody wanted the answer,
    and the platform did not have it. An item added here has **no evidence ids**
    and cannot score retrieval recall — by construction, because retrieval
    returned nothing. What it scores is whether the gap ever closes.

    Explicit ids rather than "add everything": a backlog is a list of things to
    decide about, and adding it wholesale would make the deciding step
    disappear — the same reason candidate queries are not approved by default.
    """
    existing = datasets.load(ctx, name=name)
    chosen = {
        uuid.UUID(g["id"]): g
        for g in gaps.backlog(ctx, collection=payload.collection, limit=500)
        if uuid.UUID(g["id"]) in set(payload.gap_ids)
    }
    missing = sorted(str(i) for i in set(payload.gap_ids) - set(chosen))
    if missing:
        raise HTTPException(
            status_code=404,
            detail=(
                f"{len(missing)} of the requested gaps are not in this "
                f"collection's open backlog (already seeded, or purged)."
            ),
        )

    # Item ids derived from the gap, so re-mining the same gap is the same item
    # rather than a duplicate question under a new name.
    items = [item.to_dict() for item in (existing.items if existing else [])]
    seen = {item["id"] for item in items}
    added: list[str] = []
    for gap_id, gap in chosen.items():
        item_id = f"g_{gap_id.hex[:12]}"
        if item_id in seen:
            continue
        items.append({
            "id": item_id,
            "question": gap["question"],
            "expected_answer": "",
            # Empty, necessarily: retrieval returned nothing, which is why this
            # is here. Recall is not scoreable for it and the run says so.
            "must_cite": [],
        })
        added.append(item_id)

    if not added:
        return {"dataset": name, "added": 0,
                "note": "every requested gap is already in this set"}

    try:
        dataset = datasets.save(
            ctx, name=name,
            collection=(existing.collection if existing else payload.collection),
            items=items,
        )
    except InvalidDataset as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None

    labels.set_origin(ctx, name, added, "mined")
    gaps.mark_seeded(ctx, list(chosen), name)

    audit.record(
        ctx, action="eval.dataset.mined", outcome=audit.Outcome.SUCCEEDED,
        resource_type="eval_dataset", resource_id=name,
        detail={"added": added, "collection": payload.collection},
    )
    return {
        "dataset": name,
        "added": len(added),
        "content_sha256": dataset.content_sha256,
        "items": len(dataset.items),
        "items_scoreable": len(dataset.scoreable_items),
        "note": (
            "Added with no evidence ids — retrieval returned nothing, which is "
            "the point. These score answer quality and whether the gap closes."
        ),
    }


@router.post("/run", status_code=202)
def start_run(payload: RunRequest, ctx: RequestContext = Depends(get_context)) -> dict:
    """Queue an eval. 202 — the score does not exist yet.

    Refuses a dataset that does not exist *here*, in the request, rather than
    letting the worker discover it: a queued run against a missing dataset is a
    failure the caller only sees by going to look for it.
    """
    dataset = datasets.load(
        ctx, name=payload.dataset, content_sha256=payload.content_sha256
    )
    if dataset is None:
        raise HTTPException(status_code=404, detail="Dataset not found.")

    with tenant_session(ctx.tenant) as s:
        run_id, created = enqueue_run(
            s, ctx,
            workload=WORKLOAD,
            payload={
                "dataset": payload.dataset,
                # Pinned to the version resolved above. Without this a rebuild of
                # the dataset between queueing and execution would score a
                # different set than the caller asked for, and the run would
                # carry the new hash while the request meant the old one.
                "content_sha256": dataset.content_sha256,
                "top_k": payload.top_k,
            },
            idempotency_key=ctx.idempotency_key,
        )

    logger.info("queued eval of %s (%s) as run %s",
                payload.dataset, dataset.content_sha256[:12], run_id)
    return {
        "run_id": str(run_id),
        "run_created": created,
        "dataset": payload.dataset,
        "content_sha256": dataset.content_sha256,
        "status": "queued",
    }


@router.get("/runs/{run_id}")
def get_run(run_id: uuid.UUID, ctx: RequestContext = Depends(get_context)) -> dict:
    """One run with its per-item outcomes, and what the gate would say about it.

    The verdict is recomputed here rather than read back from the workload's
    result, because the baseline can move between a run finishing and someone
    looking at it — and a stale "would promote" is worse than none.
    """
    completed = runner.load(ctx, run_id)
    if completed is None:
        raise HTTPException(status_code=404, detail="Eval run not found.")

    with tenant_session(ctx.tenant) as s:
        name = s.execute(
            text(
                "SELECT d.name FROM eval_run r JOIN eval_dataset d ON d.id = r.dataset_id "
                "WHERE r.id = :id"
            ),
            {"id": run_id},
        ).scalar_one_or_none()

    decision = promotion.evaluate(ctx, completed, dataset_name=name) if name else None
    return {
        **completed.summary(),
        "dataset": name,
        "outcomes": [
            {
                "item_id": o.item_id, "question": o.question,
                "must_cite": o.must_cite, "retrieved": o.retrieved,
                "retrieval_recall": o.retrieval_recall, "passed": o.passed,
                "answer": o.answer, "detail": o.detail,
            }
            for o in completed.outcomes
        ],
        "gate": None if decision is None else {
            "would_promote": decision.promoted,
            "reasons": decision.reasons,
            "deltas": decision.deltas,
            "baseline_run_id": (decision.baseline or {}).get("run_id"),
        },
    }


@router.post("/runs/{run_id}/promote")
def promote_run(run_id: uuid.UUID, payload: PromoteRequest,
                ctx: RequestContext = Depends(get_context)) -> dict:
    """Make a run the baseline, if the gate allows — or with ``force``, despite it.

    ``force`` is not a bypass to be embarrassed about: a deliberate, reviewed
    regression is a real decision, and a gate with no override gets circumvented
    by deleting the baseline, which loses the history that made the gate worth
    having. Every forced promotion is audited with the reasons it overrode.
    """
    completed = runner.load(ctx, run_id, with_outcomes=False)
    if completed is None:
        raise HTTPException(status_code=404, detail="Eval run not found.")

    decision = promotion.promote(
        ctx, completed, dataset_name=payload.dataset,
        note=payload.note, force=payload.force,
    )
    return {
        "run_id": str(run_id),
        "dataset": payload.dataset,
        "promoted": decision.promoted,
        "forced": payload.force and bool(decision.reasons),
        "reasons": decision.reasons,
        "deltas": decision.deltas,
    }
