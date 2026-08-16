"""relay may read tenant identity, and only read it

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-12

`0003` granted the relay role its three tenant-scoped tables and nothing else,
which was the right instinct and one table short. The relay and the Celery task
handler both have to turn a `tenant_id` from an outbox row into a `Tenant` —
`(id, slug)` — before they can open a tenant-scoped session at all. Without it
the end-to-end transport failed with `permission denied for table tenant`.

Deliberately **SELECT only**. The relay must never be able to write this table:

* creating a tenant mints a new isolation scope, and a compromised relay
  credential that could do that would be able to place work in a scope of its
  own making;
* updating one could move a tenant's budget ceilings, which is a privileged
  operation belonging to `BUDGET_MANAGE`, not to a delivery process.

`tenant` carries no tenant data — it is the scope, not the contents — so reading
it does not cross the isolation boundary. What it does do is widen a credential,
so it is a reviewed migration rather than a grant tacked onto an existing one,
and `tests/properties/test_tenant_isolation.py` asserts the read/write asymmetry
rather than merely that the read works.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RELAY_ROLE = "platform_relay"


def upgrade() -> None:
    op.execute(f"GRANT SELECT ON tenant TO {RELAY_ROLE}")
    # Stated explicitly rather than relied on as the default. A future
    # `GRANT ALL` written for convenience would otherwise silently include the
    # writes this migration exists to withhold.
    op.execute(f"REVOKE INSERT, UPDATE, DELETE ON tenant FROM {RELAY_ROLE}")


def downgrade() -> None:
    op.execute(f"REVOKE SELECT ON tenant FROM {RELAY_ROLE}")
