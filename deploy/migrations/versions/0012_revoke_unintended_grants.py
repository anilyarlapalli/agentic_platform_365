"""revoke grants that ALTER DEFAULT PRIVILEGES handed out silently

Revision ID: 0012
Revises: 0011
Create Date: 2026-08-12

`00-roles.sql` sets:

    ALTER DEFAULT PRIVILEGES FOR ROLE platform_owner IN SCHEMA public
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO platform_app;

so **every** table the owner creates arrives with all four privileges for the
app role. Later migrations that write `GRANT SELECT, INSERT ON x TO
platform_app` are therefore additive and change nothing — the app already had
UPDATE and DELETE.

Two places state an intent that was never enforced:

* `0006` — "No UPDATE or DELETE granted on either. The ledger and the audit log
  are both append-only by intent." The audit log's immutability was in fact held
  entirely by the trigger; the ledger's by nothing at all.
* `0011` — `GRANT SELECT, INSERT ON release_observation` for the same reason.

Found by dumping `information_schema.role_table_grants` while documenting the
architecture, not by a test — which is why `test_privilege_model` now asserts the
grant matrix directly rather than trusting what a migration says it did.

Note what *did* work: `0011`'s `REVOKE INSERT, UPDATE, DELETE ON release` and
`0001`'s equivalent on `tenant` both held, because an explicit REVOKE overrides
the default. Stating a narrower grant does not; revoking the excess does.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "platform_app"

# Tables the application may append to but never alter or remove from.
APPEND_ONLY = ("audit_event", "llm_usage", "release_observation")


def upgrade() -> None:
    for table in APPEND_ONLY:
        op.execute(f"REVOKE UPDATE, DELETE ON {table} FROM {APP_ROLE}")

    # Stop the same thing happening to the next table added. The default now
    # grants only what an application legitimately needs on arrival; anything
    # more is an explicit decision in the migration that needs it.
    op.execute(
        f"ALTER DEFAULT PRIVILEGES FOR ROLE platform_owner IN SCHEMA public "
        f"REVOKE UPDATE, DELETE ON TABLES FROM {APP_ROLE}"
    )
    op.execute(
        f"ALTER DEFAULT PRIVILEGES FOR ROLE platform_owner IN SCHEMA public "
        f"GRANT SELECT, INSERT ON TABLES TO {APP_ROLE}"
    )

    # Restore the mutable tables explicitly. Listing them is the point: a table
    # the application can modify is now a stated decision rather than a default,
    # and the list is short enough to review.
    mutable = (
        "principal", "document", "chunk", "run", "capability_grant",
        "tool_approval", "outbox", "side_effect", "session",
        "eval_dataset", "eval_run", "eval_result", "eval_baseline",
    )
    op.execute(f"GRANT UPDATE, DELETE ON {', '.join(mutable)} TO {APP_ROLE}")


def downgrade() -> None:
    op.execute(
        f"ALTER DEFAULT PRIVILEGES FOR ROLE platform_owner IN SCHEMA public "
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {APP_ROLE}"
    )
    for table in APPEND_ONLY:
        op.execute(f"GRANT UPDATE, DELETE ON {table} TO {APP_ROLE}")
