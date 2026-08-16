"""SME labels on eval items, held outside the versioned content

Revision ID: 0018
Revises: 0017
Create Date: 2026-08-14

## Why a table and not three more fields on the item

A dataset is ``(name, content_sha256)``, and the hash covers the item dicts. That
is what makes a promotion comparison legitimate: a candidate and a baseline
scored on different questions produce two real numbers that mean nothing next to
each other, and ``promotion.evaluate`` refuses the comparison by hash.

Putting review state — confirmed, who wrote the answer, whether the item needs a
graph traversal — inside the item would fold it into that hash. Ticking
"confirmed" would mint a new dataset version, orphan the baseline, and break the
gate on the first click of the review workflow it exists to support. The reviewer
would be punished for reviewing.

So labels are keyed by ``(tenant, dataset_name, item_id)`` and live here. Two
consequences worth having:

* confirming an answer never perturbs ``content_sha256``;
* labels are keyed by dataset **name**, not version, so they carry forward when
  the questions are re-versioned — a reviewer does not re-confirm forty items
  because one question was rephrased.

## What stays content, and why

``question``, ``expected_answer`` and ``must_cite`` remain in the hash. Editing
an expected answer *should* produce a new version: it changes the yardstick, and
comparing against a baseline scored on different answers is exactly the
incomparability the gate refuses.

``unusable_reason`` is the awkward one and sits here anyway. It excludes an item
from a run, so it behaves like content — but it is a *review* verdict about the
evidence, recorded so a bad chunk becomes a standing signal for the corpus
backlog rather than a silent deletion. The run records how many items were
excluded, which is what keeps the count honest without moving the hash.

## The rubber-stamp signal

``answer_source`` distinguishes an answer a human wrote or edited from one that
was drafted and never read. Confirming while still ``llm_drafted`` is not
SME-attested ground truth, and the review surface says so rather than counting it
as such. Without this column the distinction is unrecoverable after the fact.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels = None
depends_on = None

APP_ROLE = "platform_app"
READONLY_ROLE = "platform_readonly"

ANSWER_SOURCES = ("empty", "llm_drafted", "sme_edited", "sme_authored")


def upgrade() -> None:
    op.create_table(
        "eval_item_label",
        sa.Column("id", postgresql.UUID(as_uuid=True),
                  server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False),
        # By dataset *name*, not by id or hash. A label survives re-versioning of
        # the questions; that is the point of holding it out here.
        sa.Column("dataset_name", sa.String(128), nullable=False),
        sa.Column("item_id", sa.String(128), nullable=False),

        sa.Column("answer_source", sa.String(16), nullable=False,
                  server_default="empty"),
        # Which model drafted it, recorded per item rather than per run: a set
        # annotated over several sessions can carry answers from more than one
        # model, and "which model wrote this yardstick" is not answerable later
        # from a run-level field.
        sa.Column("annotator_model", sa.String(64)),
        sa.Column("annotated_at", sa.DateTime(timezone=True)),

        sa.Column("confirmed", sa.Boolean, nullable=False,
                  server_default=sa.text("false")),
        sa.Column("confirmed_by", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("principal.id", ondelete="SET NULL")),
        sa.Column("confirmed_at", sa.DateTime(timezone=True)),

        # The instrument that makes a graph change provable: it marks the items
        # whose answer needs a traversal, so a predicate or entity-taxonomy fix
        # can be measured on the slice it should actually move rather than on
        # an average that dilutes it.
        sa.Column("requires_kg_hop", sa.Boolean, nullable=False,
                  server_default=sa.text("false")),
        sa.Column("unusable_reason", sa.Text),

        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),

        sa.CheckConstraint(
            "answer_source IN " + str(ANSWER_SOURCES),
            name="eval_item_label_answer_source_valid",
        ),
        # A confirmation carries its confirmer, like every other decision in this
        # schema. A half-written verdict is not a verdict.
        sa.CheckConstraint(
            "confirmed = false OR (confirmed_by IS NOT NULL AND confirmed_at IS NOT NULL)",
            name="eval_item_label_confirmation_complete",
        ),
        sa.UniqueConstraint("tenant_id", "dataset_name", "item_id",
                            name="eval_item_label_uniq"),
    )
    op.create_index("eval_item_label_lookup_idx", "eval_item_label",
                    ["tenant_id", "dataset_name"])

    op.execute("ALTER TABLE eval_item_label ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE eval_item_label FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY eval_item_label_tenant_isolation ON eval_item_label
            FOR ALL
            TO {APP_ROLE}, {READONLY_ROLE}
            USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
            WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
        """
    )
    op.execute(
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON eval_item_label TO {APP_ROLE}"
    )
    op.execute(f"GRANT SELECT ON eval_item_label TO {READONLY_ROLE}")


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS eval_item_label_tenant_isolation ON eval_item_label")
    op.drop_index("eval_item_label_lookup_idx", table_name="eval_item_label")
    op.drop_table("eval_item_label")
