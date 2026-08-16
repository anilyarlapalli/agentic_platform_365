"""Onboarding artifacts: where the engine looks for them, and where they live.

The engine resolves three artifact locations from its own source position —
``Path(__file__).resolve().parents[N] / "data" / …``:

* ``data/instance_tables/<domain>.json`` via ``InstanceTable._path_for``
* ``data/predicate_maps/<domain>.json``, computed **inline** in
  ``KnowledgeGraph.__init__``
* ``data/extraction_cache/<domain>/`` via ``extraction._CACHE_DIR``

All three point into the engine tree, which is read-only here, and none of them
is tenant-aware — two tenants onboarding the same domain would write over each
other. So the paths are redirected rather than the tree modified.

## Why patching is the right seam

Every one of these is reached through a **deferred import** inside the function
that uses it, so rebinding the module attribute before the call is observed by
the engine without touching a line of its source. That is the same technique
``engine.py`` already uses for the embedding stub, and the same one
``graphrag-azure/bootstrap.py`` established. The alternative — copying the
engine tree per tenant — costs a full copy per build and still leaves the
process-wide constant shared.

## Concurrency

The redirect target is **thread-local**. FastAPI runs sync handlers in a
threadpool, so two tenants can build graphs at the same time in one process; a
module-global root would let one tenant's build read the other's instance table.
That failure is silent and produces a *plausible* graph, which is the worst
kind. A thread-local root means the patched functions resolve whatever the
calling thread is working on.
"""

from __future__ import annotations

import json
import logging
import shutil
import sys
import tempfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

logger = logging.getLogger("platform.workloads.graphrag.artifacts")

# The redirect target for the current thread. None means "no tenant artifacts" —
# the engine's own defaults are left alone, which is what an un-onboarded domain
# should see.
_local = threading.local()
_patched = False
_patch_lock = threading.Lock()


def _root() -> Path | None:
    return getattr(_local, "root", None)


def instance_table_path(root: Path, domain: str) -> Path:
    return root / "data" / "instance_tables" / f"{domain.strip().lower()}.json"


def predicate_map_path(root: Path, domain: str) -> Path:
    return root / "data" / "predicate_maps" / f"{domain.strip().lower()}.json"


def extraction_cache_dir(root: Path, domain: str) -> Path:
    return root / "data" / "extraction_cache" / domain.strip().lower()


def schema_yaml_path(root: Path, domain: str) -> Path:
    return root / "schemas" / f"{domain.strip().lower()}.yaml"


def install() -> None:
    """Patch the engine's artifact seams. Idempotent.

    Must run after ``engine.install()`` — these modules import engine config at
    import time, and importing them first would snapshot the wrong environment.
    """
    global _patched
    with _patch_lock:
        if _patched:
            return

        from core.kg import bootstrap as kg_bootstrap
        from core.kg import instance_table as kg_instance_table
        from core.kg import relation_bootstrap as kg_relation_bootstrap
        from core.onboarding_steps import extraction as kg_extraction

        # ── 1. instance table reads ──────────────────────────────────────
        # `_path_for` is a staticmethod taking (domain, repo_root=None), called
        # by `InstanceTable.load` when no explicit path is given.
        _orig_path_for = kg_instance_table.InstanceTable._path_for

        def _path_for(domain, repo_root=None):
            root = _root()
            if root is not None and repo_root is None:
                return instance_table_path(root, domain)
            return _orig_path_for(domain, repo_root)

        kg_instance_table.InstanceTable._path_for = staticmethod(_path_for)

        # ── 2. extraction cache ──────────────────────────────────────────
        # A module constant read by `resolve_cache_dir` at call time, so a
        # dynamic property is enough to make it follow the calling thread.
        # `_CACHE_DIR` is only ever used as `_CACHE_DIR / domain`, so returning
        # the per-thread base preserves the engine's own layout underneath.
        class _CacheDirProxy:
            """Stands in for a Path constant, resolving per thread on use."""

            def __truediv__(self, other):
                root = _root()
                base = (
                    root / "data" / "extraction_cache"
                    if root is not None
                    else _orig_cache_dir
                )
                return base / other

            def __fspath__(self):
                root = _root()
                base = (
                    root / "data" / "extraction_cache"
                    if root is not None
                    else _orig_cache_dir
                )
                return str(base)

            def __getattr__(self, name):
                root = _root()
                base = (
                    root / "data" / "extraction_cache"
                    if root is not None
                    else _orig_cache_dir
                )
                return getattr(base, name)

        _orig_cache_dir = kg_extraction._CACHE_DIR
        kg_extraction._CACHE_DIR = _CacheDirProxy()

        # ── 3. predicate map reads ───────────────────────────────────────
        # The caller computes this path inline against the engine root, so the
        # argument cannot be intercepted upstream — the loader itself has to
        # ignore it and resolve from the thread's root instead.
        _orig_load_predicate_map = kg_relation_bootstrap.load_predicate_map

        def _load_predicate_map(path, *args, **kwargs):
            root = _root()
            if root is not None:
                domain = Path(path).stem
                redirected = predicate_map_path(root, domain)
                return _orig_load_predicate_map(redirected, *args, **kwargs)
            return _orig_load_predicate_map(path, *args, **kwargs)

        kg_relation_bootstrap.load_predicate_map = _load_predicate_map

        # ── 4. artifact writes ───────────────────────────────────────────
        # `_step_bootstrap_artifacts` computes `save_to` inline against the
        # engine root. Rewriting the kwarg here keeps every write inside the
        # thread's scratch root — without this, onboarding writes into the
        # read-only engine tree.
        def _redirect_save_to(orig, path_fn):
            def wrapper(*args, **kwargs):
                root = _root()
                save_to = kwargs.get("save_to")
                if root is not None and save_to is not None:
                    domain = kwargs.get("domain") or Path(save_to).stem
                    target = path_fn(root, domain)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    kwargs["save_to"] = target
                return orig(*args, **kwargs)

            return wrapper

        kg_bootstrap.bootstrap_from_llm_cache = _redirect_save_to(
            kg_bootstrap.bootstrap_from_llm_cache, instance_table_path
        )
        kg_relation_bootstrap.bootstrap_relations_from_llm_cache = _redirect_save_to(
            kg_relation_bootstrap.bootstrap_relations_from_llm_cache,
            predicate_map_path,
        )

        # ── 5. schema loading ────────────────────────────────────────────
        # `KnowledgeGraph.__init__` calls `load_default_schema(domain)`, which
        # resolves through `config.SCHEMA_PATHS` — a dict snapshotted from the
        # engine tree at import time. A domain onboarded here is not in it, so
        # the engine raises `unknown domain` and the newly approved taxonomy is
        # unusable by the thing it was drafted for.
        #
        # Patched rather than worked around by writing into the engine's
        # `schemas/` directory, which is read-only and shared across tenants.
        from core.kg import schema as kg_schema

        _orig_load_default_schema = kg_schema.load_default_schema

        def _load_default_schema(domain, *args, **kwargs):
            root = _root()
            if root is not None:
                path = schema_yaml_path(root, domain)
                if path.exists():
                    return kg_schema.load_schema(path)
            return _orig_load_default_schema(domain, *args, **kwargs)

        kg_schema.load_default_schema = _load_default_schema

        # `knowledge_graph` does a top-level `from core.kg.schema import
        # load_default_schema`, so it captured the original symbol. Rebind every
        # module that holds a reference, the same way the chat patch does.
        for name, module in list(sys.modules.items()):
            if not name.startswith("core."):
                continue
            if getattr(module, "load_default_schema", None) is _orig_load_default_schema:
                module.load_default_schema = _load_default_schema

        _patched = True
        logger.info("engine artifact paths redirected (schema, instance table, "
                    "predicate map, extraction cache, artifact writes)")


@contextmanager
def using(root: Path | None) -> Iterator[Path | None]:
    """Resolve engine artifact paths under ``root`` for this thread."""
    if root is not None:
        install()
    previous = getattr(_local, "root", None)
    _local.root = root
    try:
        yield root
    finally:
        _local.root = previous


def tenant_root(ctx, domain: str) -> Path:
    """A stable artifact root for one tenant's domain.

    Keyed by tenant id rather than slug: a slug can be renamed, and two tenants
    sharing a directory would mean one tenant's instance table shaping the
    other's graph — a silent cross-boundary failure that produces a *plausible*
    result, which is the hardest kind to notice.
    """
    from platform_core.settings import get_settings

    base = Path(get_settings().cassette_dir).parent / "graphrag" / "onboarding"
    return base / str(ctx.tenant.id) / domain.strip().lower()


@contextmanager
def scratch() -> Iterator[Path]:
    """A temporary artifact root, removed on exit."""
    path = Path(tempfile.mkdtemp(prefix="lp-onboarding-"))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


# ── materialise / capture ──────────────────────────────────────────────────

def materialize(root: Path, domain: str, artifacts: dict[str, object]) -> dict:
    """Write stored artifacts to disk in the layout the engine expects.

    ``artifacts`` is keyed by kind, as returned by the onboarding store:
    ``instance_table`` and ``predicate_map`` map to one JSON document each, and
    ``extraction_cache`` maps to ``{filename: payload}``.
    """
    domain = domain.strip().lower()
    written = {"schema": False, "instance_table": False,
               "predicate_map": False, "cache_files": 0}

    # The schema first: without it the engine refuses the domain outright, so a
    # bundle whose other artifacts landed would still be unusable.
    schema = artifacts.get("schema")
    if isinstance(schema, dict) and schema.get("yaml"):
        p = schema_yaml_path(root, domain)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(str(schema["yaml"]))
        written["schema"] = True

    table = artifacts.get("instance_table")
    if table:
        p = instance_table_path(root, domain)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(table))
        written["instance_table"] = True

    pmap = artifacts.get("predicate_map")
    if pmap:
        p = predicate_map_path(root, domain)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(pmap))
        written["predicate_map"] = True

    cache = artifacts.get("extraction_cache") or {}
    if isinstance(cache, dict) and cache:
        d = extraction_cache_dir(root, domain)
        d.mkdir(parents=True, exist_ok=True)
        for name, payload in cache.items():
            # Names come from the database, but they are used to build a path.
            # Take the basename so a stored `../` can never escape the cache
            # directory — defence in depth behind the store's own writes.
            (d / Path(str(name)).name).write_text(json.dumps(payload))
        written["cache_files"] = len(cache)

    # All three are required for edges. Saying so here means the caller can log
    # one honest line instead of discovering it as a zero edge count later.
    written["relations_available"] = bool(
        written["instance_table"] and written["predicate_map"] and written["cache_files"]
    )
    return written


def capture(root: Path, domain: str) -> dict[str, object]:
    """Read whatever the engine wrote under ``root`` back into plain data."""
    domain = domain.strip().lower()
    out: dict[str, object] = {}

    p = instance_table_path(root, domain)
    if p.exists():
        out["instance_table"] = json.loads(p.read_text())

    p = predicate_map_path(root, domain)
    if p.exists():
        out["predicate_map"] = json.loads(p.read_text())

    d = extraction_cache_dir(root, domain)
    if d.is_dir():
        cache: dict[str, object] = {}
        for f in sorted(d.glob("*.json")):
            try:
                cache[f.name] = json.loads(f.read_text())
            except json.JSONDecodeError:
                # A malformed cache entry becomes a missing edge rather than a
                # crash, but it must not pass silently — a partial cache is
                # worse than a small one because the gap is invisible.
                logger.warning("skipping unparseable extraction cache file %s", f.name)
        if cache:
            out["extraction_cache"] = cache

    return out


# ── schema validation ──────────────────────────────────────────────────────


class InvalidSchema(ValueError):
    """The YAML would not load as a schema, or would load as a useless one.

    Raised at the edge, before anything is stored. A schema that fails to parse
    is not a degraded schema: ``KnowledgeGraph.__init__`` falls back to
    ``load_default_schema``, which raises ``unknown domain`` for an onboarded
    name, which ``build_graph`` catches and turns into a graph with no
    artifacts — an edgeless graph that answers exactly like a populated one.
    Rejecting here is the only place the reviewer sees a message they can act on.
    """


def validate_schema_yaml(yaml_text: str) -> dict[str, object]:
    """Parse a taxonomy and report what it declares.

    The report exists because the reviewer editing this is usually editing it to
    fix the same thing: ``EdgeType.accepts`` is
    ``src in self.source and tgt in self.target``, so an edge type whose endpoint
    types are not declared as entity types can never admit an edge. That is
    checkable from the YAML alone, with no corpus and no LLM call, and it is the
    difference between an edit made blind and an edit made informed.

    Measured 2026-08-14: a drafted schema declaring three entity types for a
    twelve-type corpus admitted 1 of 306 candidate edges. Nothing in the platform
    said so before the graph was built and queried.
    """
    if not (yaml_text or "").strip():
        raise InvalidSchema("the taxonomy is empty")

    # Schema validation deliberately avoids loading the full graph/document
    # runtime. It needs the external namespace and its environment prepared,
    # but not NetworkX, parsers, rerankers, or vector-store clients.
    from workloads.graphrag import engine

    engine.prepare_imports()
    from core.kg import schema as kg_schema

    with tempfile.TemporaryDirectory(prefix="lp-schema-") as tmp:
        path = Path(tmp) / "candidate.yaml"
        path.write_text(yaml_text)
        try:
            schema = kg_schema.load_schema(path)
        except Exception as exc:
            raise InvalidSchema(f"{type(exc).__name__}: {exc}") from exc

    entity_types = sorted(schema.entity_types)
    edge_types = sorted(schema.edge_types)
    if not entity_types:
        raise InvalidSchema("a taxonomy with no entity types can never admit an edge")

    declared = set(entity_types)
    unreachable: list[dict[str, object]] = []
    for name, edge in schema.edge_types.items():
        missing = sorted(
            {*(edge.source or ()), *(edge.target or ())} - declared
        )
        if missing:
            unreachable.append({"edge_type": name, "undeclared_endpoint_types": missing})

    return {
        "domain": schema.domain,
        "version": schema.version,
        "entity_types": entity_types,
        "edge_types": edge_types,
        # Edge types that cannot fire as written. Reported, not rejected: a
        # taxonomy may legitimately be edited in two passes, and refusing the
        # first would make the second impossible to reach.
        "unreachable_edge_types": unreachable,
    }


def taxonomy_fit(entity_types: list[str], instance_table: dict | None) -> dict[str, object]:
    """Whether a taxonomy actually covers the entities drafted from the corpus.

    The signal nothing surfaced before. The engine's type classifier assigns
    ``raw:<free-form>`` to an entity it cannot place in a declared type, and
    ``EdgeType.accepts`` requires *both* endpoints to be declared types — so
    every relation touching a ``raw:`` entity is discarded when the graph is
    built. Silently, and long after the taxonomy was approved and published.

    All of this is knowable the moment the draft finishes: the instance table
    already records ``entity_type`` and the free-form ``raw_type`` it came from.
    Measured 2026-08-14 on the ``datacenter`` domain — 74 of 88 instances
    unclassified, and the published graph admitted 1 of 306 candidate edges.

    ``suggested_entity_types`` is the actionable half: the free-form types the
    classifier fell back to, most common first. Declaring them is the edit that
    turns the graph on, and it is an edit a reviewer can make in the box in front
    of them rather than by paying for a second draft.
    """
    instances = list((instance_table or {}).get("instances") or [])
    declared = set(entity_types)

    unclassified = [
        inst for inst in instances
        if str(inst.get("entity_type", "")).startswith("raw:")
        or inst.get("entity_type") not in declared
    ]

    suggestions: dict[str, int] = {}
    for inst in unclassified:
        raw = str(
            inst.get("raw_type")
            or str(inst.get("entity_type", "")).removeprefix("raw:")
        ).strip()
        if raw:
            suggestions[raw] = suggestions.get(raw, 0) + 1

    total = len(instances)
    return {
        "instances": total,
        "instances_unclassified": len(unclassified),
        # Reported as a share as well as a count because the count alone is
        # meaningless without the size of the table it came from.
        "unclassified_share": round(len(unclassified) / total, 3) if total else 0.0,
        "declared_entity_types": sorted(declared),
        "suggested_entity_types": [
            {"type": name, "instances": n}
            for name, n in sorted(suggestions.items(), key=lambda kv: -kv[1])[:20]
        ],
    }
