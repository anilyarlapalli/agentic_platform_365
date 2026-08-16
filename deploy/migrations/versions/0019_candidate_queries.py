"""candidate queries as an onboarding artifact

Revision ID: 0019
Revises: 0018
Create Date: 2026-08-14

## What this unlocks

Eval sets had to be authored by hand, because nothing produced questions that
were both **grounded in the corpus** and **carrying ids the retriever emits**.
Writing them by hand means writing the chunk ids by hand too, and an id that is
one character wrong scores a permanent miss indistinguishable from a real
retrieval failure — which is the exact defect ``build_dataset`` rejects
non-canonical citations to prevent.

The onboarding drafter already reads the corpus through
``PgVectorRetriever.load_documents()``, whose ``chunk_id`` **is** the canonical
``c_<sha1:16>`` the retriever returns. So questions generated from those chunks
come with citations that are correct by construction.

## Why this was hard in the reference deployment and is not here

``azure_deploy_graphrag/eval_store.py`` documents the failure at length: its
onboarding drafter chunks the corpus independently of the ingestion pipeline, so
``evidence_chunk_ids`` and the published chunk ids are "two different namespaces
over two different chunk sets. No derivation reconciles them after the fact —
verified by trying every combination of (source, section_path, ordinal) against
the published metadata and matching none." It falls back to locating evidence by
``source_file`` + ``page``, and says plainly that this "does NOT make retrieval
recall scoreable".

The engine supports doing it properly — ``_chunk_identifier`` prefers an explicit
``chunk_id`` for exactly this reason — and here we can supply one, because there
is only ever one chunking pass and the eval gate reads the same live build.

## Why an artifact rather than a table

Candidate queries belong to the drafting session that produced them, live and die
with it, and are reviewed on the same screen as the taxonomy. ``onboarding_artifact``
already stores per-session derived state with tenant isolation and an upsert on
``(session_id, kind, name)``. A dedicated table would add a second lifecycle for
data with the same one.

They are deliberately **not** an ``eval_dataset``. Seeding one is a separate,
explicit act carrying ``release:promote``, because a dataset is what the gate
measures against — and a set that appeared automatically at the end of drafting
would be ground truth nobody chose.
"""

from __future__ import annotations

from alembic import op

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels = None
depends_on = None

OLD_KINDS = ("schema", "instance_table", "predicate_map", "extraction_cache")
NEW_KINDS = (*OLD_KINDS, "candidate_queries")


def upgrade() -> None:
    op.drop_constraint(
        "onboarding_artifact_kind_valid", "onboarding_artifact", type_="check"
    )
    op.create_check_constraint(
        "onboarding_artifact_kind_valid",
        "onboarding_artifact",
        "kind IN " + str(NEW_KINDS),
    )


def downgrade() -> None:
    # Rows of the new kind would violate the narrower constraint, so they go
    # first. Dropping them is correct rather than lossy: they are derived state
    # regenerable by re-drafting, and any eval set already seeded from them is a
    # separate row in `eval_dataset` that this does not touch.
    op.execute("DELETE FROM onboarding_artifact WHERE kind = 'candidate_queries'")
    op.execute(
        "ALTER TABLE onboarding_artifact "
        "DROP CONSTRAINT IF EXISTS onboarding_artifact_kind_valid"
    )
    op.create_check_constraint(
        "onboarding_artifact_kind_valid",
        "onboarding_artifact",
        "kind IN " + str(OLD_KINDS),
    )
