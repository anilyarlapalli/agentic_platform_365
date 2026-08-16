"""Object storage: documents, artifacts, cassettes, evidence.

Keys are **derived**, never accepted. :meth:`ObjectStore.key_for` builds the
path from the tenant and a caller-supplied name, so a caller cannot address
another tenant's object by passing a crafted key. Path prefixing alone is a
convention; deriving the key in the port is what makes it a control.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

from platform_core.identity.principal import RequestContext


@dataclass(frozen=True, slots=True)
class StoredObject:
    key: str
    size_bytes: int
    content_type: str
    etag: str
    modified_at: datetime
    # Content hash, recorded so a caller can verify what it got is what was put
    # without trusting the store's own etag semantics, which vary by backend.
    sha256: str | None = None


@runtime_checkable
class ObjectStore(Protocol):
    def key_for(self, ctx: RequestContext, *parts: str) -> str:
        """Derive a tenant-scoped key. The only supported way to name an object."""
        ...

    def put(
        self,
        ctx: RequestContext,
        key: str,
        data: bytes,
        *,
        content_type: str = "application/octet-stream",
        if_absent: bool = False,
    ) -> StoredObject:
        """Store bytes.

        ``if_absent=True`` makes the write conditional, raising
        :class:`ConflictError` if the key exists. That is the primitive an
        idempotent side effect is built from: a retried step can attempt the
        write and treat the conflict as "already applied" rather than
        overwriting work a concurrent worker committed.
        """
        ...

    def get(self, ctx: RequestContext, key: str) -> bytes:
        """Fetch bytes. Raises :class:`NotFoundError` — including for another
        tenant's key, which must be indistinguishable from absent."""
        ...

    def head(self, ctx: RequestContext, key: str) -> StoredObject:
        ...

    def list(self, ctx: RequestContext, prefix: str = "") -> list[StoredObject]:
        """List within the tenant's scope. ``prefix`` is relative to it."""
        ...

    def delete(self, ctx: RequestContext, key: str) -> bool:
        """Returns False when the key was already absent. Idempotent by design."""
        ...
