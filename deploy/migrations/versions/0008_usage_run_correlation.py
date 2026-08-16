"""llm_usage.run_id is a correlation id, not a foreign key

Revision ID: 0008
Revises: 0007
Create Date: 2026-08-12

`0006` gave `llm_usage.run_id` a foreign key to `run`. That conflated two things
that are not the same:

* **a run** — a queued unit of work with a lease, an attempt count and a row;
* **a unit of work** — `RequestContext.run_id`, which exists for *every* piece of
  work including an interactive chat request that was never queued.

An interactive call therefore carried a `run_id` with no matching `run` row, and
the ledger insert failed the foreign key. Found by `scripts/e2e_llm.py` on the
first real OpenAI call: the call succeeded, the answer was returned, and the
charge was **not recorded**.

Worth noting how it surfaced, because the behaviour was correct throughout.
`Ledger.record` deliberately swallows its own failures — losing a ledger row
must not lose the answer that was already paid for and produced — so it logged
at ERROR and returned. That is the right trade, and it is also exactly why the
live check exists: a subsystem designed not to fail loudly needs something that
looks at the outcome rather than the return value.

So `run_id` becomes a plain, indexed uuid: a correlation column that joins to
`run` **when there is one**, and is simply an id for the unit of work when there
is not. `audit_event.run_id` was already modelled this way.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("llm_usage_run_id_fkey", "llm_usage", type_="foreignkey")
    # Still indexed: "what did this run cost" is a question worth answering
    # quickly, and it is the join that a cost investigation always starts from.
    op.create_index("llm_usage_run_idx", "llm_usage", ["run_id"])


def downgrade() -> None:
    op.drop_index("llm_usage_run_idx", table_name="llm_usage")
    # Restoring the constraint will fail if any usage row references a run that
    # does not exist — which is the state this migration exists to permit. That
    # is correct: the downgrade should refuse rather than silently discard rows.
    op.create_foreign_key(
        "llm_usage_run_id_fkey", "llm_usage", "run", ["run_id"], ["id"],
        ondelete="SET NULL",
    )
