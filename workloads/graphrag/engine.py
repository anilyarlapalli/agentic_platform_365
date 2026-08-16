"""Wire the GraphRAG engine to this platform. Import before anything touches it.

The engine is borrowed, not forked: nothing here writes to its tree. It is
adapted through the seams it already has, which is the approach
``graphrag-azure/azure_deploy_graphrag/bootstrap.py`` established and proved.
That module's ordering constraints are load-bearing and are reproduced here
because they are properties of the engine, not of Azure:

1. **The embedding stub must be registered before anything imports
   ``doc_pipeline.embeddings``**, which binds ``sentence_transformers`` at module
   level. Register it afterwards and the real symbol is already bound — or the
   import already failed on a missing torch.
2. **Engine environment must be set before the first ``import config``**, because
   that module snapshots ``os.environ`` into constants at import time. Setting
   ``USE_RERANKER`` later has no effect.
3. ``CrossEncoder`` is deliberately **not** defined on the stub, so the
   reranker's guarded import raises and it degrades to passthrough — which is
   what a reranker-free deployment wants anyway.

## What is different here

The Azure wrapper pointed the engine at Azure OpenAI. This points it at the
platform's **instrumented** client, so the engine's own ``encode()`` calls pass
through identity → budget → cache → retry → meter → trace like everything else.
Borrowed code does not get an unmetered side door: a corpus rebuild inside the
engine is charged to the tenant that asked for it, and refused if that tenant is
over its ceiling.

The other difference is that nothing is written to disk. The engine saves a
knowledge graph to a per-domain path in its own tree; here graphs are per
*tenant* and held in memory, because two tenants sharing one file would
overwrite each other's graph and a shared path would silently cross the
isolation boundary.
"""

from __future__ import annotations

import logging
import os
import sys
import types
from pathlib import Path
from typing import Any

import numpy as np

from platform_core.settings import get_settings

logger = logging.getLogger("platform.workloads.graphrag.engine")

class GraphRAGDisabled(RuntimeError):
    """The borrowed engine is not available in this deployment.

    A refusal, not a failure: the platform is configured without GraphRAG, so
    callers should report that plainly rather than surface an import error.
    """


_installed = False
_report: dict[str, Any] = {}
_prepared_root: Path | None = None

# The instrumented client the stub encoder routes through. Set by install();
# the encoder needs a RequestContext per call, supplied via set_context below.
_llm = None
_ctx = None


def set_context(llm, ctx) -> None:
    """Bind the client and tenant context the engine's encoder will use.

    The engine calls ``encode()`` from deep inside retrieval with no way to pass
    a context, so it is bound at the boundary instead. Set on every request —
    never left over from a previous one, which is the mistake that makes spend
    attributable to whoever ran last.
    """
    global _llm, _ctx
    _llm, _ctx = llm, ctx


class PlatformEncoder:
    """``SentenceTransformer``-shaped, backed by the platform's LLM chain.

    Every method the engine actually calls, and nothing else. The engine
    constructs this by name (``SentenceTransformer(model_name)``) from several
    places, including ``core.kg.type_classifier``, so the constructor has to
    tolerate arbitrary arguments.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._dim = get_settings().embedding_dimensions

    def get_sentence_embedding_dimension(self) -> int:
        return self._dim

    @property
    def max_seq_length(self) -> int:
        return 8191

    def encode(self, texts, batch_size: int = 32, show_progress_bar: bool = False,
               convert_to_numpy: bool = True, normalize_embeddings: bool = False,
               **kwargs: Any):
        """Embed through the platform chain, so the engine cannot spend unmetered."""
        single = isinstance(texts, str)
        items = [texts] if single else list(texts)
        if not items:
            return np.zeros((0, self._dim), dtype=np.float32)

        if _llm is None or _ctx is None:
            # Refusing beats returning zeros. A zero vector is a *plausible*
            # embedding — retrieval would return arbitrary neighbours and look
            # like it worked, which is far worse than an error at the boundary.
            raise RuntimeError(
                "the GraphRAG engine called encode() with no platform context "
                "bound; call engine.set_context(llm, ctx) first"
            )

        vectors = _llm.embed(_ctx, items)
        array = np.asarray(vectors, dtype=np.float32)
        if normalize_embeddings:
            norms = np.linalg.norm(array, axis=1, keepdims=True)
            array = array / np.clip(norms, 1e-12, None)
        return array[0] if single else array


def prepare_imports(engine_root: Path | None = None) -> Path:
    """Prepare the external engine namespace without loading its full runtime.

    Schema review needs only ``core.kg.schema``. Loading ``KnowledgeGraph`` for
    that path also imports the graph, ranking, and document stacks, turning a
    pure YAML check into an accidental dependency on the entire engine.

    This is the one place the engine tree is resolved, so it is also where
    ``graphrag_enabled`` is enforced. Every path that reaches the borrowed
    engine — graph chat, onboarding drafts, and schema validation — arrives
    here first.
    """
    global _prepared_root
    settings = get_settings()
    if not settings.graphrag_enabled:
        # Previously the flag guarded only a startup existence check, so a
        # deployment with GRAPHRAG_ENABLED=false still imported the engine on
        # the first graph request and failed at request time with whatever the
        # missing tree happened to raise. Setting the flag false removed the
        # warning rather than the feature.
        raise GraphRAGDisabled(
            "GraphRAG is not enabled in this deployment. Set GRAPHRAG_ENABLED=true "
            "and GRAPHRAG_ENGINE_ROOT to the canonical engine tree to use graph "
            "mode, onboarding drafts, or schema validation."
        )
    if engine_root is None and settings.graphrag_engine_root is None:
        raise GraphRAGDisabled(
            "GRAPHRAG_ENABLED=true but GRAPHRAG_ENGINE_ROOT is unset. The engine "
            "tree has no default: it lives outside this repository and its "
            "location is per-machine."
        )
    root = Path(engine_root or settings.graphrag_engine_root).resolve()
    if not root.exists():
        raise FileNotFoundError(f"GraphRAG engine tree not found at {root}")
    if _prepared_root is not None:
        if _prepared_root != root:
            raise RuntimeError(
                f"GraphRAG imports are already bound to {_prepared_root}; "
                f"refusing to switch to {root} in the same process"
            )
        return root

    # ── 0. The embedding stub, before any engine import ───────────────────
    if "sentence_transformers" in sys.modules:
        raise RuntimeError(
            "sentence_transformers is already imported — the stub is too late to "
            "take effect. Import workloads.graphrag.engine before anything that "
            "touches doc_pipeline.embeddings."
        )
    if "config" in sys.modules:
        raise RuntimeError(
            "the GraphRAG config module is already imported, so its environment "
            "snapshot cannot be configured safely"
        )
    stub = types.ModuleType("sentence_transformers")
    stub.SentenceTransformer = PlatformEncoder
    # CrossEncoder deliberately absent: the reranker's guarded import then fails
    # and it degrades to passthrough, which is the intended configuration.
    sys.modules["sentence_transformers"] = stub

    # ── 1. Engine environment, before the first `import config` ───────────
    os.environ.setdefault("USE_RERANKER", "false")
    os.environ.setdefault("QDRANT_URL", "")          # never fall back to embedded Qdrant
    os.environ.setdefault("USE_HITL", "false")
    os.environ.setdefault("EMBEDDING_MODEL", settings.embedding_model)
    # The engine writes caches relative to its tree; point them at ours so the
    # borrowed tree stays read-only.
    cache_root = Path(settings.cassette_dir).parent / "graphrag"
    cache_root.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("DATA_DIR", str(cache_root))

    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

    # Config snapshots environment at import time, so it belongs to the shared
    # preparation boundary even when the caller needs only the schema module.
    import config  # noqa: F401  — snapshots env; must come after step 1

    _prepared_root = root
    return root


def install(engine_root: Path | None = None) -> dict[str, Any]:
    """Register the full engine seams. Idempotent; returns a report for /health."""
    global _installed, _report
    if _installed:
        return _report

    settings = get_settings()
    root = prepare_imports(engine_root)

    # ── 2. Import the engine and suppress its disk writes ─────────────────
    from core.knowledge_graph import KnowledgeGraph

    # A knowledge graph here belongs to a tenant, not to a domain. The engine
    # saves to a per-domain path in its own tree, so two tenants would overwrite
    # each other and a shared file would cross the isolation boundary. Graphs
    # are held in memory per tenant instead.
    KnowledgeGraph.save = lambda self, path=None: None      # type: ignore[method-assign]

    _report = {
        "engine_root": str(root),
        "schemas": sorted(p.stem for p in (root / "schemas").glob("*.yaml")
                          if not p.stem.endswith(".permissions")),
        "encoder": "platform-instrumented",
        "reranker": "passthrough (CrossEncoder undefined)",
        "kg_persistence": "in-memory, per tenant",
        "embedding_model": settings.embedding_model,
        "embedding_dimensions": settings.embedding_dimensions,
    }
    _installed = True
    logger.info("graphrag engine wired: %s", _report)
    return _report


def report() -> dict[str, Any]:
    return dict(_report)


_chat_patched = False


def install_metered_chat() -> None:
    """Route the engine's chat completions through the platform's client.

    ``install()`` covers ``encode()`` — the only engine LLM path retrieval uses.
    Onboarding is different: the orchestrator makes hundreds of *chat* calls
    (one per chunk for extraction, then eight synthesis steps), and those go
    through ``core.llm_client.call_llm`` straight to OpenAI. Left alone they are
    unmetered, unattributed, and — the part that matters — **unbudgeted**: the
    single most expensive path in the platform would be the one path a tenant
    ceiling could not stop.

    ``call_llm`` is the right seam because it is the only one. ``llm_router.call``
    delegates to it, and every one of the 26 engine modules that make a chat call
    reaches it. Patching here means a budget refusal surfaces as
    ``BudgetExceededError`` from inside the orchestrator and fails the draft,
    which is the correct outcome for a background path: fail closed.
    """
    global _chat_patched
    if _chat_patched:
        return

    import core.llm_client as llm_client

    from platform_core.ports.llm import ChatRequest

    _orig_call_llm = llm_client.call_llm

    def call_llm(system_prompt, user_prompt, temperature=0.3, max_tokens=1500,
                 model=None, response_format=None):
        if _llm is None or _ctx is None:
            # Better a loud failure than a silent unmetered call. Reaching here
            # means a caller invoked the engine without binding a context, and
            # the correct response is to refuse rather than quietly spend.
            raise RuntimeError(
                "the GraphRAG engine called call_llm() with no platform context "
                "bound; call engine.set_context(llm, ctx) first"
            )
        response = _llm.chat(
            _ctx,
            ChatRequest(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                # The engine resolves its own model per task — the cheap tier
                # for per-chunk extraction, a stronger one for synthesis. That
                # choice is deliberate and load-bearing (running hundreds of
                # chunks on the expensive model rate-limits hard), so it is
                # passed through rather than overridden. The fallback only
                # applies when the engine asked for no model at all.
                model=model or get_settings().llm_model_cheap,
                temperature=temperature,
                max_tokens=max_tokens,
                response_format=response_format,
                # Extraction runs the same prompt shape over hundreds of chunks
                # with different content, so the cache would miss on every call
                # while still paying to look. Left off deliberately.
                cacheable=False,
            ),
        )
        return response.content

    llm_client.call_llm = call_llm

    # `llm_router.call` imports `call_llm` lazily inside the function body, so
    # rebinding the module attribute is observed. Modules that did a top-level
    # `from core.llm_client import call_llm` captured the original, so rebind
    # those too — extraction.py is one of them, and it is the hot path.
    for name, module in list(sys.modules.items()):
        if not name.startswith("core."):
            continue
        if getattr(module, "call_llm", None) is _orig_call_llm:
            module.call_llm = call_llm

    _chat_patched = True
    logger.info("engine chat routed through the platform's instrumented client")
