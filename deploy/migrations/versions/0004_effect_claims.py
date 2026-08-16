"""side_effect claims expire, so a live attempt is distinguishable from a dead one

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-12

`0003` gave every side effect a `started` status. That is enough to tell "this
step was begun" but **not** enough to tell *why* it is still begun, and those two
cases need opposite handling:

    started, and the claimant is alive   → another attempt is in flight.
                                           Back off. Do not run.
    started, and the claimant is gone    → a previous attempt died mid-effect.
                                           Apply the retry policy.

Without the distinction, `perform_once` read every `started` row as the second
case. A concurrent second attempt therefore re-ran an effect that was still
executing — found by `tests/properties/test_idempotency.py`, which observed the
effect body run twice for one `(run_id, step)`.

The unique constraint alone cannot fix this: it makes the *row* unique, not the
*execution*. The claim needs a deadline, exactly as the run lease does, so a
claimant that stops heartbeating loses its grip on the step in the same way it
loses its grip on the run.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Expand-only: both columns are nullable with no default, so a previous
    # revision running beside this one is unaffected. A NULL `claim_expires_at`
    # on a `started` row means "written before this migration" and is treated as
    # already expired, which is the safe reading — it can only cause an extra
    # retry of an effect whose policy already permits repeating.
    op.add_column("side_effect", sa.Column("claimed_by", sa.String(128)))
    op.add_column(
        "side_effect", sa.Column("claim_expires_at", sa.DateTime(timezone=True))
    )

    # The reconciliation worklist query: started rows whose claim has lapsed.
    op.execute(
        "CREATE INDEX side_effect_stale_claim_idx ON side_effect (claim_expires_at) "
        "WHERE status = 'started'"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS side_effect_stale_claim_idx")
    op.drop_column("side_effect", "claim_expires_at")
    op.drop_column("side_effect", "claimed_by")
