"""release tracking, per-revision observations, and out-of-process sessions

Revision ID: 0011
Revises: 0010
Create Date: 2026-08-12

## release / release_observation

A canary needs three things the Azure build has none of: a record of which
revisions exist and how traffic is split between them, per-revision outcome data
to compare, and a rollback that is an action rather than a redeploy.

`deploy.sh` there runs `az containerapp update --image` in the default
single-revision mode — no `--revision-mode multiple`, no traffic split anywhere
in the tree — so rollback means rebuilding the previous tag and waiting. Its own
Traps section prices this: two deploy cycles chasing a judge bug that was already
fixed, because a draining old replica served the run.

`release_observation` is deliberately narrow — revision, outcome, latency,
timestamp. It exists so the SLO comparison has a source that can be queried
transactionally in a test. Production would read the same numbers from
Prometheus; the comparison logic does not care, which is why it takes a source
rather than a connection.

## session

`_Singleton.sessions` in the Azure build is a plain dict on the replica, with
`max-replicas 3` and no session affinity configured. A user's second turn lands
on a different replica roughly two thirds of the time and finds an empty state.
That is the defect its users would actually notice, and it is a defect precisely
because the state is in the process.

Sessions here are rows. Any replica can serve any turn, which also means a
rolling deploy does not drop conversations mid-flight.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "platform_app"
READONLY_ROLE = "platform_readonly"
NEW_TENANT_SCOPED = ("session",)


def upgrade() -> None:
    # ── release ───────────────────────────────────────────────────────────
    # Platform-wide, not tenant-scoped: a revision serves every tenant. Readable
    # by the app so a request can stamp its own revision onto observations.
    op.create_table(
        "release",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("revision", sa.String(64), nullable=False, unique=True),
        sa.Column("image_tag", sa.String(128), nullable=False),
        # The migration head this revision expects. Recorded so a rollback can
        # tell whether the schema also has to move — the question the Azure
        # build cannot answer, since its tables are CREATE TABLE IF NOT EXISTS
        # executed at startup with no version anywhere.
        sa.Column("schema_version", sa.String(32), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="candidate"),
        # Percent of traffic. The two live revisions must sum to 100.
        sa.Column("traffic_weight", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("promoted_at", sa.DateTime(timezone=True)),
        sa.Column("rolled_back_at", sa.DateTime(timezone=True)),
        sa.Column("rollback_reason", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "status IN ('candidate','canary','active','rolled_back','retired')",
            name="release_status_valid",
        ),
        sa.CheckConstraint(
            "traffic_weight BETWEEN 0 AND 100", name="release_weight_range"
        ),
    )
    op.create_index("release_status_idx", "release", ["status"])

    op.create_table(
        "release_observation",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column("revision", sa.String(64), nullable=False),
        sa.Column("route", sa.String(200), nullable=False),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column("status_code", sa.Integer()),
        sa.Column("latency_ms", sa.Float(), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("outcome IN ('success','error')", name="release_observation_outcome_valid"),
    )
    # The SLO window query: outcomes for a revision since a timestamp.
    op.create_index("release_observation_window_idx", "release_observation", ["revision", "at"])

    # ── session ───────────────────────────────────────────────────────────
    op.create_table(
        "session",
        sa.Column("id", sa.String(128), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False),
        # Bound to the principal, not merely to a session id. In the Azure build
        # sessions are keyed `(domain, session_id)` with no user binding, and
        # `GET /api/sessions/{id}` has no auth dependency at all — so anyone who
        # can guess an id reads someone else's conversation.
        sa.Column("principal_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("principal.id", ondelete="CASCADE"), nullable=False),
        sa.Column("workload", sa.String(64), nullable=False),
        sa.Column("state", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("turn_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("session_tenant_principal_idx", "session", ["tenant_id", "principal_id"])
    op.create_index("session_expiry_idx", "session", ["expires_at"])

    op.execute("ALTER TABLE session ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE session FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY session_tenant_isolation ON session
            FOR ALL
            TO {APP_ROLE}, {READONLY_ROLE}
            USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
            WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
        """
    )

    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON session TO {APP_ROLE}")
    op.execute(f"GRANT SELECT ON session TO {READONLY_ROLE}")
    op.execute(f"GRANT SELECT ON release TO {APP_ROLE}, {READONLY_ROLE}")
    op.execute(f"GRANT SELECT, INSERT ON release_observation TO {APP_ROLE}")
    op.execute(f"GRANT SELECT ON release_observation TO {READONLY_ROLE}")
    # Releases are moved by the deploy path holding the owner credential, never
    # by a request. A compromised app credential must not be able to promote
    # itself a revision or shift traffic.
    op.execute(f"REVOKE INSERT, UPDATE, DELETE ON release FROM {APP_ROLE}")
    op.execute(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {APP_ROLE}")


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS session_tenant_isolation ON session")
    op.drop_index("session_expiry_idx", table_name="session")
    op.drop_index("session_tenant_principal_idx", table_name="session")
    op.drop_table("session")
    op.drop_index("release_observation_window_idx", table_name="release_observation")
    op.drop_table("release_observation")
    op.drop_index("release_status_idx", table_name="release")
    op.drop_table("release")
