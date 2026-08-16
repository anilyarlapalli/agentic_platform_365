"""eval: versioned datasets, retained run history, and a baseline pointer

Revision ID: 0010
Revises: 0009
Create Date: 2026-08-12

## Why runs are retained rather than overwritten

The Azure build writes eval results to a single blob per domain —
`onboarding/{domain}/eval_results.json` — so every run overwrites the last.
"Did this change regress retrieval?" is therefore unanswerable by construction:
there is nothing to compare against. A number with no history is a reading, not
a measurement.

Here `eval_run` is append-only in practice and `eval_baseline` is a *pointer* to
whichever run is currently the reference. Promotion moves the pointer; it never
destroys the run it moved away from, so a regression can always be diffed
against what it regressed from.

## Why a dataset is identified by its content

`eval_dataset.content_sha256` is the hash of the items. A score is only
comparable to another score computed over the same questions, so the dataset
hash is part of a run's identity alongside the code revision and the model.
Comparing a run against a baseline computed over a *different* dataset is the
subtlest way to produce a confident wrong answer, so the gate refuses it.

## Why the cassette hash is recorded

A gate has to be deterministic to be a gate. `cassette_sha` records which
recorded responses produced a run, so "the score changed" can be separated from
"the model's answers changed underneath us".
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NEW_TENANT_SCOPED = ("eval_dataset", "eval_run", "eval_result", "eval_baseline")
APP_ROLE = "platform_app"
READONLY_ROLE = "platform_readonly"


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


def upgrade() -> None:
    op.create_table(
        "eval_dataset",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("collection", sa.String(128), nullable=False),
        # The hash of the items. Identity of the questions, so two scores are
        # only ever compared when they were computed over the same set.
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column("items", postgresql.JSONB(), nullable=False),
        sa.Column("item_count", sa.Integer(), nullable=False),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("principal.id", ondelete="SET NULL")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        # A name plus a content hash is a version. Editing questions produces a
        # new row rather than mutating the old one, so historical runs keep
        # pointing at the exact questions they were scored on.
        sa.UniqueConstraint("tenant_id", "name", "content_sha256", name="eval_dataset_version_uniq"),
    )

    op.create_table(
        "eval_run",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False),
        sa.Column("dataset_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("eval_dataset.id", ondelete="CASCADE"), nullable=False),
        # The four coordinates that make a score comparable. A run is only
        # comparable to another with the same dataset_sha; the rest explain a
        # difference rather than invalidating the comparison.
        sa.Column("dataset_sha", sa.String(64), nullable=False),
        sa.Column("code_rev", sa.String(64), nullable=False),
        sa.Column("model_id", sa.String(96), nullable=False),
        sa.Column("cassette_sha", sa.String(64)),
        sa.Column("status", sa.String(16), nullable=False, server_default="running"),
        # Reported separately, never averaged together: a wrong answer because
        # the evidence was never retrieved and a wrong answer despite it are
        # different bugs on different surfaces.
        sa.Column("answer_pass_rate", sa.Float()),
        sa.Column("retrieval_recall", sa.Float()),
        sa.Column("items_run", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("items_scoreable", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("metrics", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("error", sa.Text()),
        sa.CheckConstraint("status IN ('running','completed','failed')", name="eval_run_status_valid"),
    )
    op.create_index("eval_run_lookup_idx", "eval_run", ["tenant_id", "dataset_sha", "started_at"])

    op.create_table(
        "eval_result",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False),
        sa.Column("eval_run_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("eval_run.id", ondelete="CASCADE"), nullable=False),
        sa.Column("item_id", sa.String(64), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("passed", sa.Boolean()),
        # Canonical chunk ids only. The Azure build stores whatever the
        # retriever emitted, which is an ordinal into a build-specific list — so
        # a stored result silently means something different after the next
        # rebuild renumbers everything.
        sa.Column("must_cite", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("retrieved", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("retrieval_recall", sa.Float()),
        sa.Column("answer", sa.Text()),
        sa.Column("detail", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.UniqueConstraint("eval_run_id", "item_id", name="eval_result_run_item_uniq"),
    )

    op.create_table(
        "eval_baseline",
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenant.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("dataset_name", sa.String(128), primary_key=True),
        # A pointer, not a copy. Promotion moves it; the run it moved away from
        # is still there to diff against.
        sa.Column("eval_run_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("eval_run.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("promoted_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("principal.id", ondelete="SET NULL")),
        sa.Column("promoted_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("note", sa.Text()),
    )

    for table in NEW_TENANT_SCOPED:
        _protect(table)

    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {', '.join(NEW_TENANT_SCOPED)} TO {APP_ROLE}")
    op.execute(f"GRANT SELECT ON {', '.join(NEW_TENANT_SCOPED)} TO {READONLY_ROLE}")
    op.execute(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {APP_ROLE}")


def downgrade() -> None:
    for table in NEW_TENANT_SCOPED:
        op.execute(f"DROP POLICY IF EXISTS {table}_tenant_isolation ON {table}")
    op.drop_table("eval_baseline")
    op.drop_table("eval_result")
    op.drop_index("eval_run_lookup_idx", table_name="eval_run")
    op.drop_table("eval_run")
    op.drop_table("eval_dataset")
