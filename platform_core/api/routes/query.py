"""The chat surface. One route, already declared in the policy table.

`POST /api/query` was a forward declaration for several phases — a capability
with no handler. Default-deny meant that was safe (an undeclared *or*
unimplemented route is refused), but it also meant the platform had no way to
answer a question.

The handler is thin on purpose. Everything that makes the call safe already
exists: the middleware resolved and authorised the caller, the session is bound
to the principal, retrieval runs inside a tenant session, and the model call goes
through the single instrumented chain. The route's own job is request shape and
error mapping.
"""

from __future__ import annotations

import logging
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from platform_core.api.deps import get_context
from platform_core.identity.principal import RequestContext
from platform_core.ports.errors import BudgetExceededError, TransientError
from platform_core.scaling import sessions
from workloads.chat import service as chat

# Safe at module level: `engine` imports only stdlib, numpy and settings, and
# touches the external tree no earlier than `prepare_imports`. The *service*
# import stays deferred inside the handler for that reason. Importing the
# exception lazily inside the `try` would leave it undefined in the `except`
# clause on the dense path, which is a NameError waiting for an outage.
from workloads.graphrag.engine import GraphRAGDisabled

logger = logging.getLogger("platform.api.query")
router = APIRouter(prefix="/api", tags=["query"])

_llm = None


def _client():
    """Built once. The cache is attached here so every chat turn shares it."""
    global _llm
    if _llm is None:
        from platform_core.observability.llm import build_client
        from platform_core.scaling.cache import RedisSemanticCache

        _llm = build_client(cache=_CacheAdapter(RedisSemanticCache()))
    return _llm


class _CacheAdapter:
    """Bridges the cache to the shape the LLM chain expects.

    The chain asks for `get(ctx, key)` returning a response dict and
    `put(ctx, key, response)`. Keeping the adapter here rather than widening the
    cache's own interface means the cache stays a plain store.
    """

    def __init__(self, cache) -> None:
        self._cache = cache
        self.stats = cache.stats

    def get(self, ctx, key):
        data = self._cache.get(ctx, key)
        if data is None:
            return None
        return {
            "content": data["content"],
            "model": data["model"],
            "usage": __import__("platform_core.ports.llm", fromlist=["TokenUsage"]).TokenUsage(
                data["usage"]["input_tokens"], data["usage"]["output_tokens"],
                reported=data["usage"].get("reported", True),
            ),
            "cost_usd": 0.0,
            "finish_reason": data.get("finish_reason", "stop"),
            "attempts": 1,
        }

    def put(self, ctx, key, response):
        self._cache.put(ctx, key, response)


class QueryRequest(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    collection: str = Field(default="maintenance", max_length=128)
    session_id: str | None = Field(default=None, max_length=128)
    # "dense" is pure vector similarity; "graph" adds lexical retrieval and
    # knowledge-graph entity traversal, fused. Defaulted to dense because it has
    # no schema dependency — graph mode needs a schema that fits the corpus, and
    # a mismatched one yields an entity-poor graph that costs time and adds
    # nothing.
    mode: Literal["dense", "graph"] = "dense"
    schema_domain: str = Field(default="manufacturing", max_length=64)


class SourceOut(BaseModel):
    canonical_id: str
    text: str
    distance: float | None = None
    score: float | None = None
    # Which retriever put this chunk in context. When a GraphRAG answer is
    # wrong, this is the first question asked and the hardest to reconstruct
    # afterwards — so it is recorded at fusion time.
    signals: list[str] | None = None


class GraphOut(BaseModel):
    nodes: int
    edges: int
    schema_domain: str
    documents: int
    build_ms: float
    # Surfaced deliberately: an edgeless graph answers exactly like a populated
    # one, with no error and worse retrieval. It is a live defect, not a detail.
    edgeless: bool


class QueryResponse(BaseModel):
    answer: str
    mode: str
    sources: list[SourceOut]
    graph: GraphOut | None = None
    retrieval: dict | None = None
    session_id: str
    grounded: bool
    cache_hit: bool
    cost_usd: float
    input_tokens: int
    output_tokens: int
    latency_ms: float


@router.post("/query", response_model=QueryResponse)
def query(payload: QueryRequest, ctx: RequestContext = Depends(get_context)) -> QueryResponse:
    """Answer a question from the tenant's indexed sources.

    ``task="chat"`` is stamped on the context because the ledger's per-task
    policy reads it: chat fails **open** if the ledger is unreadable, since a
    user is waiting and one call costs cents. Background paths fail closed.
    """
    scoped = RequestContext(
        principal=ctx.principal,
        request_id=ctx.request_id,
        run_id=ctx.run_id,
        idempotency_key=ctx.idempotency_key,
        labels={**ctx.labels, "workload": chat.WORKLOAD, "task": "chat"},
    )

    try:
        if payload.mode == "graph":
            from workloads.graphrag import service as graphrag

            g = graphrag.answer(
                scoped, question=payload.question, collection=payload.collection,
                llm=_client(), session_id=payload.session_id,
                schema_domain=payload.schema_domain,
            )
            return QueryResponse(
                answer=g.answer, mode="graph",
                sources=[
                    SourceOut(canonical_id=s["chunk_id"], text=s["text"],
                              score=s["score"], signals=s["signals"])
                    for s in g.sources
                ],
                graph=GraphOut(
                    nodes=g.graph.nodes, edges=g.graph.edges,
                    schema_domain=g.graph.schema_domain, documents=g.graph.documents,
                    build_ms=g.graph.build_ms, edgeless=g.graph.edgeless,
                ),
                retrieval=g.retrieval,
                session_id=g.session_id, grounded=g.grounded, cache_hit=False,
                cost_usd=g.cost_usd, input_tokens=g.input_tokens,
                output_tokens=g.output_tokens, latency_ms=round(g.latency_ms, 1),
            )

        result = chat.answer(
            scoped,
            question=payload.question,
            collection=payload.collection,
            llm=_client(),
            session_id=payload.session_id,
        )
    except GraphRAGDisabled as exc:
        # 501, not 500: the request was well formed and the platform is working
        # correctly — this deployment simply does not carry the graph engine.
        # A 5xx that says "not configured" is the difference between a client
        # retrying forever and a client switching to dense mode.
        raise HTTPException(status_code=501, detail=str(exc)) from None
    except sessions.SessionNotFound:
        # 404 whether the session is absent or belongs to someone else — telling
        # them apart confirms an id exists.
        raise HTTPException(status_code=404, detail="Session not found.") from None
    except BudgetExceededError as exc:
        # 429, not 500: the request was refused deliberately and retrying later
        # is the correct client behaviour.
        raise HTTPException(status_code=429, detail=str(exc)) from None
    except TransientError as exc:
        raise HTTPException(status_code=503, detail=f"Upstream unavailable: {exc}") from None

    return QueryResponse(
        answer=result.answer,
        mode="dense",
        sources=[
            SourceOut(canonical_id=s.canonical_id, text=s.text, distance=round(s.distance, 4))
            for s in result.sources
        ],
        session_id=result.session_id,
        grounded=result.grounded,
        cache_hit=result.cache_hit,
        cost_usd=result.cost_usd,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        latency_ms=round(result.latency_ms, 1),
    )
