"""Seed a demo tenant, a login, and a small embedded corpus.

Costs roughly $0.0002 — twelve short chunks through text-embedding-3-small.

Slugs are `demo-` prefixed on purpose. The test fixtures use
`acme-industrial` / `globex-motors`, and conftest's autouse cleanup deletes
exactly those slugs after every test — so demo data sharing that namespace is
destroyed the next time anyone runs the suite. It was, once.

Two tenants are created, not one. A single-tenant demo cannot show the property
that matters: the second tenant holds a document with a *contradictory* torque
figure, so asking the same question as each tenant returns different answers.
If isolation ever broke, the demo would show it rather than hide it.

    .venv/bin/python -m scripts.seed_chat
"""

from __future__ import annotations

import hashlib
import json
import os
import sys

from sqlalchemy import text

# (collection, filename, chunk text) — deliberately specific, so a wrong answer
# is obvious rather than plausible.
ACME_CORPUS = [
    ("maintenance", "spindle-specs.md",
     "Spindle assembly SA-400: final torque specification is 145 Nm, applied in "
     "three stages of 60, 110 and 145 Nm. Re-torque after 50 operating hours."),
    ("maintenance", "spindle-specs.md",
     "Spindle assembly SA-200: final torque specification is 90 Nm. Do not "
     "exceed 95 Nm; the housing thread strips above that figure."),
    ("maintenance", "vfd-faults.md",
     "VFD fault F-051 indicates DC bus overvoltage. Most commonly caused by too "
     "short a deceleration ramp on a high-inertia load. Extend decel time or "
     "fit a braking resistor."),
    ("maintenance", "vfd-faults.md",
     "VFD fault F-023 indicates motor overtemperature from the PTC input. Check "
     "the thermistor wiring before assuming a genuine thermal event; an open "
     "circuit reads identically to an over-temperature."),
    ("maintenance", "lubrication.md",
     "Main bearing lubrication interval is 500 operating hours under normal "
     "load, reduced to 250 hours above 60 percent duty cycle. Use ISO VG 68."),
    ("maintenance", "commissioning.md",
     "Commissioning sequence: verify phase rotation, confirm the motor "
     "nameplate matches drive parameter P-101, run an autotune with the load "
     "decoupled, then reconnect and verify no-load current is below 40 percent "
     "of rated."),
]

# Same collection name, same question space, one deliberately different fact.
GLOBEX_CORPUS = [
    ("maintenance", "spindle-specs.md",
     "Spindle assembly SA-400: final torque specification is 210 Nm on the "
     "Globex variant, which uses an M16 fastener rather than the M12 used "
     "elsewhere. Re-torque after 100 operating hours."),
    ("maintenance", "coolant.md",
     "Coolant concentration must be held between 6 and 8 percent. Below 5 "
     "percent, tool life falls sharply and corrosion appears within days."),
]

DEMO_PASSWORD = "demo-password-1234"


def canonical_id(chunk_text: str) -> str:
    """c_<sha1:16> — content-addressed, stable across rebuilds.

    The single id namespace the platform uses. Nothing derived from a position
    in a list ever crosses a boundary, which is what keeps a stored eval result
    meaningful after the corpus is rebuilt.
    """
    return "c_" + hashlib.sha1(chunk_text.strip().encode()).hexdigest()[:16]


def main() -> int:
    os.environ.setdefault("SERVICE_ROLE", "test")
    os.environ.setdefault("ENVIRONMENT", "local")

    from platform_core.db.engine import owner_session, tenant_session
    from platform_core.identity.auth import hash_password
    from platform_core.identity.principal import (
        ActorType,
        Principal,
        RequestContext,
        Role,
        Tenant,
    )
    from platform_core.observability.llm import build_client
    from platform_core.settings import get_settings

    settings = get_settings()
    if not settings.openai_api_key:
        print("OPENAI_API_KEY is not set — embedding needs it.", file=sys.stderr)
        return 1

    llm = build_client()
    created = []

    for slug, name, corpus, subject, owner_subject, reviewer_subject in (
        ("demo-acme", "Acme Industrial (demo)", ACME_CORPUS,
         "operator@acme.example", "owner@acme.example", "reviewer@acme.example"),
        ("demo-globex", "Globex Motors (demo)", GLOBEX_CORPUS,
         "operator@globex.example", "owner@globex.example", "reviewer@globex.example"),
    ):
        with owner_session() as s:
            tenant_id = s.execute(
                text(
                    "INSERT INTO tenant (slug, name) VALUES (:s, :n) "
                    "ON CONFLICT (slug) DO UPDATE SET name = EXCLUDED.name RETURNING id"
                ),
                {"s": slug, "n": name},
            ).scalar_one()
        tenant = Tenant(id=tenant_id, slug=slug)

        with tenant_session(tenant) as s:
            principal_id = s.execute(
                text(
                    "INSERT INTO principal (tenant_id, subject, roles, password_hash) "
                    "VALUES (:t, :sub, ARRAY['operator'], :pw) "
                    "ON CONFLICT (tenant_id, subject) DO UPDATE "
                    "  SET roles = EXCLUDED.roles, password_hash = EXCLUDED.password_hash "
                    "RETURNING id"
                ),
                {"t": tenant.id, "sub": subject, "pw": hash_password(DEMO_PASSWORD)},
            ).scalar_one()

            # An owner alongside the operator. The console's admin surface needs
            # budget:manage, member:manage and tool:approve — none of which an
            # operator holds — so without this principal those panels can only
            # ever render 403 and the maker/checker separation cannot be
            # demonstrated at all. Two principals per tenant is also what makes
            # the separation visible: the operator raises work, the owner
            # decides it, and neither can do the other's half.
            s.execute(
                text(
                    "INSERT INTO principal (tenant_id, subject, roles, password_hash) "
                    "VALUES (:t, :sub, ARRAY['owner'], :pw) "
                    "ON CONFLICT (tenant_id, subject) DO UPDATE "
                    "  SET roles = EXCLUDED.roles, password_hash = EXCLUDED.password_hash"
                ),
                {"t": tenant.id, "sub": owner_subject, "pw": hash_password(DEMO_PASSWORD)},
            )

            # A reviewer, so maker-cannot-be-checker is demonstrable and not
            # merely enforced. The owner drafts a schema and is then refused
            # approval of it; without a second principal holding
            # schema:approve there is no way to complete the flow at all, and
            # the separation looks like a dead end rather than a control.
            s.execute(
                text(
                    "INSERT INTO principal (tenant_id, subject, roles, password_hash) "
                    "VALUES (:t, :sub, ARRAY['reviewer'], :pw) "
                    "ON CONFLICT (tenant_id, subject) DO UPDATE "
                    "  SET roles = EXCLUDED.roles, password_hash = EXCLUDED.password_hash"
                ),
                {"t": tenant.id, "sub": reviewer_subject, "pw": hash_password(DEMO_PASSWORD)},
            )

        ctx = RequestContext(
            principal=Principal(
                id=principal_id, tenant=tenant, subject=subject,
                roles=frozenset({Role.OPERATOR}), actor_type=ActorType.HUMAN,
            ),
            labels={"workload": "chat", "task": "ingest"},
        )

        texts = [chunk for _, _, chunk in corpus]
        print(f"embedding {len(texts)} chunks for {slug}…")
        vectors = llm.embed(ctx, texts)

        # One document row per *file*, not per chunk. Writing a document per
        # chunk produced 49 document rows for 48 chunks and two filenames
        # duplicated within a collection — invisible while identity was the
        # content hash (two chunks of one file legitimately hash differently),
        # and a unique-constraint violation the moment filename became the
        # identity in migration 0016. A document is a file; chunks hang off it.
        file_texts: dict[tuple[str, str], list[str]] = {}
        for collection, filename, chunk in corpus:
            file_texts.setdefault((collection, filename), []).append(chunk)

        document_ids: dict[tuple[str, str], object] = {}
        with tenant_session(tenant) as s:
            for (collection, filename), chunks in file_texts.items():
                body = "\n\n".join(chunks)
                sha = hashlib.sha256(f"{filename}:{body}".encode()).hexdigest()
                document_ids[(collection, filename)] = s.execute(
                    text(
                        "INSERT INTO document (tenant_id, workload, collection, filename, "
                        "  content_sha256, byte_size, storage_key, uploaded_by) "
                        "VALUES (:t, 'chat', :c, :f, :sha, :size, :key, :by) "
                        "ON CONFLICT (tenant_id, collection, filename) "
                        "  WHERE superseded_at IS NULL DO UPDATE "
                        "  SET content_sha256 = EXCLUDED.content_sha256 RETURNING id"
                    ),
                    {
                        "t": tenant.id, "c": collection, "f": filename, "sha": sha,
                        "size": len(body.encode()), "key": f"{slug}/{filename}",
                        "by": principal_id,
                    },
                ).scalar_one()

            # Position within the document. Was hardcoded 0, which was harmless
            # while every chunk owned its own document row and is wrong now that
            # chunks share one — ordinal is what the lexical and graph
            # retrievers use to order passages within a file.
            seen: dict[tuple[str, str], int] = {}
            for (collection, filename, chunk), vector in zip(corpus, vectors, strict=True):
                key = (collection, filename)
                document_id = document_ids[key]
                ordinal = seen.get(key, 0)
                seen[key] = ordinal + 1
                s.execute(
                    text(
                        "INSERT INTO chunk (tenant_id, document_id, collection, "
                        "  canonical_id, ordinal, text, embedding, embedding_model, meta) "
                        "VALUES (:t, :d, :c, :cid, :ord, :txt, CAST(:vec AS vector), "
                        "        :model, :meta) "
                        "ON CONFLICT (tenant_id, collection, canonical_id, build_version) "
                        "DO UPDATE SET text = EXCLUDED.text, embedding = EXCLUDED.embedding"
                    ),
                    {
                        "t": tenant.id, "d": document_id, "c": collection,
                        "cid": canonical_id(chunk), "ord": ordinal, "txt": chunk,
                        "vec": str(vector), "model": settings.embedding_model,
                        "meta": json.dumps({"source": filename}),
                    },
                )

        created.append((slug, subject, owner_subject, reviewer_subject, len(corpus)))
        print(f"  {slug}: {len(corpus)} chunks indexed")

    print("\nSeeded. Log in with:")
    for slug, subject, owner_subject, reviewer_subject, _count in created:
        print(f"  tenant={slug:18} subject={subject:26} password={DEMO_PASSWORD}")
        print(f"  {'':18} subject={owner_subject:26} (owner — admin console)")
        print(f"  {'':18} subject={reviewer_subject:26} (reviewer — approves schemas)")
    print("\nTry asking both tenants: \"What is the torque spec for spindle SA-400?\"")
    print("They index contradictory figures on purpose — same question, different answer.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
