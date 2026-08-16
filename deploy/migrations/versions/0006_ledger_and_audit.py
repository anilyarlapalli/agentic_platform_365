"""ledger and hash-chained audit

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-12

## llm_usage — every charge, attributed to a tenant, never nullable

The column that matters is `tenant_id NOT NULL`. In the Azure build the
equivalent is a free-text `domain` that defaults to the string `"unknown"`,
because `token_budget.set_context` is called on the ingest and eval paths only —
so chat and onboarding spend, the two the module docstring names as most
important, both bill to `"unknown"`. And since the ContextVar is never reset, a
worker that ran an ingest for one domain then bills a later onboarding step to
it.

Here there is no such value to default to. A charge without a tenant cannot be
written, so `tests/properties/test_cost_attribution.py` can assert that
unattributed spend is *exactly zero* and have that mean something.

## audit_event — append-only, hash-chained

Each row carries the digest of the previous row for its tenant. Tampering with
history requires rewriting every subsequent row, which makes silent alteration
detectable rather than merely discouraged.

Enforced by triggers, not by convention: `UPDATE` and `DELETE` are rejected at
the database. An audit log the application can quietly rewrite is a log that
proves nothing about the application.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NEW_TENANT_SCOPED = ("llm_usage", "audit_event")
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
    # ── llm_usage ─────────────────────────────────────────────────────────
    op.create_table(
        "llm_usage",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        # NOT NULL is the whole point. There is no "unknown" tenant to fall back
        # to, so an unattributable charge cannot be recorded at all.
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False),
        sa.Column("principal_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("principal.id", ondelete="SET NULL")),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("run.id", ondelete="SET NULL")),
        sa.Column("workload", sa.String(64), nullable=False),
        # 'chat' | 'query' | 'ingest' | 'onboard' | 'eval' — drives the per-task
        # budget policy decided on 2026-08-12: interactive fails open, background
        # fails closed.
        sa.Column("task", sa.String(32), nullable=False),
        sa.Column("model", sa.String(96), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "total_tokens", sa.Integer(),
            sa.Computed("input_tokens + output_tokens", persisted=True),
        ),
        sa.Column("cost_usd", sa.Numeric(12, 6), nullable=False, server_default="0"),
        sa.Column("cache_hit", sa.Boolean(), nullable=False, server_default="false"),
        # False when the provider returned no usage block. Surfaced rather than
        # coerced to zero: silent under-counting is indistinguishable from thrift
        # right up until the invoice.
        sa.Column("usage_reported", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("release", sa.String(64)),
        sa.Column("at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("input_tokens >= 0 AND output_tokens >= 0", name="llm_usage_tokens_nonneg"),
        sa.CheckConstraint("cost_usd >= 0", name="llm_usage_cost_nonneg"),
    )
    # The budget window query: spend for a tenant since a timestamp.
    op.create_index("llm_usage_tenant_at_idx", "llm_usage", ["tenant_id", "at"])
    op.create_index("llm_usage_task_idx", "llm_usage", ["tenant_id", "task", "at"])

    # ── audit_event ───────────────────────────────────────────────────────
    op.create_table(
        "audit_event",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False),
        sa.Column("principal_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("principal.id", ondelete="SET NULL")),
        sa.Column("actor_subject", sa.String(320), nullable=False),
        sa.Column("action", sa.String(96), nullable=False),
        sa.Column("resource_type", sa.String(64)),
        sa.Column("resource_id", sa.String(200)),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column("detail", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("request_id", postgresql.UUID(as_uuid=True)),
        sa.Column("run_id", postgresql.UUID(as_uuid=True)),
        sa.Column("release", sa.String(64)),
        sa.Column("at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        # The chain. `prev_hash` is the previous event's `hash` for this tenant;
        # `hash` covers this row's content and `prev_hash`. Per tenant rather
        # than global so one tenant's write rate cannot serialise every other
        # tenant's audit writes behind it.
        sa.Column("prev_hash", sa.String(64)),
        sa.Column("hash", sa.String(64), nullable=False),
        sa.CheckConstraint(
            "outcome IN ('allowed','denied','succeeded','failed')",
            name="audit_event_outcome_valid",
        ),
    )
    op.create_index("audit_event_tenant_at_idx", "audit_event", ["tenant_id", "at"])
    op.create_index("audit_event_chain_idx", "audit_event", ["tenant_id", "id"])

    # Append-only, enforced by the database.
    #
    # A trigger rather than a revoked grant: REVOKE UPDATE would stop the app
    # role but not the owner, and the point is that *nothing* rewrites history.
    # An audit log the application can quietly alter proves nothing about the
    # application.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION audit_event_append_only()
        RETURNS TRIGGER AS $$
        BEGIN
            RAISE EXCEPTION 'audit_event is append-only: % is not permitted', TG_OP
                USING ERRCODE = 'insufficient_privilege';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER audit_event_no_update
            BEFORE UPDATE OR DELETE ON audit_event
            FOR EACH ROW EXECUTE FUNCTION audit_event_append_only()
        """
    )

    for table in NEW_TENANT_SCOPED:
        _protect(table)

    op.execute(f"GRANT SELECT, INSERT ON {', '.join(NEW_TENANT_SCOPED)} TO {APP_ROLE}")
    op.execute(f"GRANT SELECT ON {', '.join(NEW_TENANT_SCOPED)} TO {READONLY_ROLE}")
    op.execute(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {APP_ROLE}")
    # No UPDATE or DELETE granted on either. The ledger and the audit log are
    # both append-only by intent; the trigger above makes it true for audit even
    # against a role that somehow acquired the grant.


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS audit_event_no_update ON audit_event")
    op.execute("DROP FUNCTION IF EXISTS audit_event_append_only()")
    for table in NEW_TENANT_SCOPED:
        op.execute(f"DROP POLICY IF EXISTS {table}_tenant_isolation ON {table}")
    op.drop_index("audit_event_chain_idx", table_name="audit_event")
    op.drop_index("audit_event_tenant_at_idx", table_name="audit_event")
    op.drop_table("audit_event")
    op.drop_index("llm_usage_task_idx", table_name="llm_usage")
    op.drop_index("llm_usage_tenant_at_idx", table_name="llm_usage")
    op.drop_table("llm_usage")
