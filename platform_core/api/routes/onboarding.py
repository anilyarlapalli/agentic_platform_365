"""Onboarding: draft a taxonomy, have a human approve it, publish it.

The route split mirrors the authority split. Drafting spends real budget and
writes a proposal (``schema:author``); approving is a different act by a
different person (``schema:approve``); reading a draft is neither
(``schema:read``), because a reviewer must be able to look before deciding and
granting approval-in-order-to-look would defeat the separation.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text

from platform_core.api.deps import get_context
from platform_core.correctness.outbox import enqueue_run
from platform_core.db.engine import tenant_session
from platform_core.identity.principal import RequestContext
from platform_core.observability import audit
from workloads.onboarding import store
from workloads.onboarding.workload import DEFAULT_SAMPLE, WORKLOAD

logger = logging.getLogger("platform.api.onboarding")
router = APIRouter(prefix="/api/onboard", tags=["onboarding"])


class SessionCreate(BaseModel):
    domain: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    collection: str = Field(default="maintenance", max_length=128)
    sample: int = Field(default=DEFAULT_SAMPLE, ge=1, le=1000)


@router.post("/sessions", status_code=202)
def start_session(payload: SessionCreate, ctx: RequestContext = Depends(get_context)) -> dict:
    """Queue a drafting run. Returns immediately with the session id.

    202, not 201: the session row exists but the taxonomy does not yet. The work
    is queued through the outbox in the same transaction that creates the
    session, so there is no window in which one exists without the other.
    """
    domain = payload.domain.strip().lower()

    with tenant_session(ctx.tenant) as s:
        existing = s.execute(
            text(
                "SELECT id FROM onboarding_session "
                "WHERE domain = :d AND status = 'drafting' LIMIT 1"
            ),
            {"d": domain},
        ).scalar_one_or_none()
        if existing is not None:
            # Two concurrent drafts of one domain would both spend a full
            # corpus of extraction calls to produce competing taxonomies.
            raise HTTPException(
                status_code=409,
                detail=f"A draft for {domain!r} is already running "
                       f"(session {existing}). Cancel it first.",
            )

        session_id = store.create(s, ctx, domain=domain, collection=payload.collection)
        run_id, created = enqueue_run(
            s, ctx,
            workload=WORKLOAD,
            payload={
                "session_id": str(session_id),
                "domain": domain,
                "collection": payload.collection,
                "sample": payload.sample,
            },
            idempotency_key=ctx.idempotency_key or f"onboard:{domain}:{session_id}",
        )
        s.execute(
            text("UPDATE onboarding_session SET run_id = :r WHERE id = :id"),
            {"r": run_id, "id": session_id},
        )
        audit.append_in_session(
            s,
            ctx,
            action="onboarding.session.created",
            outcome=audit.Outcome.SUCCEEDED,
            resource_type="onboarding_session",
            resource_id=str(session_id),
            detail={"domain": domain, "collection": payload.collection, "run_id": str(run_id)},
        )

    logger.info("queued onboarding draft for %s/%s (session %s, run %s)",
                ctx.tenant.slug, domain, session_id, run_id)
    return {
        "id": str(session_id),
        "domain": domain,
        "collection": payload.collection,
        "status": "drafting",
        "run_id": str(run_id),
        "run_created": created,
    }


@router.get("/sessions")
def list_sessions(domain: str | None = None,
                  ctx: RequestContext = Depends(get_context)) -> dict:
    return {"sessions": store.list_sessions(ctx, domain=domain)}


@router.get("/sessions/{session_id}")
def get_session(session_id: uuid.UUID, ctx: RequestContext = Depends(get_context)) -> dict:
    session = store.get(ctx, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found.")
    session["artifact_counts"] = store.artifact_counts(ctx, session_id)
    session.update(_staleness(ctx, session))
    # The schema is the thing a reviewer actually reads, so it is inlined rather
    # than left behind a second request. The extraction cache is not — it can be
    # hundreds of documents and nobody reviews it by eye.
    artifacts = store.artifacts_for(ctx, session_id)
    session["schema_yaml"] = (artifacts.get("schema") or {}).get("yaml")
    session["predicate_map"] = artifacts.get("predicate_map")

    # Whether the taxonomy covers the entities drafted from the corpus. Computed
    # here rather than left for the reviewer to infer: an entity the schema does
    # not declare becomes a `raw:` type, and every relation touching one is
    # discarded when the graph is built — after approval, after publish, with no
    # error. It is the question the reviewer is actually being asked, and until
    # now nothing in the platform answered it.
    schema_yaml = session["schema_yaml"]
    if schema_yaml:
        from workloads.graphrag.artifacts import (
            InvalidSchema,
            taxonomy_fit,
            validate_schema_yaml,
        )

        try:
            report = validate_schema_yaml(schema_yaml)
        except InvalidSchema as exc:
            # A stored schema that no longer parses is worth saying out loud
            # rather than omitting the section as though it were fine.
            session["schema_error"] = str(exc)
        else:
            session["schema"] = report
            session["taxonomy_fit"] = taxonomy_fit(
                report["entity_types"], artifacts.get("instance_table")
            )
    return session


class QueryCurate(BaseModel):
    text: str | None = Field(default=None, min_length=8, max_length=1000)
    approved: bool | None = None


class SeedEval(BaseModel):
    dataset: str | None = Field(default=None, max_length=128)


@router.get("/sessions/{session_id}/queries")
def list_queries(session_id: uuid.UUID,
                 ctx: RequestContext = Depends(get_context)) -> dict:
    """Corpus-grounded questions proposed during the draft, with review state."""
    if store.get(ctx, session_id) is None:
        raise HTTPException(status_code=404, detail="Session not found.")
    queries = store.candidate_queries(ctx, session_id)
    return {
        "queries": queries,
        "approved": sum(1 for q in queries if q.get("approved")),
        "edited": sum(1 for q in queries if q.get("edited")),
        # An item with no evidence can never score retrieval recall, so it is
        # worth knowing before the set is seeded rather than after a run reports
        # a smaller scoreable count than the reviewer expected.
        "without_evidence": sum(1 for q in queries if not q.get("evidence_chunk_ids")),
    }


@router.post("/sessions/{session_id}/queries/{query_id}")
def curate_query(session_id: uuid.UUID, query_id: str, payload: QueryCurate,
                 ctx: RequestContext = Depends(get_context)) -> dict:
    """Keep, edit or drop one proposed question.

    ``schema:author``, because writing the questions a domain must answer is
    authoring, exactly as rewriting its taxonomy is. Approving them here is not
    the same act as approving the *taxonomy* — these do not feed the schema, so
    curating carries no maker/checker consequence.
    """
    try:
        query = store.curate_query(
            ctx, session_id, query_id,
            query_text=payload.text, approved=payload.approved,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    return {"query": query}


@router.post("/sessions/{session_id}/seed-eval", status_code=201)
def seed_eval_set(session_id: uuid.UUID, payload: SeedEval,
                  ctx: RequestContext = Depends(get_context)) -> dict:
    """Turn the approved questions into a golden set. No LLM cost.

    The point of seeding rather than generating: a reviewer has already said
    "the domain must answer these", and each question carries the canonical ids
    of the chunks it was drawn from — so ``must_cite`` names chunks the retriever
    actually emits. Generating fresh questions from random chunks is the right
    tool when there is no drafting session to draw on and the wrong one when
    there is.

    Carries ``release:promote``, like every other dataset write: this creates the
    thing the gate measures against.

    Expected answers are left blank on purpose. They are drafted by the
    annotator and read by a human — see ``POST /api/eval/datasets/{name}/draft``
    — because a question and its reference answer are two separate acts of
    judgement and collapsing them is how an unread set becomes "ground truth".
    """
    session = store.get(ctx, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found.")

    approved = [
        q for q in store.candidate_queries(ctx, session_id) if q.get("approved")
    ]
    if not approved:
        raise HTTPException(
            status_code=409,
            detail=(
                "No approved questions. Review the proposals first — seeding "
                "everything that was proposed would make the review decorative."
            ),
        )

    from platform_core.gates import datasets as eval_datasets

    name = (payload.dataset or session["domain"]).strip().lower()
    try:
        dataset = eval_datasets.save(
            ctx, name=name, collection=session["collection"],
            items=[
                {
                    "id": q["id"],
                    "question": q["text"],
                    "expected_answer": "",
                    "must_cite": q.get("evidence_chunk_ids") or [],
                }
                for q in approved
            ],
        )
    except eval_datasets.InvalidDataset as exc:
        # Reaching here means a non-canonical id survived generation. Reported
        # against the eval set rather than swallowed, because the remedy is to
        # re-draft, not to loosen the validation that makes recall meaningful.
        raise HTTPException(
            status_code=409,
            detail=f"The approved questions do not form a scoreable set: {exc}",
        ) from None

    audit.record(
        ctx, action="eval.dataset.seeded", outcome=audit.Outcome.SUCCEEDED,
        resource_type="eval_dataset", resource_id=name,
        detail={"session_id": str(session_id), "domain": session["domain"],
                "items": len(approved)},
    )
    return {
        "dataset": name,
        "collection": session["collection"],
        "content_sha256": dataset.content_sha256,
        "items": len(dataset.items),
        "items_scoreable": len(dataset.scoreable_items),
        "note": "Expected answers are blank — draft them, then read them.",
    }


class SchemaEdit(BaseModel):
    yaml: str = Field(min_length=1, max_length=400_000)
    # Free-form instance type -> declared entity type. Optional, because a
    # taxonomy edit that only renames a description needs no retyping; supplied
    # in the normal case, because a new entity type that no instance carries
    # changes nothing about the graph. See `store.edit_schema`.
    retype: dict[str, str] = Field(default_factory=dict)


@router.post("/sessions/{session_id}/schema")
def edit_schema(session_id: uuid.UUID, payload: SchemaEdit,
                ctx: RequestContext = Depends(get_context)) -> dict:
    """Replace a drafted taxonomy with a corrected one.

    Gated on ``schema:author``, not ``schema:approve``. Writing the taxonomy is
    authoring it whoever does it, and the approval path refuses the editor for
    the same reason it refuses the drafter — see migration 0017.

    Validated before it is stored, because the failure mode of an unparseable
    schema is silent: ``KnowledgeGraph`` falls back to the engine's own domain
    lookup, which does not know this domain, so ``build_graph`` catches the error
    and builds without artifacts — a graph with entities, no edges, and no
    complaint. A 400 here is the only place anyone finds out.
    """
    from workloads.graphrag.artifacts import InvalidSchema, taxonomy_fit, validate_schema_yaml

    try:
        report = validate_schema_yaml(payload.yaml)
    except InvalidSchema as exc:
        raise HTTPException(status_code=400, detail=f"Invalid taxonomy: {exc}") from None

    # Every retype target must be a type the edited schema declares. Mapping
    # `raw:alarm` onto something undeclared moves the entity from one type the
    # graph discards to another, which reads as a fix and is not one.
    undeclared = sorted(set(payload.retype.values()) - set(report["entity_types"]))
    if undeclared:
        raise HTTPException(
            status_code=400,
            detail=(
                f"retype targets {undeclared} are not declared as entity types in "
                f"this taxonomy. Declare them first, or the entities stay "
                f"unusable under a different name."
            ),
        )

    try:
        result = store.edit_schema(ctx, session_id, payload.yaml, retype=payload.retype)
    except LookupError:
        raise HTTPException(status_code=404, detail="Session not found.") from None
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None

    audit.record(
        ctx, action="onboarding.schema.edited", outcome=audit.Outcome.SUCCEEDED,
        resource_type="onboarding_session", resource_id=str(session_id),
        detail={
            "entity_types": report["entity_types"],
            "edge_types": report["edge_types"],
            "original_retained": result["original_retained"],
        },
    )

    # The fit report recomputed against the edit, so the author sees immediately
    # whether the change actually covers the corpus rather than finding out from
    # an edge count after publishing.
    artifacts = store.artifacts_for(ctx, session_id)
    result["schema"] = report
    result["taxonomy_fit"] = taxonomy_fit(
        report["entity_types"], artifacts.get("instance_table")
    )
    return result


@router.post("/sessions/{session_id}/approve")
def approve_session(session_id: uuid.UUID,
                    ctx: RequestContext = Depends(get_context)) -> dict:
    """Approve a draft. Refuses self-approval — maker cannot be checker."""
    try:
        status = store.approve(ctx, session_id)
    except LookupError:
        raise HTTPException(status_code=404, detail="Session not found.") from None
    except PermissionError as exc:
        audit.record(
            ctx,
            action="onboarding.session.approve",
            outcome=audit.Outcome.DENIED,
            resource_type="onboarding_session",
            resource_id=str(session_id),
            detail={"reason": str(exc)},
        )
        raise HTTPException(status_code=403, detail=str(exc)) from None
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    return {"id": str(session_id), "status": status}


@router.post("/sessions/{session_id}/publish")
def publish_session(session_id: uuid.UUID,
                    ctx: RequestContext = Depends(get_context)) -> dict:
    """Make an approved taxonomy live, and drop the cached graphs it invalidates."""
    try:
        result = store.publish(ctx, session_id)
    except LookupError:
        raise HTTPException(status_code=404, detail="Session not found.") from None
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None

    # Graphs are cached per tenant and collection, keyed on the corpus
    # fingerprint. Publishing changes the *artifacts* rather than the corpus, so
    # the fingerprint is unchanged and a cached graph would keep serving the old
    # edge count indefinitely. This is the invalidation the Azure build's
    # rollout notes warn about: "a reload is not enough".
    from workloads.graphrag import service as graphrag

    dropped = graphrag.invalidate(ctx)
    result["graphs_invalidated"] = dropped
    return result


@router.post("/sessions/{session_id}/cancel")
def cancel_session(session_id: uuid.UUID,
                   ctx: RequestContext = Depends(get_context)) -> dict:
    if not store.cancel(ctx, session_id):
        raise HTTPException(
            status_code=409,
            detail="Only a drafting or draft_ready session can be cancelled.",
        )
    return {"id": str(session_id), "status": "cancelled"}


@router.get("/domains")
def list_domains(ctx: RequestContext = Depends(get_context)) -> dict:
    """Which taxonomies are live, and whether each one can produce edges."""
    domains: dict[str, dict] = {}
    for session in store.list_sessions(ctx, limit=200):
        entry = domains.setdefault(session["domain"], {
            "domain": session["domain"], "published": None, "sessions": 0,
        })
        entry["sessions"] += 1
        if session["status"] == "published":
            entry["published"] = {
                "id": session["id"],
                "collection": session["collection"],
                "published_at": session["published_at"],
                "stats": session["stats"],
                # Restated at the top level because it is the one fact that
                # decides whether this domain traverses or only matches entities.
                "relations_available": bool(
                    (session["stats"] or {}).get("relations_available")
                ),
                **_staleness(ctx, session),
            }
    return {"domains": sorted(domains.values(), key=lambda d: d["domain"])}


def _staleness(ctx: RequestContext, session: dict) -> dict:
    """Whether the corpus has moved since this taxonomy was drafted.

    Artifacts are derived from a corpus and have no invalidation edge back to
    it: documents can be replaced or withdrawn afterwards, and the instance
    table will still name entities that no chunk contains. Surfaced rather than
    auto-invalidated — re-drafting spends real budget and needs an approval, so
    the decision belongs to a person. Same treatment as ``edgeless``: state it,
    do not hide it and do not act unilaterally.
    """
    from platform_core.corpus import builds as corpus_builds

    drafted = (session.get("stats") or {}).get("corpus_fingerprint")
    if not drafted:
        # Drafted before fingerprints were recorded. "Unknown" is the honest
        # answer; claiming it is current would be a guess.
        return {"corpus_status": "unknown", "corpus_drifted": None}

    live_build = corpus_builds.live_version_or_none(ctx, session["collection"])
    if live_build is None:
        return {"corpus_status": "collection has no live build", "corpus_drifted": True}

    current = corpus_builds.fingerprint(ctx, session["collection"], live_build)
    drifted = current != drafted
    return {
        "corpus_status": "drifted" if drifted else "current",
        "corpus_drifted": drifted,
        "corpus_build_version": live_build,
    }
