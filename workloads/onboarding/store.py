"""Persistence for onboarding sessions and their artifacts.

Every query runs inside ``tenant_session``, so none of them carries a
``WHERE tenant_id = …`` clause — Postgres applies the boundary. A handler that
forgets a filter returns nothing rather than another tenant's taxonomy.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from sqlalchemy import text

from platform_core.db.engine import tenant_session
from platform_core.identity.principal import RequestContext
from platform_core.observability import audit

logger = logging.getLogger("platform.workloads.onboarding.store")

# Kinds with exactly one *effective* artifact per session, stored under a row
# whose ``name`` equals its ``kind``. Siblings may exist under other names —
# ``schema_drafted`` holds the drafter's original once a reviewer has edited it —
# and they are history, not inputs. See :func:`artifacts_for`.
SINGLETON_KINDS = ("schema", "instance_table", "predicate_map",
                   "candidate_queries")


def drafted_name(kind: str) -> str:
    """Where a kind's original goes the first time a human overwrites it.

    One rule for every singleton kind rather than a constant per kind, because
    the schema is not the only artifact a correction touches — retyping the
    instance table is the other half of the same edit.
    """
    return f"{kind}_drafted"


DRAFTED_SCHEMA_NAME = drafted_name("schema")


def _row(r) -> dict[str, Any]:
    return {
        "id": str(r.id),
        "domain": r.domain,
        "collection": r.collection,
        "status": r.status,
        "progress": r.progress or [],
        "stats": r.stats or {},
        "error": r.error,
        "run_id": str(r.run_id) if r.run_id else None,
        "created_by": str(r.created_by) if r.created_by else None,
        "created_at": r.created_at.isoformat(),
        "updated_at": r.updated_at.isoformat(),
        "approved_by": str(r.approved_by) if r.approved_by else None,
        "approved_at": r.approved_at.isoformat() if r.approved_at else None,
        "published_at": r.published_at.isoformat() if r.published_at else None,
        # Surfaced, not inferred. "The model proposed this" and "a human rewrote
        # it and someone else signed it off" are different provenance, and a
        # reader cannot tell them apart from the schema alone.
        "schema_edited_by": str(r.schema_edited_by) if r.schema_edited_by else None,
        "schema_edited_at": (
            r.schema_edited_at.isoformat() if r.schema_edited_at else None
        ),
    }


_SELECT = (
    "SELECT id, domain, collection, status, progress, stats, error, run_id, "
    "created_by, created_at, updated_at, approved_by, approved_at, published_at, "
    "schema_edited_by, schema_edited_at "
    "FROM onboarding_session "
)


def create(session, ctx: RequestContext, *, domain: str, collection: str,
           run_id: uuid.UUID | None = None) -> uuid.UUID:
    """Insert a drafting session. Joins the caller's transaction.

    Takes an open ``session`` because the row and the outbox entry that queues
    its work must commit together — a session with no queued run is a draft that
    never starts, and a queued run with no session is a worker with nothing to
    write to.
    """
    return session.execute(
        text(
            "INSERT INTO onboarding_session "
            "(tenant_id, domain, collection, status, run_id, created_by) "
            "VALUES (:t, :d, :c, 'drafting', :r, :by) RETURNING id"
        ),
        {
            "t": ctx.tenant.id, "d": domain.strip().lower(),
            "c": collection, "r": run_id, "by": ctx.principal.id,
        },
    ).scalar_one()


def get(ctx: RequestContext, session_id: uuid.UUID) -> dict | None:
    with tenant_session(ctx.tenant) as s:
        r = s.execute(text(_SELECT + "WHERE id = :id"), {"id": session_id}).one_or_none()
    return _row(r) if r else None


def list_sessions(ctx: RequestContext, *, domain: str | None = None,
                  limit: int = 50) -> list[dict]:
    with tenant_session(ctx.tenant) as s:
        if domain:
            rows = s.execute(
                text(_SELECT + "WHERE domain = :d ORDER BY created_at DESC LIMIT :l"),
                {"d": domain.strip().lower(), "l": limit},
            ).all()
        else:
            rows = s.execute(
                text(_SELECT + "ORDER BY created_at DESC LIMIT :l"), {"l": limit}
            ).all()
    return [_row(r) for r in rows]


def append_progress(ctx: RequestContext, session_id: uuid.UUID, entry: dict) -> None:
    """Append one step record.

    Written as it happens rather than accumulated and saved at the end: a draft
    that dies mid-run must leave evidence of how far it got, which is exactly
    what the Azure build lost when its replica was reclaimed mid-review.
    """
    with tenant_session(ctx.tenant) as s:
        s.execute(
            text(
                "UPDATE onboarding_session "
                "SET progress = progress || CAST(:e AS jsonb), updated_at = now() "
                "WHERE id = :id"
            ),
            {"e": json.dumps([entry]), "id": session_id},
        )


def set_status(ctx: RequestContext, session_id: uuid.UUID, status: str, *,
               error: str | None = None, stats: dict | None = None) -> None:
    with tenant_session(ctx.tenant) as s:
        s.execute(
            text(
                "UPDATE onboarding_session SET status = :st, updated_at = now(), "
                "error = COALESCE(:err, error), "
                "stats = COALESCE(CAST(:stats AS jsonb), stats) WHERE id = :id"
            ),
            {
                "st": status, "err": error,
                "stats": json.dumps(stats) if stats is not None else None,
                "id": session_id,
            },
        )


def put_artifact(ctx: RequestContext, session_id: uuid.UUID, kind: str,
                 name: str, payload: Any) -> None:
    with tenant_session(ctx.tenant) as s:
        s.execute(
            text(
                "INSERT INTO onboarding_artifact "
                "(tenant_id, session_id, kind, name, payload) "
                "VALUES (:t, :s, :k, :n, CAST(:p AS jsonb)) "
                "ON CONFLICT (session_id, kind, name) DO UPDATE "
                "  SET payload = EXCLUDED.payload, created_at = now()"
            ),
            {
                "t": ctx.tenant.id, "s": session_id, "k": kind,
                "n": name, "p": json.dumps(payload),
            },
        )


def artifacts_for(ctx: RequestContext, session_id: uuid.UUID) -> dict[str, Any]:
    """Artifacts in the shape ``artifacts.materialize`` expects.

    A singleton kind resolves to the row whose ``name`` equals its ``kind``, and
    nothing else. The earlier version keyed purely on ``kind``, so any sibling row
    under the same kind overwrote it in whatever order the rows came back — which
    made the published schema depend on a query's ordering the moment a second
    row existed. It does exist now: ``schema_drafted`` retains the drafter's
    original after a reviewer edits it, and that must never be what gets built.
    """
    with tenant_session(ctx.tenant) as s:
        rows = s.execute(
            text(
                "SELECT kind, name, payload FROM onboarding_artifact "
                "WHERE session_id = :s"
            ),
            {"s": session_id},
        ).all()

    out: dict[str, Any] = {}
    cache: dict[str, Any] = {}
    for r in rows:
        if r.kind == "extraction_cache":
            cache[r.name] = r.payload
        elif r.kind in SINGLETON_KINDS and r.name == r.kind:
            out[r.kind] = r.payload
    if cache:
        out["extraction_cache"] = cache
    return out


def artifact_counts(ctx: RequestContext, session_id: uuid.UUID) -> dict[str, int]:
    with tenant_session(ctx.tenant) as s:
        rows = s.execute(
            text(
                "SELECT kind, count(*) AS n FROM onboarding_artifact "
                "WHERE session_id = :s GROUP BY kind"
            ),
            {"s": session_id},
        ).all()
    return {r.kind: r.n for r in rows}


def _retain_original(s, ctx: RequestContext, session_id: uuid.UUID, kind: str) -> bool:
    """Copy a singleton artifact to its ``_drafted`` sibling, once. Joins the caller's
    transaction.

    Once, and only once: a second edit must not overwrite the drafter's version
    with the editor's previous attempt, or "what the model proposed" stops being
    answerable after the first correction. Returns whether a copy was made.
    """
    name = drafted_name(kind)
    if s.execute(
        text(
            "SELECT 1 FROM onboarding_artifact "
            "WHERE session_id = :s AND kind = :k AND name = :n"
        ),
        {"s": session_id, "k": kind, "n": name},
    ).scalar_one_or_none():
        return False

    s.execute(
        text(
            "INSERT INTO onboarding_artifact (tenant_id, session_id, kind, name, payload) "
            "SELECT tenant_id, session_id, kind, :n, payload FROM onboarding_artifact "
            "WHERE session_id = :s AND kind = :k AND name = :k"
        ),
        {"s": session_id, "k": kind, "n": name},
    )
    return True


def candidate_queries(ctx: RequestContext, session_id: uuid.UUID) -> list[dict[str, Any]]:
    """The proposed questions for this session, with their review state."""
    with tenant_session(ctx.tenant) as s:
        payload = s.execute(
            text(
                "SELECT payload FROM onboarding_artifact "
                "WHERE session_id = :s AND kind = 'candidate_queries' "
                "  AND name = 'candidate_queries'"
            ),
            {"s": session_id},
        ).scalar_one_or_none()
    return list((payload or {}).get("queries") or [])


def curate_query(
    ctx: RequestContext,
    session_id: uuid.UUID,
    query_id: str,
    *,
    query_text: str | None = None,
    approved: bool | None = None,
) -> dict[str, Any]:
    """Keep, edit or drop one proposed question.

    Editing marks the query ``edited``, kept separately from ``approved`` for the
    same reason the eval set separates ``sme_edited`` from ``confirmed``: a
    question a human rewrote and a question they waved through are different
    provenance, and only one of them is evidence that anybody read it.

    Allowed while the session is unpublished. Curating after publication would
    change what a published domain claims its questions are, without any of the
    approval the taxonomy went through.
    """
    with tenant_session(ctx.tenant) as s:
        row = s.execute(
            text("SELECT status FROM onboarding_session WHERE id = :id"),
            {"id": session_id},
        ).one_or_none()
        if row is None:
            raise LookupError("Session not found.")
        if row.status == "published":
            raise ValueError(
                "This session is published; its questions are fixed. Seed an "
                "eval set from them and edit it there."
            )

        payload = s.execute(
            text(
                "SELECT payload FROM onboarding_artifact "
                "WHERE session_id = :s AND kind = 'candidate_queries' "
                "  AND name = 'candidate_queries'"
            ),
            {"s": session_id},
        ).scalar_one_or_none()
        queries = list((payload or {}).get("queries") or [])
        if not queries:
            raise ValueError("This session proposed no candidate queries.")

        found = None
        for query in queries:
            if query.get("id") == query_id:
                found = query
                break
        if found is None:
            raise LookupError("Query not found in this session.")

        if query_text is not None and query_text.strip() != found["text"]:
            found["text"] = query_text.strip()
            found["edited"] = True
        if approved is not None:
            found["approved"] = approved

        s.execute(
            text(
                "UPDATE onboarding_artifact SET payload = CAST(:p AS jsonb) "
                "WHERE session_id = :s AND kind = 'candidate_queries' "
                "  AND name = 'candidate_queries'"
            ),
            {"s": session_id, "p": json.dumps({**(payload or {}), "queries": queries})},
        )
    return found


def edit_schema(
    ctx: RequestContext,
    session_id: uuid.UUID,
    yaml_text: str,
    *,
    retype: dict[str, str] | None = None,
) -> dict:
    """Replace the drafted taxonomy with a human-authored one, and retype with it.

    Only while the session is ``draft_ready``. After approval the bytes that were
    approved must be the bytes that get published, and an edit at that point
    would silently break that — so it is refused rather than allowed with a
    warning.

    ## Why ``retype`` is part of the same act

    Editing the schema alone does not fix a graph with no edges, and discovering
    that after publishing would make this feature worse than useless — it would
    look like a fix. ``KnowledgeGraph`` admits an instance-table entity whose type
    the schema does not declare *as a node carrying the literal type*
    ``raw:alarm``, and ``EdgeType.accepts`` compares that string against the
    schema's declared endpoint types. Adding ``Alarm`` to the taxonomy therefore
    changes nothing on its own: the node is still typed ``raw:alarm`` and every
    edge touching it is still discarded.

    The types live in the instance table, which the drafter derived under the
    *old* schema. So a correction is two writes or it is not a correction, and
    they belong in one transaction for the same reason approval and demotion do:
    a session whose schema declares types its instance table never uses is a
    state nobody should be able to observe.
    """
    with tenant_session(ctx.tenant) as s:
        row = s.execute(
            text(
                "SELECT status, created_by, schema_edited_by "
                "FROM onboarding_session WHERE id = :id"
            ),
            {"id": session_id},
        ).one_or_none()
        if row is None:
            raise LookupError("Session not found.")
        if row.status != "draft_ready":
            raise ValueError(
                f"Session is {row.status}; only a draft_ready taxonomy can be edited. "
                f"Editing an approved one would publish bytes nobody approved."
            )

        current = s.execute(
            text(
                "SELECT payload FROM onboarding_artifact "
                "WHERE session_id = :s AND kind = 'schema' AND name = 'schema'"
            ),
            {"s": session_id},
        ).scalar_one_or_none()
        if current is None:
            raise ValueError(
                "This session has no drafted schema to edit — the draft produced "
                "no taxonomy, so there is nothing to correct."
            )

        kept_schema = _retain_original(s, ctx, session_id, "schema")
        s.execute(
            text(
                "UPDATE onboarding_artifact SET payload = CAST(:p AS jsonb), "
                "  created_at = now() "
                "WHERE session_id = :s AND kind = 'schema' AND name = 'schema'"
            ),
            {"s": session_id, "p": json.dumps({"yaml": yaml_text})},
        )

        retyped = 0
        if retype:
            table = s.execute(
                text(
                    "SELECT payload FROM onboarding_artifact "
                    "WHERE session_id = :s AND kind = 'instance_table' "
                    "  AND name = 'instance_table'"
                ),
                {"s": session_id},
            ).scalar_one_or_none()
            if table is None:
                raise ValueError(
                    "This session has no instance table, so there is nothing to "
                    "retype. Edit the taxonomy alone, or re-draft."
                )

            _retain_original(s, ctx, session_id, "instance_table")
            instances = list((table or {}).get("instances") or [])
            for inst in instances:
                current_type = str(inst.get("entity_type", ""))
                mapped = retype.get(current_type) or retype.get(
                    current_type.removeprefix("raw:")
                )
                if mapped and mapped != current_type:
                    inst["entity_type"] = mapped
                    retyped += 1

            table["instances"] = instances
            s.execute(
                text(
                    "UPDATE onboarding_artifact SET payload = CAST(:p AS jsonb), "
                    "  created_at = now() "
                    "WHERE session_id = :s AND kind = 'instance_table' "
                    "  AND name = 'instance_table'"
                ),
                {"s": session_id, "p": json.dumps(table)},
            )

        s.execute(
            text(
                "UPDATE onboarding_session SET schema_edited_by = :by, "
                "  schema_edited_at = now(), updated_at = now() WHERE id = :id"
            ),
            {"by": ctx.principal.id, "id": session_id},
        )

    logger.info(
        "schema for session %s edited by %s (%d instances retyped)",
        session_id, ctx.principal.subject, retyped,
    )
    return {
        "id": str(session_id),
        "edited_by": str(ctx.principal.id),
        "original_retained": kept_schema,
        "instances_retyped": retyped,
    }


def approve(ctx: RequestContext, session_id: uuid.UUID) -> str:
    """Record approval. Returns the new status, or raises ValueError.

    The self-approval refusal lives in three places on purpose: the capability
    check, the conditional UPDATE below, and a CHECK constraint in migration
    0013. The constraint is the guarantee; this is what turns a constraint
    violation into an intelligible message.
    """
    with tenant_session(ctx.tenant) as s:
        row = s.execute(
            text(
                "SELECT status, created_by, schema_edited_by "
                "FROM onboarding_session WHERE id = :id"
            ),
            {"id": session_id},
        ).one_or_none()
        if row is None:
            raise LookupError("Session not found.")
        if row.status != "draft_ready":
            raise ValueError(
                f"Session is {row.status}; only a draft_ready session can be approved."
            )
        if row.created_by == ctx.principal.id:
            raise PermissionError("You drafted this schema; maker cannot be checker.")
        # Editing is authoring. Without this a reviewer could rewrite the
        # taxonomy and sign off their own rewrite, which is the unilateral path
        # to production the rule above exists to close — the same act, wearing a
        # different verb.
        if row.schema_edited_by == ctx.principal.id:
            raise PermissionError(
                "You edited this schema; maker cannot be checker. Another "
                "principal has to approve what you wrote."
            )

        updated = s.execute(
            text(
                "UPDATE onboarding_session SET status = 'approved', "
                "approved_by = :by, approved_at = now(), updated_at = now() "
                "WHERE id = :id AND status = 'draft_ready' RETURNING status"
            ),
            {"by": ctx.principal.id, "id": session_id},
        ).scalar_one_or_none()
        if updated is not None:
            audit.append_in_session(
                s,
                ctx,
                action="onboarding.session.approved",
                outcome=audit.Outcome.SUCCEEDED,
                resource_type="onboarding_session",
                resource_id=str(session_id),
            )

    if updated is None:
        raise ValueError("Session changed state concurrently; reload and retry.")
    return updated


def publish(ctx: RequestContext, session_id: uuid.UUID) -> dict:
    """Make an approved session the live taxonomy for its domain.

    Demoting the incumbent and promoting this one happen in one transaction
    because the partial unique index permits exactly one published session per
    domain — doing it in two steps would either violate the index or leave the
    domain with no live taxonomy in between.
    """
    with tenant_session(ctx.tenant) as s:
        row = s.execute(
            text("SELECT status, domain FROM onboarding_session WHERE id = :id"),
            {"id": session_id},
        ).one_or_none()
        if row is None:
            raise LookupError("Session not found.")
        if row.status != "approved":
            raise ValueError(f"Session is {row.status}; only an approved session can be published.")

        s.execute(
            text(
                "UPDATE onboarding_session SET status = 'approved', updated_at = now() "
                "WHERE domain = :d AND status = 'published'"
            ),
            {"d": row.domain},
        )
        s.execute(
            text(
                "UPDATE onboarding_session SET status = 'published', "
                "published_at = now(), updated_at = now() WHERE id = :id"
            ),
            {"id": session_id},
        )
        audit.append_in_session(
            s,
            ctx,
            action="onboarding.session.published",
            outcome=audit.Outcome.SUCCEEDED,
            resource_type="onboarding_session",
            resource_id=str(session_id),
            detail={"domain": row.domain},
        )

    return {"id": str(session_id), "domain": row.domain, "status": "published"}


def published_session(ctx: RequestContext, domain: str) -> dict | None:
    with tenant_session(ctx.tenant) as s:
        r = s.execute(
            text(_SELECT + "WHERE domain = :d AND status = 'published'"),
            {"d": domain.strip().lower()},
        ).one_or_none()
    return _row(r) if r else None


def published_artifacts(ctx: RequestContext, domain: str) -> dict[str, Any]:
    """The live artifacts for a domain, or ``{}`` when nothing is published."""
    live = published_session(ctx, domain)
    if live is None:
        return {}
    return artifacts_for(ctx, uuid.UUID(live["id"]))


def cancel(ctx: RequestContext, session_id: uuid.UUID) -> bool:
    with tenant_session(ctx.tenant) as s:
        updated = s.execute(
            text(
                "UPDATE onboarding_session SET status = 'cancelled', updated_at = now() "
                "WHERE id = :id AND status IN ('drafting','draft_ready') RETURNING id"
            ),
            {"id": session_id},
        ).scalar_one_or_none()
        if updated is not None:
            audit.append_in_session(
                s,
                ctx,
                action="onboarding.session.cancelled",
                outcome=audit.Outcome.SUCCEEDED,
                resource_type="onboarding_session",
                resource_id=str(session_id),
            )
    return updated is not None
