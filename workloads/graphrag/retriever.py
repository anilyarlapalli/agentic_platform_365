"""The engine's vector-retriever shape, backed by tenant-scoped pgvector.

``HybridRetriever`` calls exactly three things on whatever it is given:
``build_index(documents)``, ``retrieve(query, top_k)`` and
``retrieve_sparse(query, top_k)``. That is the whole contract, so that is the
whole class.

## One id namespace

The engine's own Azure store returns ``SearchResult.chunk_id`` as an **integer
ordinal** into an in-memory list, while ``HybridRetriever`` keys its documents by
a **string** id, and ``pipeline/adapter`` mints a **third** by re-hashing. Three
namespaces for one chunk is what made retrieval recall read 0.0 in that
deployment while retrieval worked perfectly — and cost two rollout cycles to
diagnose.

Nothing here uses an ordinal. Chunks are addressed by ``canonical_id``
(``c_<sha1:16>``, content-derived) from the database through the retriever and
out to the citation in the answer. A rebuild renumbers nothing, so a stored
result still means what it meant.

## Scoping

Queries run inside ``tenant_session``, so row-level security applies underneath.
There is no tenant predicate to forget: a bug returns nothing rather than
someone else's documents.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text

from platform_core.corpus import builds
from platform_core.db.engine import tenant_session
from platform_core.identity.principal import RequestContext

logger = logging.getLogger("platform.workloads.graphrag.retriever")


@dataclass
class ScoredChunk:
    """Field-compatible with the engine's ``SearchResult``.

    Declared here rather than imported so that loading this module does not drag
    in ``doc_pipeline.embeddings`` — which binds ``sentence_transformers`` at
    module level and would defeat the stub. The engine never
    ``isinstance``-checks it; consumers read the attributes.
    """

    text: str
    metadata: dict
    score: float
    chunk_id: str          # canonical id — deliberately not an ordinal


class PgVectorRetriever:
    """Dense retrieval over the caller's own chunks."""

    def __init__(self, ctx: RequestContext, collection: str, llm) -> None:
        self._ctx = ctx
        self._collection = collection
        self._llm = llm
        self.documents: list[dict[str, Any]] = []

    # ── the engine's contract ─────────────────────────────────────────────

    def build_index(self, documents: list[dict]) -> None:
        """No-op beyond bookkeeping — the index already exists.

        The engine expects to build an index from documents it holds in memory.
        Here the vectors are already in Postgres with an HNSW index, written at
        ingestion. Rebuilding on every query would be pure waste, so this keeps
        the document list for hydration and returns.
        """
        self.documents = documents
        logger.debug("build_index: %d documents already indexed in pgvector",
                     len(documents))

    def retrieve(self, query, top_k: int = 5) -> list[ScoredChunk]:
        """Nearest neighbours within the tenant.

        ``query`` may arrive as a string or as an already-embedded vector — the
        engine does both depending on which path calls it, so both are handled
        rather than assuming the common one.
        """
        vector = (
            self._llm.embed(self._ctx, [query])[0]
            if isinstance(query, str)
            else list(query)
        )

        with tenant_session(self._ctx.tenant) as s:
            build_version = builds.live_version_or_none(
                self._ctx, self._collection, session=s
            )
            if build_version is None:
                return []
            rows = s.execute(
                text(
                    "SELECT canonical_id, text, meta, "
                    "  1 - (embedding <=> CAST(:probe AS vector)) AS score "
                    "FROM chunk "
                    "WHERE collection = :c AND build_version = :v "
                    "  AND embedding IS NOT NULL "
                    "ORDER BY embedding <=> CAST(:probe AS vector) "
                    "LIMIT :k"
                ),
                {"probe": str(vector), "c": self._collection, "k": top_k,
                 "v": build_version},
            ).all()

        return [
            ScoredChunk(text=r.text, metadata=r.meta or {},
                        score=float(r.score), chunk_id=r.canonical_id)
            for r in rows
        ]

    def retrieve_sparse(self, query: str, top_k: int = 5) -> list[ScoredChunk]:
        """Lexical retrieval via Postgres full-text search.

        Returns an empty list rather than raising when nothing matches, which is
        the contract the hybrid retriever expects — it degrades to dense-only
        rather than failing, so a query with no lexical hits still answers.
        """
        try:
            with tenant_session(self._ctx.tenant) as s:
                build_version = builds.live_version_or_none(
                    self._ctx, self._collection, session=s
                )
                if build_version is None:
                    return []
                rows = s.execute(
                    text(
                        "SELECT canonical_id, text, meta, "
                        "  ts_rank(to_tsvector('english', text), "
                        "          plainto_tsquery('english', :q)) AS score "
                        "FROM chunk "
                        "WHERE collection = :c AND build_version = :v "
                        # Spelled exactly as the GIN expression index in 0015 —
                        # a differing regconfig, or one passed as a bind rather
                        # than a literal, silently falls back to a scan.
                        "  AND to_tsvector('english', text) @@ plainto_tsquery('english', :q) "
                        "ORDER BY score DESC LIMIT :k"
                    ),
                    {"q": query, "c": self._collection, "k": top_k, "v": build_version},
                ).all()
        except Exception:
            logger.debug("sparse retrieval failed — degrading to dense only",
                         exc_info=True)
            return []

        return [
            ScoredChunk(text=r.text, metadata=r.meta or {},
                        score=float(r.score), chunk_id=r.canonical_id)
            for r in rows
        ]

    # ── platform-side helpers ─────────────────────────────────────────────

    def load_documents(self) -> list[dict[str, Any]]:
        """Every chunk for this collection, in the engine's document shape.

        The knowledge graph and BM25 both need the corpus resident. Keyed by
        ``canonical_id`` so the graph's ``chunk_ids`` sets, the BM25 index and
        the dense results all speak one namespace.

        Scoped to the live build like every other read. Omitting it here would
        be worse than in the ranked paths: the graph would be built from every
        build at once, so a rebuild would double the corpus the graph sees and
        the *entity counts* would silently inflate.
        """
        with tenant_session(self._ctx.tenant) as s:
            build_version = builds.live_version_or_none(
                self._ctx, self._collection, session=s
            )
            if build_version is None:
                return []
            rows = s.execute(
                text(
                    "SELECT canonical_id, text, meta FROM chunk "
                    "WHERE collection = :c AND build_version = :v "
                    "ORDER BY canonical_id"
                ),
                {"c": self._collection, "v": build_version},
            ).all()

        return [
            {
                "chunk_id": r.canonical_id,
                "text": r.text,
                "metadata": r.meta or {},
                "source": (r.meta or {}).get("source", "unknown"),
            }
            for r in rows
        ]
