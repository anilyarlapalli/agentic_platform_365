"""Retrieval over embedded chunks.

One id namespace, enforced at the port. Chunks are addressed by
``canonical_id`` — ``c_<sha1:16>`` of the normalised text — and nothing else
crosses this boundary.

That constraint exists because of a specific, expensive failure in the Azure
build. Three namespaces are live there for the same chunk: the canonical id that
ingestion mints and eval sets cite, an *ordinal* position in a loaded list that
the retriever returns, and a third minted by an adapter as
``md5(f"{source_stem}::{index}")[:12]``. Comparing the eval set's canonical ids
against the retriever's ordinals produced a retrieval recall of 0.0 while
retrieval was working perfectly — and two rollout cycles were spent
investigating it as a plumbing failure before the namespaces were understood.

Ordinals still exist inside an adapter, because BM25 and graph retrievers need a
stable position within one build. They do not appear in this interface, they are
never persisted in an artifact, and they never travel through a queue.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from platform_core.identity.principal import RequestContext


@dataclass(frozen=True, slots=True)
class SearchHit:
    canonical_id: str
    text: str
    score: float
    metadata: dict[str, Any]
    # Which build produced this. Lets a caller notice that results came from a
    # corpus older than the one it thought it was querying, instead of silently
    # answering from a stale index.
    build_version: int


@runtime_checkable
class VectorIndex(Protocol):
    def upsert(
        self,
        ctx: RequestContext,
        collection: str,
        chunks: list[dict[str, Any]],
        *,
        build_version: int,
    ) -> int:
        """Add or replace chunks for a build. Returns the count written.

        Versioned rather than destructive: a rebuild writes a new
        ``build_version`` and the previous one stays queryable until it is
        promoted away. The Azure build recreates the whole index in place, which
        means a failed rebuild leaves the tenant with nothing to serve — the
        rollback story for a corpus is "re-ingest and wait".
        """
        ...

    def search(
        self,
        ctx: RequestContext,
        collection: str,
        query_embedding: list[float],
        *,
        top_k: int = 10,
        build_version: int | None = None,
        filters: dict[str, Any] | None = None,
    ) -> list[SearchHit]:
        """Nearest neighbours within the caller's tenant.

        Tenant scoping is not a parameter and cannot be disabled. The adapter
        runs inside a tenant session, so the database applies the boundary even
        if this method's own filtering were wrong.
        """
        ...

    def search_lexical(
        self, ctx: RequestContext, collection: str, query: str, *, top_k: int = 10,
        build_version: int | None = None,
    ) -> list[SearchHit]:
        """Keyword search. Empty list when the backend has none — never an error.

        A hybrid retriever should degrade to dense-only rather than fail, and
        making that the port's contract stops each caller inventing its own
        fallback.
        """
        ...

    def delete_collection(self, ctx: RequestContext, collection: str,
                          *, build_version: int | None = None) -> int:
        ...

    def stats(self, ctx: RequestContext, collection: str) -> dict[str, Any]:
        """Chunk count, build versions present, embedding model and dimension.

        The dimension is reported because a collection built at one width and
        queried at another is a silent-nonsense failure, and this is what a
        health check compares against the configured model.
        """
        ...
