"""GraphRAG retrieval — dense + lexical + graph traversal — through the platform.

What the graph adds over the dense chat in ``workloads/chat``: entity matching
and neighbour traversal. A question naming a component pulls chunks that mention
it *and* chunks about things it is connected to, which dense similarity alone
does not reach — the classic case being a symptom whose cause is described in a
chunk that shares no vocabulary with the question.

## The knowledge graph is built without an LLM

Worth stating because it is the surprising part of the engine's design and it is
what makes this affordable: ``KnowledgeGraph.build_from_documents`` is
**deterministic** — regex, vocabulary, instance table, cached relations, driven
by ``schemas/<domain>.yaml``. The LLM's discovery work happens once at onboarding
and is captured as artifacts. So building a graph over a tenant's corpus costs no
tokens and takes milliseconds.

## Graphs are per tenant, in memory

The engine persists a graph to a per-*domain* path in its own tree. Two tenants
using the same schema would overwrite each other, and a shared file would cross
the isolation boundary — so the disk write is suppressed in ``engine.py`` and
graphs are cached per ``(tenant, collection)`` here, rebuilt when the corpus
changes.

## Degradation is a first-class outcome

An edgeless graph answers queries exactly like a populated one, with no error and
worse results — the failure that shipped a 39-node/0-edge build in the deployment
this was learned from. So :func:`build_graph` reports node and edge counts, the
API surfaces them, and a graph with no edges is reported as such rather than
quietly serving degraded retrieval.
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any

from platform_core.corpus import gaps
from platform_core.identity.principal import RequestContext
from platform_core.ports.llm import ChatRequest
from platform_core.scaling import sessions
from workloads.graphrag import engine
from workloads.graphrag.retriever import PgVectorRetriever

logger = logging.getLogger("platform.workloads.graphrag")

WORKLOAD = "graphrag"
DEFAULT_SCHEMA_DOMAIN = "manufacturing"
MAX_CONTEXT_CHUNKS = 5
MAX_HISTORY_TURNS = 6

SYSTEM_PROMPT = """You answer questions about industrial equipment using ONLY the \
provided sources.

Rules:
- Every factual claim must come from a source. Cite it inline as [c_xxxx].
- If the sources do not contain the answer, say so plainly. Do not infer or \
fill gaps from prior knowledge.
- Be concise. Prefer the specific number or procedure over a paraphrase."""


@dataclass(frozen=True, slots=True)
class GraphStats:
    nodes: int
    edges: int
    schema_domain: str
    documents: int
    build_ms: float
    corpus_fingerprint: str

    @property
    def edgeless(self) -> bool:
        """An edgeless graph is queried like a populated one — silently worse."""
        return self.nodes > 0 and self.edges == 0


@dataclass(frozen=True, slots=True)
class GraphAnswer:
    answer: str
    sources: list[dict[str, Any]]
    session_id: str
    grounded: bool
    graph: GraphStats
    retrieval: dict[str, Any]
    cost_usd: float
    input_tokens: int
    output_tokens: int
    latency_ms: float


# (tenant_id, collection) -> (fingerprint, graph, retriever, stats)
_cache: dict[tuple[str, str], tuple[str, Any, Any, GraphStats]] = {}
_lock = threading.RLock()


def _fingerprint(documents: list[dict]) -> str:
    """Corpus identity. A changed corpus must rebuild the graph.

    Content-derived rather than a row count: two documents swapped for two
    others would leave a count-based check believing nothing changed.
    """
    digest = hashlib.sha256()
    for doc in documents:
        digest.update(doc["chunk_id"].encode())
    return digest.hexdigest()[:16]


def build_graph(ctx: RequestContext, collection: str, llm, *,
                schema_domain: str = DEFAULT_SCHEMA_DOMAIN, force: bool = False):
    """Build (or reuse) this tenant's knowledge graph over a collection."""
    engine.install()
    engine.set_context(llm, ctx)

    key = (str(ctx.tenant.id), collection)
    retriever = PgVectorRetriever(ctx, collection, llm)
    documents = retriever.load_documents()
    fingerprint = _fingerprint(documents)

    with _lock:
        cached = _cache.get(key)
        if cached and cached[0] == fingerprint and not force:
            _, graph, cached_retriever, stats = cached
            return graph, cached_retriever, documents, stats

    if not documents:
        stats = GraphStats(nodes=0, edges=0, schema_domain=schema_domain,
                           documents=0, build_ms=0.0, corpus_fingerprint=fingerprint)
        return None, retriever, documents, stats

    from core.knowledge_graph import KnowledgeGraph

    from workloads.graphrag import artifacts as ga
    from workloads.onboarding import store as onboarding_store

    # Published onboarding artifacts, if this tenant has any for the domain.
    # Without them `CachedRelationExtractor` is never constructed and the graph
    # builds with zero edges — answering exactly like a populated one.
    #
    # Materialised to a stable per-tenant directory rather than a temp one that
    # is deleted on exit: the extractors hold the cache path and the graph is
    # cached in-process afterwards, so a root removed at the end of this
    # function would leave a live graph pointing at a deleted directory.
    artifact_root = ga.tenant_root(ctx, schema_domain)
    materialized = {}
    try:
        stored = onboarding_store.published_artifacts(ctx, schema_domain)
        if stored:
            ga.install()
            materialized = ga.materialize(artifact_root, schema_domain, stored)
    except Exception:
        # A failure to load artifacts must not fail the query — it degrades to
        # the edgeless behaviour that was the status quo. But it is logged
        # loudly, because "no edges" and "artifacts unreadable" look identical
        # from the outside and only one of them is expected.
        logger.exception(
            "could not materialise onboarding artifacts for %s/%s — building "
            "without them; the graph will have no edges",
            ctx.tenant.slug, schema_domain,
        )

    started = time.monotonic()
    with ga.using(artifact_root if materialized else None):
        graph = KnowledgeGraph(domain=schema_domain)
        graph.build_from_documents(documents)      # deterministic; save() suppressed
    build_ms = (time.monotonic() - started) * 1000

    stats = GraphStats(
        nodes=graph.graph.number_of_nodes(),
        edges=graph.graph.number_of_edges(),
        schema_domain=schema_domain,
        documents=len(documents),
        build_ms=round(build_ms, 1),
        corpus_fingerprint=fingerprint,
    )

    if stats.edgeless:
        # Loud, because the alternative is serving degraded retrieval that looks
        # identical to working retrieval.
        logger.warning(
            "tenant %s collection %s: graph has %d nodes and NO edges — entity "
            "matching will work, traversal will not. Schema %r may not fit this "
            "corpus.", ctx.tenant.slug, collection, stats.nodes, schema_domain,
        )

    with _lock:
        _cache[key] = (fingerprint, graph, retriever, stats)

    logger.info("built graph for %s/%s: %d nodes, %d edges, %d docs in %.0fms",
                ctx.tenant.slug, collection, stats.nodes, stats.edges,
                stats.documents, build_ms)
    return graph, retriever, documents, stats


def answer(
    ctx: RequestContext,
    *,
    question: str,
    collection: str,
    llm,
    session_id: str | None = None,
    schema_domain: str = DEFAULT_SCHEMA_DOMAIN,
    model: str | None = None,
) -> GraphAnswer:
    """One GraphRAG turn: build/reuse the graph, retrieve hybrid, answer."""
    from platform_core.settings import get_settings

    settings = get_settings()
    model = model or settings.llm_model_cheap
    started = time.monotonic()

    session = (sessions.load(ctx, session_id) if session_id
               else sessions.create(ctx, workload=WORKLOAD))
    history = (session.state.get("turns") or [])[-MAX_HISTORY_TURNS:]

    graph, retriever, documents, stats = build_graph(
        ctx, collection, llm, schema_domain=schema_domain
    )

    if not documents:
        message = ("I don't have any indexed sources for that collection.")
        gaps.record(ctx, collection=collection, question=question, mode="graph")
        sessions.append_turn(ctx, session.id, {"role": "user", "content": question})
        sessions.append_turn(
            ctx, session.id,
            {"role": "assistant", "content": message, "grounded": False,
             "collection": collection},
        )
        return GraphAnswer(
            answer=message, sources=[], session_id=session.id, grounded=False,
            graph=stats, retrieval={"mode": "none"}, cost_usd=0.0,
            input_tokens=0, output_tokens=0,
            latency_ms=(time.monotonic() - started) * 1000,
        )

    engine.set_context(llm, ctx)
    sources, retrieval_detail = _hybrid_retrieve(
        ctx, question, graph, retriever, documents, llm
    )

    if not sources:
        message = ("Nothing in the indexed sources matches that question.")
        gaps.record(ctx, collection=collection, question=question, mode="graph")
        sessions.append_turn(ctx, session.id, {"role": "user", "content": question})
        sessions.append_turn(
            ctx, session.id,
            {"role": "assistant", "content": message, "grounded": False,
             "collection": collection},
        )
        return GraphAnswer(
            answer=message, sources=[], session_id=session.id, grounded=False,
            graph=stats, retrieval=retrieval_detail, cost_usd=0.0,
            input_tokens=0, output_tokens=0,
            latency_ms=(time.monotonic() - started) * 1000,
        )

    context_block = "\n\n".join(f"[{s['chunk_id']}] {s['text']}" for s in sources)
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        *[{"role": t["role"], "content": t["content"]} for t in history],
        {"role": "user", "content": f"Sources:\n{context_block}\n\nQuestion: {question}"},
    ]

    response = llm.chat(
        ctx,
        ChatRequest(model=model, messages=messages, temperature=0, max_tokens=400,
                    cacheable=not history),
    )

    sessions.append_turn(ctx, session.id, {"role": "user", "content": question})
    sessions.append_turn(
        ctx, session.id,
        {"role": "assistant", "content": response.content, "grounded": True,
         "collection": collection,
         "retrieved": [s["chunk_id"] for s in sources]},
    )

    return GraphAnswer(
        answer=response.content, sources=sources, session_id=session.id,
        grounded=True, graph=stats, retrieval=retrieval_detail,
        cost_usd=response.cost_usd,
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
        latency_ms=(time.monotonic() - started) * 1000,
    )


def _hybrid_retrieve(ctx, question, graph, retriever, documents, llm):
    """Dense + lexical + graph, fused, with every contributor recorded.

    The per-signal attribution is not decoration. When a GraphRAG answer is
    wrong, the first question is always *which retriever put that chunk in
    context* — and without recording it at fusion time the answer is
    unrecoverable afterwards.
    """
    by_id = {doc["chunk_id"]: doc for doc in documents}
    scores: dict[str, float] = {}
    contributors: dict[str, list[str]] = {}

    def contribute(chunk_id: str, weight: float, signal: str) -> None:
        if chunk_id not in by_id:
            return
        scores[chunk_id] = scores.get(chunk_id, 0.0) + weight
        contributors.setdefault(chunk_id, []).append(signal)

    dense = retriever.retrieve(question, top_k=MAX_CONTEXT_CHUNKS * 2)
    for rank, hit in enumerate(dense):
        contribute(hit.chunk_id, 1.0 / (rank + 1), "dense")

    sparse = retriever.retrieve_sparse(question, top_k=MAX_CONTEXT_CHUNKS * 2)
    for rank, hit in enumerate(sparse):
        contribute(hit.chunk_id, 0.7 / (rank + 1), "lexical")

    graph_hits: list[str] = []
    entities: list[str] = []
    if graph is not None and graph.graph.number_of_nodes():
        try:
            from core.retrieval.graph_retriever import GraphRetriever

            gr = GraphRetriever(graph)
            entities = sorted(graph._match_query_to_entities(question))
            for rank, item in enumerate(gr.retrieve_by_entity(question, top_k=MAX_CONTEXT_CHUNKS * 2)):
                chunk_id = item.get("chunk_id") if isinstance(item, dict) else item
                if chunk_id:
                    contribute(str(chunk_id), 0.8 / (rank + 1), "graph")
                    graph_hits.append(str(chunk_id))
        except Exception:
            # Graph traversal failing must not take the answer down — dense and
            # lexical already have candidates.
            logger.exception("graph traversal failed — continuing without it")

    ordered = sorted(scores.items(), key=lambda kv: -kv[1])[:MAX_CONTEXT_CHUNKS]
    sources = [
        {
            "chunk_id": chunk_id,
            "text": by_id[chunk_id]["text"],
            "score": round(score, 4),
            "signals": sorted(set(contributors[chunk_id])),
            "source": by_id[chunk_id].get("source", "unknown"),
        }
        for chunk_id, score in ordered
    ]

    detail = {
        "mode": "hybrid",
        "dense_hits": len(dense),
        "lexical_hits": len(sparse),
        "graph_hits": len(set(graph_hits)),
        "entities_matched": entities[:10],
        "fused_candidates": len(scores),
    }
    return sources, detail


def invalidate(ctx: RequestContext | None = None) -> int:
    """Drop cached graphs. All of them, or just this tenant's."""
    with _lock:
        if ctx is None:
            count = len(_cache)
            _cache.clear()
            return count
        keys = [k for k in _cache if k[0] == str(ctx.tenant.id)]
        for key in keys:
            _cache.pop(key, None)
        return len(keys)
