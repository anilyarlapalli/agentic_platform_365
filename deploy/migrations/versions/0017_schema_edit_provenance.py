"""reviewer edits to a drafted taxonomy, with provenance

Revision ID: 0017
Revises: 0016
Create Date: 2026-08-14

## What was wrong

A reviewer could approve a drafted taxonomy or refuse it, and nothing else. The
schema was rendered read-only and published exactly as the drafter produced it.

That is not a small ergonomic gap. Measured on 2026-08-14: a draft over a
96-chunk corpus declared three entity types for a domain containing racks, PDUs,
air conditioners, switches, alarms, procedures and parts. Every edge type
constrains both endpoints to declared entity types, so **295 of 306 candidate
edges were discarded** and the published graph had 84 nodes and 1 edge. It
answered like a populated graph and traversed almost nothing.

The remedy available was to re-draft: a full corpus of extraction calls, paid
again, to fix a taxonomy a reviewer could see was too coarse while looking at it.

## What this adds

Two columns recording that an edit happened and who made it, mirroring the
approval columns beside them. The edited YAML itself is not a column — it upserts
into the ``onboarding_artifact`` row the drafter wrote, because that row is
already what ``materialize`` reads and adding a second source of the schema would
mean two places that disagree.

The *original* draft is preserved as a sibling artifact named ``schema_drafted``,
so "what the model proposed" and "what a human published" stay separately
answerable. That is the whole point of recording provenance: an approval that
cannot be distinguished from an edit-then-approval is not a record of anything.

## Editing is authoring, so an editor cannot approve

The existing rule is "you cannot approve your own draft", enforced by
``onboarding_session_no_self_approval``. Once a human can change the taxonomy,
that rule has a hole in it: a reviewer could rewrite the schema and approve their
own rewrite, which is exactly the unilateral path to production the constraint
exists to close. So the rule is not extended with a new concept — it is applied
to the same act. Whoever last wrote the content cannot be the one who checks it.

The consequence is deliberate and worth stating plainly: a reviewer who fixes a
taxonomy has made themselves its author, and a *different* principal must
approve it. Editing therefore requires ``schema:author``, not ``schema:approve``.
The reference implementation this project studies allows the reviewer to edit and
approve in one motion and discloses it afterwards with a
``yaml_edited_by_reviewer`` flag; disclosure after the fact is a weaker control
than refusal before it.

## Why an edit does not clear the approval

It cannot: the edit is only permitted while the session is ``draft_ready``, which
is before there is an approval to clear. Editing after approval would mean the
bytes that were approved are not the bytes that get published, which is the
failure this table's CHECK constraints exist to make impossible. The route
enforces the status; this migration records the fact.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "onboarding_session",
        sa.Column(
            "schema_edited_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("principal.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column(
        "onboarding_session",
        sa.Column("schema_edited_at", sa.DateTime(timezone=True), nullable=True),
    )

    # Same shape as `onboarding_session_approval_complete`: a half-written record
    # of who did what is not a record. Either both are set or neither is.
    op.create_check_constraint(
        "onboarding_session_edit_complete",
        "onboarding_session",
        "(schema_edited_by IS NULL) = (schema_edited_at IS NULL)",
    )

    # The structural half of "editing is authoring". The route refuses this
    # independently; the constraint makes it unrecordable even if a future code
    # path forgets to ask — the same division of labour as
    # `onboarding_session_no_self_approval`, which this sits beside rather than
    # replaces (a draft nobody edited is still governed by that one).
    op.create_check_constraint(
        "onboarding_session_editor_is_not_approver",
        "onboarding_session",
        "approved_by IS NULL OR schema_edited_by IS NULL "
        "OR approved_by <> schema_edited_by",
    )


def downgrade() -> None:
    # IF EXISTS rather than `op.drop_constraint`: a downgrade runs precisely when
    # something is already wrong, and one absent constraint aborting the whole
    # transaction leaves the schema stranded between two revisions with no path
    # in either direction. Learned by doing it — see ROLLOUT.md.
    op.execute(
        "ALTER TABLE onboarding_session "
        "DROP CONSTRAINT IF EXISTS onboarding_session_editor_is_not_approver"
    )
    op.execute(
        "ALTER TABLE onboarding_session "
        "DROP CONSTRAINT IF EXISTS onboarding_session_edit_complete"
    )
    op.drop_column("onboarding_session", "schema_edited_at")
    op.drop_column("onboarding_session", "schema_edited_by")
