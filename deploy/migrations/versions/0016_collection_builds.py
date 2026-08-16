"""collection builds: make a corpus replaceable without going dark

Revision ID: 0016
Revises: 0015
Create Date: 2026-08-13

## What was wrong

There was no way to change a document. Upload deduplicated on
``content_sha256``, so an edited file had a different hash and became a *new*
document — the old one stayed indexed, stayed retrievable, and could win a
similarity match and be cited as the answer. Nothing superseded it and nothing
flagged it. The only replace path was delete-then-upload, by hand, in that
order.

``chunk.build_version`` existed for exactly this and was never wired: every
writer used the default ``1`` and no reader filtered on it. The ``VectorIndex``
port documented the intent — "a rebuild writes a new build_version and the
previous one stays queryable until it is promoted away" — against an
implementation that only ever had one version. A declared mechanism with no
behaviour behind it, which is the same shape as the Azure build's tool-approval
flag routing to a disabled gate.

## The model

A document's identity is now ``(tenant, collection, filename)``. The content
hash becomes the *version* of that document rather than its identity, so a
re-upload under the same name supersedes instead of accumulating.

``collection_build`` names which build a collection currently serves. Writes go
to build N+1 while reads continue on N; promotion is one UPDATE. A rebuild that
dies half-written leaves the live build untouched and still serving — the
failure the port docstring calls out, where recreating an index in place leaves
the tenant with nothing to serve and a rollback story of "re-ingest and wait".

## Why at most two builds may coexist

Reads now carry a ``build_version`` predicate, and the HNSW index does not
contain it — so it is a post-filter on the vector search, the same class of
defect 0014 was written to fix. With two builds the filter discards at most
half the candidates, which ``hnsw.iterative_scan`` absorbs. With five it would
quietly gut recall again. The bound is a correctness constraint, not tidiness,
and the reaper enforces it rather than documenting it.

## The identity swap is a real behaviour change

The old unique on ``(tenant_id, collection, content_sha256)`` is dropped. Two
files with different names but identical bytes were previously one document;
they are now two. Idempotency is preserved where it matters — a retried upload
sends the same filename *and* the same hash, and is still a no-op — but
cross-filename deduplication is gone, deliberately, because it is incompatible
with filename being the identity.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "platform_app"
READONLY_ROLE = "platform_readonly"

# building → live → superseded. `failed` is distinct from `superseded` so a
# rebuild that died is not mistaken for one that was replaced on purpose.
STATUSES = ("building", "live", "superseded", "failed")


def upgrade() -> None:
    op.create_table(
        "collection_build",
        sa.Column("id", postgresql.UUID(as_uuid=True),
                  server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False),
        sa.Column("collection", sa.String(128), nullable=False),
        sa.Column("build_version", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="building"),
        # Content identity of the build, so a consumer of derived state — the
        # knowledge graph, an onboarding bundle — can tell whether what it was
        # built from is still what is being served.
        sa.Column("corpus_fingerprint", sa.String(64)),
        sa.Column("chunk_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("run_id", postgresql.UUID(as_uuid=True)),
        sa.Column("error", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("promoted_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("status IN " + str(STATUSES),
                           name="collection_build_status_valid"),
        sa.UniqueConstraint("tenant_id", "collection", "build_version",
                            name="collection_build_version_uniq"),
    )
    op.create_index("collection_build_lookup_idx", "collection_build",
                    ["tenant_id", "collection", "status"])

    # Exactly one live build per collection. Expressed as a constraint because
    # "which build do I read" must have one answer — two live builds would make
    # retrieval return rows from both, which reads as duplicated sources rather
    # than as an error.
    op.execute(
        "CREATE UNIQUE INDEX collection_build_one_live_idx "
        "ON collection_build (tenant_id, collection) WHERE status = 'live'"
    )

    op.execute("ALTER TABLE collection_build ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE collection_build FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY collection_build_tenant_isolation ON collection_build
            FOR ALL
            TO {APP_ROLE}, {READONLY_ROLE}
            USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
            WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
        """
    )
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON collection_build TO {APP_ROLE}")
    op.execute(f"GRANT SELECT ON collection_build TO {READONLY_ROLE}")

    # ── document identity ────────────────────────────────────────────────
    op.add_column("document", sa.Column("superseded_at", sa.DateTime(timezone=True)))
    op.add_column("document", sa.Column("superseded_by",
                                        postgresql.UUID(as_uuid=True),
                                        nullable=True))
    op.create_foreign_key(
        "document_superseded_by_fkey", "document", "document",
        ["superseded_by"], ["id"], ondelete="SET NULL",
    )

    op.drop_constraint("document_tenant_collection_sha_uniq", "document", type_="unique")

    # ── repair: one document row per file, not per chunk ─────────────────
    # The seeders created a document row for *every chunk*, so a file split
    # across two chunks became two document rows sharing a filename — 49
    # document rows for 48 chunks. Content-hash identity hid it, because two
    # chunks of one file genuinely do have different hashes. Filename identity
    # exposes it, and the unique index below would fail on it.
    #
    # Chunks are repointed at a surviving row rather than the duplicates being
    # superseded. Superseding would leave live chunks owned by a superseded
    # document, and the next rebuild — which reads only current documents —
    # would silently drop that content from the corpus.
    _RANKED = (
        "SELECT id, first_value(id) OVER ("
        "  PARTITION BY tenant_id, collection, filename ORDER BY created_at, id"
        ") AS keep_id FROM document"
    )
    op.execute(
        f"WITH ranked AS ({_RANKED}) "
        "UPDATE chunk c SET document_id = r.keep_id "
        "FROM ranked r WHERE c.document_id = r.id AND r.id <> r.keep_id"
    )
    op.execute(
        f"WITH ranked AS ({_RANKED}) "
        "DELETE FROM document d USING ranked r WHERE d.id = r.id AND r.id <> r.keep_id"
    )

    # Partial: only *current* documents contend for a filename. Superseded rows
    # stay for history, and several versions of one filename can coexist.
    op.execute(
        "CREATE UNIQUE INDEX document_tenant_collection_filename_uniq "
        "ON document (tenant_id, collection, filename) WHERE superseded_at IS NULL"
    )
    # Still worth an index: the ingest path looks up "same filename, same bytes"
    # to decide whether an upload is a no-op or a new version.
    op.create_index("document_content_sha_idx", "document",
                    ["tenant_id", "collection", "content_sha256"])

    # ── backfill ─────────────────────────────────────────────────────────
    # Every collection that already has chunks becomes live at build 1, which is
    # the version every existing row was written with. Without this, the new
    # read filter would resolve to no build and every existing corpus would go
    # dark on deploy — a migration that silently empties retrieval.
    op.execute(
        """
        INSERT INTO collection_build
            (tenant_id, collection, build_version, status, chunk_count, promoted_at)
        SELECT tenant_id, collection, 1, 'live', count(*), now()
        FROM chunk
        GROUP BY tenant_id, collection
        """
    )


def downgrade() -> None:
    op.drop_index("document_content_sha_idx", table_name="document")
    op.execute("DROP INDEX IF EXISTS document_tenant_collection_filename_uniq")
    op.create_unique_constraint(
        "document_tenant_collection_sha_uniq", "document",
        ["tenant_id", "collection", "content_sha256"],
    )
    op.drop_constraint("document_superseded_by_fkey", "document", type_="foreignkey")
    op.drop_column("document", "superseded_by")
    op.drop_column("document", "superseded_at")
    op.execute("DROP INDEX IF EXISTS collection_build_one_live_idx")
    op.drop_table("collection_build")
