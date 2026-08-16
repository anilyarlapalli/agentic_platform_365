"""Retained content: whose bytes these are, and that a document becomes chunks.

Two guarantees that only mean anything together. The object store's is a
*boundary* — one tenant's key must be unreachable from another's context, and
unreachable in a way that does not admit the key exists. Ingest's is a
*completeness* one: a document the platform lists as current must contribute
retrievable content, or say plainly that it does not.

Tested against real MinIO, like the rest of the substrate. A fake object store
would prove the calling code compiles and nothing about whether S3's semantics —
conditional writes, delete-on-absent — are what the adapter assumes they are.
The embedding client is faked, because embeddings cost money and the property
under test is which rows get written, not what the vectors contain.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from platform_core.adapters.local.object_store import KeyRejected, S3ObjectStore
from platform_core.api.app import app
from platform_core.api.routes.documents import ALLOWED_SUFFIXES
from platform_core.corpus import chunking
from platform_core.db.engine import tenant_session
from platform_core.identity.auth import issue_token
from platform_core.identity.principal import ActorType, Principal, RequestContext, Role
from platform_core.observability import audit
from platform_core.ports.errors import ConflictError, NotFoundError
from platform_core.ports.object_store import ObjectStore
from workloads.reindex import workload as reindex

COLLECTION = "ingest-properties"

SAMPLE = b"""# Spindle Assembly SA-700

Spindle assembly SA-700: final torque specification is 180 Nm, applied in two
stages. Re-torque after 60 operating hours.

## Coolant

Coolant concentration must be held between 6 and 8 percent at all times.
"""


def _ctx(tenant) -> RequestContext:
    return RequestContext(
        principal=Principal(
            id=uuid.uuid4(), tenant=tenant, subject="ingest@example.com",
            roles=frozenset({Role.OPERATOR}), actor_type=ActorType.HUMAN,
        ),
        labels={"workload": "reindex", "task": "reindex"},
    )


@pytest.fixture
def store() -> S3ObjectStore:
    s = S3ObjectStore()
    s.ensure_bucket()
    return s


@pytest.fixture
def ctx_a(tenant_a):
    return _ctx(tenant_a)


@pytest.fixture
def ctx_b(tenant_b):
    return _ctx(tenant_b)


@pytest.fixture(autouse=True)
def _sweep_objects(store, tenant_a, tenant_b):
    """Delete each tenant's objects between cases.

    The autouse tenant cleanup in conftest removes rows, not objects, and a
    content-addressed key survives its tenant. Leftovers would make a later
    ``if_absent`` assertion pass for the wrong reason.
    """
    yield
    for tenant in (tenant_a, tenant_b):
        ctx = _ctx(tenant)
        for stored in store.list(ctx):
            store.delete(ctx, stored.key)


class _FakeEmbedder:
    """Deterministic vectors, and a record of how many texts were embedded.

    The count is the assertion that matters for copy-forward: a rebuild that
    re-embeds unchanged content is not wrong in its output, only in its bill,
    which is exactly the kind of regression a result-only assertion misses.
    """

    def __init__(self) -> None:
        self.embedded: list[str] = []

    def embed(self, ctx, texts, *, model=None):
        self.embedded.extend(texts)
        return [[0.1] * 1536 for _ in texts]


@pytest.fixture
def fake_llm(monkeypatch) -> _FakeEmbedder:
    fake = _FakeEmbedder()
    monkeypatch.setattr(reindex, "build_client", lambda: fake)
    return fake


def _register(ctx, store, *, filename: str, data: bytes, key: str | None = None):
    """Put the bytes and the row the way upload does, minus the HTTP layer."""
    import hashlib

    sha = hashlib.sha256(data).hexdigest()
    storage_key = key if key is not None else store.key_for(
        ctx, "documents", COLLECTION, f"{sha}.md"
    )
    if key is None:
        store.put(ctx, storage_key, data, content_type="text/markdown")

    with tenant_session(ctx.tenant) as s:
        return s.execute(
            text(
                "INSERT INTO document (tenant_id, workload, collection, filename, "
                "  content_sha256, byte_size, storage_key) "
                "VALUES (:t, 'echo', :c, :f, :sha, :size, :key) "
                "ON CONFLICT (tenant_id, collection, filename) "
                "  WHERE superseded_at IS NULL DO UPDATE "
                "  SET content_sha256 = EXCLUDED.content_sha256, "
                "      storage_key = EXCLUDED.storage_key RETURNING id"
            ),
            {
                "t": ctx.tenant.id, "c": COLLECTION, "f": filename,
                "sha": sha, "size": len(data), "key": storage_key,
            },
        ).scalar_one()


# ── the boundary ──────────────────────────────────────────────────────────


def test_the_adapter_satisfies_the_port(store):
    """The Protocol is runtime-checkable, so this is a real check, not a comment."""
    assert isinstance(store, ObjectStore)


def test_a_key_is_derived_never_accepted(store, ctx_a, record_evidence):
    """A caller cannot name an object outside its tenant, even by trying.

    Each rejected part is a way one tenant could otherwise address another's
    object: a traversal segment, an embedded separator, an absolute path. They
    are refused rather than sanitised — rewriting ``../x`` into ``x`` silently
    hands back a key the caller did not ask for and collapses two distinct
    inputs onto one object.
    """
    derived = store.key_for(ctx_a, "documents", "abc.md")
    assert derived.startswith(f"t/{ctx_a.tenant.id}/")

    rejected = ["../escape", "a/b", "/absolute", ".hidden", "", "x" * 300]
    assert rejected, "the precondition is empty; this test would pass vacuously"
    for part in rejected:
        with pytest.raises(KeyRejected):
            store.key_for(ctx_a, part)

    record_evidence(
        "object_key_is_derived", holds=True,
        detail=f"{len(rejected)} unsafe key parts refused; derived keys carry the tenant id",
    )


def test_another_tenants_object_is_indistinguishable_from_absent(
    store, ctx_a, ctx_b, record_evidence
):
    """Cross-tenant access returns NotFound, and the object survives the attempt.

    The precondition matters: the object is read back as A *first*, so a passing
    result cannot mean "there was nothing there anyway". And the final read
    proves B's ``delete`` was refused rather than quietly performed — a delete
    that returns NotFound while removing the object is the worst of both.
    """
    key = store.key_for(ctx_a, "documents", "shared-name.md")
    store.put(ctx_a, key, SAMPLE)
    assert store.get(ctx_a, key) == SAMPLE, "precondition: the object must exist"

    for operation in (store.get, store.head, store.delete):
        with pytest.raises(NotFoundError):
            operation(ctx_b, key)

    assert store.get(ctx_a, key) == SAMPLE, "the object must survive a refused delete"

    record_evidence(
        "object_cross_tenant_is_not_found", holds=True,
        detail="get, head and delete on another tenant's key raise NotFound and change nothing",
    )


def test_a_listing_cannot_escape_its_tenant(store, ctx_a, ctx_b, record_evidence):
    """``list`` is scoped by the caller, not by the prefix it passes."""
    store.put(ctx_a, store.key_for(ctx_a, "documents", "a-one.md"), b"a1")
    store.put(ctx_a, store.key_for(ctx_a, "documents", "a-two.md"), b"a2")
    store.put(ctx_b, store.key_for(ctx_b, "documents", "b-one.md"), b"b1")

    a_keys = {o.key for o in store.list(ctx_a)}
    b_keys = {o.key for o in store.list(ctx_b)}

    # Both non-empty, or "no overlap" is trivially true.
    assert len(a_keys) == 2 and len(b_keys) == 1
    assert not (a_keys & b_keys)
    assert all(k.startswith(f"t/{ctx_a.tenant.id}/") for k in a_keys)

    # A crafted prefix narrows, never widens.
    with pytest.raises(NotFoundError):
        store.list(ctx_a, prefix=f"../{ctx_b.tenant.id}/")

    record_evidence(
        "object_listing_is_tenant_scoped", holds=True,
        detail="listings never intersect and a traversal prefix is refused",
    )


def test_a_conditional_write_loses_to_the_incumbent(store, ctx_a, record_evidence):
    """``if_absent`` is enforced by the store, not by a check-then-write.

    Asserted on the *bytes*, not on the exception alone: an adapter that raised
    ConflictError after overwriting would pass a test that only checked the
    error, and would corrupt the very content the flag exists to protect.
    """
    key = store.key_for(ctx_a, "documents", "conditional.md")
    store.put(ctx_a, key, b"original", if_absent=True)

    with pytest.raises(ConflictError):
        store.put(ctx_a, key, b"replacement", if_absent=True)

    assert store.get(ctx_a, key) == b"original"

    # Without the flag the write is an ordinary overwrite; asserting this is
    # what stops the adapter from making every put conditional and calling it
    # safe.
    store.put(ctx_a, key, b"replacement")
    assert store.get(ctx_a, key) == b"replacement"

    record_evidence(
        "object_conditional_write_is_atomic", holds=True,
        detail="a conditional put over an existing key raises and leaves the bytes untouched",
    )


def test_delete_reports_whether_it_removed_anything(store, ctx_a):
    """Idempotent, and honest about which call did the work."""
    key = store.key_for(ctx_a, "documents", "transient.md")
    store.put(ctx_a, key, b"x")
    assert store.delete(ctx_a, key) is True
    assert store.delete(ctx_a, key) is False


def test_irreversible_purge_removes_bytes_row_and_records_audit(
    store, tenant_a, record_evidence
):
    with tenant_session(tenant_a) as session:
        principal_id = session.execute(
            text(
                "INSERT INTO principal (tenant_id, subject, roles) "
                "VALUES (:t, 'privacy-owner@example.com', ARRAY['owner']) RETURNING id"
            ),
            {"t": tenant_a.id},
        ).scalar_one()
    principal = Principal(
        id=principal_id,
        tenant=tenant_a,
        subject="privacy-owner@example.com",
        roles=frozenset({Role.OWNER}),
        actor_type=ActorType.HUMAN,
    )
    ctx = RequestContext(principal=principal)
    key = store.key_for(ctx, "documents", COLLECTION, "privacy.md")
    store.put(ctx, key, b"personal data")
    with tenant_session(tenant_a) as session:
        document_id = session.execute(
            text(
                "INSERT INTO document (tenant_id, workload, collection, filename, "
                "content_sha256, byte_size, storage_key, uploaded_by, superseded_at) "
                "VALUES (:t, 'echo', :c, 'privacy.md', :sha, 13, :key, :by, now()) "
                "RETURNING id"
            ),
            {
                "t": tenant_a.id,
                "c": COLLECTION,
                "sha": "9" * 64,
                "key": key,
                "by": principal_id,
            },
        ).scalar_one()

    with TestClient(app) as client:
        response = client.delete(
            f"/api/documents/{document_id}/purge",
            headers={"Authorization": f"Bearer {issue_token(principal)}"},
        )
    assert response.status_code == 200, response.text
    assert response.json()["irreversible"] is True
    with pytest.raises(NotFoundError):
        store.get(ctx, key)
    with tenant_session(tenant_a) as session:
        assert session.execute(
            text("SELECT id FROM document WHERE id = :id"), {"id": document_id}
        ).scalar_one_or_none() is None
    assert any(
        event.action == "document.purged" and event.resource_id == str(document_id)
        for event in audit.recent(ctx)
    )

    record_evidence(
        "document_erasure_is_enforced_and_audited",
        holds=True,
        document_id=str(document_id),
        object_removed=True,
    )


# ── what can be ingested ──────────────────────────────────────────────────


def test_upload_accepts_exactly_what_can_be_chunked(record_evidence):
    """The two lists are one list.

    They drifted before: upload accepted .pdf, .docx and .xlsx, which the
    platform stored and could never chunk — a document listed as current,
    reported as indexed, contributing nothing. Compared to a literal rather
    than to ``SUPPORTED_SUFFIXES`` alone, so widening the constant cannot make
    this pass by moving the goalposts.
    """
    assert ALLOWED_SUFFIXES == chunking.SUPPORTED_SUFFIXES
    assert frozenset({".txt", ".md", ".csv", ".html"}) == ALLOWED_SUFFIXES
    assert ".pdf" not in ALLOWED_SUFFIXES

    record_evidence(
        "upload_accepts_only_chunkable_formats", holds=True,
        detail="every accepted suffix has a text extractor behind it",
    )


def test_an_unreadable_format_is_refused_not_silently_empty():
    """"Cannot read this" and "this has no text" lead to different fixes."""
    with pytest.raises(chunking.UnsupportedDocument):
        chunking.chunk_document("manual.pdf", b"%PDF-1.4")


def test_chunk_identity_is_content_not_position(record_evidence):
    """The same bytes chunk to the same ids, every time.

    This is what makes a rebuild idempotent and a stored eval result still
    meaningful afterwards. Prepending a section shifts every chunk's position
    and must not change the id of a chunk whose text is unchanged.
    """
    first = chunking.chunk_document("spec.md", SAMPLE)
    assert first, "precondition: the sample must produce chunks"
    assert [c.canonical_id for c in first] == [
        c.canonical_id for c in chunking.chunk_document("spec.md", SAMPLE)
    ]

    shifted = chunking.chunk_document("spec.md", b"# Preface\n\nUnrelated opening.\n\n" + SAMPLE)
    unchanged = {c.canonical_id for c in first} & {c.canonical_id for c in shifted}
    assert unchanged, "chunks whose text did not change must keep their ids"
    assert [c.ordinal for c in shifted] == list(range(len(shifted)))

    record_evidence(
        "chunk_id_is_content_addressed", holds=True,
        detail=f"{len(unchanged)} chunk ids survived a positional shift",
    )


# ── completeness ──────────────────────────────────────────────────────────


def test_a_retained_document_becomes_chunks_in_the_new_build(
    store, ctx_a, fake_llm, record_evidence
):
    """The gap this phase closed: upload → retrievable content, no manual step."""
    _register(ctx_a, store, filename="sa700.md", data=SAMPLE)

    result = reindex.run(_ctx(ctx_a.tenant), {"collection": COLLECTION})

    assert result["ingested_chunks"] > 0, "the document must have been chunked"
    assert result["documents_without_chunks"] == 0
    assert not result["skipped_documents"]

    with tenant_session(ctx_a.tenant) as s:
        rows = s.execute(
            text(
                "SELECT text FROM chunk WHERE collection = :c AND build_version = :v"
            ),
            {"c": COLLECTION, "v": result["build_version"]},
        ).scalars().all()
    assert any("180 Nm" in r for r in rows), "the content itself must be retrievable"

    record_evidence(
        "ingest_produces_retrievable_chunks", holds=True,
        chunks=result["ingested_chunks"],
        detail="a document with retained bytes contributes chunks to the build it is current in",
    )


def test_an_unchanged_document_is_copied_forward_not_re_embedded(
    store, ctx_a, fake_llm, record_evidence
):
    """Rebuilding must cost nothing for content that did not change.

    Asserted on the embedder's call log, not on the chunk count: a rebuild that
    re-embeds every chunk produces an identical corpus and an unbounded bill,
    and only the call log can tell the two apart.
    """
    _register(ctx_a, store, filename="sa700.md", data=SAMPLE)
    first = reindex.run(_ctx(ctx_a.tenant), {"collection": COLLECTION})
    assert fake_llm.embedded, "precondition: the first build must have embedded something"

    embedded_after_first = len(fake_llm.embedded)
    second = reindex.run(_ctx(ctx_a.tenant), {"collection": COLLECTION})

    assert len(fake_llm.embedded) == embedded_after_first, "a rebuild must not re-embed"
    assert second["ingested_chunks"] == 0
    assert second["copied_chunks"] == first["ingested_chunks"]
    assert second["documents_without_chunks"] == 0

    record_evidence(
        "unchanged_content_is_not_re_embedded", holds=True,
        detail=f"{second['copied_chunks']} chunks copied forward with zero embedding calls",
    )


def test_a_replaced_document_is_re_chunked_from_its_new_bytes(
    store, ctx_a, fake_llm, record_evidence
):
    """A replacement's content must reach the corpus, and its predecessor's leave it."""
    _register(ctx_a, store, filename="sa700.md", data=SAMPLE)
    reindex.run(_ctx(ctx_a.tenant), {"collection": COLLECTION})

    revised = SAMPLE.replace(b"180 Nm", b"195 Nm")
    with tenant_session(ctx_a.tenant) as s:
        s.execute(
            text(
                "UPDATE document SET superseded_at = now() "
                "WHERE collection = :c AND filename = 'sa700.md' AND superseded_at IS NULL"
            ),
            {"c": COLLECTION},
        )
    _register(ctx_a, store, filename="sa700.md", data=revised)

    result = reindex.run(_ctx(ctx_a.tenant), {"collection": COLLECTION})
    assert result["documents_without_chunks"] == 0, (
        "a replaced document used to contribute nothing — that is the defect this closes"
    )

    with tenant_session(ctx_a.tenant) as s:
        rows = s.execute(
            text("SELECT text FROM chunk WHERE collection = :c AND build_version = :v"),
            {"c": COLLECTION, "v": result["build_version"]},
        ).scalars().all()
    assert any("195 Nm" in r for r in rows)
    assert not any("180 Nm" in r for r in rows), "the superseded figure must not still be served"

    record_evidence(
        "replaced_content_reaches_the_corpus", holds=True,
        detail="the new bytes are chunked and the superseded ones stop being served",
    )


def test_a_document_whose_bytes_are_missing_is_counted_not_hidden(
    store, ctx_a, fake_llm, record_evidence
):
    """One unreadable document must not fail the rebuild, and must not vanish.

    This is the state every document written before this phase is in. Reporting
    it is the whole difference between a corpus that is complete and one that
    only looks complete — the same reason ``documents_without_chunks`` exists.
    """
    _register(ctx_a, store, filename="sa700.md", data=SAMPLE)
    _register(
        ctx_a, store, filename="orphan.md", data=b"never stored",
        key=store.key_for(ctx_a, "documents", COLLECTION, "deadbeef.md"),
    )

    result = reindex.run(_ctx(ctx_a.tenant), {"collection": COLLECTION})

    assert result["ingested_documents"] == 1, "the readable document must still be ingested"
    assert result["skipped_documents"].get("bytes_missing") == 1
    assert result["documents_without_chunks"] == 1
    # ``chunk_count`` is only present on the promotion path — the unpromoted
    # result carries ``promoted: False`` and a reason instead. Asserting on it
    # is what says the collection went live despite the bad document.
    assert result.get("chunk_count", 0) > 0, "one bad document must not fail the collection"

    record_evidence(
        "missing_bytes_are_reported_not_swallowed", holds=True,
        detail="a document with no retained content is skipped by reason and counted",
    )
