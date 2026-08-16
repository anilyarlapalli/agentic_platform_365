"""Which build of a collection is being served, and how it gets replaced.

A collection's chunks are versioned by ``build_version``. Exactly one build is
``live`` at a time, and every read resolves through here rather than reading
whatever rows happen to exist. Writes go to the next version while reads stay on
the current one, so a rebuild that fails half-written leaves the previous corpus
serving instead of emptying it.

## The two-build bound is a correctness constraint

Reads carry a ``build_version`` predicate that the HNSW index does not contain,
so it is applied *after* the vector search — the same post-filter shape that
migration 0014 was written to fix. With two builds coexisting the filter
discards at most half the candidates, which ``hnsw.iterative_scan`` absorbs.
With several builds it would quietly gut recall again, exactly as before and
just as invisibly. :func:`reap` enforces the bound; it is not a tidiness pass.
"""

from __future__ import annotations

import hashlib
import logging
import uuid

from sqlalchemy import text

from platform_core.db.engine import tenant_session
from platform_core.identity.principal import RequestContext

logger = logging.getLogger("platform.corpus.builds")

# Live plus its immediate predecessor. See the module docstring — this is the
# bound the read-path filter depends on, not a retention preference.
MAX_COEXISTING_BUILDS = 2


class NoLiveBuild(Exception):
    """The collection has never been built, or its build went missing.

    Distinguished from "the collection is empty" because the remedies differ: an
    empty collection needs documents, a missing build needs a rebuild. Returning
    zero rows for both would make a lost pointer look like an empty corpus.
    """


def live_version(ctx: RequestContext, collection: str, *, session=None) -> int:
    """The build a read should use. Raises :class:`NoLiveBuild` when absent."""

    def _query(s) -> int | None:
        return s.execute(
            text(
                "SELECT build_version FROM collection_build "
                "WHERE collection = :c AND status = 'live'"
            ),
            {"c": collection},
        ).scalar_one_or_none()

    if session is not None:
        version = _query(session)
    else:
        with tenant_session(ctx.tenant) as s:
            version = _query(s)

    if version is None:
        raise NoLiveBuild(
            f"collection {collection!r} has no live build — it has never been "
            f"indexed, or its build pointer was lost. Reads are refused rather "
            f"than silently returning nothing."
        )
    return version


def live_version_or_none(ctx: RequestContext, collection: str, *, session=None) -> int | None:
    """As :func:`live_version`, but ``None`` instead of raising.

    For callers that legitimately run before a corpus exists — the graph builder
    treats an unbuilt collection as an empty one.
    """
    try:
        return live_version(ctx, collection, session=session)
    except NoLiveBuild:
        return None


def fingerprint(ctx: RequestContext, collection: str, build_version: int) -> str:
    """Content identity of one build.

    Derived from the canonical ids it contains, so two builds with the same
    documents in a different insertion order fingerprint identically, and a
    single changed chunk changes it. Consumers of derived state — the knowledge
    graph, an onboarding bundle — compare this to know whether what they were
    built from is still what is being served.
    """
    with tenant_session(ctx.tenant) as s:
        ids = s.execute(
            text(
                "SELECT canonical_id FROM chunk "
                "WHERE collection = :c AND build_version = :v ORDER BY canonical_id"
            ),
            {"c": collection, "v": build_version},
        ).scalars().all()

    digest = hashlib.sha256()
    for canonical_id in ids:
        digest.update(canonical_id.encode())
    return digest.hexdigest()


def begin(ctx: RequestContext, collection: str, *, run_id: uuid.UUID | None = None) -> int:
    """Open the next build. Returns its version.

    The row is written as ``building`` immediately so a crashed rebuild is
    visibly incomplete rather than indistinguishable from one still running —
    the same reason onboarding sessions record their status up front.
    """
    with tenant_session(ctx.tenant) as s:
        current = s.execute(
            text(
                "SELECT COALESCE(max(build_version), 0) FROM collection_build "
                "WHERE collection = :c"
            ),
            {"c": collection},
        ).scalar_one()
        version = int(current) + 1
        s.execute(
            text(
                "INSERT INTO collection_build "
                "(tenant_id, collection, build_version, status, run_id) "
                "VALUES (:t, :c, :v, 'building', :r)"
            ),
            {"t": ctx.tenant.id, "c": collection, "v": version, "r": run_id},
        )
    return version


def promote(ctx: RequestContext, collection: str, build_version: int) -> dict:
    """Make a build live, in one transaction.

    Demotion of the incumbent and promotion of the new build happen together
    because the partial unique index permits exactly one live build — doing it
    in two steps would either violate the index or leave the collection with no
    live build in between, which is a window where every read fails.
    """
    with tenant_session(ctx.tenant) as s:
        chunk_count = s.execute(
            text(
                "SELECT count(*) FROM chunk WHERE collection = :c AND build_version = :v"
            ),
            {"c": collection, "v": build_version},
        ).scalar_one()

        if chunk_count == 0:
            # Promoting an empty build would take the collection dark while
            # reporting success. The previous build stays live instead.
            raise ValueError(
                f"refusing to promote build {build_version} of {collection!r}: it "
                f"has no chunks. The live build is left untouched."
            )

        s.execute(
            text(
                "UPDATE collection_build SET status = 'superseded' "
                "WHERE collection = :c AND status = 'live'"
            ),
            {"c": collection},
        )
        s.execute(
            text(
                "UPDATE collection_build SET status = 'live', promoted_at = now(), "
                "chunk_count = :n WHERE collection = :c AND build_version = :v"
            ),
            {"c": collection, "v": build_version, "n": chunk_count},
        )

    logger.info("promoted %s/%s to build %d (%d chunks)",
                ctx.tenant.slug, collection, build_version, chunk_count)
    return {"collection": collection, "build_version": build_version,
            "chunk_count": chunk_count}


def fail(ctx: RequestContext, collection: str, build_version: int, error: str) -> None:
    """Mark a build failed and drop its partial rows.

    The live build is untouched, which is the whole point of building beside it
    rather than in place.
    """
    with tenant_session(ctx.tenant) as s:
        s.execute(
            text(
                "DELETE FROM chunk WHERE collection = :c AND build_version = :v"
            ),
            {"c": collection, "v": build_version},
        )
        s.execute(
            text(
                "UPDATE collection_build SET status = 'failed', error = :e "
                "WHERE collection = :c AND build_version = :v AND status = 'building'"
            ),
            {"c": collection, "v": build_version, "e": error[:2000]},
        )


def reap(ctx: RequestContext, collection: str) -> dict:
    """Delete builds beyond the coexistence bound.

    Keeps the live build and its immediate predecessor. The predecessor is kept
    so a bad promotion can be reverted without re-embedding; everything older is
    deleted, because each extra build widens the read-path post-filter.

    The live build is excluded by construction rather than by ordering luck: it
    is selected out explicitly, so a mis-sorted query cannot delete the corpus
    that is currently being served.
    """
    with tenant_session(ctx.tenant) as s:
        live = s.execute(
            text(
                "SELECT build_version FROM collection_build "
                "WHERE collection = :c AND status = 'live'"
            ),
            {"c": collection},
        ).scalar_one_or_none()

        if live is None:
            # Never reap when there is nothing serving — that is the state in
            # which deleting anything could leave the collection unrecoverable.
            return {"collection": collection, "deleted_builds": [], "reason": "no live build"}

        keep = s.execute(
            text(
                "SELECT build_version FROM collection_build "
                "WHERE collection = :c AND build_version < :live "
                "  AND status IN ('live', 'superseded') "
                "ORDER BY build_version DESC LIMIT :n"
            ),
            {"c": collection, "live": live, "n": MAX_COEXISTING_BUILDS - 1},
        ).scalars().all()
        keep_set = {int(live), *(int(k) for k in keep)}

        doomed = s.execute(
            text(
                "SELECT build_version FROM collection_build "
                "WHERE collection = :c AND build_version <> ALL(:keep)"
            ),
            {"c": collection, "keep": list(keep_set)},
        ).scalars().all()

        for version in doomed:
            s.execute(
                text("DELETE FROM chunk WHERE collection = :c AND build_version = :v"),
                {"c": collection, "v": version},
            )
        if doomed:
            s.execute(
                text(
                    "DELETE FROM collection_build "
                    "WHERE collection = :c AND build_version <> ALL(:keep)"
                ),
                {"c": collection, "keep": list(keep_set)},
            )

    if doomed:
        logger.info("reaped builds %s of %s/%s (kept %s)",
                    sorted(doomed), ctx.tenant.slug, collection, sorted(keep_set))
    return {"collection": collection, "deleted_builds": sorted(int(d) for d in doomed),
            "kept": sorted(keep_set)}
