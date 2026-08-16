"""partition chunk by tenant, so ANN search stops being a global search

Revision ID: 0014
Revises: 0013
Create Date: 2026-08-13

## The defect this closes

An HNSW index is a navigation graph, not a sorted list. A query cannot enter it
"only at one tenant's rows" — those rows are scattered throughout. So Postgres
either uses the index and applies the tenant predicate to whatever it returns,
or ignores the index and brute-forces. The first is the dangerous one:

    ORDER BY embedding <=> :probe LIMIT 5     -- plus RLS: tenant_id = …

``hnsw.ef_search`` (default 40) bounds how many candidates the index examines.
When one tenant owns a small fraction of a large index, those 40 global
candidates contain almost none of that tenant's rows, the filter discards the
rest, and the query returns **fewer rows than asked for — with no error**. Ask
for 5 sources, get 1. The chat answers from thin context and reads as a merely
mediocre answer rather than a broken retrieval path.

This is the same failure shape as the edgeless knowledge graph: degraded
retrieval that is indistinguishable from working retrieval. It is invisible at
48 rows because the planner just sequentially scans, and it appears at the exact
point the corpus grows past eyeballing.

## Why HASH and not LIST

LIST — one partition per tenant — is the strongest form: each index would hold
exactly one tenant and the post-filter would disappear entirely. It also makes
creating a tenant a DDL operation, which turns provisioning into a privileged
schema change. With tenant cardinality unknown, that is a large operational bet
to take for a difference that ``iterative_scan`` already covers.

HASH with 16 partitions takes the reduction without the bet: pruning sends each
query to one partition holding ~1/16th of the tenants, and the residual mixing
is handled by ``hnsw.iterative_scan`` (set below), which makes the index resume
scanning instead of giving up when the filter rejects candidates. Convertible to
LIST later if tenant counts stay low.

## Two things that are easy to get wrong

**The primary key must contain the partition key.** ``PRIMARY KEY (id)`` is not
permitted on a table partitioned by ``tenant_id``; it becomes ``(id, tenant_id)``.
The existing unique constraint already leads with ``tenant_id`` and is unchanged.

**RLS is not inherited by partitions.** A policy on the parent applies to rows
reached *through* the parent. Querying ``chunk_p3`` directly is subject only to
``chunk_p3``'s own policies — and ``platform_app`` holds SELECT on it via default
privileges. So every partition gets ENABLE + FORCE + its own policy. Without
that, this migration would have opened a direct-access hole in the exact control
the table exists to enforce.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PARTITIONS = 16
APP_ROLE = "platform_app"
READONLY_ROLE = "platform_readonly"

_COLUMNS = (
    "id, tenant_id, document_id, collection, canonical_id, ordinal, "
    "build_version, text, meta, embedding, embedding_model, created_at"
)


def _protect(table: str) -> None:
    """RLS on one relation. Applied to the parent *and* every partition."""
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
    # Keep the old rows aside rather than dumping to a file: the copy back
    # happens inside this transaction, so a failure anywhere leaves the original
    # table intact and the migration simply does not commit.
    op.execute("ALTER TABLE chunk RENAME TO chunk_unpartitioned")
    op.execute("ALTER INDEX chunk_pkey RENAME TO chunk_unpartitioned_pkey")
    op.execute(
        "ALTER INDEX chunk_tenant_collection_canonical_build_uniq "
        "RENAME TO chunk_unpart_canonical_uniq"
    )
    op.execute("ALTER INDEX chunk_tenant_collection_idx RENAME TO chunk_unpart_tc_idx")
    op.execute("DROP INDEX chunk_embedding_hnsw_idx")

    op.execute(
        """
        CREATE TABLE chunk (
            id              uuid NOT NULL DEFAULT gen_random_uuid(),
            tenant_id       uuid NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
            document_id     uuid NOT NULL REFERENCES document(id) ON DELETE CASCADE,
            collection      varchar(128) NOT NULL,
            canonical_id    varchar(32) NOT NULL,
            ordinal         integer NOT NULL,
            build_version   bigint NOT NULL DEFAULT 1,
            text            text NOT NULL,
            meta            jsonb NOT NULL DEFAULT '{}',
            -- 1536 = text-embedding-3-small. settings refuses to start if the
            -- configured model disagrees with this width.
            embedding       vector(1536),
            embedding_model varchar(64),
            created_at      timestamptz NOT NULL DEFAULT now(),
            -- Must include the partition key; see the module docstring.
            PRIMARY KEY (id, tenant_id),
            CONSTRAINT chunk_tenant_collection_canonical_build_uniq
                UNIQUE (tenant_id, collection, canonical_id, build_version)
        ) PARTITION BY HASH (tenant_id)
        """
    )

    for i in range(PARTITIONS):
        op.execute(
            f"CREATE TABLE chunk_p{i} PARTITION OF chunk "
            f"FOR VALUES WITH (MODULUS {PARTITIONS}, REMAINDER {i})"
        )

    # Created on the parent, which propagates a child index to every partition.
    # That is the whole point: sixteen smaller HNSW graphs rather than one
    # global one, so a pruned query searches only its own partition's graph.
    op.execute("CREATE INDEX chunk_tenant_collection_idx ON chunk (tenant_id, collection)")
    op.execute(
        """
        CREATE INDEX chunk_embedding_hnsw_idx ON chunk
            USING hnsw (embedding vector_cosine_ops)
            WITH (m = 16, ef_construction = 64)
        """
    )

    op.execute(
        f"INSERT INTO chunk ({_COLUMNS}) SELECT {_COLUMNS} FROM chunk_unpartitioned"
    )
    op.execute("DROP TABLE chunk_unpartitioned")

    # The parent, then every partition. A policy on the parent does not cover
    # direct access to a partition, and platform_app can reach them by name.
    _protect("chunk")
    for i in range(PARTITIONS):
        _protect(f"chunk_p{i}")

    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON chunk TO {APP_ROLE}")
    op.execute(f"GRANT SELECT ON chunk TO {READONLY_ROLE}")
    for i in range(PARTITIONS):
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON chunk_p{i} TO {APP_ROLE}")
        op.execute(f"GRANT SELECT ON chunk_p{i} TO {READONLY_ROLE}")

    # Make the index resume scanning when the tenant filter rejects candidates,
    # instead of returning short. Set on the database so it covers every
    # connection — the application, psql, and anything exploring in pgAdmin —
    # rather than depending on each call site to remember it.
    #
    # `strict_order` preserves exact distance ordering. `relaxed_order` is
    # faster and may return results slightly out of order, which is the wrong
    # trade for a retrieval path whose scores are shown to users and recorded
    # as evidence.
    op.execute("ALTER DATABASE platform SET hnsw.iterative_scan = 'strict_order'")


def downgrade() -> None:
    op.execute("ALTER DATABASE platform RESET hnsw.iterative_scan")
    op.execute("ALTER TABLE chunk RENAME TO chunk_partitioned")

    op.execute(
        """
        CREATE TABLE chunk (
            id              uuid NOT NULL DEFAULT gen_random_uuid() PRIMARY KEY,
            tenant_id       uuid NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
            document_id     uuid NOT NULL REFERENCES document(id) ON DELETE CASCADE,
            collection      varchar(128) NOT NULL,
            canonical_id    varchar(32) NOT NULL,
            ordinal         integer NOT NULL,
            build_version   bigint NOT NULL DEFAULT 1,
            text            text NOT NULL,
            meta            jsonb NOT NULL DEFAULT '{}',
            embedding       vector(1536),
            embedding_model varchar(64),
            created_at      timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT chunk_tenant_collection_canonical_build_uniq
                UNIQUE (tenant_id, collection, canonical_id, build_version)
        )
        """
    )
    op.execute("CREATE INDEX chunk_tenant_collection_idx ON chunk (tenant_id, collection)")
    op.execute(
        "CREATE INDEX chunk_embedding_hnsw_idx ON chunk "
        "USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64)"
    )
    op.execute(f"INSERT INTO chunk ({_COLUMNS}) SELECT {_COLUMNS} FROM chunk_partitioned")
    op.execute("DROP TABLE chunk_partitioned")

    _protect("chunk")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON chunk TO {APP_ROLE}")
    op.execute(f"GRANT SELECT ON chunk TO {READONLY_ROLE}")
