"""questions the corpus could not answer, kept as a backlog

Revision ID: 0020
Revises: 0019
Create Date: 2026-08-15

## Why this table exists and why it is this narrow

Eval sets are seeded from questions proposed *about* the corpus. Nothing
captured the questions people actually asked it — and the most valuable of those
are the ones it failed: a real user, a real information need, and a corpus that
had nothing to say. That is a backlog item, an eval item and a retrieval bug
report in one.

Sessions already hold the transcript, and they expire after twelve hours. Mining
them alone would only ever see the current afternoon, so anything asked on a
Friday evening is gone by Saturday. Something has to be written down.

**Only the failures are.** This is deliberately not a transcript. A durable
record of everything users ask is a materially different thing to hold — readable
by every capability holder, retained past the conversation, and justified by
nothing stronger than "it might be useful". Recording only ungrounded turns keeps
the table purpose-bound: it is a list of things the corpus should cover and does
not, which is exactly what somebody is entitled to read.

Questions that *were* answered remain minable from the live session window, where
they already live and already expire.

## Deduplicated on a normalised key

``question_key`` is the question lowercased with whitespace collapsed and
trailing punctuation removed, so "What is the SA-400 torque?" and "what is the
sa-400 torque" are one row with ``occurrences = 2``. The count is the point: a
gap forty people hit is not the same backlog item as one somebody hit once, and
without the count the table is an undifferentiated list.

## Retention is enforced, not documented

Unseeded rows older than ``unanswered_question_retention_days`` are deleted by
the worker's ``sweep``. Rows already seeded into an eval set are kept — they are
few, and "this gap was reported forty times before we fixed it" is the sentence
the count exists to make sayable.

``sessions.purge_expired`` was written in Phase 5 and never called by anything;
the same sweep now runs it. A retention mechanism nothing invokes is the shape of
defect this codebase keeps finding, and adding a second one would have been
careless.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels = None
depends_on = None

APP_ROLE = "platform_app"
READONLY_ROLE = "platform_readonly"

ORIGINS = ("hand", "onboarding", "mined")


def upgrade() -> None:
    op.create_table(
        "unanswered_question",
        sa.Column("id", postgresql.UUID(as_uuid=True),
                  server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False),
        sa.Column("collection", sa.String(128), nullable=False),
        sa.Column("question", sa.Text, nullable=False),
        # Normalised for deduplication. Stored rather than computed on read so
        # the unique index can use it.
        sa.Column("question_key", sa.Text, nullable=False),
        sa.Column("mode", sa.String(16)),
        sa.Column("occurrences", sa.Integer, nullable=False, server_default="1"),
        sa.Column("first_asked_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("last_asked_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        # Who asked most recently. Nulled rather than cascaded when a principal
        # is removed: the gap outlives the person who found it.
        sa.Column("last_asked_by", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("principal.id", ondelete="SET NULL")),
        sa.Column("seeded_into", sa.String(128)),
        sa.Column("seeded_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "(seeded_into IS NULL) = (seeded_at IS NULL)",
            name="unanswered_question_seeding_complete",
        ),
        sa.UniqueConstraint("tenant_id", "collection", "question_key",
                            name="unanswered_question_uniq"),
    )
    # Ordered by how often a gap was hit, which is how the backlog is read.
    op.create_index(
        "unanswered_question_backlog_idx", "unanswered_question",
        ["tenant_id", "collection", sa.text("occurrences DESC")],
    )

    op.execute("ALTER TABLE unanswered_question ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE unanswered_question FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY unanswered_question_tenant_isolation ON unanswered_question
            FOR ALL
            TO {APP_ROLE}, {READONLY_ROLE}
            USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
            WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
        """
    )
    op.execute(
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON unanswered_question TO {APP_ROLE}"
    )
    op.execute(f"GRANT SELECT ON unanswered_question TO {READONLY_ROLE}")

    # Where an eval item came from. A set built from real failures and one built
    # from proposals about the corpus are different instruments, and a reader
    # cannot tell them apart from the questions alone.
    op.add_column(
        "eval_item_label",
        sa.Column("origin", sa.String(16), nullable=False, server_default="hand"),
    )
    op.create_check_constraint(
        "eval_item_label_origin_valid", "eval_item_label",
        "origin IN " + str(ORIGINS),
    )


def downgrade() -> None:
    op.drop_constraint("eval_item_label_origin_valid", "eval_item_label", type_="check")
    op.drop_column("eval_item_label", "origin")
    op.execute(
        "DROP POLICY IF EXISTS unanswered_question_tenant_isolation "
        "ON unanswered_question"
    )
    op.drop_index("unanswered_question_backlog_idx", table_name="unanswered_question")
    op.drop_table("unanswered_question")
