"""cost_usd needs more than six decimal places

Revision ID: 0009
Revises: 0008
Create Date: 2026-08-12

`0006` typed `cost_usd` as `NUMERIC(12, 6)` — six decimal places, i.e. a
resolution of one millionth of a dollar. That looks generous and is not:

    gpt-4o-mini, 12 in + 1 out  →  $0.0000024  →  stored as  $0.000002

A 17% under-count, silently, on every small call. Cheap models are exactly the
ones used in the highest volume — per-chunk extraction, classification, embedding
— so the error concentrates precisely where the call count is largest. A million
such calls is $2.40 spent and $2.00 recorded, and nothing anywhere reports a
discrepancy.

Found by `scripts/e2e_llm.py` printing the response cost and the ledger cost next
to each other on one real call. Neither number was wrong on its own; only the
comparison showed it. The property suite did not catch it because its fixtures
use round numbers that survive the rounding.

`NUMERIC(20, 12)` gives twelve decimal places — a resolution of one picodollar,
which is four orders of magnitude below the cheapest per-token rate in the table
and leaves eight integer digits for the total.

`NUMERIC` rather than `DOUBLE PRECISION` throughout: binary floating point cannot
represent most decimal fractions exactly, so summing millions of tiny float costs
accumulates error in a value that is supposed to reconcile against an invoice.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Widening a NUMERIC is a metadata-only change in Postgres — no table
    # rewrite, no lock beyond the catalog update — so this is safe to apply to a
    # live table. Existing rows keep the values they were truncated to; the
    # under-count already recorded is not recoverable, which is worth stating
    # rather than pretending the migration repairs history.
    op.alter_column(
        "llm_usage", "cost_usd",
        existing_type=sa.Numeric(12, 6),
        type_=sa.Numeric(20, 12),
        existing_nullable=False,
        existing_server_default="0",
    )


def downgrade() -> None:
    # Narrowing DOES rewrite and DOES lose precision. Stated explicitly because
    # a downgrade that silently truncates money is worse than one that refuses.
    op.alter_column(
        "llm_usage", "cost_usd",
        existing_type=sa.Numeric(20, 12),
        type_=sa.Numeric(12, 6),
        existing_nullable=False,
        existing_server_default="0",
    )
