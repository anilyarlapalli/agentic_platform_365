"""foundation: tenants, principals, documents, chunks, runs — all RLS-protected

Revision ID: 0001
Revises:
Create Date: 2026-08-12

The first migration establishes the isolation boundary, because a boundary added
later is a boundary that was absent for every row written before it.

Three things every tenant-scoped table gets, together, in one place:

  1. ``ENABLE ROW LEVEL SECURITY``  — turns policies on for non-owners.
  2. ``FORCE ROW LEVEL SECURITY``   — applies them to the owner too, so a future
                                      mistake that runs the app as the owner
                                      does not silently reopen the boundary.
  3. A policy keyed on ``current_setting('app.tenant_id', true)``.

The policy is written ``USING`` **and** ``WITH CHECK``. ``USING`` filters reads
and the pre-image of writes; ``WITH CHECK`` validates the post-image — the row
as it would exist after the statement. Without a correct post-image check a
tenant can INSERT rows carrying another tenant's id, or UPDATE its own row to
hand it to someone else: it cannot read the result, but it has written across
the boundary, and a cross-tenant write is worse than a cross-tenant read.

One subtlety worth stating, because it is easy to get backwards. For a
``FOR ALL`` policy, *omitting* ``WITH CHECK`` is safe — Postgres reuses the
``USING`` expression as the check. The dangerous form is an explicit permissive
clause such as ``WITH CHECK (true)``, which silently accepts any post-image.
``scripts/mutation_check.py`` mutates to that form specifically; an earlier
version of it mutated to the omitted form, found nothing, and was itself the
thing that was wrong.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Kept in lockstep with platform_core.db.models.TENANT_SCOPED_TABLES. The
# property test asserts the catalog matches that tuple, so a table added to one
# and not the other fails the build.
TENANT_SCOPED = ("principal", "document", "chunk", "run")

APP_ROLE = "platform_app"
READONLY_ROLE = "platform_readonly"


def _protect(table: str) -> None:
    """Apply the full isolation contract to one table."""
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")

    # NULLIF + the `true` argument to current_setting mean an unset context
    # yields NULL, and `tenant_id = NULL` is never true — so a query issued
    # outside a tenant session returns zero rows instead of every row.
    op.execute(
        f"""
        CREATE POLICY {table}_tenant_isolation ON {table}
            FOR ALL
            TO {APP_ROLE}, {READONLY_ROLE}
            USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
            WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
        """
    )


def _unprotect(table: str) -> None:
    op.execute(f"DROP POLICY IF EXISTS {table}_tenant_isolation ON {table}")
    op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")


def upgrade() -> None:
    # ── tenant ────────────────────────────────────────────────────────────
    # Not tenant-scoped: it *is* the scope. Readable by the app role so a
    # principal can resolve its own tenant during authentication, before any
    # tenant context exists to set.
    op.create_table(
        "tenant",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("slug", sa.String(64), nullable=False, unique=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("daily_token_cap", sa.BigInteger()),
        sa.Column("monthly_cost_cap_usd", sa.Float()),
        sa.CheckConstraint("slug ~ '^[a-z0-9][a-z0-9_-]{1,62}$'", name="tenant_slug_format"),
    )

    # ── principal ─────────────────────────────────────────────────────────
    op.create_table(
        "principal",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False),
        sa.Column("subject", sa.String(320), nullable=False),
        sa.Column("actor_type", sa.String(16), nullable=False, server_default="human"),
        sa.Column("roles", postgresql.ARRAY(sa.Text()), nullable=False, server_default="{}"),
        sa.Column("password_hash", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("disabled_at", sa.DateTime(timezone=True)),
        # Scoped, not global: a globally unique email would leak the existence
        # of an account in another tenant through a signup collision.
        sa.UniqueConstraint("tenant_id", "subject", name="principal_tenant_subject_uniq"),
    )
    op.create_index("principal_tenant_idx", "principal", ["tenant_id"])

    # ── document ──────────────────────────────────────────────────────────
    op.create_table(
        "document",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False),
        sa.Column("workload", sa.String(64), nullable=False),
        sa.Column("collection", sa.String(128), nullable=False),
        sa.Column("filename", sa.Text(), nullable=False),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column("byte_size", sa.BigInteger(), nullable=False),
        sa.Column("storage_key", sa.Text(), nullable=False),
        sa.Column("uploaded_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("principal.id", ondelete="SET NULL")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        # Same bytes into the same collection is one document. Deduplication
        # here is an idempotency guarantee, not a storage optimisation: it is
        # what makes a retried upload safe.
        sa.UniqueConstraint("tenant_id", "collection", "content_sha256", name="document_tenant_collection_sha_uniq"),
    )
    op.create_index("document_tenant_idx", "document", ["tenant_id"])

    # ── chunk ─────────────────────────────────────────────────────────────
    op.create_table(
        "chunk",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("document.id", ondelete="CASCADE"), nullable=False),
        sa.Column("collection", sa.String(128), nullable=False),
        sa.Column("canonical_id", sa.String(32), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("build_version", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("meta", postgresql.JSONB(), nullable=False, server_default="{}"),
        # 1536 = text-embedding-3-small. platform_core.settings refuses to start
        # if the configured model disagrees with this width, because a mismatch
        # is not a degraded result — it is a rejected write or a meaningless one.
        sa.Column("embedding", Vector(1536)),
        sa.Column("embedding_model", sa.String(64)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint(
            "tenant_id", "collection", "canonical_id", "build_version",
            name="chunk_tenant_collection_canonical_build_uniq",
        ),
    )
    op.create_index("chunk_tenant_collection_idx", "chunk", ["tenant_id", "collection"])

    # HNSW over cosine distance. Built now, while empty, so the first ingestion
    # does not pay for it — and so the index definition is part of the reviewed
    # schema rather than something applied by hand later on a live table.
    op.execute(
        """
        CREATE INDEX chunk_embedding_hnsw_idx ON chunk
            USING hnsw (embedding vector_cosine_ops)
            WITH (m = 16, ef_construction = 64)
        """
    )

    # ── run ───────────────────────────────────────────────────────────────
    op.create_table(
        "run",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False),
        sa.Column("workload", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("idempotency_key", sa.String(200)),
        sa.Column("requested_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("principal.id", ondelete="SET NULL")),
        sa.Column("input", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("result", postgresql.JSONB()),
        sa.Column("error", sa.Text()),
        sa.Column("leased_by", sa.String(128)),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("release", sa.String(64)),
        sa.Column("correlation_id", postgresql.UUID(as_uuid=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        # The idempotency control. Two requests with the same key in the same
        # tenant collapse to one run; the second gets the first's result.
        sa.UniqueConstraint("tenant_id", "idempotency_key", name="run_tenant_idempotency_uniq"),
        sa.CheckConstraint(
            "status IN ('pending','leased','succeeded','failed','cancelled')",
            name="run_status_valid",
        ),
        # A leased run must have both a holder and an expiry, or the reaper
        # cannot tell an active worker from a dead one.
        sa.CheckConstraint(
            "(status <> 'leased') OR (leased_by IS NOT NULL AND lease_expires_at IS NOT NULL)",
            name="run_lease_complete",
        ),
    )
    op.create_index("run_lease_idx", "run", ["status", "lease_expires_at"])
    op.create_index("run_tenant_created_idx", "run", ["tenant_id", "created_at"])

    # ── isolation ─────────────────────────────────────────────────────────
    for table in TENANT_SCOPED:
        _protect(table)

    # `tenant` itself is readable but never writable by the app: creating a
    # tenant is a platform operation, not a tenant operation. Without this, a
    # compromised app credential could mint itself a new isolation scope.
    op.execute(f"GRANT SELECT ON tenant TO {APP_ROLE}, {READONLY_ROLE}")
    op.execute(f"REVOKE INSERT, UPDATE, DELETE ON tenant FROM {APP_ROLE}")

    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {', '.join(TENANT_SCOPED)} TO {APP_ROLE}")
    op.execute(f"GRANT SELECT ON {', '.join(TENANT_SCOPED)} TO {READONLY_ROLE}")


def downgrade() -> None:
    # A real down path, exercised by tests/release/. A migration whose
    # downgrade has never run is a rollback plan nobody has tested.
    for table in TENANT_SCOPED:
        _unprotect(table)

    op.drop_index("run_tenant_created_idx", table_name="run")
    op.drop_index("run_lease_idx", table_name="run")
    op.drop_table("run")

    op.execute("DROP INDEX IF EXISTS chunk_embedding_hnsw_idx")
    op.drop_index("chunk_tenant_collection_idx", table_name="chunk")
    op.drop_table("chunk")

    op.drop_index("document_tenant_idx", table_name="document")
    op.drop_table("document")

    op.drop_index("principal_tenant_idx", table_name="principal")
    op.drop_table("principal")

    op.drop_table("tenant")
