"""Seed a corpus rich enough for onboarding to synthesise **edge** types.

The `make seed-chat` corpus is six short chunks. It is the right size for
demonstrating tenant isolation and grounded answers, and the wrong size for
onboarding: a draft over it aggregated 4 relations, synthesised 0 edge types,
and wrote a predicate map with 0 entries — a schema-only bundle that builds
entities and no edges.

That is not a bug in the pipeline. Edge-type synthesis needs the same relation
to recur across chunks before it will promote it to a type; a corpus where every
relation appears once looks like noise, and treating it otherwise is how a
taxonomy fills with spurious edges.

So this corpus is built for that property on purpose: a small cast of recurring
entities (spindle SA-400, VFD-11 drive, coolant pump CP-7 …) tied by a small set
of relations that each appear many times — *requires*, *part of*, *causes*,
*measured by*, *replaced during*. It is still small enough to be cheap.

    .venv/bin/python -m scripts.seed_onboarding_corpus

Roughly 40 chunks: ~40 cheap extraction calls plus the fixed 8-step synthesis,
about $0.05 per draft at current prices.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys

from sqlalchemy import text

TENANT_SLUG = "demo-acme"
COLLECTION = "kg-demo"

# Deliberately repetitive in structure. Each relation verb recurs across many
# chunks so aggregation sees it often enough to promote it to an edge type.
CORPUS: list[tuple[str, str]] = [
    ("spindle-sa400-overview.md",
     "Spindle assembly SA-400 is part of machining cell MC-2. The SA-400 contains "
     "bearing set BRG-12 and is driven by variable frequency drive VFD-11. Routine "
     "service of the SA-400 requires torque procedure TP-145."),
    ("spindle-sa400-torque.md",
     "Torque procedure TP-145 applies to spindle assembly SA-400. TP-145 requires "
     "calibrated wrench CW-3. The final torque for SA-400 is 145 Nm applied in three "
     "stages. Bearing set BRG-12 is replaced during procedure TP-145."),
    ("bearing-brg12.md",
     "Bearing set BRG-12 is part of spindle assembly SA-400. BRG-12 wear causes "
     "vibration fault F-207. BRG-12 condition is measured by vibration sensor VS-9. "
     "Replacement of BRG-12 requires torque procedure TP-145."),
    ("vfd11-drive.md",
     "Variable frequency drive VFD-11 drives spindle assembly SA-400. VFD-11 is part "
     "of machining cell MC-2. Overheating of VFD-11 causes fault F-311. VFD-11 "
     "temperature is measured by thermocouple TC-4."),
    ("fault-f207.md",
     "Fault F-207 is a vibration fault on spindle assembly SA-400. F-207 is caused by "
     "bearing set BRG-12 wear. Clearing F-207 requires torque procedure TP-145. F-207 "
     "is detected by vibration sensor VS-9."),
    ("fault-f311.md",
     "Fault F-311 is a thermal trip on variable frequency drive VFD-11. F-311 is "
     "caused by blocked filter FLT-2. Clearing F-311 requires procedure TP-090. F-311 "
     "is detected by thermocouple TC-4."),
    ("coolant-pump-cp7.md",
     "Coolant pump CP-7 is part of machining cell MC-2. CP-7 supplies coolant to "
     "spindle assembly SA-400. CP-7 failure causes fault F-455. Coolant concentration "
     "is measured by refractometer RF-1."),
    ("fault-f455.md",
     "Fault F-455 is a coolant starvation alarm. F-455 is caused by coolant pump CP-7 "
     "failure. Clearing F-455 requires procedure TP-220. F-455 is detected by flow "
     "sensor FS-5."),
    ("procedure-tp090.md",
     "Procedure TP-090 services variable frequency drive VFD-11. TP-090 requires "
     "filter FLT-2 replacement. Filter FLT-2 is part of variable frequency drive "
     "VFD-11. TP-090 is performed every 500 operating hours."),
    ("procedure-tp220.md",
     "Procedure TP-220 services coolant pump CP-7. TP-220 requires seal kit SK-8. "
     "Seal kit SK-8 is part of coolant pump CP-7. TP-220 is performed every 1000 "
     "operating hours."),
    ("filter-flt2.md",
     "Filter FLT-2 is part of variable frequency drive VFD-11. FLT-2 blockage causes "
     "fault F-311. FLT-2 is replaced during procedure TP-090. FLT-2 differential "
     "pressure is measured by gauge DP-6."),
    ("seal-kit-sk8.md",
     "Seal kit SK-8 is part of coolant pump CP-7. SK-8 wear causes fault F-455. SK-8 "
     "is replaced during procedure TP-220. SK-8 has a service life of 1000 hours."),
    ("sensor-vs9.md",
     "Vibration sensor VS-9 monitors spindle assembly SA-400. VS-9 measures bearing "
     "set BRG-12 condition. VS-9 detects fault F-207. VS-9 is part of machining cell "
     "MC-2."),
    ("sensor-tc4.md",
     "Thermocouple TC-4 monitors variable frequency drive VFD-11. TC-4 measures VFD-11 "
     "winding temperature. TC-4 detects fault F-311. TC-4 is part of machining cell "
     "MC-2."),
    ("sensor-fs5.md",
     "Flow sensor FS-5 monitors coolant pump CP-7. FS-5 measures coolant flow rate. "
     "FS-5 detects fault F-455. FS-5 is part of machining cell MC-2."),
    ("cell-mc2.md",
     "Machining cell MC-2 contains spindle assembly SA-400, variable frequency drive "
     "VFD-11 and coolant pump CP-7. MC-2 is monitored by vibration sensor VS-9, "
     "thermocouple TC-4 and flow sensor FS-5."),
    ("wrench-cw3.md",
     "Calibrated wrench CW-3 is required by torque procedure TP-145. CW-3 is "
     "calibrated every 6 months. CW-3 measures applied torque. Use of CW-3 is "
     "mandatory for spindle assembly SA-400."),
    ("gauge-dp6.md",
     "Gauge DP-6 measures filter FLT-2 differential pressure. DP-6 is part of variable "
     "frequency drive VFD-11. A DP-6 reading above 2 bar indicates FLT-2 blockage, "
     "which causes fault F-311."),
    ("refractometer-rf1.md",
     "Refractometer RF-1 measures coolant concentration for coolant pump CP-7. RF-1 is "
     "part of machining cell MC-2. Low concentration causes fault F-455."),
    ("spindle-sa400-service.md",
     "Servicing spindle assembly SA-400 requires torque procedure TP-145 and "
     "calibrated wrench CW-3. Bearing set BRG-12 is replaced during this service. "
     "After service, vibration sensor VS-9 must confirm fault F-207 is cleared."),
    ("vfd11-service.md",
     "Servicing variable frequency drive VFD-11 requires procedure TP-090. Filter "
     "FLT-2 is replaced during this service. After service, thermocouple TC-4 must "
     "confirm fault F-311 is cleared."),
    ("cp7-service.md",
     "Servicing coolant pump CP-7 requires procedure TP-220. Seal kit SK-8 is replaced "
     "during this service. After service, flow sensor FS-5 must confirm fault F-455 is "
     "cleared."),
    ("brg12-inspection.md",
     "Bearing set BRG-12 inspection requires vibration sensor VS-9 readings. BRG-12 is "
     "part of spindle assembly SA-400. Excessive play in BRG-12 causes fault F-207 and "
     "requires torque procedure TP-145."),
    ("mc2-shutdown.md",
     "Shutting down machining cell MC-2 requires stopping variable frequency drive "
     "VFD-11 and coolant pump CP-7. Spindle assembly SA-400 must come to rest before "
     "procedure TP-145 is performed."),
    ("torque-stages.md",
     "Torque procedure TP-145 requires three stages of 60, 110 and 145 Nm. TP-145 "
     "applies to spindle assembly SA-400 and requires calibrated wrench CW-3. "
     "Re-torque after 100 operating hours."),
    ("coolant-spec.md",
     "Coolant concentration for machining cell MC-2 must be held between 6 and 8 "
     "percent. Concentration is measured by refractometer RF-1. Low concentration "
     "causes fault F-455 and damages coolant pump CP-7."),
    ("vibration-limits.md",
     "Vibration on spindle assembly SA-400 must stay below 4.5 mm/s. Vibration is "
     "measured by vibration sensor VS-9. Exceeding the limit causes fault F-207 and "
     "indicates bearing set BRG-12 wear."),
    ("thermal-limits.md",
     "Winding temperature on variable frequency drive VFD-11 must stay below 85 C. "
     "Temperature is measured by thermocouple TC-4. Exceeding the limit causes fault "
     "F-311 and indicates filter FLT-2 blockage."),
    ("flow-limits.md",
     "Coolant flow from coolant pump CP-7 must exceed 12 litres per minute. Flow is "
     "measured by flow sensor FS-5. Falling below the limit causes fault F-455 and "
     "indicates seal kit SK-8 wear."),
    ("maintenance-schedule.md",
     "Torque procedure TP-145 is performed every 100 hours on spindle assembly SA-400. "
     "Procedure TP-090 is performed every 500 hours on variable frequency drive "
     "VFD-11. Procedure TP-220 is performed every 1000 hours on coolant pump CP-7."),
    ("spare-parts.md",
     "Bearing set BRG-12 is a spare for spindle assembly SA-400. Filter FLT-2 is a "
     "spare for variable frequency drive VFD-11. Seal kit SK-8 is a spare for coolant "
     "pump CP-7. Each is replaced during its respective procedure."),
    ("fault-tree.md",
     "Fault F-207 is caused by bearing set BRG-12 wear. Fault F-311 is caused by "
     "filter FLT-2 blockage. Fault F-455 is caused by seal kit SK-8 wear. Each fault "
     "requires its corresponding procedure to clear."),
    ("sensor-map.md",
     "Vibration sensor VS-9 detects fault F-207. Thermocouple TC-4 detects fault "
     "F-311. Flow sensor FS-5 detects fault F-455. All three sensors are part of "
     "machining cell MC-2."),
    ("tooling.md",
     "Calibrated wrench CW-3 is required by torque procedure TP-145. Gauge DP-6 is "
     "required by procedure TP-090. Refractometer RF-1 is required by procedure "
     "TP-220."),
    ("cell-overview.md",
     "Machining cell MC-2 contains three major assemblies. Spindle assembly SA-400 "
     "performs cutting. Variable frequency drive VFD-11 drives the spindle. Coolant "
     "pump CP-7 supplies coolant to the spindle."),
    ("commissioning.md",
     "Commissioning machining cell MC-2 requires torque procedure TP-145 on spindle "
     "assembly SA-400, procedure TP-090 on variable frequency drive VFD-11, and "
     "procedure TP-220 on coolant pump CP-7."),
    ("inspection-round.md",
     "The daily inspection round reads vibration sensor VS-9, thermocouple TC-4 and "
     "flow sensor FS-5. Any reading outside limits indicates a fault on spindle "
     "assembly SA-400, variable frequency drive VFD-11 or coolant pump CP-7."),
    ("wear-indicators.md",
     "Bearing set BRG-12 wear is measured by vibration sensor VS-9. Filter FLT-2 "
     "blockage is measured by gauge DP-6. Seal kit SK-8 wear is measured by flow "
     "sensor FS-5."),
    ("escalation.md",
     "Fault F-207 on spindle assembly SA-400 escalates to a cell stop of machining "
     "cell MC-2. Fault F-311 on variable frequency drive VFD-11 escalates to a cell "
     "stop. Fault F-455 on coolant pump CP-7 escalates to a cell stop."),
    ("post-service-checks.md",
     "After torque procedure TP-145, vibration sensor VS-9 confirms spindle assembly "
     "SA-400 is within limits. After procedure TP-090, thermocouple TC-4 confirms "
     "variable frequency drive VFD-11 is within limits."),
]


def canonical_id(chunk_text: str) -> str:
    return "c_" + hashlib.sha1(chunk_text.strip().encode()).hexdigest()[:16]


def main() -> int:
    os.environ.setdefault("SERVICE_ROLE", "test")
    os.environ.setdefault("ENVIRONMENT", "local")

    from platform_core.db.engine import owner_session, tenant_session
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

    with owner_session() as s:
        row = s.execute(
            text("SELECT id, slug FROM tenant WHERE slug = :s"), {"s": TENANT_SLUG}
        ).one_or_none()
    if row is None:
        print(f"tenant {TENANT_SLUG!r} not found — run `make seed-chat` first.",
              file=sys.stderr)
        return 1
    tenant = Tenant(id=row.id, slug=row.slug)

    with tenant_session(tenant) as s:
        principal_id = s.execute(
            text("SELECT id FROM principal WHERE subject = :sub"),
            {"sub": "operator@acme.example"},
        ).scalar_one()

    ctx = RequestContext(
        principal=Principal(
            id=principal_id, tenant=tenant, subject="operator@acme.example",
            roles=frozenset({Role.OPERATOR}), actor_type=ActorType.HUMAN,
        ),
        labels={"workload": "chat", "task": "ingest"},
    )

    llm = build_client()
    texts = [chunk for _, chunk in CORPUS]
    print(f"embedding {len(texts)} chunks into {TENANT_SLUG}/{COLLECTION}…")
    vectors = llm.embed(ctx, texts)

    with tenant_session(tenant) as s:
        for ordinal, ((filename, chunk), vector) in enumerate(
            zip(CORPUS, vectors, strict=True)
        ):
            sha = hashlib.sha256(f"{filename}:{chunk}".encode()).hexdigest()
            document_id = s.execute(
                text(
                    "INSERT INTO document (tenant_id, workload, collection, filename, "
                    "content_sha256, byte_size, storage_key, uploaded_by) "
                    "VALUES (:t, 'echo', :c, :f, :sha, :size, :key, :by) "
                    "ON CONFLICT (tenant_id, collection, filename) "
                    "  WHERE superseded_at IS NULL DO UPDATE "
                    "  SET content_sha256 = EXCLUDED.content_sha256 RETURNING id"
                ),
                {
                    "t": tenant.id, "c": COLLECTION, "f": filename, "sha": sha,
                    "size": len(chunk.encode()),
                    "key": f"{tenant.slug}/{COLLECTION}/{sha}.md",
                    "by": principal_id,
                },
            ).scalar_one()
            s.execute(
                text(
                    "INSERT INTO chunk (tenant_id, document_id, collection, "
                    "canonical_id, ordinal, text, embedding, embedding_model, meta) "
                    "VALUES (:t, :d, :c, :cid, :ord, :txt, :vec, :model, "
                    "        CAST(:meta AS jsonb)) "
                    "ON CONFLICT (tenant_id, collection, canonical_id, build_version) "
                    "DO UPDATE SET text = EXCLUDED.text, embedding = EXCLUDED.embedding"
                ),
                {
                    "t": tenant.id, "d": document_id, "c": COLLECTION,
                    "cid": canonical_id(chunk), "ord": ordinal,
                    "txt": chunk, "vec": str(list(vector)),
                    "model": settings.embedding_model,
                    "meta": json.dumps({"source": filename}),
                },
            )

    print(f"seeded {len(CORPUS)} chunks into {TENANT_SLUG}/{COLLECTION}")
    print("\nNext: draft a schema over it —")
    print(f'  POST /api/onboard/sessions {{"domain":"kgdemo","collection":"{COLLECTION}"}}')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
