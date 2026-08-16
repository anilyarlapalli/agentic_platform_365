"""capabilities: resource-scoped grants and the tool approval gate

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-12

Two tables, both tenant-scoped and both RLS-protected on the same terms as
everything in 0001.

``capability_grant`` is what lets authority be finer than a role without
inventing a parallel concept each time. The Azure build needed exactly this and
arrived at it as an exception — a per-domain reviewer table bolted on when
``admin`` proved too coarse for schema review.

``tool_approval`` is the gate that makes ``requires_approval`` on a tool mean
something. In the Azure build the tool registry declares ``side_effect`` and
``requires_approval`` correctly, but HITL was retired on 2026-06-27, so the flag
routes to a disabled gate: any write-side-effect tool registered today executes
with no human in the loop. Latent rather than live, because no write tools are
registered — but the durable checkpointer built to make that gate survive
replica loss is currently guarding nothing.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NEW_TENANT_SCOPED = ("capability_grant", "tool_approval")
APP_ROLE = "platform_app"
READONLY_ROLE = "platform_readonly"


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
        "capability_grant",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False),
        sa.Column("principal_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("principal.id", ondelete="CASCADE"), nullable=False),
        sa.Column("capability", sa.String(64), nullable=False),
        # '*' means tenant-wide. Scoping to a named resource is what makes
        # "reviewer for this collection only" expressible.
        sa.Column("resource", sa.String(200), nullable=False, server_default="*"),
        sa.Column("granted_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("principal.id", ondelete="SET NULL")),
        sa.Column("granted_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        # Soft revocation: the row stays so the audit trail keeps the history of
        # who held what and when. A deleted grant is an unanswerable question
        # during an investigation.
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("principal.id", ondelete="SET NULL")),
        sa.UniqueConstraint(
            "tenant_id", "principal_id", "capability", "resource",
            name="capability_grant_uniq",
        ),
    )
    op.create_index(
        "capability_grant_lookup_idx", "capability_grant",
        ["tenant_id", "principal_id", "capability"],
    )

    op.create_table(
        "tool_approval",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("run.id", ondelete="CASCADE"), nullable=False),
        sa.Column("tool_name", sa.String(128), nullable=False),
        sa.Column("side_effect", sa.String(16), nullable=False),
        # The exact arguments that were approved. Re-checked at execution
        # against a hash, so an approval cannot be replayed against different
        # arguments than the reviewer actually saw.
        sa.Column("arguments", postgresql.JSONB(), nullable=False),
        sa.Column("arguments_sha256", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("requested_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("principal.id", ondelete="SET NULL"), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("decided_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("principal.id", ondelete="SET NULL")),
        sa.Column("decided_at", sa.DateTime(timezone=True)),
        sa.Column("decision_note", sa.Text()),
        # A pending approval that nobody actions must not pin a run forever.
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        # Set when the approved call actually ran, so an approval is single-use.
        sa.Column("consumed_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "status IN ('pending','approved','rejected','expired')",
            name="tool_approval_status_valid",
        ),
        sa.CheckConstraint("side_effect IN ('none','write')", name="tool_approval_side_effect_valid"),
        # Maker-cannot-be-checker, enforced by the database rather than only by
        # application code. An approval whose decider is its requester cannot be
        # written at all, so no code path can bypass it.
        sa.CheckConstraint(
            "decided_by IS NULL OR decided_by <> requested_by",
            name="tool_approval_no_self_approval",
        ),
    )
    op.create_index("tool_approval_pending_idx", "tool_approval", ["tenant_id", "status"])
    op.create_index("tool_approval_run_idx", "tool_approval", ["run_id"])

    for table in NEW_TENANT_SCOPED:
        _protect(table)

    op.execute(
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON {', '.join(NEW_TENANT_SCOPED)} TO {APP_ROLE}"
    )
    op.execute(f"GRANT SELECT ON {', '.join(NEW_TENANT_SCOPED)} TO {READONLY_ROLE}")


def downgrade() -> None:
    for table in NEW_TENANT_SCOPED:
        op.execute(f"DROP POLICY IF EXISTS {table}_tenant_isolation ON {table}")
    op.drop_index("tool_approval_run_idx", table_name="tool_approval")
    op.drop_index("tool_approval_pending_idx", table_name="tool_approval")
    op.drop_table("tool_approval")
    op.drop_index("capability_grant_lookup_idx", table_name="capability_grant")
    op.drop_table("capability_grant")
