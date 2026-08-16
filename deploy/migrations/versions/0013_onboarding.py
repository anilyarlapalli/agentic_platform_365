"""onboarding: schema sessions and the artifacts that give a graph its edges

Revision ID: 0013
Revises: 0012
Create Date: 2026-08-13

Onboarding is the missing half of GraphRAG here. Entity extraction is
deterministic and works from the schema alone, but relation extraction reads a
*learned* instance table and predicate map produced by a prior onboarding run.
Without them ``CachedRelationExtractor`` is never constructed and every graph
builds with zero edges — answering exactly like a populated one, with no error
and worse retrieval.

Two tables:

``onboarding_session``
    One drafting run: sample the corpus, characterise it, synthesise a schema,
    then wait for a human. Long-lived and resumable, because the review gate is
    a human and the work before it costs real tokens.

``onboarding_artifact``
    The outputs, stored as rows rather than files.

## Why Postgres rather than object storage

The Azure build stages these to Blob and had to, because its worker scales to
zero with no mounted volume — a review that outlasted the 300s cooldown
destroyed the container holding the artifacts, and one domain published
``relations_available=false`` after drafting 543 instances, reported as success.

Here the constraint is different and so is the answer. This platform's isolation
boundary is row-level security, and it has no object-store adapter — only the
port. Putting artifacts in Postgres means they inherit the boundary that is
already enforced and already tested, instead of introducing a second tenancy
story in a new backend. They are also small: the failing Azure case was 543
instances and 24 predicates.

(Addendum, 2026-08-14: there *is* an object-store adapter now — Phase 10 added
one for document content. The decision above still stands and artifacts stay in
Postgres. The reasoning was never "there is nowhere else to put them"; it was
that RLS is one tested boundary and the store's is a second, application-level
one. Small structured artifacts do not need it.)

The durability lesson still applies and is why these are rows written as the
draft completes, not values held in memory until someone clicks approve.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NEW_TENANT_SCOPED = ("onboarding_session", "onboarding_artifact")
APP_ROLE = "platform_app"
READONLY_ROLE = "platform_readonly"

# The lifecycle. `drafting` and `failed` are distinct from `draft_ready` so a
# crashed run cannot be mistaken for one still working, which is the state the
# Azure build could not express and had to infer from a missing container.
STATUSES = ("drafting", "draft_ready", "approved", "published", "failed", "cancelled")

ARTIFACT_KINDS = ("schema", "instance_table", "predicate_map", "extraction_cache")


def _protect(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY {table}_tenant_isolation ON {table}
            FOR ALL
            TO {APP_ROLE}, {READONLY_ROLE}
            USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
            WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
        """
    )


def upgrade() -> None:
    op.create_table(
        "onboarding_session",
        sa.Column("id", postgresql.UUID(as_uuid=True),
                  server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False),
        # The schema domain being onboarded, e.g. 'manufacturing'. Lowercased by
        # the service; the engine lowercases its own path lookups.
        sa.Column("domain", sa.String(64), nullable=False),
        # Which collection the corpus was sampled from. A schema is only
        # meaningful against the corpus it was drafted from, so this is recorded
        # rather than assumed.
        sa.Column("collection", sa.String(128), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="drafting"),
        # Per-step progress from the orchestrator, so a long draft is legible
        # while it runs instead of being an opaque spinner.
        sa.Column("progress", postgresql.JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("stats", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("error", sa.Text),
        sa.Column("run_id", postgresql.UUID(as_uuid=True)),
        sa.Column("created_by", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("principal.id", ondelete="SET NULL")),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        # Maker/checker, same shape as tool_approval in 0002: the approver is
        # recorded and constrained to be someone other than the drafter.
        sa.Column("approved_by", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("principal.id", ondelete="SET NULL")),
        sa.Column("approved_at", sa.DateTime(timezone=True)),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "status IN " + str(STATUSES), name="onboarding_session_status_valid",
        ),
        # The structural half of maker-cannot-be-checker. The capability check
        # refuses self-approval independently; this makes it impossible to
        # record even if a future code path forgets to ask.
        sa.CheckConstraint(
            "approved_by IS NULL OR approved_by <> created_by",
            name="onboarding_session_no_self_approval",
        ),
        # An approval must carry its approver and vice versa — a half-written
        # decision is not a decision.
        sa.CheckConstraint(
            "(approved_by IS NULL) = (approved_at IS NULL)",
            name="onboarding_session_approval_complete",
        ),
    )
    op.create_index(
        "onboarding_session_lookup_idx", "onboarding_session",
        ["tenant_id", "domain", "status"],
    )

    op.create_table(
        "onboarding_artifact",
        sa.Column("id", postgresql.UUID(as_uuid=True),
                  server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False),
        sa.Column("session_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("onboarding_session.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        # For extraction_cache this is the per-chunk filename; for the singleton
        # kinds it is the kind again. Together with kind it identifies the file
        # the engine expects on disk.
        sa.Column("name", sa.String(256), nullable=False),
        sa.Column("payload", postgresql.JSONB, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "kind IN " + str(ARTIFACT_KINDS), name="onboarding_artifact_kind_valid",
        ),
        # Rewriting an artifact within a session is an upsert, not a second row.
        # Two rows for one filename would materialise non-deterministically.
        sa.UniqueConstraint("session_id", "kind", "name", name="onboarding_artifact_uniq"),
    )
    op.create_index(
        "onboarding_artifact_session_idx", "onboarding_artifact",
        ["tenant_id", "session_id", "kind"],
    )

    for table in NEW_TENANT_SCOPED:
        _protect(table)
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {APP_ROLE}")
        op.execute(f"GRANT SELECT ON {table} TO {READONLY_ROLE}")

    # At most one published session per domain. A partial unique index rather
    # than a status column elsewhere: "which taxonomy is live" must have exactly
    # one answer, and two published sessions would make the artifact
    # materialisation order-dependent.
    op.execute(
        "CREATE UNIQUE INDEX onboarding_session_one_published_idx "
        "ON onboarding_session (tenant_id, domain) WHERE status = 'published'"
    )


def downgrade() -> None:
    op.drop_index("onboarding_session_one_published_idx", table_name="onboarding_session")
    op.drop_table("onboarding_artifact")
    op.drop_table("onboarding_session")
