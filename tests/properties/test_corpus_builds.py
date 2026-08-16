"""The corpus build lifecycle: what must hold while a collection is replaced.

Every assertion here is about a *transition*. A corpus that is correct while
nothing is happening is not the property worth testing — the failures this
guards against all occur during a rebuild, when two versions of the same
collection exist and something has to decide which one answers.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from platform_core.corpus import builds
from platform_core.db.engine import tenant_session
from platform_core.identity.principal import RequestContext

COLLECTION = "build-lifecycle"


def _ctx(tenant, principal_id) -> RequestContext:
    from platform_core.identity.principal import ActorType, Principal, Role

    return RequestContext(
        principal=Principal(
            id=principal_id, tenant=tenant, subject="builder@example.com",
            roles=frozenset({Role.OPERATOR}), actor_type=ActorType.HUMAN,
        )
    )


def _seed_chunk(ctx, *, build_version: int, canonical_id: str, document_id) -> None:
    with tenant_session(ctx.tenant) as s:
        s.execute(
            text(
                "INSERT INTO chunk (tenant_id, document_id, collection, canonical_id, "
                "  ordinal, build_version, text, embedding, embedding_model) "
                "VALUES (:t, :d, :c, :cid, 0, :v, :txt, CAST(:vec AS vector), 'test') "
                "ON CONFLICT (tenant_id, collection, canonical_id, build_version) "
                "DO NOTHING"
            ),
            {
                "t": ctx.tenant.id, "d": document_id, "c": COLLECTION,
                "cid": canonical_id, "v": build_version,
                "txt": f"body of {canonical_id}",
                "vec": str([0.1] * 1536),
            },
        )


@pytest.fixture
def ctx(tenant_a):
    # A synthetic principal id. These properties are about build transitions,
    # and nothing on this path has a foreign key to `principal` — the documents
    # below are inserted with `uploaded_by` null. Depending on a seeded
    # principal would couple these tests to an unrelated fixture.
    return _ctx(tenant_a, uuid.uuid4())


@pytest.fixture
def document_id(ctx):
    with tenant_session(ctx.tenant) as s:
        return s.execute(
            text(
                "INSERT INTO document (tenant_id, workload, collection, filename, "
                "  content_sha256, byte_size, storage_key) "
                "VALUES (:t, 'echo', :c, 'lifecycle.md', :sha, 10, 'k') "
                "ON CONFLICT (tenant_id, collection, filename) "
                "  WHERE superseded_at IS NULL DO UPDATE "
                "  SET content_sha256 = EXCLUDED.content_sha256 RETURNING id"
            ),
            {"t": ctx.tenant.id, "c": COLLECTION, "sha": "a" * 64},
        ).scalar_one()


def test_a_collection_with_no_live_build_serves_nothing(ctx, record_evidence):
    """An unbuilt collection must not fall back to reading whatever exists.

    Without the live-build predicate a read returns every row in the table for
    that collection — including a half-written build. Refusing is the only
    answer that cannot serve a partial corpus.
    """
    with pytest.raises(builds.NoLiveBuild):
        builds.live_version(ctx, "never-built-collection")

    record_evidence(
        "corpus_no_live_build_refuses", holds=True,
        detail="a collection with no live build raises rather than reading loose rows",
    )


def test_a_half_written_build_is_never_served(ctx, document_id, record_evidence):
    """Rows written to a building version must be invisible until promotion.

    This is the reason writes go beside the live build rather than into it. The
    Azure build recreates its index in place, so a rebuild is a window in which
    the corpus is partial and still answering.
    """
    v1 = builds.begin(ctx, COLLECTION)
    _seed_chunk(ctx, build_version=v1, canonical_id="c_first", document_id=document_id)
    builds.promote(ctx, COLLECTION, v1)

    v2 = builds.begin(ctx, COLLECTION)
    _seed_chunk(ctx, build_version=v2, canonical_id="c_partial", document_id=document_id)

    # v2 exists and has rows, but is not live.
    assert builds.live_version(ctx, COLLECTION) == v1

    with tenant_session(ctx.tenant) as s:
        served = s.execute(
            text(
                "SELECT count(*) FROM chunk WHERE collection = :c AND build_version = :v"
            ),
            {"c": COLLECTION, "v": builds.live_version(ctx, COLLECTION)},
        ).scalar_one()
    assert served == 1, "the live build must not contain the in-progress build's rows"

    record_evidence(
        "corpus_partial_build_not_served", holds=True,
        live=v1, building=v2,
        detail="rows in a building version are invisible until promotion",
    )


def test_a_failed_build_leaves_the_previous_one_serving(ctx, document_id, record_evidence):
    """The rollback story. A failed rebuild must not empty the collection."""
    v1 = builds.begin(ctx, COLLECTION)
    _seed_chunk(ctx, build_version=v1, canonical_id="c_keep", document_id=document_id)
    builds.promote(ctx, COLLECTION, v1)

    v2 = builds.begin(ctx, COLLECTION)
    _seed_chunk(ctx, build_version=v2, canonical_id="c_doomed", document_id=document_id)
    builds.fail(ctx, COLLECTION, v2, "simulated failure")

    assert builds.live_version(ctx, COLLECTION) == v1, (
        "a failed rebuild must leave the previous build live"
    )
    with tenant_session(ctx.tenant) as s:
        orphans = s.execute(
            text(
                "SELECT count(*) FROM chunk WHERE collection = :c AND build_version = :v"
            ),
            {"c": COLLECTION, "v": v2},
        ).scalar_one()
    assert orphans == 0, "a failed build must not leave its partial rows behind"

    record_evidence(
        "corpus_failed_build_rolls_back", holds=True,
        detail="failed rebuild drops its rows and leaves the previous build serving",
    )


def test_promoting_an_empty_build_is_refused(ctx, document_id, record_evidence):
    """Promotion of an empty build would take the collection dark reporting success."""
    v1 = builds.begin(ctx, COLLECTION)
    _seed_chunk(ctx, build_version=v1, canonical_id="c_only", document_id=document_id)
    builds.promote(ctx, COLLECTION, v1)

    v2 = builds.begin(ctx, COLLECTION)
    with pytest.raises(ValueError, match="no chunks"):
        builds.promote(ctx, COLLECTION, v2)

    assert builds.live_version(ctx, COLLECTION) == v1

    record_evidence(
        "corpus_empty_promotion_refused", holds=True,
        detail="an empty build cannot be promoted; the live build is untouched",
    )


def test_exactly_one_build_is_live(ctx, document_id, record_evidence):
    """Two live builds would make retrieval read from both and duplicate sources."""
    for _ in range(3):
        v = builds.begin(ctx, COLLECTION)
        _seed_chunk(ctx, build_version=v, canonical_id=f"c_{v}", document_id=document_id)
        builds.promote(ctx, COLLECTION, v)

    with tenant_session(ctx.tenant) as s:
        live = s.execute(
            text(
                "SELECT count(*) FROM collection_build "
                "WHERE collection = :c AND status = 'live'"
            ),
            {"c": COLLECTION},
        ).scalar_one()
    assert live == 1, "exactly one build may be live at a time"

    record_evidence(
        "corpus_single_live_build", holds=True,
        detail="the partial unique index permits exactly one live build per collection",
    )


def test_the_reaper_never_deletes_the_live_build(ctx, document_id, record_evidence):
    """The bound that keeps the read-path post-filter from gutting recall.

    ``build_version`` is not in the HNSW index, so it is applied after the
    vector search. Two coexisting builds means the filter discards at most half
    the candidates; several would silently degrade recall the way an unfiltered
    global index did before 0014. The reaper enforces the bound — and must never
    reap what is being served.
    """
    versions = []
    for _ in range(4):
        v = builds.begin(ctx, COLLECTION)
        _seed_chunk(ctx, build_version=v, canonical_id=f"c_{v}", document_id=document_id)
        builds.promote(ctx, COLLECTION, v)
        versions.append(v)

    live_before = builds.live_version(ctx, COLLECTION)
    builds.reap(ctx, COLLECTION)

    assert builds.live_version(ctx, COLLECTION) == live_before, (
        "the reaper deleted or demoted the live build"
    )
    with tenant_session(ctx.tenant) as s:
        remaining = s.execute(
            text("SELECT count(*) FROM collection_build WHERE collection = :c"),
            {"c": COLLECTION},
        ).scalar_one()
        live_chunks = s.execute(
            text(
                "SELECT count(*) FROM chunk WHERE collection = :c AND build_version = :v"
            ),
            {"c": COLLECTION, "v": live_before},
        ).scalar_one()

    # Compared against a **literal**, not against MAX_COEXISTING_BUILDS. An
    # assertion that reads the constant it is guarding moves its own goalposts:
    # raising the constant to 99 would keep this test green while silently
    # reopening the recall problem. The bound is 2 because that is what the
    # post-filter argument supports, so 2 is what the test asserts.
    assert builds.MAX_COEXISTING_BUILDS <= 2, (
        f"MAX_COEXISTING_BUILDS is {builds.MAX_COEXISTING_BUILDS}. build_version is "
        f"not in the HNSW index, so every extra coexisting build widens a "
        f"post-filter on the vector search — the defect 0014 was written to fix."
    )
    assert remaining <= 2, (
        f"{remaining} builds coexist; the read-path filter is only safe up to 2"
    )
    assert live_chunks > 0, "the live build lost its chunks to the reaper"

    record_evidence(
        "corpus_reaper_bounds_builds", holds=True,
        remaining=remaining, max_allowed=builds.MAX_COEXISTING_BUILDS,
        detail="reaping bounds coexisting builds and never touches the live one",
    )
