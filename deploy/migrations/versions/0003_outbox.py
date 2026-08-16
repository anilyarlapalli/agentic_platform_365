"""outbox: transactional intent, leases, and idempotent side effects

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-12

Three tables that together make "recoverable under failure" a property rather
than a hope.

## outbox — intent committed with the state change

The Azure build enqueues after inserting the job row. Two systems, two writes,
no transaction between them: a crash in the gap leaves a `queued` row nobody
will ever deliver, and its own docstring acknowledges this by describing the row
as "a visible `queued` row rather than an upload that silently vanished". Visible
is better than silent. It is still stuck.

Here the state change and the intent to publish commit **together**, in one
Postgres transaction. A relay then moves rows to the broker at-least-once. The
gap moves from "between two systems" to "between a committed row and its
delivery", and the second is recoverable by re-reading the table.

## side_effect — the idempotency ledger

The failure this closes, from `worker.py`: artifacts are published to Blob, then
`jobs.finish` marks the job succeeded. A crash between them leaves the manifest
version bumped — so every API replica reloads it — while the job sits `running`
forever. On redelivery `claim()` returns False, the handler returns normally,
and the message is deleted. The work is orphaned with nothing left to retry it.

Recording each effect under `(run_id, step)` before applying it makes a retried
step *ask* whether it already happened rather than assume. Combined with leases,
a crash at any point is resumable.

## Leases live on `run` (added here, not in 0001)

`0001` shipped `leased_by` / `lease_expires_at` on `run` already. This migration
adds the index the reaper needs and the heartbeat column, which is an example of
the expand/contract discipline the release phase will lean on: additive columns
first, backfill, then constrain.

## The cross-tenant escape hatch is a ROLE, not a flag

The relay and the reaper are genuinely cross-tenant — one relay drains every
tenant's outbox. The first version of this migration expressed that as a policy
keyed on a session variable: any connection that set `app.system_reason` and no
tenant could read across the boundary.

That was wrong, and measurably so. `system_session()` is also used by the
readiness probe and by the authentication path's tenant lookup, so the moment
those policies existed, a `system_session` opened by **login** could see every
tenant's runs. Verified before reverting: it returned 2 rows across 2 tenants.

The defect is not the policy expression, it is the shape. A privilege boundary
must be something a process either **holds or does not hold** — a credential —
not a string any caller can choose to set. So cross-tenant access is now granted
to a dedicated `platform_relay` login role, and only the relay process is
configured with that DSN. The API cannot assume it, because it does not have the
password.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NEW_TENANT_SCOPED = ("outbox", "side_effect")
APP_ROLE = "platform_app"
READONLY_ROLE = "platform_readonly"
RELAY_ROLE = "platform_relay"

# Exactly the tables the relay and reaper need, and no others. The relay moves
# outbox rows and returns expired leases; it has no business reading documents,
# chunks or principals, so it cannot.
RELAY_TABLES = ("outbox", "side_effect", "run")


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


def _relay_access(table: str) -> None:
    """Cross-tenant access for the relay role only.

    Keyed on the *role*, not on a settable session variable. The API process
    connects as `platform_app` and has no way to become `platform_relay`, so no
    code path in it — however it is called, whatever string it passes — can read
    across the boundary.
    """
    op.execute(
        f"""
        CREATE POLICY {table}_relay_access ON {table}
            FOR ALL
            TO {RELAY_ROLE}
            USING (true)
            WITH CHECK (true)
        """
    )


def upgrade() -> None:
    # ── the relay credential ──────────────────────────────────────────────
    # Created here rather than in the container's init script so that the role
    # and the policies that reference it arrive in the same reviewed change,
    # and so an existing database gets it without being recreated.
    op.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{RELAY_ROLE}') THEN
                CREATE ROLE {RELAY_ROLE} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
                    NOBYPASSRLS PASSWORD 'platform_dev_only';
            END IF;
        END
        $$
        """
    )
    op.execute(f"GRANT CONNECT ON DATABASE platform TO {RELAY_ROLE}")
    op.execute(f"GRANT USAGE ON SCHEMA public TO {RELAY_ROLE}")

    # ── outbox ────────────────────────────────────────────────────────────
    op.create_table(
        "outbox",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("run.id", ondelete="CASCADE"), nullable=False),
        sa.Column("workload", sa.String(64), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        # W3C traceparent captured at write time, so the span that caused the
        # work and the span that performs it join up even though minutes and a
        # process boundary separate them.
        sa.Column("trace_context", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.Column("publish_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text()),
        # BIGSERIAL ordering gives the relay a stable cursor. Ordering by
        # created_at would be wrong: two rows can share a timestamp, and the
        # clock can move backwards.
    )
    # Partial index: the relay only ever scans unpublished rows, and a partial
    # index keeps that scan proportional to the backlog rather than to history.
    op.execute(
        "CREATE INDEX outbox_unpublished_idx ON outbox (id) WHERE published_at IS NULL"
    )
    op.create_index("outbox_run_idx", "outbox", ["run_id"])

    # ── side_effect ───────────────────────────────────────────────────────
    op.create_table(
        "side_effect",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("run.id", ondelete="CASCADE"), nullable=False),
        # The idempotency key for one unit of external work.
        sa.Column("step", sa.String(128), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="started"),
        # What the effect produced, so a retry can return the original result
        # instead of redoing work that cannot be undone.
        sa.Column("result", postgresql.JSONB()),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("error", sa.Text()),
        # One row per (run, step). The INSERT is the claim: a second worker
        # attempting the same step gets a unique violation rather than a second
        # execution. This is the whole mechanism.
        sa.UniqueConstraint("run_id", "step", name="side_effect_run_step_uniq"),
        sa.CheckConstraint(
            "status IN ('started','completed','failed')", name="side_effect_status_valid"
        ),
    )
    op.create_index("side_effect_tenant_idx", "side_effect", ["tenant_id"])

    # ── leases ────────────────────────────────────────────────────────────
    # Expand: add the heartbeat column additively. Nothing reads it yet, so this
    # is safe to deploy beside a running previous revision — the property the
    # release phase depends on.
    op.add_column("run", sa.Column("last_heartbeat_at", sa.DateTime(timezone=True)))

    # The reaper's query: expired leases, oldest first. Partial, because only
    # leased rows are ever candidates.
    op.execute(
        "CREATE INDEX run_expired_lease_idx ON run (lease_expires_at) "
        "WHERE status = 'leased'"
    )
    # Claim query: the next pending run for a workload.
    op.execute(
        "CREATE INDEX run_claimable_idx ON run (workload, created_at) "
        "WHERE status = 'pending'"
    )

    for table in NEW_TENANT_SCOPED:
        _protect(table)

    op.execute(
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON {', '.join(NEW_TENANT_SCOPED)} TO {APP_ROLE}"
    )
    op.execute(f"GRANT SELECT ON {', '.join(NEW_TENANT_SCOPED)} TO {READONLY_ROLE}")
    op.execute(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {APP_ROLE}")

    # ── relay access: role-scoped, three tables, nothing else ─────────────
    for table in RELAY_TABLES:
        _relay_access(table)
    op.execute(
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON {', '.join(RELAY_TABLES)} TO {RELAY_ROLE}"
    )
    op.execute(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {RELAY_ROLE}")


def downgrade() -> None:
    for table in RELAY_TABLES:
        op.execute(f"DROP POLICY IF EXISTS {table}_relay_access ON {table}")
    for table in NEW_TENANT_SCOPED:
        op.execute(f"DROP POLICY IF EXISTS {table}_tenant_isolation ON {table}")

    # Revoke before dropping tables, so the role has no dependent grants left.
    # The role itself is left in place: dropping a login role that another
    # database object might still reference turns a downgrade into a failure,
    # and an unused role with no grants is inert.
    op.execute(
        f"REVOKE ALL ON {', '.join(RELAY_TABLES)} FROM {RELAY_ROLE}"
    )

    op.execute("DROP INDEX IF EXISTS run_claimable_idx")
    op.execute("DROP INDEX IF EXISTS run_expired_lease_idx")
    op.drop_column("run", "last_heartbeat_at")

    op.drop_index("side_effect_tenant_idx", table_name="side_effect")
    op.drop_table("side_effect")
    op.execute("DROP INDEX IF EXISTS outbox_unpublished_idx")
    op.drop_index("outbox_run_idx", table_name="outbox")
    op.drop_table("outbox")
