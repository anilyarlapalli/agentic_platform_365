"""The echo workload — trivial logic, real side effects.

This exists so the platform's correctness properties are tested against
something that is **not** the RAG. If leases, idempotency and recovery only hold
for GraphRAG, they were never platform properties; they were RAG features.

The logic is deliberately meaningless — it uppercases a string. What is not
meaningless is the *shape*: three ordered steps, each an externally-visible side
effect, with a natural-idempotency difference between them that the retry policy
has to express.

    reserve   → INSERT a document row.       Safe to repeat: content-addressed,
                                             ON CONFLICT collapses duplicates.
    transform → write the result to storage. Safe to repeat: same key, same bytes.
    announce  → append to an append-only     NOT safe to repeat: appending twice
                notification log.            produces two notifications.

That third step is the interesting one. It is the class of effect the Azure
build's publish-then-finish sequence cannot handle at all: if the process dies
after the effect and before the record of it, a naive retry doubles it. Here it
is declared ``NEEDS_RECONCILIATION``, so a retry raises instead of silently
sending twice.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any

from sqlalchemy import text

from platform_core.correctness.crash_points import maybe_crash
from platform_core.correctness.side_effects import RetryPolicy, perform_once
from platform_core.db.engine import tenant_session
from platform_core.identity.principal import RequestContext

logger = logging.getLogger("platform.workloads.echo")

WORKLOAD = "echo"

# The ordered steps, exported so the chaos harness can enumerate every boundary
# to crash at rather than guessing which ones matter.
STEPS = ("reserve", "transform", "announce")


def run(ctx: RequestContext, payload: dict[str, Any]) -> dict[str, Any]:
    """Execute the workload. Safe to call again on the same ``ctx.run_id``."""
    message = str(payload.get("message", ""))
    if not message:
        raise ValueError("echo workload requires a non-empty 'message'")

    digest = hashlib.sha256(message.encode()).hexdigest()

    # ── step 1: reserve ───────────────────────────────────────────────────
    maybe_crash("before:reserve")
    reserved = perform_once(
        ctx, "reserve",
        lambda: _reserve(ctx, message, digest),
        retry_policy=RetryPolicy.SAFE_TO_REPEAT,
    )
    maybe_crash("after:reserve")

    # ── step 2: transform ─────────────────────────────────────────────────
    maybe_crash("before:transform")
    transformed = perform_once(
        ctx, "transform",
        lambda: _transform(ctx, message, digest),
        retry_policy=RetryPolicy.SAFE_TO_REPEAT,
    )
    maybe_crash("after:transform")

    # ── step 3: announce ──────────────────────────────────────────────────
    # NEEDS_RECONCILIATION: a second append is a second notification, and there
    # is no key that would collapse them.
    maybe_crash("before:announce")
    announced = perform_once(
        ctx, "announce",
        lambda: _announce(ctx, digest, transformed.result["output"]),
        retry_policy=RetryPolicy.NEEDS_RECONCILIATION,
    )
    maybe_crash("after:announce")

    return {
        "output": transformed.result["output"],
        "digest": digest,
        "document_id": reserved.result["document_id"],
        "notification_id": announced.result["notification_id"],
        "repeated_steps": [
            name
            for name, outcome in (
                ("reserve", reserved), ("transform", transformed), ("announce", announced)
            )
            if outcome.repeated
        ],
    }


def _reserve(ctx: RequestContext, message: str, digest: str) -> dict[str, Any]:
    """Register a document row. Idempotent at the far end via ON CONFLICT."""
    with tenant_session(ctx.tenant) as s:
        document_id = s.execute(
            text(
                "INSERT INTO document (tenant_id, workload, collection, filename, "
                "  content_sha256, byte_size, storage_key, uploaded_by) "
                "VALUES (:t, 'echo', 'echo', :fn, :sha, :size, :key, :by) "
                # Identity is the filename since 0016, and the index is partial,
                # so the inference has to repeat its WHERE clause. The filename
                # is derived from the digest here, so a repeated message still
                # resolves to one row — the idempotency this step relies on is
                # unchanged.
                "ON CONFLICT (tenant_id, collection, filename) "
                "  WHERE superseded_at IS NULL DO UPDATE "
                "  SET content_sha256 = EXCLUDED.content_sha256 "
                "RETURNING id"
            ),
            {
                "t": ctx.tenant.id, "fn": f"{digest[:12]}.txt", "sha": digest,
                "size": len(message.encode()), "key": f"echo/{digest}.txt",
                "by": ctx.principal.id,
            },
        ).scalar_one()
    return {"document_id": str(document_id)}


def _transform(ctx: RequestContext, message: str, digest: str) -> dict[str, Any]:
    """The 'work'. Deterministic, so a repeat produces identical bytes."""
    return {"output": message.upper(), "key": f"echo/{digest}.out"}


def _announce(ctx: RequestContext, digest: str, output: str) -> dict[str, Any]:
    """Append to the notification log. **Not** idempotent at the far end.

    A chunk row stands in for an append-only notification here: it has no
    natural key that would collapse a duplicate, so appending twice genuinely
    produces two records. That is what makes it a useful test of the retry
    policy rather than a formality.
    """
    with tenant_session(ctx.tenant) as s:
        notification_id = s.execute(
            text(
                "INSERT INTO chunk (tenant_id, document_id, collection, canonical_id, "
                "  ordinal, text, build_version) "
                "SELECT :t, d.id, 'echo-notifications', :cid, "
                "  coalesce(max(c.ordinal), -1) + 1, :text, 1 "
                "FROM document d LEFT JOIN chunk c "
                "  ON c.tenant_id = d.tenant_id AND c.collection = 'echo-notifications' "
                "WHERE d.tenant_id = :t AND d.content_sha256 = :sha "
                "GROUP BY d.id "
                "RETURNING id"
            ),
            {
                "t": ctx.tenant.id,
                # Unique per run, so a genuine duplicate announcement is a
                # constraint violation rather than a silently accepted second row.
                "cid": f"c_{str(ctx.run_id).replace('-', '')[:14]}",
                "text": output[:2000], "sha": digest,
            },
        ).scalar_one()
    return {"notification_id": str(notification_id)}
