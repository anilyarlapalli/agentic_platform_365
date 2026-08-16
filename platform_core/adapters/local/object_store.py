"""The ``ObjectStore`` port over S3 (MinIO locally).

## The key is derived, and then checked again

:meth:`key_for` builds ``t/<tenant_id>/<parts…>`` from the request context. That
alone is a convention: nothing stops a caller passing a key it built by hand, or
one it read out of a ``document`` row written by different code a year earlier.
So every operation re-derives the caller's prefix and refuses a key outside it.
The derivation is the ergonomics; the check is the control.

The refusal is :class:`NotFoundError`, never a distinct "forbidden". Telling a
caller that a key exists but belongs to someone else is an existence oracle —
the ``document`` row's ``storage_key`` embeds a content hash, so confirming one
is confirming that another tenant holds that exact file.

**Tenant id, not slug.** A slug is a display name and can be changed; keys must
outlive that. Worse, a renamed tenant frees its slug for the next one, which
would inherit the objects — a tenancy bug that would look like a naming bug.

## if_absent is atomic, not check-then-write

``put(if_absent=True)`` sends ``If-None-Match: *`` and lets the *store* refuse.
Verified against the pinned MinIO release: a second put returns 412 and the
original bytes survive. A ``head`` followed by a ``put`` would look identical in
the happy path and lose the race under the concurrency it exists to handle,
which is the shape of bug that only shows up once there are two workers.

## What counts as transient

Only transport faults and 5xx/throttle responses map to
:class:`TransientError`. A 403 from a wrong credential is deterministic: retrying
it turns one misconfiguration into an infinite redelivery loop, which is the
failure ``ports/errors.py`` describes.
"""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import UTC, datetime
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from platform_core.identity.principal import RequestContext
from platform_core.ports.errors import ConflictError, NotFoundError, TransientError
from platform_core.ports.object_store import StoredObject
from platform_core.settings import get_settings

logger = logging.getLogger("platform.adapters.object_store")

# Every tenant's objects live under this root, so a future non-tenant prefix
# (evidence, cassettes) cannot collide with a tenant id by accident.
TENANT_ROOT = "t"

# Deliberately strict. A key part is a name, not a path: no separators, no
# traversal, no leading dot, nothing that a downstream tool might expand. The
# suffixes callers actually need (`.md`, `-v2`, a sha256) all pass.
_SAFE_PART = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,255}$")

# Retried by botocore itself before it ever reaches us; listed so the mapping
# below is explicit about what it considers worth another attempt.
_TRANSIENT_CODES = frozenset(
    {"InternalError", "ServiceUnavailable", "SlowDown", "RequestTimeout", "503", "500"}
)


class KeyRejected(ValueError):
    """A caller supplied a key part that cannot be made into a safe key.

    Distinct from :class:`NotFoundError`: this is a *programming* error at the
    call site, caught before any request is made, and it must not be confused
    with the runtime refusal of a well-formed key belonging to another tenant.
    """


class S3ObjectStore:
    """Implements :class:`platform_core.ports.object_store.ObjectStore`."""

    def __init__(self, *, bucket: str | None = None, client: Any | None = None) -> None:
        settings = get_settings()
        self._bucket = bucket or settings.s3_bucket
        self._client = client or boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint_url,
            aws_access_key_id=settings.s3_access_key.get_secret_value(),
            aws_secret_access_key=settings.s3_secret_key.get_secret_value(),
            region_name=settings.s3_region,
            config=Config(
                signature_version="s3v4",
                # Path style because MinIO is addressed by host:port; virtual
                # host style would resolve `bucket.127.0.0.1` and fail.
                s3={"addressing_style": "path"},
                retries={"max_attempts": 3, "mode": "standard"},
                connect_timeout=5,
                read_timeout=30,
            ),
        )

    # ── keys ──────────────────────────────────────────────────────────────

    def _prefix(self, ctx: RequestContext) -> str:
        return f"{TENANT_ROOT}/{ctx.tenant.id}/"

    def key_for(self, ctx: RequestContext, *parts: str) -> str:
        """Derive a tenant-scoped key. The only supported way to name an object."""
        if not parts:
            raise KeyRejected("a key needs at least one part")
        for part in parts:
            if not _SAFE_PART.match(part):
                # Refusing here rather than sanitising: silently rewriting
                # `../other` into `other` would produce a key the caller did not
                # ask for and cannot predict, and two different inputs would
                # collapse onto one object.
                raise KeyRejected(
                    f"key part {part!r} is not a safe name — letters, digits, "
                    f"dot, dash and underscore only, and it may not start with a dot"
                )
        return self._prefix(ctx) + "/".join(parts)

    def _checked(self, ctx: RequestContext, key: str) -> str:
        """The key, if it belongs to this tenant. Otherwise indistinguishable
        from absent.

        This is the control the module docstring describes. It runs on every
        operation — including ``delete``, where skipping it would let one tenant
        destroy another's object while learning nothing, which is worse than a
        read leak, not better.
        """
        prefix = self._prefix(ctx)
        if not key.startswith(prefix) or ".." in key:
            logger.warning(
                "refused key outside tenant scope: tenant=%s key=%r",
                ctx.tenant.slug, key[:200],
            )
            raise NotFoundError("object not found")
        return key

    # ── operations ────────────────────────────────────────────────────────

    def put(
        self,
        ctx: RequestContext,
        key: str,
        data: bytes,
        *,
        content_type: str = "application/octet-stream",
        if_absent: bool = False,
    ) -> StoredObject:
        checked = self._checked(ctx, key)
        sha256 = hashlib.sha256(data).hexdigest()

        kwargs: dict[str, Any] = {
            "Bucket": self._bucket,
            "Key": checked,
            "Body": data,
            "ContentType": content_type,
            # Recorded on the object so ownership and integrity survive without
            # the database — the pair that lets a recovery answer "whose is this
            # and is it intact" from the bucket alone.
            "Metadata": {"sha256": sha256, "tenant": str(ctx.tenant.id)},
        }
        if if_absent:
            kwargs["IfNoneMatch"] = "*"

        try:
            response = self._client.put_object(**kwargs)
        except ClientError as exc:
            code = _error_code(exc)
            if code in ("PreconditionFailed", "412"):
                raise ConflictError(f"object already exists at {key}", cause=exc) from exc
            raise _translate(exc, f"put {key}") from exc
        except BotoCoreError as exc:
            raise TransientError(f"object store unreachable on put {key}", cause=exc) from exc

        return StoredObject(
            key=checked,
            size_bytes=len(data),
            content_type=content_type,
            etag=str(response.get("ETag", "")).strip('"'),
            modified_at=datetime.now(UTC),
            sha256=sha256,
        )

    def get(self, ctx: RequestContext, key: str) -> bytes:
        checked = self._checked(ctx, key)
        try:
            response = self._client.get_object(Bucket=self._bucket, Key=checked)
            return response["Body"].read()
        except ClientError as exc:
            raise _translate(exc, f"get {key}") from exc
        except BotoCoreError as exc:
            raise TransientError(f"object store unreachable on get {key}", cause=exc) from exc

    def head(self, ctx: RequestContext, key: str) -> StoredObject:
        checked = self._checked(ctx, key)
        try:
            response = self._client.head_object(Bucket=self._bucket, Key=checked)
        except ClientError as exc:
            raise _translate(exc, f"head {key}") from exc
        except BotoCoreError as exc:
            raise TransientError(f"object store unreachable on head {key}", cause=exc) from exc

        return StoredObject(
            key=checked,
            size_bytes=int(response.get("ContentLength", 0)),
            content_type=str(response.get("ContentType", "application/octet-stream")),
            etag=str(response.get("ETag", "")).strip('"'),
            modified_at=response.get("LastModified") or datetime.now(UTC),
            sha256=(response.get("Metadata") or {}).get("sha256"),
        )

    def list(self, ctx: RequestContext, prefix: str = "") -> list[StoredObject]:
        """List within the tenant's scope. ``prefix`` is relative to it.

        Relative, so a caller cannot list the bucket by passing ``""`` with a
        crafted absolute prefix — the tenant root is prepended here and the
        caller's fragment can only ever narrow it.
        """
        if ".." in prefix or prefix.startswith("/"):
            raise NotFoundError("object not found")

        scoped = self._prefix(ctx) + prefix
        out: list[StoredObject] = []
        try:
            paginator = self._client.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=self._bucket, Prefix=scoped):
                for item in page.get("Contents", []):
                    out.append(
                        StoredObject(
                            key=item["Key"],
                            size_bytes=int(item.get("Size", 0)),
                            content_type="application/octet-stream",
                            etag=str(item.get("ETag", "")).strip('"'),
                            modified_at=item.get("LastModified") or datetime.now(UTC),
                        )
                    )
        except ClientError as exc:
            if _error_code(exc) == "NoSuchBucket":
                return []
            raise _translate(exc, f"list {prefix}") from exc
        except BotoCoreError as exc:
            raise TransientError("object store unreachable on list", cause=exc) from exc
        return out

    def delete(self, ctx: RequestContext, key: str) -> bool:
        """Returns False when the key was already absent. Idempotent by design.

        S3 ``DeleteObject`` succeeds on an absent key, so the answer comes from a
        ``head`` first. That is not a race worth guarding: two concurrent deletes
        both removing the object is the intended outcome, and the boolean only
        reports which one observed it present.
        """
        checked = self._checked(ctx, key)
        try:
            self._client.head_object(Bucket=self._bucket, Key=checked)
            existed = True
        except ClientError as exc:
            if _error_code(exc) in ("404", "NoSuchKey", "NotFound"):
                existed = False
            else:
                raise _translate(exc, f"delete {key}") from exc
        except BotoCoreError as exc:
            raise TransientError(f"object store unreachable on delete {key}", cause=exc) from exc

        if existed:
            try:
                self._client.delete_object(Bucket=self._bucket, Key=checked)
            except ClientError as exc:
                raise _translate(exc, f"delete {key}") from exc
            except BotoCoreError as exc:
                raise TransientError(
                    f"object store unreachable on delete {key}", cause=exc
                ) from exc
        return existed

    # ── bootstrap ─────────────────────────────────────────────────────────

    def ensure_bucket(self) -> bool:
        """Create the bucket if absent. Returns True if this call created it.

        Explicit rather than lazy on first write. A per-request "create if
        missing" would make every upload depend on a privilege the runtime
        should not need, and would mask a misconfigured endpoint as a working
        one right up until the bucket policy mattered.
        """
        try:
            self._client.head_bucket(Bucket=self._bucket)
            return False
        except ClientError as exc:
            if _error_code(exc) not in ("404", "NoSuchBucket", "NotFound"):
                raise _translate(exc, f"head bucket {self._bucket}") from exc
        except BotoCoreError as exc:
            raise TransientError("object store unreachable on head bucket", cause=exc) from exc

        try:
            self._client.create_bucket(Bucket=self._bucket)
        except ClientError as exc:
            # Another process won the race. That is the intended outcome.
            if _error_code(exc) in ("BucketAlreadyOwnedByYou", "BucketAlreadyExists"):
                return False
            raise _translate(exc, f"create bucket {self._bucket}") from exc
        logger.info("created object store bucket %s", self._bucket)
        return True

    def ready(self) -> None:
        """Verify endpoint, credentials, and bucket access without mutating state."""
        try:
            self._client.head_bucket(Bucket=self._bucket)
        except ClientError as exc:
            raise _translate(exc, f"head bucket {self._bucket}") from exc
        except BotoCoreError as exc:
            raise TransientError("object store unreachable on readiness", cause=exc) from exc


def _error_code(exc: ClientError) -> str:
    error = exc.response.get("Error", {})
    code = str(error.get("Code", ""))
    if code:
        return code
    return str(exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", ""))


def _translate(exc: ClientError, what: str) -> Exception:
    """Map an S3 error onto a port error.

    ``NoSuchBucket`` becomes :class:`NotFoundError` rather than something
    louder on purpose: to a caller asking for one object, a missing bucket and a
    missing object are the same answer, and the bootstrap path is where a
    missing bucket is supposed to be noticed.
    """
    code = _error_code(exc)
    status = int(exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0) or 0)

    if code in ("404", "NoSuchKey", "NotFound", "NoSuchBucket"):
        return NotFoundError("object not found", cause=exc)
    if code in _TRANSIENT_CODES or status >= 500 or status == 429:
        return TransientError(f"object store failed on {what}", cause=exc)
    # Everything else — 403, malformed request, wrong credentials — is
    # deterministic. Retrying it is how one bad configuration becomes an
    # infinite loop.
    return RuntimeError(f"object store rejected {what}: {code}")


_STORE: S3ObjectStore | None = None


def get_object_store() -> S3ObjectStore:
    """Process-wide client. boto3 clients are thread-safe; sessions are not."""
    global _STORE
    if _STORE is None:
        _STORE = S3ObjectStore()
    return _STORE
