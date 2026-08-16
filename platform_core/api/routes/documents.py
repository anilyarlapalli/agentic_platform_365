"""Documents. Upload is validated at the door, retained, and deduplicated by content."""

from __future__ import annotations

import hashlib
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text

from platform_core.adapters.local.object_store import get_object_store
from platform_core.api.deps import get_context
from platform_core.corpus.chunking import SUPPORTED_SUFFIXES
from platform_core.correctness.outbox import enqueue_run
from platform_core.db.engine import tenant_session
from platform_core.identity.principal import RequestContext
from platform_core.observability import audit
from platform_core.ports.errors import ConflictError, TransientError
from workloads.reindex.workload import WORKLOAD as REINDEX

logger = logging.getLogger("platform.api.documents")

router = APIRouter(prefix="/api", tags=["documents"])

# One authority, not two. This used to also list .pdf, .docx and .xlsx, which
# the platform accepted, stored a row for, and could never chunk — a document
# that is listed as current and contributes nothing retrievable. Binary formats
# come back when there is an extractor for them, and the same constant is what
# will let them in.
ALLOWED_SUFFIXES = SUPPORTED_SUFFIXES
MAX_UPLOAD_BYTES = 50 * 1024 * 1024

# Content types are advisory here; the suffix is what extraction dispatches on.
_CONTENT_TYPES = {
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".csv": "text/csv",
    ".html": "text/html",
}


class DocumentCreate(BaseModel):
    collection: str = Field(min_length=1, max_length=128, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    filename: str = Field(min_length=1, max_length=512)
    content_base64: str


def _serialize(row) -> dict:
    return {
        "id": str(row.id),
        "collection": row.collection,
        "filename": row.filename,
        "byte_size": row.byte_size,
        "content_sha256": row.content_sha256,
        "created_at": row.created_at.isoformat(),
    }


@router.get("/documents")
def list_documents(
    collection: str | None = None,
    include_superseded: bool = False,
    ctx: RequestContext = Depends(get_context),
) -> dict:
    """Current documents by default.

    Superseded rows are retained for history — prior versions and withdrawn
    documents — but listing them by default would show a filename twice and make
    a replaced document look like a duplicate upload. ``include_superseded``
    opts into the full history.
    """
    where = ["1 = 1"] if include_superseded else ["superseded_at IS NULL"]
    params: dict = {}
    if collection:
        where.append("collection = :c")
        params["c"] = collection

    with tenant_session(ctx.tenant) as s:
        rows = s.execute(
            text(
                "SELECT id, collection, filename, byte_size, content_sha256, "
                "  created_at, superseded_at "
                f"FROM document WHERE {' AND '.join(where)} "
                "ORDER BY created_at DESC LIMIT 200"
            ),
            params,
        ).all()

    out = []
    for r in rows:
        item = _serialize(r)
        item["superseded_at"] = r.superseded_at.isoformat() if r.superseded_at else None
        out.append(item)
    return {"documents": out}


@router.get("/documents/{document_id}")
def get_document(document_id: uuid.UUID, ctx: RequestContext = Depends(get_context)) -> dict:
    with tenant_session(ctx.tenant) as s:
        row = s.execute(
            text(
                "SELECT id, collection, filename, byte_size, content_sha256, created_at "
                "FROM document WHERE id = :id"
            ),
            {"id": document_id},
        ).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Document not found.")
    return _serialize(row)


@router.post("/documents", status_code=201)
def create_document(
    payload: DocumentCreate, ctx: RequestContext = Depends(get_context)
) -> dict:
    """Store a document and queue the rebuild that will index it.

    Validated before anything is stored: rejecting an oversized or unsupported
    file at the door keeps it out of the object store and off the queue
    entirely, rather than discovering it in a worker after two network hops.

    **The bytes are written before the row, deliberately.** The two orders fail
    differently and one of them is recoverable: an object with no row is garbage
    that costs disk and can be reaped, while a row with no object is a document
    the platform lists as current, reports as indexed, and can never chunk — the
    exact state this endpoint was in before there was an object store at all.
    The key is content-addressed, so the write is idempotent and a retry after a
    crash costs nothing.
    """
    import base64
    import binascii
    from pathlib import Path

    suffix = Path(payload.filename).suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        raise HTTPException(
            status_code=415,
            detail=f"Unsupported file type {suffix!r}. Allowed: {sorted(ALLOWED_SUFFIXES)}",
        )

    try:
        content = base64.b64decode(payload.content_base64, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(status_code=400, detail="content_base64 is not valid base64.") from None

    if not content:
        raise HTTPException(status_code=400, detail="File is empty.")
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413, detail=f"File exceeds the {MAX_UPLOAD_BYTES // 1024 // 1024} MB limit."
        )

    sha256 = hashlib.sha256(content).hexdigest()

    store = get_object_store()
    # Derived, never composed by hand. The previous version interpolated the
    # tenant slug into an f-string, which is a convention a later caller can
    # forget; key_for is the control, and it is checked again on every read.
    storage_key = store.key_for(ctx, "documents", payload.collection, f"{sha256}{suffix}")
    try:
        store.put(
            ctx,
            storage_key,
            content,
            content_type=_CONTENT_TYPES.get(suffix, "application/octet-stream"),
            # The key is the content hash, so an existing object holds these
            # exact bytes. The conflict is the store reporting the work already
            # done, not a failure.
            if_absent=True,
        )
    except ConflictError:
        logger.debug("document bytes already stored at %s", storage_key)
    except TransientError as exc:
        # Refuse the upload rather than record a document whose content was
        # never retained. A 503 asks the client to retry; a 201 here would be a
        # lie that only surfaces at the next rebuild.
        raise HTTPException(
            status_code=503, detail="Object store is unavailable; the upload was not stored."
        ) from exc

    with tenant_session(ctx.tenant) as s:
        # Identity is (collection, filename) since 0016. Three cases, and they
        # are genuinely different acts:
        #
        #   same filename, same bytes  → no-op; a retried upload resolves to the
        #                                original row rather than a second one
        #   same filename, new bytes   → a new *version*: the old row is
        #                                superseded and linked, not overwritten,
        #                                so the history survives
        #   new filename               → a new document
        existing = s.execute(
            text(
                "SELECT id, collection, filename, byte_size, content_sha256, created_at "
                "FROM document WHERE collection = :c AND filename = :f "
                "  AND superseded_at IS NULL"
            ),
            {"c": payload.collection, "f": payload.filename},
        ).one_or_none()

        if existing is not None and existing.content_sha256 == sha256:
            out = _serialize(existing)
            out["unchanged"] = True
            return out

        if existing is not None:
            # Retire the incumbent *before* inserting. The unique index is
            # partial on `superseded_at IS NULL` and is checked per statement,
            # so inserting first would momentarily leave two current rows for
            # one filename and be rejected. Retiring first leaves zero, then
            # one — never two.
            s.execute(
                text("UPDATE document SET superseded_at = now() WHERE id = :old"),
                {"old": existing.id},
            )

        row = s.execute(
            text(
                "INSERT INTO document (tenant_id, workload, collection, filename, "
                "content_sha256, byte_size, storage_key, uploaded_by) "
                "VALUES (:t, 'echo', :c, :f, :sha, :size, :key, :by) "
                "RETURNING id, collection, filename, byte_size, content_sha256, created_at"
            ),
            {
                "t": ctx.tenant.id,
                "c": payload.collection,
                "f": payload.filename,
                "sha": sha256,
                "size": len(content),
                "key": storage_key,
                "by": ctx.principal.id,
            },
        ).one()

        if existing is not None:
            # Link the versions once the successor has an id, so the history is
            # a chain rather than a set of orphaned rows.
            s.execute(
                text("UPDATE document SET superseded_by = :new WHERE id = :old"),
                {"new": row.id, "old": existing.id},
            )

        run_id, _ = enqueue_run(
            s, ctx,
            workload=REINDEX,
            payload={"collection": payload.collection},
            # One rebuild per collection per upload batch. Without this key two
            # quick uploads queue two rebuilds that race on the same collection
            # — the exact fan-out the Azure build hits on double-clicked upload.
            idempotency_key=f"reindex:{payload.collection}:{sha256}",
        )
        audit.append_in_session(
            s,
            ctx,
            action="document.ingested",
            outcome=audit.Outcome.SUCCEEDED,
            resource_type="document",
            resource_id=str(row.id),
            detail={
                "collection": payload.collection,
                "content_sha256": sha256,
                "byte_size": len(content),
                "replaced": str(existing.id) if existing is not None else None,
            },
        )

    out = _serialize(row)
    out["unchanged"] = False
    out["replaced"] = str(existing.id) if existing is not None else None
    out["reindex_run_id"] = str(run_id)
    return out


@router.delete("/documents/{document_id}")
def delete_document(document_id: uuid.UUID, ctx: RequestContext = Depends(get_context)) -> dict:
    """Withdraw a document from the corpus.

    Marks it superseded rather than deleting the row. A hard DELETE would
    cascade to its chunks immediately, taking content out of the **live** build
    that is still serving queries — a delete would be felt mid-request. Marking
    it instead means the next rebuild simply omits it, and the live build keeps
    answering consistently until the flip. The row also stays for audit: "who
    removed what, and when" is unanswerable once it is gone.

    **The stored object is left in place**, for the same reason. Reverting to
    the previous build has to be possible without a re-upload, and that build's
    chunks are only reproducible while their bytes exist. Reclaiming the space
    is a separate, audited purge — deleting content here would make a withdrawal
    quietly irreversible, which is not what the caller asked for.
    """
    with tenant_session(ctx.tenant) as s:
        row = s.execute(
            text(
                "UPDATE document SET superseded_at = now() "
                "WHERE id = :id AND superseded_at IS NULL "
                "RETURNING id, collection"
            ),
            {"id": document_id},
        ).one_or_none()

        if row is None:
            # 404 whether absent, another tenant's, or already withdrawn.
            raise HTTPException(status_code=404, detail="Document not found.")

        run_id, _ = enqueue_run(
            s, ctx,
            workload=REINDEX,
            payload={"collection": row.collection},
            idempotency_key=f"reindex:{row.collection}:delete:{document_id}",
        )
        audit.append_in_session(
            s,
            ctx,
            action="document.withdrawn",
            outcome=audit.Outcome.SUCCEEDED,
            resource_type="document",
            resource_id=str(document_id),
            detail={"collection": row.collection, "reindex_run_id": str(run_id)},
        )

    return {
        "id": str(row.id),
        "deleted": True,
        "collection": row.collection,
        # The content is still being served until this run promotes a build
        # without it. Saying so is more useful than reporting a delete as
        # instantaneous when it is not.
        "reindex_run_id": str(run_id),
        "note": "withdrawn; still served by the live build until the rebuild promotes",
    }


@router.delete("/documents/{document_id}/purge")
def purge_document(document_id: uuid.UUID, ctx: RequestContext = Depends(get_context)) -> dict:
    """Irreversibly erase a withdrawn document and its stored bytes.

    Withdrawal remains reversible. Purge is a separate owner-only capability,
    requires the document to have been withdrawn, and removes the object before
    the row so a crash can leave visible metadata to retry but cannot leave
    undiscoverable personal data in object storage.
    """
    with tenant_session(ctx.tenant) as session:
        row = session.execute(
            text(
                "SELECT id, collection, storage_key, superseded_at FROM document "
                "WHERE id = :id"
            ),
            {"id": document_id},
        ).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Document not found.")
    if row.superseded_at is None:
        raise HTTPException(
            status_code=409,
            detail="Withdraw the document before irreversible purge.",
        )

    try:
        object_removed = get_object_store().delete(ctx, row.storage_key)
    except TransientError as exc:
        raise HTTPException(
            status_code=503,
            detail="Object store is unavailable; no database record was purged.",
        ) from exc

    with tenant_session(ctx.tenant) as session:
        removed = session.execute(
            text(
                "DELETE FROM document WHERE id = :id AND superseded_at IS NOT NULL "
                "RETURNING id, collection"
            ),
            {"id": document_id},
        ).one_or_none()
        if removed is None:
            raise HTTPException(status_code=409, detail="Document state changed; retry purge.")
        run_id, _ = enqueue_run(
            session,
            ctx,
            workload=REINDEX,
            payload={"collection": removed.collection},
            idempotency_key=f"reindex:{removed.collection}:purge:{document_id}",
        )
        audit.append_in_session(
            session,
            ctx,
            action="document.purged",
            outcome=audit.Outcome.SUCCEEDED,
            resource_type="document",
            resource_id=str(document_id),
            detail={
                "collection": removed.collection,
                "object_removed": object_removed,
                "reindex_run_id": str(run_id),
                "irreversible": True,
            },
        )

    return {
        "id": str(document_id),
        "purged": True,
        "object_removed": object_removed,
        "reindex_run_id": str(run_id),
        "irreversible": True,
    }
