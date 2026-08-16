"""cooperative cancellation, delayed retries, fairness, and budget reservations

Revision ID: 0022
Revises: 0021
Create Date: 2026-08-15
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "platform_app"
READONLY_ROLE = "platform_readonly"


def upgrade() -> None:
    # `available_at` turns a transient failure into a delayed retry rather than
    # a hot loop. `last_enqueued_at` lets the sweeper distinguish a due retry
    # from a pointer that was already sent. Cancellation is a request while a
    # lease is live; the worker acknowledges it at a safe boundary.
    op.add_column(
        "run",
        sa.Column(
            "available_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.add_column("run", sa.Column("last_enqueued_at", sa.DateTime(timezone=True)))
    op.add_column("run", sa.Column("cancel_requested_at", sa.DateTime(timezone=True)))
    op.add_column(
        "run",
        sa.Column(
            "cancel_requested_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("principal.id", ondelete="SET NULL"),
        ),
    )
    op.add_column(
        "run",
        sa.Column("priority", sa.SmallInteger(), nullable=False, server_default="0"),
    )
    op.create_check_constraint("run_priority_bounded", "run", "priority BETWEEN -10 AND 10")
    op.create_index(
        "run_claimable_v2_idx",
        "run",
        ["status", "available_at", sa.text("priority DESC"), "created_at"],
    )

    # A pre-flight read is not a ceiling under concurrency: two callers can
    # both see the same remaining headroom and both spend it. Reservations are
    # inserted while holding a per-tenant transaction lock, then settled into
    # actual usage after the provider responds.
    op.create_table(
        "budget_reservation",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            primary_key=True,
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenant.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "principal_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("principal.id", ondelete="SET NULL"),
        ),
        sa.Column("request_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("task", sa.String(32), nullable=False),
        sa.Column("model", sa.String(96), nullable=False),
        sa.Column("estimated_tokens", sa.Integer(), nullable=False),
        sa.Column("estimated_cost_usd", sa.Numeric(12, 6), nullable=False),
        sa.Column("actual_tokens", sa.Integer()),
        sa.Column("actual_cost_usd", sa.Numeric(12, 6)),
        sa.Column("status", sa.String(16), nullable=False, server_default="reserved"),
        sa.Column("release_reason", sa.String(200)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("settled_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "estimated_tokens >= 0 AND estimated_cost_usd >= 0",
            name="budget_reservation_estimate_nonnegative",
        ),
        sa.CheckConstraint(
            "actual_tokens IS NULL OR actual_tokens >= 0",
            name="budget_reservation_actual_tokens_nonnegative",
        ),
        sa.CheckConstraint(
            "actual_cost_usd IS NULL OR actual_cost_usd >= 0",
            name="budget_reservation_actual_cost_nonnegative",
        ),
        sa.CheckConstraint(
            "status IN ('reserved','settled','released','expired')",
            name="budget_reservation_status_valid",
        ),
    )
    op.create_index(
        "budget_reservation_active_idx",
        "budget_reservation",
        ["tenant_id", "status", "expires_at"],
    )
    op.execute("ALTER TABLE budget_reservation ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE budget_reservation FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY budget_reservation_tenant_isolation ON budget_reservation
            FOR ALL TO {APP_ROLE}, {READONLY_ROLE}
            USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
            WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
        """
    )
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON budget_reservation TO {APP_ROLE}")
    op.execute(f"GRANT SELECT ON budget_reservation TO {READONLY_ROLE}")

    # The API must not carry the table-owner credential merely to change two
    # columns. This function owns exactly that operation and refuses a tenant id
    # other than the transaction's RLS scope.
    op.execute(
        """
        CREATE FUNCTION platform_set_tenant_budget_caps(
            p_tenant uuid, p_daily bigint, p_monthly numeric
        ) RETURNS TABLE(daily_token_cap bigint, monthly_cost_cap_usd numeric)
        LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            scoped_tenant uuid := NULLIF(current_setting('app.tenant_id', true), '')::uuid;
        BEGIN
            IF scoped_tenant IS NULL OR scoped_tenant <> p_tenant THEN
                RAISE EXCEPTION 'budget tenant does not match transaction scope'
                    USING ERRCODE = '42501';
            END IF;
            IF p_daily IS NOT NULL AND p_daily < 0 THEN
                RAISE EXCEPTION 'daily token cap cannot be negative';
            END IF;
            IF p_monthly IS NOT NULL AND p_monthly < 0 THEN
                RAISE EXCEPTION 'monthly cost cap cannot be negative';
            END IF;
            RETURN QUERY
            UPDATE public.tenant AS t
            SET daily_token_cap = coalesce(p_daily, t.daily_token_cap),
                monthly_cost_cap_usd = coalesce(p_monthly, t.monthly_cost_cap_usd)
            WHERE t.id = p_tenant
            RETURNING t.daily_token_cap, t.monthly_cost_cap_usd;
        END
        $$
        """
    )
    op.execute(
        "REVOKE ALL ON FUNCTION "
        "platform_set_tenant_budget_caps(uuid,bigint,numeric) FROM PUBLIC"
    )
    op.execute(
        f"GRANT EXECUTE ON FUNCTION "
        f"platform_set_tenant_budget_caps(uuid,bigint,numeric) TO {APP_ROLE}"
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS platform_set_tenant_budget_caps(uuid,bigint,numeric)")
    op.execute(
        "DROP POLICY IF EXISTS budget_reservation_tenant_isolation ON budget_reservation"
    )
    op.drop_index("budget_reservation_active_idx", table_name="budget_reservation")
    op.drop_table("budget_reservation")
    op.drop_index("run_claimable_v2_idx", table_name="run")
    op.drop_constraint("run_priority_bounded", "run", type_="check")
    op.drop_column("run", "priority")
    op.drop_column("run", "cancel_requested_by")
    op.drop_column("run", "cancel_requested_at")
    op.drop_column("run", "last_enqueued_at")
    op.drop_column("run", "available_at")
