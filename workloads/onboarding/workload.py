"""Draft a schema for a domain, and produce the artifacts that give it edges.

This is the missing half of GraphRAG here. Entity extraction is deterministic
and needs only the schema; relation extraction needs a *learned* instance table
and predicate map, and without them every graph builds with zero edges while
answering exactly like a populated one.

The engine's ``analyze()`` produces all of it in one call: per-chunk extraction
(cached to disk, which is non-optional — the bootstrap step reads those files),
aggregation, schema synthesis, then ``bootstrap_artifacts``. What this workload
adds is the platform's half:

* the run is queued through the outbox, so it is leased, retried and swept like
  any other work rather than living in a request handler;
* engine chat is routed through the instrumented client, so hundreds of
  extraction calls are attributed and **budget-checked** — onboarding is the
  most expensive path in the platform and must not be the one path a ceiling
  cannot stop;
* artifacts are written to Postgres as they are produced, not held until a human
  approves. The Azure build held them on a scale-to-zero replica and lost 543
  instances to a 300s cooldown, publishing a schema-only bundle reported as
  success.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from platform_core.correctness.cancellation import cancellation_point
from platform_core.identity.principal import RequestContext
from workloads.onboarding import store

logger = logging.getLogger("platform.workloads.onboarding")

WORKLOAD = "onboarding"

# How many corpus chunks to sample for drafting. Every chunk costs one
# extraction call, so this bounds the spend of a single draft. The engine wants
# breadth over depth here — it is characterising a corpus, not indexing it.
DEFAULT_SAMPLE = 120

# How many corpus-grounded questions to propose for review. Each becomes a
# candidate eval item, and a reviewer has to read every one — so this is bounded
# by human attention rather than by cost. Roughly four extra LLM calls on top of
# a draft that already makes one per sampled chunk.
DEFAULT_QUERIES = 25


def _chunk_views(documents: list[dict[str, Any]]) -> list[Any]:
    """Adapt corpus documents to what the engine's query generator expects.

    The only line that matters is ``chunk_id``. ``_chunk_identifier`` prefers an
    explicit one and falls back to synthesising ``<file>#p<page>#<sha1:7>``, and
    its own docstring says why that matters: without a canonical id
    "``CandidateQuery.evidence_chunk_ids`` … live in a private namespace and every
    recall / citation metric computed against them reads 0.0."

    The reference deployment hits exactly that. Its onboarding drafter chunks the
    corpus separately from ingestion, so it cannot supply one, and its eval sets
    cite ids the retriever never emits. Here ``load_documents`` returns the
    canonical ``c_<sha1:16>`` from the live build — the same id the retriever
    returns and the same one ``datasets.build_dataset`` validates — so carrying it
    through is the whole fix.
    """
    from types import SimpleNamespace

    views = []
    for doc in documents:
        meta = doc.get("metadata") or {}
        views.append(
            SimpleNamespace(
                chunk_id=doc["chunk_id"],
                content=doc.get("text") or "",
                source_file=doc.get("source") or meta.get("source") or "",
                # Absent for markdown; the sampler filters falsy pages out before
                # deciding what is front matter, so 0 disables that skip rather
                # than excluding everything.
                page=int(meta.get("page") or 0),
            )
        )
    return views


def _propose_queries(
    documents: list[dict[str, Any]], *, n_queries: int
) -> dict[str, Any]:
    """Corpus-grounded questions for a reviewer to keep, edit or drop.

    Never raises. A draft costs a full corpus of extraction calls, and losing it
    because a cheap follow-on step failed would be a poor trade — the queries can
    be regenerated, the draft cannot be re-obtained for free.
    """
    if not documents:
        # Not an error and not worth importing the engine for. An empty corpus
        # already failed the draft upstream; here it simply proposes nothing.
        return {"queries": []}

    try:
        # Imported inside the guard on purpose: an engine that will not import is
        # precisely the "cheap follow-on step failed" case this promises to
        # survive, and it was outside it once.
        from core.onboarding_steps.queries import propose_candidate_queries

        proposed = propose_candidate_queries(
            SimpleNamespaceCtx(chunks=_chunk_views(documents)), n_queries=n_queries
        )
    except Exception as exc:
        logger.exception("candidate query generation failed")
        return {"queries": [], "error": f"{type(exc).__name__}: {exc}"}

    queries = [
        {
            "id": f"q{index}",
            "text": q.text,
            # Canonical ids, so a seeded eval item cites chunks the retriever
            # actually returns. Filtered to those the corpus really contains: the
            # generator drops queries grounded in invented ids, but an id that
            # slipped through would fail `build_dataset` at seed time with a
            # message about the eval set rather than about the draft.
            "evidence_chunk_ids": [
                cid for cid in q.evidence_chunk_ids
                if cid in {d["chunk_id"] for d in documents}
            ],
            "source_file": q.source_file,
            "page": q.page,
            "entity_hints": list(q.suggested_entity_hints),
            # Nothing is approved by default. A question nobody read is not a
            # question the domain must answer, and defaulting to approved would
            # make the review step decorative.
            "approved": False,
            "edited": False,
        }
        for index, q in enumerate(proposed)
    ]
    return {"queries": queries}


class SimpleNamespaceCtx:
    """The minimal shape ``propose_candidate_queries`` reads: ``.chunks``."""

    def __init__(self, chunks: list[Any]) -> None:
        self.chunks = chunks


def run(ctx: RequestContext, payload: dict[str, Any]) -> dict[str, Any]:
    """Execute one drafting run. Safe to call again on the same ``ctx.run_id``."""
    cancellation_point(ctx)
    session_id = uuid.UUID(str(payload["session_id"]))
    domain = str(payload["domain"]).strip().lower()
    collection = str(payload["collection"])
    sample = int(payload.get("sample", DEFAULT_SAMPLE))

    current = store.get(ctx, session_id)
    if current is None:
        # The session was deleted after the pointer was published. Nothing to
        # do and nothing to retry.
        logger.warning("onboarding session %s no longer exists", session_id)
        return {"outcome": "session_gone"}
    if current["status"] not in ("drafting",):
        # A duplicate delivery of a run whose draft already finished. Not an
        # error — the lease made execution exclusive, this makes it idempotent.
        logger.info("session %s is %s — nothing to draft", session_id, current["status"])
        return {"outcome": "already_done", "status": current["status"]}

    from platform_core.observability.llm import build_client
    from workloads.graphrag import artifacts, engine
    from workloads.graphrag.retriever import PgVectorRetriever

    llm = build_client()

    # Order matters and is load-bearing: install() registers the embedding stub
    # before any engine import, and both the artifact redirect and the chat
    # routing must be in place before the orchestrator makes its first call.
    engine.install()
    engine.set_context(llm, ctx)
    engine.install_metered_chat()
    artifacts.install()

    documents = PgVectorRetriever(ctx, collection, llm).load_documents()
    if not documents:
        store.set_status(
            ctx, session_id, "failed",
            error=f"Collection {collection!r} has no indexed documents. "
                  "A schema drafted from an empty corpus would describe nothing.",
        )
        return {"outcome": "empty_corpus"}

    texts = [d["text"] for d in documents][:sample]
    store.append_progress(ctx, session_id, {
        "step": "sample",
        "detail": f"{len(texts)} of {len(documents)} chunks from {collection}",
    })

    try:
        with artifacts.scratch() as root, artifacts.using(root):
            from core.onboarding_agent import analyze

            def on_progress(step: str, note: str, index: int, total: int) -> None:
                # Persisted per step rather than accumulated, so a draft that
                # dies mid-run still shows how far it got.
                cancellation_point(ctx)
                store.append_progress(ctx, session_id, {
                    "step": step, "detail": (note or "")[:2000],
                    "index": index, "total": total,
                })

            response = analyze(
                domain_id=domain,
                docs=texts,
                on_progress=on_progress,
                # The reranker is passthrough here (CrossEncoder is deliberately
                # undefined), so asking for one would describe a capability the
                # deployment does not have.
                use_reranker=False,
            )

            produced = artifacts.capture(root, domain)

        # Written after capture but before the status flips to draft_ready, so a
        # session is never reviewable without the artifacts it claims to have.
        if response.yaml:
            store.put_artifact(ctx, session_id, "schema", "schema", {"yaml": response.yaml})
        if "instance_table" in produced:
            store.put_artifact(ctx, session_id, "instance_table", "instance_table",
                               produced["instance_table"])
        if "predicate_map" in produced:
            store.put_artifact(ctx, session_id, "predicate_map", "predicate_map",
                               produced["predicate_map"])
        for name, payload_json in (produced.get("extraction_cache") or {}).items():
            store.put_artifact(ctx, session_id, "extraction_cache", name, payload_json)

        instances = len((produced.get("instance_table") or {}).get("instances") or [])
        predicates = len((produced.get("predicate_map") or {}).get("predicate_map") or {})
        cache_files = len(produced.get("extraction_cache") or {})
        relations_available = bool(instances and predicates and cache_files)

        # The corpus this taxonomy was drafted from. Artifacts are derived state
        # with no invalidation edge back to their source — delete half the
        # documents and the instance table still lists entities from them, and
        # the graph will happily extract entities no chunk contains. Recording
        # the fingerprint is what lets that be *detected* later; it is
        # deliberately not auto-invalidated, because re-drafting spends real
        # budget and needs an approval.
        from platform_core.corpus import builds as corpus_builds

        live_build = corpus_builds.live_version_or_none(ctx, collection)
        corpus_fingerprint = (
            corpus_builds.fingerprint(ctx, collection, live_build)
            if live_build is not None else None
        )

        # Corpus-grounded questions, after the expensive work has been captured.
        # Run here rather than before `analyze` so a failure costs four calls
        # instead of the whole draft.
        proposed = _propose_queries(documents, n_queries=int(
            payload.get("n_queries") or DEFAULT_QUERIES
        ))
        if proposed["queries"]:
            store.put_artifact(
                ctx, session_id, "candidate_queries", "candidate_queries", proposed
            )

        stats = {
            "instances": instances,
            "predicates": predicates,
            "cache_files": cache_files,
            # Surfaced as a count for the same reason `relations_available` is:
            # the sampler skips chunks under 300 characters, so a corpus of short
            # chunks yields zero questions and returns an empty list. Zero must
            # read as "nothing could be proposed", never as "nothing was needed".
            "candidate_queries": len(proposed["queries"]),
            "candidate_queries_error": proposed.get("error"),
            "corpus_fingerprint": corpus_fingerprint,
            "corpus_build_version": live_build,
            # The single number that decides whether this domain gets edges.
            # Recorded so it is knowable at review time rather than discovered
            # as a zero edge count after publishing.
            "relations_available": relations_available,
            "chunks_sampled": len(texts),
            "entities_discovered": sum(
                len(v) for v in (response.discovered_entities or {}).values()
            ),
            "repair_attempts": response.repair_attempts,
            "validation": response.validation or {},
        }

        if not relations_available:
            logger.warning(
                "session %s drafted a schema-only bundle (instances=%d predicates=%d "
                "cache=%d) — publishing it will build entities and 0 edges",
                session_id, instances, predicates, cache_files,
            )

        store.set_status(ctx, session_id, "draft_ready", stats=stats)
        return {"outcome": "draft_ready", **stats}

    except Exception as exc:
        # Failure is recorded on the session, not just raised. A run that dies
        # leaving status='drafting' is indistinguishable from one still working,
        # which is precisely the ambiguity this table was shaped to remove.
        logger.exception("onboarding draft failed for session %s", session_id)
        store.set_status(ctx, session_id, "failed", error=f"{type(exc).__name__}: {exc}"[:2000])
        raise
