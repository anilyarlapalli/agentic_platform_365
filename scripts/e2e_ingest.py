"""Live check: upload → retained bytes → chunks → replace → re-chunk.

The property suite proves each piece against a scratch tenant. This runs the
whole lifecycle against real MinIO and the real embedding API, because the thing
worth checking is the *seam*: that the bytes upload wrote are the bytes reindex
reads, under a key derived in one place and checked in another.

Costs roughly $0.0001 — a handful of short chunks through text-embedding-3-small.

    .venv/bin/python -m scripts.e2e_ingest
"""

from __future__ import annotations

import base64
import os
import sys
import uuid

from sqlalchemy import text

SPEC_V1 = b"""# Spindle Assembly SA-900

Spindle assembly SA-900: final torque specification is 300 Nm, applied in three
stages of 120, 220 and 300 Nm. Re-torque after 75 operating hours.

## Coolant

Coolant concentration for the SA-900 must be held between 7 and 9 percent.
"""

# Same filename, different content — a new *version*, not a new document.
SPEC_V2 = b"""# Spindle Assembly SA-900

Spindle assembly SA-900: final torque specification is 315 Nm following
revision C, applied in three stages of 120, 220 and 315 Nm. Re-torque after 75
operating hours.

## Coolant

Coolant concentration for the SA-900 must be held between 7 and 9 percent.

## Fasteners

The SA-900 uses M20 fasteners. Earlier revisions used M18 and are not
interchangeable.
"""


def main() -> int:
    os.environ.setdefault("SERVICE_ROLE", "test")
    os.environ.setdefault("ENVIRONMENT", "local")

    from platform_core.adapters.local.object_store import S3ObjectStore
    from platform_core.api.routes.documents import DocumentCreate, create_document
    from platform_core.db.engine import owner_session, tenant_session
    from platform_core.identity.principal import (
        ActorType,
        Principal,
        RequestContext,
        Role,
        Tenant,
    )
    from platform_core.settings import get_settings
    from workloads.reindex import workload as reindex

    settings = get_settings()
    if not settings.openai_api_key:
        print("OPENAI_API_KEY is not set — embedding needs it.", file=sys.stderr)
        return 1

    store = S3ObjectStore()
    store.ensure_bucket()

    slug = f"ingest-{uuid.uuid4().hex[:8]}"
    with owner_session() as s:
        tenant_id = s.execute(
            text("INSERT INTO tenant (slug, name) VALUES (:s, 'Ingest check') RETURNING id"),
            {"s": slug},
        ).scalar_one()
    tenant = Tenant(id=tenant_id, slug=slug)

    failures: list[str] = []

    def check(label: str, condition: bool, detail: str = "") -> None:
        print(f"  {'ok  ' if condition else 'FAIL'}  {label}{f' — {detail}' if detail else ''}")
        if not condition:
            failures.append(label)

    try:
        with tenant_session(tenant) as s:
            principal_id = s.execute(
                text(
                    "INSERT INTO principal (tenant_id, subject, roles) "
                    "VALUES (:t, 'ingest@example.com', ARRAY['owner']) RETURNING id"
                ),
                {"t": tenant.id},
            ).scalar_one()

        def fresh_ctx() -> RequestContext:
            return RequestContext(
                principal=Principal(
                    id=principal_id, tenant=tenant, subject="ingest@example.com",
                    roles=frozenset({Role.OWNER}), actor_type=ActorType.HUMAN,
                ),
                labels={"workload": "reindex", "task": "reindex"},
            )

        # ── upload ────────────────────────────────────────────────────────
        print("\nupload")
        first = create_document(
            DocumentCreate(
                collection="specs", filename="sa900.md",
                content_base64=base64.b64encode(SPEC_V1).decode(),
            ),
            fresh_ctx(),
        )
        check("upload returns a new document", first["unchanged"] is False)

        with tenant_session(tenant) as s:
            storage_key = s.execute(
                text("SELECT storage_key FROM document WHERE id = :i"), {"i": first["id"]}
            ).scalar_one()
        check("storage_key is tenant-derived", storage_key.startswith(f"t/{tenant.id}/"),
              storage_key)
        check("the bytes are actually retained",
              store.get(fresh_ctx(), storage_key) == SPEC_V1)

        repeat = create_document(
            DocumentCreate(
                collection="specs", filename="sa900.md",
                content_base64=base64.b64encode(SPEC_V1).decode(),
            ),
            fresh_ctx(),
        )
        check("re-uploading identical bytes is a no-op", repeat.get("unchanged") is True)

        # ── first build ───────────────────────────────────────────────────
        print("\nfirst build")
        build_one = reindex.run(fresh_ctx(), {"collection": "specs"})
        check("the build was promoted", build_one.get("promoted") is not False)
        check("chunks were ingested, not copied",
              build_one["ingested_chunks"] > 0 and build_one["copied_chunks"] == 0,
              f"ingested={build_one['ingested_chunks']} copied={build_one['copied_chunks']}")
        check("no current document is left without chunks",
              build_one["documents_without_chunks"] == 0)
        check("nothing was skipped", not build_one["skipped_documents"],
              str(build_one["skipped_documents"]))

        with tenant_session(tenant) as s:
            texts = s.execute(
                text(
                    "SELECT text FROM chunk WHERE collection = 'specs' "
                    "AND build_version = :v ORDER BY ordinal"
                ),
                {"v": build_one["build_version"]},
            ).scalars().all()
        check("the torque figure is retrievable", any("300 Nm" in t for t in texts))
        check("headings became breadcrumbs", any(t.startswith("Coolant") for t in texts))

        # ── replace ───────────────────────────────────────────────────────
        print("\nreplace")
        replaced = create_document(
            DocumentCreate(
                collection="specs", filename="sa900.md",
                content_base64=base64.b64encode(SPEC_V2).decode(),
            ),
            fresh_ctx(),
        )
        check("the replacement supersedes the original",
              replaced.get("replaced") == first["id"])

        build_two = reindex.run(fresh_ctx(), {"collection": "specs"})
        check("the replacement was re-chunked from its bytes",
              build_two["ingested_chunks"] > 0,
              f"ingested={build_two['ingested_chunks']}")
        check("no current document is left without chunks",
              build_two["documents_without_chunks"] == 0)

        with tenant_session(tenant) as s:
            texts_two = s.execute(
                text(
                    "SELECT text FROM chunk WHERE collection = 'specs' "
                    "AND build_version = :v"
                ),
                {"v": build_two["build_version"]},
            ).scalars().all()
        check("the new figure is served", any("315 Nm" in t for t in texts_two))
        check("the superseded figure is gone from the live build",
              not any("300 Nm" in t for t in texts_two))
        check("new content in the replacement was picked up",
              any("M20" in t for t in texts_two))

        # ── idempotence ───────────────────────────────────────────────────
        print("\nrebuild with nothing changed")
        build_three = reindex.run(fresh_ctx(), {"collection": "specs"})
        check("an unchanged rebuild re-embeds nothing",
              build_three["ingested_chunks"] == 0,
              f"ingested={build_three['ingested_chunks']}")
        check("it copied the corpus forward instead",
              build_three["copied_chunks"] == len(texts_two),
              f"copied={build_three['copied_chunks']} of {len(texts_two)}")

    finally:
        with owner_session() as s:
            s.execute(text("DELETE FROM tenant WHERE id = :t"), {"t": tenant.id})
        for stored in store.list(
            RequestContext(
                principal=Principal(
                    id=uuid.uuid4(), tenant=tenant, subject="cleanup@example.com",
                    roles=frozenset({Role.OWNER}), actor_type=ActorType.HUMAN,
                )
            )
        ):
            store.delete(
                RequestContext(
                    principal=Principal(
                        id=uuid.uuid4(), tenant=tenant, subject="cleanup@example.com",
                        roles=frozenset({Role.OWNER}), actor_type=ActorType.HUMAN,
                    )
                ),
                stored.key,
            )

    print()
    if failures:
        print(f"{len(failures)} check(s) failed: {', '.join(failures)}")
        return 1
    print("ingest lifecycle green")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
