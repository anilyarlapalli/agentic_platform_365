"""Retrieval-augmented chat, assembled from platform parts rather than beside them.

Nothing here is novel — that is the point. Retrieval is a tenant-scoped pgvector
query, the model call goes through the one instrumented chain, conversation state
is a row, and the budget refuses before dispatch. The workload contributes the
prompt and the citation discipline; every guarantee it inherits.

Two decisions worth stating because they are where RAG usually goes wrong:

**Citations are canonical ids.** The answer references ``c_<sha1:16>`` values that
are stable across rebuilds, and the response carries the retrieved chunks so a
caller can show sources. The alternative — a positional index into whatever list
happened to be loaded — is the failure that made retrieval recall read 0.0 in the
system this platform was derived from while retrieval worked perfectly.

**Grounding is checked, not assumed.** If retrieval returns nothing, the workload
answers that it does not know rather than letting the model fill the gap. An
ungrounded answer that looks confident is worse than a refusal, because nothing
downstream can tell them apart.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text

from platform_core.corpus import builds, gaps
from platform_core.db.engine import tenant_session
from platform_core.identity.principal import RequestContext
from platform_core.ports.llm import ChatRequest
from platform_core.scaling import sessions

logger = logging.getLogger("platform.workloads.chat")

WORKLOAD = "chat"
MAX_CONTEXT_CHUNKS = 5
MAX_HISTORY_TURNS = 6

SYSTEM_PROMPT = """You answer questions about industrial equipment using ONLY the \
provided sources.

Rules:
- Every factual claim must come from a source. Cite it inline as [c_xxxx].
- If the sources do not contain the answer, say so plainly. Do not infer, \
generalise, or fill gaps from prior knowledge.
- Be concise. Prefer the specific number or procedure over a paraphrase."""


@dataclass(frozen=True, slots=True)
class Source:
    canonical_id: str
    text: str
    distance: float
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ChatAnswer:
    answer: str
    sources: list[Source]
    session_id: str
    grounded: bool
    cache_hit: bool
    cost_usd: float
    input_tokens: int
    output_tokens: int
    latency_ms: float


def retrieve(ctx: RequestContext, collection: str, query_embedding: list[float],
             *, top_k: int = MAX_CONTEXT_CHUNKS) -> list[Source]:
    """Nearest neighbours within the caller's tenant.

    No tenant predicate in the SQL — the session carries it and row-level
    security applies underneath. A bug here returns nothing rather than someone
    else's documents.

    Scoped to the collection's **live build**. Without that predicate a rebuild
    in progress would have its half-written chunks answering queries alongside
    the current corpus, mixing two versions of a document in one answer.
    """
    with tenant_session(ctx.tenant) as s:
        build_version = builds.live_version_or_none(ctx, collection, session=s)
        if build_version is None:
            # Never indexed. An empty result is the honest answer; the caller
            # reports the turn as ungrounded rather than inventing one.
            return []

        rows = s.execute(
            text(
                "SELECT canonical_id, text, meta, "
                "  embedding <=> CAST(:probe AS vector) AS distance "
                "FROM chunk "
                "WHERE collection = :c AND build_version = :v AND embedding IS NOT NULL "
                "ORDER BY embedding <=> CAST(:probe AS vector) "
                "LIMIT :k"
            ),
            {"probe": str(query_embedding), "c": collection, "k": top_k,
             "v": build_version},
        ).all()

    return [
        Source(
            canonical_id=r.canonical_id, text=r.text,
            distance=float(r.distance), metadata=r.meta or {},
        )
        for r in rows
    ]


def answer(
    ctx: RequestContext,
    *,
    question: str,
    collection: str,
    llm,
    session_id: str | None = None,
    model: str | None = None,
) -> ChatAnswer:
    """One chat turn: retrieve, ground, answer, persist.

    ``ctx`` must carry ``task="chat"`` — the ledger's per-task policy reads it,
    and chat is the path that fails *open* when the ledger is unreadable. A
    waiting user outweighs one unmetered call; a corpus rebuild does not.
    """
    from platform_core.settings import get_settings

    settings = get_settings()
    model = model or settings.llm_model_cheap

    # Session first, so a failure to load is not charged for.
    if session_id:
        session = sessions.load(ctx, session_id)      # raises if not this principal's
    else:
        session = sessions.create(ctx, workload=WORKLOAD)

    history = (session.state.get("turns") or [])[-MAX_HISTORY_TURNS:]

    # Embedding goes through the same chain as chat: metered, charged, capped.
    # A budget that meters the answer but not the lookup bounds the cheap half.
    query_embedding = llm.embed(ctx, [question])[0]
    sources = retrieve(ctx, collection, query_embedding)

    if not sources:
        # Refuse rather than let the model answer from prior knowledge. An
        # ungrounded answer that reads as confident is indistinguishable
        # downstream from a grounded one.
        text_answer = (
            "I don't have any indexed sources for that. Nothing in this "
            "collection matches the question."
        )
        # A real question the corpus had nothing for: a backlog item, a
        # retrieval bug report and a future eval item in one. Recorded here
        # rather than mined from the session, which expires in twelve hours.
        gaps.record(ctx, collection=collection, question=question, mode="dense")
        sessions.append_turn(ctx, session.id, {"role": "user", "content": question})
        sessions.append_turn(
            ctx, session.id,
            {"role": "assistant", "content": text_answer, "grounded": False,
             "collection": collection},
        )
        return ChatAnswer(
            answer=text_answer, sources=[], session_id=session.id, grounded=False,
            cache_hit=False, cost_usd=0.0, input_tokens=0, output_tokens=0,
            latency_ms=0.0,
        )

    context_block = "\n\n".join(
        f"[{s.canonical_id}] {s.text}" for s in sources
    )
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        *[{"role": t["role"], "content": t["content"]} for t in history],
        {
            "role": "user",
            "content": f"Sources:\n{context_block}\n\nQuestion: {question}",
        },
    ]

    response = llm.chat(
        ctx,
        ChatRequest(
            model=model,
            messages=messages,
            temperature=0,
            max_tokens=400,
            # Identical question + identical retrieved context + identical
            # history is genuinely the same call. The cache key includes all of
            # it, and the tenant, so a hit cannot cross a boundary.
            cacheable=not history,
        ),
    )

    sessions.append_turn(ctx, session.id, {"role": "user", "content": question})
    # `grounded` and the collection are recorded on the turn so an answered
    # question can be mined from the live session window without matching the
    # refusal string, which would break the moment the wording changed.
    sessions.append_turn(
        ctx, session.id,
        {"role": "assistant", "content": response.content, "grounded": True,
         "collection": collection,
         "retrieved": [s.canonical_id for s in sources]},
    )

    return ChatAnswer(
        answer=response.content,
        sources=sources,
        session_id=session.id,
        grounded=True,
        cache_hit=response.cache_hit,
        cost_usd=response.cost_usd,
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
        latency_ms=response.latency_ms,
    )
