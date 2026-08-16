"""durable agent checkpoints and tool execution receipts

Revision ID: 0021
Revises: 0020
Create Date: 2026-08-15
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "platform_app"
READONLY_ROLE = "platform_readonly"
TABLES = ("agent_checkpoint", "tool_execution")


def _protect(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY {table}_tenant_isolation ON {table}
            FOR ALL TO {APP_ROLE}, {READONLY_ROLE}
            USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
            WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
        """
    )


def upgrade() -> None:
    op.create_table(
        "agent_checkpoint",
        sa.Column("id", postgresql.UUID(as_uuid=True),
                  server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False),
        sa.Column("thread_id", sa.String(200), nullable=False),
        sa.Column("step", sa.BigInteger, nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("run.id", ondelete="CASCADE")),
        sa.Column("state", postgresql.JSONB, nullable=False),
        sa.Column("state_sha256", sa.String(64), nullable=False),
        sa.Column("awaiting", sa.String(128)),
        sa.Column("created_by", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("principal.id", ondelete="SET NULL")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.CheckConstraint("step >= 0", name="agent_checkpoint_step_nonnegative"),
        sa.UniqueConstraint("tenant_id", "thread_id", "step",
                            name="agent_checkpoint_thread_step_uniq"),
    )
    op.create_index(
        "agent_checkpoint_latest_idx",
        "agent_checkpoint",
        ["tenant_id", "thread_id", sa.text("step DESC")],
    )

    op.create_table(
        "tool_execution",
        sa.Column("id", postgresql.UUID(as_uuid=True),
                  server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("run.id", ondelete="CASCADE"), nullable=False),
        sa.Column("approval_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("tool_approval.id", ondelete="SET NULL")),
        sa.Column("tool_name", sa.String(128), nullable=False),
        sa.Column("side_effect", sa.String(16), nullable=False),
        sa.Column("arguments_sha256", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(200), nullable=False),
        sa.Column("requested_by", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("principal.id", ondelete="SET NULL")),
        sa.Column("status", sa.String(24), nullable=False, server_default="started"),
        sa.Column("result", postgresql.JSONB),
        sa.Column("result_sha256", sa.String(64)),
        sa.Column("error", sa.Text),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("side_effect IN ('none','write')",
                           name="tool_execution_side_effect_valid"),
        sa.CheckConstraint(
            "status IN ('started','succeeded','failed','needs_reconciliation')",
            name="tool_execution_status_valid",
        ),
        sa.UniqueConstraint("tenant_id", "idempotency_key",
                            name="tool_execution_idempotency_uniq"),
    )
    op.create_index(
        "tool_execution_run_idx", "tool_execution", ["tenant_id", "run_id", "started_at"]
    )

    for table in TABLES:
        _protect(table)
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {', '.join(TABLES)} TO {APP_ROLE}")
    op.execute(f"GRANT SELECT ON {', '.join(TABLES)} TO {READONLY_ROLE}")


def downgrade() -> None:
    for table in reversed(TABLES):
        op.execute(f"DROP POLICY IF EXISTS {table}_tenant_isolation ON {table}")
    op.drop_index("tool_execution_run_idx", table_name="tool_execution")
    op.drop_table("tool_execution")
    op.drop_index("agent_checkpoint_latest_idx", table_name="agent_checkpoint")
    op.drop_table("agent_checkpoint")
