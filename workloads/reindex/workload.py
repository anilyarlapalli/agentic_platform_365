"""Rebuild a collection beside the one being served, then flip.

Triggered whenever a collection's document set changes — an upload, a
replacement, a delete. The rebuild writes to a new ``build_version`` while reads
continue on the live one, and promotion is a single transaction. A rebuild that
dies half-written leaves the previous corpus serving rather than emptying it,
which is the failure the ``VectorIndex`` port docstring calls out about
rebuilding an index in place.

## Copy-forward, not re-embed

Chunks whose document is unchanged are copied to the new build with
``INSERT … SELECT`` — same text, same vector, new ``build_version``. No model
call and no cost. Only content that is actually new needs embedding. At 100k
vectors the difference between copying and re-embedding is the difference
between a second and a bill.

## Ingest: what copy-forward cannot supply

Copy-forward moves chunks that already exist. A document that has none — newly
uploaded, or replaced so its predecessor's chunks belong to different content —
needs them made. :func:`_ingest_missing` fetches the retained bytes from the
object store, chunks them, embeds, and writes into the new build. That is the
step that drives ``documents_without_chunks`` to zero at its source rather than
reporting it forever.

Two documents look identical from SQL and are not: one whose bytes are stored,
and one uploaded before there was anywhere to store them. The second cannot be
ingested by anything, so it is counted and logged, never guessed at.

## Embedding happens outside the transaction

Chunking and embedding sit between two sessions rather than inside one. Holding
a Postgres transaction open across a network call to the object store and then
the embedding API means a slow API turns into database connection exhaustion —
the failure mode ``correctness/side_effects.py`` describes. The cost is that a
crash mid-ingest leaves a partially written build, which is precisely the case
``builds.fail`` exists to clean up and the reason the build is written beside
the live one.

## A budget stop must abort the build, not shrink it

:class:`BudgetExceededError` propagates. It would be easy to catch it, keep the
chunks already embedded and promote what there is — and the result would be a
corpus that silently lost documents, serving confident answers from a fraction
of its content. The build is failed and the previous one keeps serving instead.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy import text

from platform_core.adapters.local.object_store import get_object_store
from platform_core.corpus import builds
from platform_core.corpus.chunking import UnsupportedDocument, chunk_document
from platform_core.correctness.cancellation import cancellation_point
from platform_core.db.engine import tenant_session
from platform_core.identity.principal import RequestContext
from platform_core.observability.llm import build_client
from platform_core.ports.errors import NotFoundError, TransientError
from platform_core.settings import get_settings

logger = logging.getLogger("platform.workloads.reindex")

WORKLOAD = "reindex"

# Chunks per embedding request. Batched because per-chunk calls make a 500-chunk
# manual 500 round trips; bounded because one oversized request is retried whole
# and a partial failure costs the entire batch.
EMBED_BATCH = 64


def _ingest_missing(
    ctx: RequestContext, collection: str, version: int
) -> dict[str, Any]:
    """Chunk and embed every current document contributing nothing to this build.

    Returns counts rather than raising on a document it cannot read: one
    unreadable file must not take down the rebuild of a collection that is
    otherwise fine. What it will not do is hide that — every skip is counted
    under a reason and surfaced in the run result.
    """
    with tenant_session(ctx.tenant) as s:
        pending = s.execute(
            text(
                "SELECT d.id, d.filename, d.storage_key FROM document d "
                "WHERE d.collection = :c AND d.superseded_at IS NULL "
                "  AND NOT EXISTS (SELECT 1 FROM chunk c "
                "                  WHERE c.document_id = d.id "
                "                    AND c.build_version = :v) "
                "ORDER BY d.created_at"
            ),
            {"c": collection, "v": version},
        ).all()

    if not pending:
        return {"ingested_documents": 0, "ingested_chunks": 0, "skipped": {}}

    store = get_object_store()
    # task="ingest" rather than the workload name: this is the classification the
    # budget policy branches on, and ingest fails **closed** on an unreadable
    # ledger. Thousands of embedding calls with nobody waiting is the case where
    # spending uncapped and unrecorded is worse than not spending at all.
    ingest_ctx = ctx.child(step="ingest", task="ingest")

    prepared: list[tuple[Any, str, list]] = []
    skipped: dict[str, int] = {}

    def skip(reason: str, filename: str, detail: str) -> None:
        skipped[reason] = skipped.get(reason, 0) + 1
        logger.warning("%s/%s: skipping %s — %s: %s",
                       ctx.tenant.slug, collection, filename, reason, detail)

    for row in pending:
        cancellation_point(ctx)
        if not row.storage_key:
            skip("no_storage_key", row.filename, "the row predates retained content")
            continue
        try:
            data = store.get(ingest_ctx, row.storage_key)
        except NotFoundError:
            # Written before the object store existed, or the key belongs to
            # another tenant's namespace and was refused. Both mean the same
            # thing here: the bytes are not available to rebuild from.
            skip("bytes_missing", row.filename, f"no object at {row.storage_key}")
            continue
        except TransientError:
            # Deliberately not a skip. A store that is briefly down would
            # otherwise silently produce a smaller corpus and promote it.
            raise

        try:
            chunks = chunk_document(row.filename, data)
        except UnsupportedDocument as exc:
            skip("unsupported_format", row.filename, str(exc))
            continue

        if not chunks:
            skip("no_text", row.filename, "the document extracted to no text")
            continue
        prepared.append((row.id, row.filename, chunks))

    if not prepared:
        return {"ingested_documents": 0, "ingested_chunks": 0, "skipped": skipped}

    flat = [(document_id, filename, chunk)
            for document_id, filename, chunks in prepared
            for chunk in chunks]

    llm = build_client()
    settings = get_settings()
    vectors: list[list[float]] = []
    for start in range(0, len(flat), EMBED_BATCH):
        cancellation_point(ctx)
        batch = flat[start : start + EMBED_BATCH]
        vectors.extend(llm.embed(ingest_ctx, [chunk.text for _, _, chunk in batch]))

    with tenant_session(ctx.tenant) as s:
        # strict=True: a short vector list would otherwise write chunks for the
        # first N and silently drop the rest, which is a smaller corpus reported
        # as a complete one.
        for (document_id, filename, chunk), vector in zip(flat, vectors, strict=True):
            cancellation_point(ctx)
            s.execute(
                text(
                    "INSERT INTO chunk (tenant_id, document_id, collection, "
                    "  canonical_id, ordinal, build_version, text, embedding, "
                    "  embedding_model, meta) "
                    "VALUES (:t, :d, :c, :cid, :ord, :v, :txt, CAST(:vec AS vector), "
                    "        :model, :meta) "
                    "ON CONFLICT (tenant_id, collection, canonical_id, build_version) "
                    "DO NOTHING"
                ),
                {
                    "t": ctx.tenant.id, "d": document_id, "c": collection,
                    "cid": chunk.canonical_id, "ord": chunk.ordinal, "v": version,
                    "txt": chunk.text, "vec": str(vector),
                    "model": settings.embedding_model,
                    "meta": json.dumps(
                        {"source": filename, "heading": chunk.heading, "ingested": True}
                    ),
                },
            )

    logger.info("%s/%s build %d: ingested %d chunks from %d document(s)",
                ctx.tenant.slug, collection, version, len(flat), len(prepared))
    return {
        "ingested_documents": len(prepared),
        "ingested_chunks": len(flat),
        "skipped": skipped,
    }


def run(ctx: RequestContext, payload: dict[str, Any]) -> dict[str, Any]:
    """Rebuild one collection. Safe to call again on the same ``ctx.run_id``."""
    collection = str(payload["collection"])

    previous = builds.live_version_or_none(ctx, collection)
    version = builds.begin(ctx, collection, run_id=ctx.run_id)

    try:
        cancellation_point(ctx)
        with tenant_session(ctx.tenant) as s:
            # Copy forward every chunk of the live build whose document is still
            # current. Superseded and deleted documents are excluded by the
            # join, which is how a delete becomes a smaller corpus rather than a
            # cascade — the old build keeps its rows until it is reaped, so the
            # deletion is reversible right up to that point.
            copied = 0
            if previous is not None:
                copied = s.execute(
                    text(
                        "INSERT INTO chunk (tenant_id, document_id, collection, "
                        "  canonical_id, ordinal, build_version, text, meta, "
                        "  embedding, embedding_model) "
                        "SELECT c.tenant_id, c.document_id, c.collection, "
                        "  c.canonical_id, c.ordinal, :new, c.text, c.meta, "
                        "  c.embedding, c.embedding_model "
                        "FROM chunk c JOIN document d ON d.id = c.document_id "
                        "WHERE c.collection = :col AND c.build_version = :old "
                        "  AND d.superseded_at IS NULL "
                        "ON CONFLICT (tenant_id, collection, canonical_id, build_version) "
                        "DO NOTHING"
                    ),
                    {"new": version, "old": previous, "col": collection},
                ).rowcount

        # Outside the transaction on purpose — see the module docstring. Copy
        # forward first and ingest second, so a document whose chunks already
        # exist is never re-embedded just because the order was convenient.
        ingested = _ingest_missing(ctx, collection, version)

        with tenant_session(ctx.tenant) as s:
            current_documents = s.execute(
                text(
                    "SELECT count(*) FROM document "
                    "WHERE collection = :c AND superseded_at IS NULL"
                ),
                {"c": collection},
            ).scalar_one()

            # Documents that are current but contribute nothing to the new
            # build. Now that ingest exists this should be zero, and it is still
            # counted rather than assumed: a document whose bytes were never
            # retained still lands here, and the count is the only thing that
            # distinguishes a complete corpus from one that merely looks it.
            without_chunks = s.execute(
                text(
                    "SELECT count(*) FROM document d "
                    "WHERE d.collection = :c AND d.superseded_at IS NULL "
                    "  AND NOT EXISTS (SELECT 1 FROM chunk c "
                    "                  WHERE c.document_id = d.id "
                    "                    AND c.build_version = :v)"
                ),
                {"c": collection, "v": version},
            ).scalar_one()

        cancellation_point(ctx)
        result = builds.promote(ctx, collection, version)
        result["copied_chunks"] = copied
        result["previous_build"] = previous
        result["documents"] = current_documents
        result["documents_without_chunks"] = without_chunks
        result["ingested_documents"] = ingested["ingested_documents"]
        result["ingested_chunks"] = ingested["ingested_chunks"]
        result["skipped_documents"] = ingested["skipped"]

        reaped = builds.reap(ctx, collection)
        result["reaped_builds"] = reaped["deleted_builds"]

        if without_chunks:
            logger.warning(
                "%s/%s build %d: %d current document(s) contributed no chunks — "
                "their content is not retrievable",
                ctx.tenant.slug, collection, version, without_chunks,
            )

        # The corpus changed, so any graph cached against the old fingerprint is
        # stale. Dropping it here means the next query rebuilds rather than
        # serving a graph of documents that no longer exist.
        from workloads.graphrag import service as graphrag

        result["graphs_invalidated"] = graphrag.invalidate(ctx)
        return result

    except ValueError as exc:
        # promote() refuses an empty build. That is a legitimate outcome — a
        # collection whose every document was deleted — and the live build is
        # deliberately left alone rather than the collection going dark.
        builds.fail(ctx, collection, version, str(exc))
        logger.warning("reindex of %s/%s left build %d unpromoted: %s",
                       ctx.tenant.slug, collection, version, exc)
        return {"collection": collection, "build_version": version,
                "promoted": False, "reason": str(exc)}

    except Exception as exc:
        builds.fail(ctx, collection, version, f"{type(exc).__name__}: {exc}")
        logger.exception("reindex failed for %s/%s", ctx.tenant.slug, collection)
        raise
