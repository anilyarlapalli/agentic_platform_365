"""index the lexical half of hybrid retrieval

Revision ID: 0015
Revises: 0014
Create Date: 2026-08-13

Graph mode fuses three signals — dense (cosine over pgvector), lexical, and
knowledge-graph entity matching. 0001 indexed the vectors and 0014 partitioned
them, but the lexical half has never had an index at all:

    WHERE to_tsvector('english', text) @@ plainto_tsquery('english', :q)
    ORDER BY ts_rank(to_tsvector('english', text), plainto_tsquery(...)) DESC

Without a matching index Postgres computes ``to_tsvector`` for **every row on
every query**, twice — once to filter and once to rank. At 48 rows that is free;
at 100k it is a full scan plus 100k tsvector computations per query, which makes
the lexical signal slower than the vector search it exists to complement.

It fails the same way the unindexed vector search did: correct results, quietly
terrible latency, and nothing in the response to say so.

## Expression index, so the expression must match exactly

This indexes ``to_tsvector('english', text)``, not ``text``. Postgres only uses
an expression index when the query's expression is identical — including the
``'english'`` regconfig literal. ``retrieve_sparse`` spells it that way; a call
site that omits the language, or passes it as a bound parameter rather than a
literal, silently gets a sequential scan instead.

Created on the partitioned parent, which propagates a child index to each of the
sixteen partitions, so a pruned query uses only its own partition's index.

GIN rather than GiST: GIN is slower to update and considerably faster to search,
and this corpus is written once per ingest and read on every query.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE INDEX chunk_text_fts_idx ON chunk
            USING gin (to_tsvector('english', text))
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS chunk_text_fts_idx")
