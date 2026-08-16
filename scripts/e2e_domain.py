"""Live proof: a brand-new domain, from uploaded files to a chat answer with edges.

Every earlier check exercised one seam. This runs the whole thing the way an
operator would, on a domain the platform has never seen:

    upload files → reindex ingests and embeds → onboarding drafts a schema →
    a second principal approves it → publish → chat in graph mode

and reports the one number that says whether it worked: **edges**. A knowledge
graph with entities and no edges answers exactly like a populated one, with no
error and worse results, so "it returned an answer" proves nothing on its own.

## The corpus is generated, and the size is the point

`seed_onboarding_corpus.py` writes ~40 short chunks *directly into the chunk
table*. Going through the real upload path, those same 40 paragraphs are one
document of about 2,400 tokens, which this platform's chunker turns into roughly
six 420-token chunks — and six chunks produced 0 edge types when it was measured.
Edge-type synthesis only promotes a relation that **recurs across chunks**, so
the number of chunks, not the number of words, is what decides whether a domain
gets edges at all.

So the generator below targets ~40 chunks *after* chunking: roughly 17,000
tokens of dense relational prose over a small cast of recurring entities, tied by
a small set of relation verbs that each appear many times — *is part of*,
*supplies power to*, *cools*, *causes*, *is measured by*, *is detected by*,
*requires*, *is replaced during*.

Every section carries a unique identifier, because two chunks with identical text
hash to the same `canonical_id` and the second is silently dropped by the unique
index. A corpus that repeats itself verbatim would quietly be smaller than it
looks.

Costs roughly $0.05–0.10: one extraction call per sampled chunk, plus the fixed
synthesis steps, plus one chat turn.

    .venv/bin/python -m scripts.e2e_domain
    .venv/bin/python -m scripts.e2e_domain --domain datacenter --keep
"""

from __future__ import annotations

import argparse
import base64
import os
import sys

from sqlalchemy import text

TENANT_SLUG = "demo-acme"
DOMAIN = "datacenter"
COLLECTION = "datacenter-ops"

# A small cast, deliberately. Edge-type synthesis needs the *same* relation
# between the *same* kinds of thing to recur; a cast of two hundred entities
# each mentioned once looks like noise and is correctly ignored.
HALLS = ["HALL-A", "HALL-B"]
RACKS = [f"RK-{n:02d}" for n in range(1, 9)]
PDUS = ["PDU-A1", "PDU-A2", "PDU-B1"]
CRACS = ["CRAC-3", "CRAC-4"]
SWITCHES = ["SW-TOR-3", "SW-TOR-7", "SW-CORE-1"]
SENSORS = {
    "TS-11": ("temperature sensor", "supply air temperature"),
    "TS-12": ("temperature sensor", "return air temperature"),
    "HS-2": ("humidity sensor", "relative humidity"),
    "CS-5": ("current sensor", "branch circuit current"),
    "LS-4": ("leak sensor", "condensate leakage"),
    "DP-8": ("differential pressure gauge", "filter differential pressure"),
}
ALARMS = {
    "ALM-401": "supply air over-temperature",
    "ALM-512": "branch circuit overcurrent",
    "ALM-330": "humidity out of band",
    "ALM-207": "condensate leak detected",
    "ALM-118": "uplink flap",
}
PROCEDURES = {
    "PR-100": "CRAC filter replacement",
    "PR-210": "PDU breaker load test",
    "PR-055": "leak response and containment",
    "PR-140": "switch firmware upgrade",
    "PR-320": "condensate pump service",
}
PARTS = {
    "FLT-9": ("filter cartridge", "CRAC-3", "PR-100", "ALM-401"),
    "BRK-22": ("moulded case breaker", "PDU-A1", "PR-210", "ALM-512"),
    "PMP-3": ("condensate pump", "CRAC-4", "PR-320", "ALM-207"),
    "FAN-14": ("EC plug fan", "CRAC-3", "PR-100", "ALM-401"),
    "SFP-6": ("optical transceiver", "SW-TOR-7", "PR-140", "ALM-118"),
}


def _paragraphs(seq: list[str]) -> str:
    return "\n\n".join(seq)


def build_corpus() -> dict[str, bytes]:
    """Generate the corpus. Deterministic — the same bytes every run.

    Deterministic because a re-run must be recognisable as the *same* documents:
    upload deduplicates on content hash, so a generator that varied its wording
    would supersede every document and force a full re-embed each time.
    """
    docs: dict[str, list[str]] = {}

    def add(name: str, *paras: str) -> None:
        docs.setdefault(name, []).extend(paras)

    # ── halls: the containment relation everything else hangs off ─────────
    for hall in HALLS:
        racks = RACKS[:4] if hall == "HALL-A" else RACKS[4:]
        crac = CRACS[0] if hall == "HALL-A" else CRACS[1]
        pdus = PDUS[:2] if hall == "HALL-A" else PDUS[2:]
        add(
            f"hall-{hall.lower()}.md",
            f"# Data hall {hall}",
            f"Data hall {hall} contains racks {', '.join(racks)}. Each of these racks "
            f"is part of data hall {hall}. Computer room air conditioner {crac} cools "
            f"data hall {hall}. Power distribution units {' and '.join(pdus)} are part "
            f"of data hall {hall} and supply power to every rack in it.",
            f"## Environmental envelope for {hall}",
            f"Supply air temperature in data hall {hall} is measured by temperature "
            f"sensor TS-11 and must be held between 18 and 27 degrees Celsius. A "
            f"reading above 27 degrees causes alarm ALM-401. Alarm ALM-401 is detected "
            f"by temperature sensor TS-11. Clearing alarm ALM-401 requires procedure "
            f"PR-100. Return air temperature in data hall {hall} is measured by "
            f"temperature sensor TS-12 and is expected to run 10 to 14 degrees above "
            f"supply.",
            f"## Humidity control in {hall}",
            f"Relative humidity in data hall {hall} is measured by humidity sensor "
            f"HS-2 and must be held between 40 and 60 percent. A reading outside that "
            f"band causes alarm ALM-330. Alarm ALM-330 is detected by humidity sensor "
            f"HS-2. Humidity sensor HS-2 is part of data hall {hall}. Sustained "
            f"humidity below 40 percent in data hall {hall} increases electrostatic "
            f"discharge risk on every rack it contains.",
            f"## Leak detection in {hall}",
            f"Leak sensor LS-4 is part of data hall {hall} and monitors computer room "
            f"air conditioner {crac}. Condensate leakage from {crac} causes alarm "
            f"ALM-207. Alarm ALM-207 is detected by leak sensor LS-4. Clearing alarm "
            f"ALM-207 requires procedure PR-055. Condensate pump PMP-3 is part of "
            f"computer room air conditioner {crac} and is replaced during procedure "
            f"PR-320.",
        )

    # ── racks ─────────────────────────────────────────────────────────────
    for index, rack in enumerate(RACKS):
        hall = HALLS[0] if index < 4 else HALLS[1]
        pdu = PDUS[index % len(PDUS)]
        switch = SWITCHES[index % len(SWITCHES)]
        crac = CRACS[0] if index < 4 else CRACS[1]
        draw = 4.2 + index * 0.6
        add(
            f"rack-{rack.lower()}.md",
            f"# Rack {rack}",
            f"Rack {rack} is part of data hall {hall}. Power distribution unit {pdu} "
            f"supplies power to rack {rack}. Top of rack switch {switch} is part of "
            f"rack {rack}. Computer room air conditioner {crac} cools rack {rack}. "
            f"Design load for rack {rack} is {draw:.1f} kW across two feeds.",
            f"## Power for {rack}",
            f"Branch circuit current for rack {rack} is measured by current sensor "
            f"CS-5. A sustained draw above {draw + 1.5:.1f} kW on rack {rack} causes "
            f"alarm ALM-512. Alarm ALM-512 is detected by current sensor CS-5. "
            f"Clearing alarm ALM-512 requires procedure PR-210. Moulded case breaker "
            f"BRK-22 is part of power distribution unit {pdu} and protects the feed to "
            f"rack {rack}.",
            f"## Cooling for {rack}",
            f"Supply air temperature at rack {rack} is measured by temperature sensor "
            f"TS-11. Loss of airflow from computer room air conditioner {crac} causes "
            f"alarm ALM-401 at rack {rack}. Clearing alarm ALM-401 requires procedure "
            f"PR-100. Filter cartridge FLT-9 is part of computer room air conditioner "
            f"{crac} and is replaced during procedure PR-100.",
            f"## Network for {rack}",
            f"Top of rack switch {switch} is part of rack {rack} and uplinks to core "
            f"switch SW-CORE-1. Optical transceiver SFP-6 is part of top of rack "
            f"switch {switch}. Degradation of optical transceiver SFP-6 causes alarm "
            f"ALM-118. Clearing alarm ALM-118 requires procedure PR-140. Optical "
            f"transceiver SFP-6 is replaced during procedure PR-140.",
        )

    # ── CRAC units ────────────────────────────────────────────────────────
    for crac in CRACS:
        hall = HALLS[0] if crac == CRACS[0] else HALLS[1]
        add(
            f"crac-{crac.lower()}.md",
            f"# Computer room air conditioner {crac}",
            f"Computer room air conditioner {crac} cools data hall {hall}. Filter "
            f"cartridge FLT-9 is part of computer room air conditioner {crac}. EC plug "
            f"fan FAN-14 is part of computer room air conditioner {crac}. Condensate "
            f"pump PMP-3 is part of computer room air conditioner {crac}. Procedure "
            f"PR-100 services computer room air conditioner {crac}.",
            f"## Filter condition on {crac}",
            f"Filter differential pressure across computer room air conditioner {crac} "
            f"is measured by differential pressure gauge DP-8. A reading above 250 "
            f"pascals indicates that filter cartridge FLT-9 is blocked. A blocked "
            f"filter cartridge FLT-9 causes alarm ALM-401. Filter cartridge FLT-9 is "
            f"replaced during procedure PR-100. Procedure PR-100 is performed every "
            f"2000 operating hours.",
            f"## Condensate handling on {crac}",
            f"Condensate pump PMP-3 is part of computer room air conditioner {crac}. "
            f"Failure of condensate pump PMP-3 causes alarm ALM-207. Alarm ALM-207 is "
            f"detected by leak sensor LS-4. Condensate pump PMP-3 is replaced during "
            f"procedure PR-320. Procedure PR-320 services computer room air "
            f"conditioner {crac} and is performed every 4000 operating hours.",
            f"## Airflow on {crac}",
            f"EC plug fan FAN-14 is part of computer room air conditioner {crac} and "
            f"is replaced during procedure PR-100. Fan failure on computer room air "
            f"conditioner {crac} causes alarm ALM-401. Supply air temperature is "
            f"measured by temperature sensor TS-11 and return air temperature is "
            f"measured by temperature sensor TS-12. Procedure PR-100 requires both "
            f"readings to be recorded before and after the work.",
        )

    # ── PDUs ──────────────────────────────────────────────────────────────
    for pdu in PDUS:
        hall = HALLS[0] if pdu.startswith("PDU-A") else HALLS[1]
        fed = [r for i, r in enumerate(RACKS) if PDUS[i % len(PDUS)] == pdu]
        add(
            f"pdu-{pdu.lower()}.md",
            f"# Power distribution unit {pdu}",
            f"Power distribution unit {pdu} is part of data hall {hall}. Power "
            f"distribution unit {pdu} supplies power to racks {', '.join(fed)}. "
            f"Moulded case breaker BRK-22 is part of power distribution unit {pdu}. "
            f"Procedure PR-210 services power distribution unit {pdu}.",
            f"## Load monitoring on {pdu}",
            f"Branch circuit current on power distribution unit {pdu} is measured by "
            f"current sensor CS-5. Overcurrent on power distribution unit {pdu} causes "
            f"alarm ALM-512. Alarm ALM-512 is detected by current sensor CS-5. "
            f"Clearing alarm ALM-512 requires procedure PR-210. Power distribution "
            f"unit {pdu} is rated for 63 amps per phase and must not exceed 80 percent "
            f"of that rating continuously.",
            f"## Breaker testing on {pdu}",
            f"Moulded case breaker BRK-22 is part of power distribution unit {pdu} and "
            f"is replaced during procedure PR-210. Procedure PR-210 is performed every "
            f"12 months. A breaker that fails the test on power distribution unit "
            f"{pdu} causes alarm ALM-512 to latch until it is replaced. Current sensor "
            f"CS-5 measures the load on power distribution unit {pdu} throughout the "
            f"test.",
        )

    # ── switches ──────────────────────────────────────────────────────────
    for switch in SWITCHES:
        add(
            f"switch-{switch.lower()}.md",
            f"# Switch {switch}",
            f"Switch {switch} is part of the data hall network fabric. Optical "
            f"transceiver SFP-6 is part of switch {switch}. Procedure PR-140 services "
            f"switch {switch}. Degradation of optical transceiver SFP-6 causes alarm "
            f"ALM-118 on switch {switch}.",
            f"## Uplink health on {switch}",
            f"Alarm ALM-118 on switch {switch} indicates an uplink flap. Alarm ALM-118 "
            f"is caused by a degraded optical transceiver SFP-6. Clearing alarm "
            f"ALM-118 requires procedure PR-140. Optical transceiver SFP-6 is replaced "
            f"during procedure PR-140. An uplink on switch {switch} that flaps more "
            f"than three times in an hour must be shut down administratively.",
            f"## Maintenance window for {switch}",
            f"Procedure PR-140 services switch {switch} and is performed during a "
            f"scheduled window only. Procedure PR-140 requires that the redundant "
            f"uplink on switch {switch} is verified first. Optical transceiver SFP-6 "
            f"is replaced during procedure PR-140 when alarm ALM-118 has recurred.",
        )

    # ── alarms: the reverse index, so each relation recurs from both ends ─
    for alarm, description in ALARMS.items():
        add(
            f"alarm-{alarm.lower()}.md",
            f"# Alarm {alarm}",
            f"Alarm {alarm} indicates {description}.",
            f"## Cause and detection of {alarm}",
            _alarm_body(alarm),
        )

    # ── procedures ────────────────────────────────────────────────────────
    for procedure, description in PROCEDURES.items():
        add(
            f"procedure-{procedure.lower()}.md",
            f"# Procedure {procedure}",
            f"Procedure {procedure} is the {description} procedure.",
            f"## Scope of {procedure}",
            _procedure_body(procedure),
        )

    # ── parts ─────────────────────────────────────────────────────────────
    for part, (kind, parent, procedure, alarm) in PARTS.items():
        add(
            f"part-{part.lower()}.md",
            f"# Part {part}",
            f"Part {part} is a {kind}. Part {part} is part of {parent}. Part {part} is "
            f"replaced during procedure {procedure}. Failure of part {part} causes "
            f"alarm {alarm}. Procedure {procedure} services {parent}.",
            f"## Service life of {part}",
            f"Part {part} is a {kind} fitted to {parent}. Wear on part {part} causes "
            f"alarm {alarm}, and clearing alarm {alarm} requires procedure "
            f"{procedure}. Part {part} is replaced during procedure {procedure} "
            f"whenever alarm {alarm} has recurred twice within one quarter.",
        )

    return {name: _paragraphs(paras).encode() + b"\n" for name, paras in docs.items()}


def _alarm_body(alarm: str) -> str:
    bodies = {
        "ALM-401": (
            "Alarm ALM-401 is a supply air over-temperature alarm on data hall HALL-A "
            "and data hall HALL-B. Alarm ALM-401 is caused by a blocked filter "
            "cartridge FLT-9 or by failure of EC plug fan FAN-14, both of which are "
            "part of computer room air conditioner CRAC-3. Alarm ALM-401 is detected "
            "by temperature sensor TS-11. Clearing alarm ALM-401 requires procedure "
            "PR-100. Filter cartridge FLT-9 is replaced during procedure PR-100."
        ),
        "ALM-512": (
            "Alarm ALM-512 is a branch circuit overcurrent alarm on power distribution "
            "unit PDU-A1, power distribution unit PDU-A2 and power distribution unit "
            "PDU-B1. Alarm ALM-512 is caused by sustained load above the rated branch "
            "current. Alarm ALM-512 is detected by current sensor CS-5. Clearing alarm "
            "ALM-512 requires procedure PR-210. Moulded case breaker BRK-22 is "
            "replaced during procedure PR-210."
        ),
        "ALM-330": (
            "Alarm ALM-330 is a humidity out of band alarm on data hall HALL-A and "
            "data hall HALL-B. Alarm ALM-330 is caused by loss of humidification or by "
            "excess condensate from computer room air conditioner CRAC-4. Alarm "
            "ALM-330 is detected by humidity sensor HS-2. Clearing alarm ALM-330 "
            "requires procedure PR-100 when the cause is a blocked filter cartridge "
            "FLT-9."
        ),
        "ALM-207": (
            "Alarm ALM-207 is a condensate leak alarm on computer room air conditioner "
            "CRAC-3 and computer room air conditioner CRAC-4. Alarm ALM-207 is caused "
            "by failure of condensate pump PMP-3. Alarm ALM-207 is detected by leak "
            "sensor LS-4. Clearing alarm ALM-207 requires procedure PR-055. Condensate "
            "pump PMP-3 is replaced during procedure PR-320."
        ),
        "ALM-118": (
            "Alarm ALM-118 is an uplink flap alarm on switch SW-TOR-3, switch SW-TOR-7 "
            "and switch SW-CORE-1. Alarm ALM-118 is caused by a degraded optical "
            "transceiver SFP-6. Clearing alarm ALM-118 requires procedure PR-140. "
            "Optical transceiver SFP-6 is replaced during procedure PR-140."
        ),
    }
    return bodies[alarm]


def _procedure_body(procedure: str) -> str:
    bodies = {
        "PR-100": (
            "Procedure PR-100 services computer room air conditioner CRAC-3 and "
            "computer room air conditioner CRAC-4. Procedure PR-100 requires filter "
            "cartridge FLT-9 and may require EC plug fan FAN-14. Filter cartridge "
            "FLT-9 is replaced during procedure PR-100. Procedure PR-100 clears alarm "
            "ALM-401. Differential pressure gauge DP-8 measures the result, and a "
            "reading below 120 pascals confirms the work."
        ),
        "PR-210": (
            "Procedure PR-210 services power distribution unit PDU-A1, power "
            "distribution unit PDU-A2 and power distribution unit PDU-B1. Procedure "
            "PR-210 requires moulded case breaker BRK-22. Moulded case breaker BRK-22 "
            "is replaced during procedure PR-210. Procedure PR-210 clears alarm "
            "ALM-512. Current sensor CS-5 measures branch circuit current throughout."
        ),
        "PR-055": (
            "Procedure PR-055 is the leak response procedure for computer room air "
            "conditioner CRAC-3 and computer room air conditioner CRAC-4. Procedure "
            "PR-055 clears alarm ALM-207. Procedure PR-055 requires that condensate "
            "pump PMP-3 is isolated first. Leak sensor LS-4 detects alarm ALM-207 and "
            "must read dry before the hall is released."
        ),
        "PR-140": (
            "Procedure PR-140 services switch SW-TOR-3, switch SW-TOR-7 and switch "
            "SW-CORE-1. Procedure PR-140 requires optical transceiver SFP-6. Optical "
            "transceiver SFP-6 is replaced during procedure PR-140. Procedure PR-140 "
            "clears alarm ALM-118."
        ),
        "PR-320": (
            "Procedure PR-320 services computer room air conditioner CRAC-3 and "
            "computer room air conditioner CRAC-4. Procedure PR-320 requires "
            "condensate pump PMP-3. Condensate pump PMP-3 is replaced during procedure "
            "PR-320. Procedure PR-320 clears alarm ALM-207 when the cause is pump "
            "failure. Leak sensor LS-4 confirms the result."
        ),
    }
    return bodies[procedure]


def diagnose(graph, stats) -> dict:
    """Why the graph has the number of edges it has.

    ``KnowledgeGraph`` records every rejected edge with a reason on
    ``_rejected``. Reading it is the difference between "the graph is thin" and
    "295 of 296 candidate edges were discarded because the schema declares three
    entity types and the corpus contains twelve more". Only one of those is
    actionable.

    Reaching into a private attribute of a class in the read-only engine tree is
    deliberate and worth the fragility: there is no public accessor, and the
    alternative is reporting an edge count with no explanation of it. If the
    attribute disappears, this degrades to an empty dict rather than failing.
    """
    import collections

    rejected = getattr(graph, "_rejected", None)
    if rejected is None:
        return {}

    kinds = collections.Counter(r.get("kind") for r in rejected)
    types = collections.Counter(
        data.get("entity_type") for _, data in graph.graph.nodes(data=True)
    )
    raw_nodes = sum(c for t, c in types.items() if str(t).startswith("raw:"))

    type_mismatch = kinds.get("edge_endpoint_type_mismatch", 0)
    missing_endpoint = kinds.get("edge_missing_endpoint", 0)
    return {
        "nodes": stats.nodes,
        "raw_nodes": raw_nodes,
        "raw_share": raw_nodes / stats.nodes if stats.nodes else 0.0,
        "schema_entity_types": sorted(graph.schema.entity_types),
        "admitted": stats.edges,
        # Candidates are the edges that reached validation: those admitted plus
        # those rejected *as edges*. `entity_type_unknown` is a node-level
        # rejection and is reported separately rather than folded in, since it
        # would double-count.
        "candidates": stats.edges + type_mismatch + missing_endpoint,
        "type_mismatch": type_mismatch,
        "type_unknown": kinds.get("entity_type_unknown", 0),
        "missing_endpoint": missing_endpoint,
    }


# ── the run ───────────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant", default=TENANT_SLUG)
    parser.add_argument("--domain", default=DOMAIN)
    parser.add_argument("--collection", default=COLLECTION)
    parser.add_argument("--sample", type=int, default=120)
    parser.add_argument("--dry-run", action="store_true",
                        help="Generate and upload only; report what a draft would cost.")
    parser.add_argument("--reuse-published", action="store_true",
                        help="Skip drafting if the domain is already published. "
                             "For re-running the chat and taxonomy-fit report "
                             "without paying for a second draft.")
    args = parser.parse_args()

    os.environ.setdefault("SERVICE_ROLE", "test")
    os.environ.setdefault("ENVIRONMENT", "local")

    from platform_core.adapters.local.object_store import S3ObjectStore
    from platform_core.api.routes.documents import DocumentCreate, create_document
    from platform_core.api.routes.onboarding import SeedEval, seed_eval_set
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
    from workloads.graphrag import service as graphrag
    from workloads.onboarding import store as onboarding_store
    from workloads.onboarding import workload as onboarding
    from workloads.reindex import workload as reindex

    settings = get_settings()
    if not settings.openai_api_key:
        print("OPENAI_API_KEY is not set.", file=sys.stderr)
        return 1

    S3ObjectStore().ensure_bucket()

    with owner_session() as s:
        row = s.execute(
            text("SELECT id FROM tenant WHERE slug = :s"), {"s": args.tenant}
        ).scalar_one_or_none()
    if row is None:
        print(f"tenant {args.tenant!r} does not exist — run `make seed-chat` first.",
              file=sys.stderr)
        return 1
    tenant = Tenant(id=row, slug=args.tenant)

    # Two principals, because the maker/checker separation is part of what is
    # being proved: the same person cannot draft and approve.
    with tenant_session(tenant) as s:
        principals = dict(
            s.execute(
                text("SELECT subject, id FROM principal WHERE subject = ANY(:subs)"),
                {"subs": [f"owner@{args.tenant.split('-')[-1]}.example",
                          f"reviewer@{args.tenant.split('-')[-1]}.example"]},
            ).all()
        )
    if len(principals) < 2:
        print(f"need an owner and a reviewer principal in {args.tenant!r} — "
              f"run `make seed-chat`. Found: {sorted(principals)}", file=sys.stderr)
        return 1

    owner_subject = next(k for k in principals if k.startswith("owner@"))
    reviewer_subject = next(k for k in principals if k.startswith("reviewer@"))

    def ctx_for(subject: str, role: Role, **labels: str) -> RequestContext:
        return RequestContext(
            principal=Principal(
                id=principals[subject], tenant=tenant, subject=subject,
                roles=frozenset({role}), actor_type=ActorType.HUMAN,
            ),
            labels={"workload": "onboarding", "task": "ingest", **labels},
        )

    failures: list[str] = []

    def check(label: str, ok: bool, detail: str = "") -> None:
        print(f"  {'ok  ' if ok else 'FAIL'}  {label}{f' — {detail}' if detail else ''}")
        if not ok:
            failures.append(label)

    # ── 1. upload ─────────────────────────────────────────────────────────
    corpus = build_corpus()
    total_bytes = sum(len(v) for v in corpus.values())
    print(f"\n1. upload — {len(corpus)} files, {total_bytes / 1024:.0f} KB")

    uploaded = 0
    for filename, data in sorted(corpus.items()):
        result = create_document(
            DocumentCreate(
                collection=args.collection, filename=filename,
                content_base64=base64.b64encode(data).decode(),
            ),
            ctx_for(owner_subject, Role.OWNER, task="ingest"),
        )
        if not result.get("unchanged"):
            uploaded += 1
    check("every file was accepted", True, f"{uploaded} new, {len(corpus) - uploaded} unchanged")

    # ── 2. ingest ─────────────────────────────────────────────────────────
    print("\n2. reindex — chunk and embed")
    build = reindex.run(
        ctx_for(owner_subject, Role.OWNER, workload="reindex", task="reindex"),
        {"collection": args.collection},
    )
    chunks = build.get("chunk_count", 0)
    check("the build was promoted", chunks > 0, f"build {build['build_version']}, {chunks} chunks")
    check("no document was left without chunks", build["documents_without_chunks"] == 0,
          str(build.get("skipped_documents") or {}))
    # The threshold that decides whether this domain can have edges at all.
    check("the corpus is large enough for edge synthesis", chunks >= 30,
          f"{chunks} chunks — 6 chunks produced 0 edge types when measured")

    if args.dry_run:
        print(f"\ndry run: a draft would sample {min(chunks, args.sample)} chunks, "
              f"one extraction call each, plus the fixed synthesis steps.")
        return 1 if failures else 0

    # ── 3. draft ──────────────────────────────────────────────────────────
    drafter = ctx_for(owner_subject, Role.OWNER, workload="onboarding", task="ingest")
    already = onboarding_store.published_session(drafter, args.domain)

    if args.reuse_published and already:
        print(f"\n3-4. draft, approve and publish — skipped: {args.domain!r} is "
              f"already published (session {already['id']}). Re-running these "
              f"would pay for a second draft of the same corpus.")
    else:
        print(f"\n3. onboarding draft — domain {args.domain!r} (this spends money)")
        with tenant_session(tenant) as s:
            existing = s.execute(
                text("SELECT id FROM onboarding_session WHERE domain = :d "
                     "AND status IN ('drafting','draft_ready','approved')"),
                {"d": args.domain},
            ).scalar_one_or_none()
            if existing is not None:
                s.execute(
                    text("UPDATE onboarding_session SET status = 'cancelled' WHERE id = :i"),
                    {"i": existing},
                )
                print(f"   (cancelled a prior unpublished session {existing})")
            session_id = onboarding_store.create(
                s, drafter, domain=args.domain, collection=args.collection
            )

        draft = onboarding.run(drafter, {
            "session_id": str(session_id), "domain": args.domain,
            "collection": args.collection, "sample": args.sample,
        })
        check("the draft completed", draft.get("outcome") == "draft_ready",
              str(draft.get("outcome")))
        check("a predicate map was synthesised", draft.get("predicates", 0) > 0,
              f"{draft.get('predicates', 0)} predicates, "
              f"{draft.get('instances', 0)} instances")
        check("relations are available", bool(draft.get("relations_available")),
              "without this the published graph will have entities and no edges")

        # ── 3b. candidate queries → a seeded eval set ─────────────────────
        #
        # Before the approval, because curating is refused once the session is
        # published — and because these questions are what the domain is *for*,
        # which a reviewer should read before signing off the taxonomy.
        print("\n3b. candidate questions and the seeded eval set")
        proposed = onboarding_store.candidate_queries(drafter, session_id)
        check("questions were proposed from the corpus", bool(proposed),
              f"{len(proposed)} proposed")
        check("every proposal cites a canonical chunk id",
              all(c.startswith("c_")
                  for q in proposed for c in q["evidence_chunk_ids"]),
              "ids the retriever cannot emit score a permanent false miss")

        for q in proposed:
            onboarding_store.curate_query(drafter, session_id, q["id"], approved=True)
        seeded = seed_eval_set(
            session_id, SeedEval(), ctx_for(owner_subject, Role.OWNER)
        )
        check("an eval set was seeded with no model call",
              seeded["items"] == len(proposed),
              f"{seeded['items']} items, {seeded['items_scoreable']} scoreable")
        check("the seeded citations survive dataset validation",
              seeded["items_scoreable"] > 0,
              "build_dataset refuses non-canonical ids, so this is the proof")

        # ── 4. approve and publish ────────────────────────────────────────
        print("\n4. approve and publish")
        try:
            onboarding_store.approve(drafter, session_id)
            check("the drafter cannot approve their own schema", False,
                  "self-approval was allowed")
        except PermissionError:
            check("the drafter cannot approve their own schema", True,
                  "maker is not checker")

        status = onboarding_store.approve(
            ctx_for(reviewer_subject, Role.REVIEWER, workload="onboarding"), session_id
        )
        check("the reviewer approved it", status == "approved", status)
        published = onboarding_store.publish(
            ctx_for(owner_subject, Role.OWNER, workload="onboarding"), session_id
        )
        check("it is published", published["status"] == "published", published["domain"])

    # ── 5. chat, in graph mode, against the new domain ────────────────────
    print("\n5. chat — graph mode on the new domain")
    graphrag.invalidate(drafter)
    chat_ctx = ctx_for(owner_subject, Role.OWNER, workload="chat", task="chat")
    answer = graphrag.answer(
        chat_ctx,
        question="What causes alarm ALM-401, and which procedure clears it?",
        collection=args.collection,
        llm=build_client(),
        schema_domain=args.domain,
    )
    stats = answer.graph
    check("the graph has edges", stats.edges > 0,
          f"{stats.nodes} nodes, {stats.edges} edges over {stats.documents} chunks")
    check("it is not reported as edgeless", not stats.edgeless)
    check("the graph contributed to retrieval",
          bool(answer.retrieval.get("graph_hits")), str(answer.retrieval))
    check("the answer is grounded", answer.grounded)

    # ── 6. is the taxonomy any good? ──────────────────────────────────────
    #
    # Separate from the checks above, and separately reported, because it is a
    # different question. Everything above asks "did the flow run"; this asks
    # "did it produce a graph worth having". A graph with 84 nodes and 1 edge
    # passes every check above — `relations_available` is true, the extractor is
    # enabled, the cache hits — and still cannot traverse. That is the failure
    # one step beyond the Azure build's `relations_available=false`, and nothing
    # in the platform currently names it.
    graph, _, _, _ = graphrag.build_graph(
        chat_ctx, args.collection, build_client(), schema_domain=args.domain
    )
    quality = diagnose(graph, stats) if graph is not None else {}
    print("\n6. taxonomy fit (reported, not a flow failure)")
    if quality:
        print(f"   entity types declared by the schema : "
              f"{quality['schema_entity_types']}")
        print(f"   nodes with an unclassified raw: type: "
              f"{quality['raw_nodes']}/{quality['nodes']} "
              f"({quality['raw_share']:.0%})")
        print(f"   candidate edges                     : {quality['candidates']}")
        print(f"     admitted                          : {quality['admitted']}")
        print(f"     rejected, endpoint type mismatch  : {quality['type_mismatch']}")
        print(f"     rejected, entity type unknown     : {quality['type_unknown']}")
        print(f"     rejected, endpoint missing        : {quality['missing_endpoint']}")
        if quality["raw_share"] >= 0.5 or quality["admitted"] < quality["candidates"] * 0.5:
            print(
                "\n   ⚠ The drafted schema does not fit this corpus. Its edge types\n"
                "     constrain both endpoints to declared entity types, so every\n"
                "     relation touching a raw: type is discarded — silently, and\n"
                "     after the taxonomy was approved and published. Re-draft with a\n"
                "     finer entity taxonomy, or the graph will keep answering like a\n"
                "     populated one while traversing almost nothing."
            )

    print("\n   Q: What causes alarm ALM-401, and which procedure clears it?")
    print(f"   A: {answer.answer.strip()[:600]}")
    print(f"\n   cost ${answer.cost_usd:.5f} · {answer.latency_ms:.0f} ms · "
          f"{len(answer.sources)} sources")

    print()
    if failures:
        print(f"{len(failures)} check(s) failed: {', '.join(failures)}")
        return 1
    print(f"the flow works: {args.domain!r} went from files on disk to a grounded, "
          f"graph-assisted answer.")
    if quality and quality["admitted"] < quality["candidates"] * 0.5:
        # Said here as well as above, because a run that scrolls off the top
        # should not end on a line that reads like unqualified success.
        print(f"the taxonomy does not: {quality['admitted']} of "
              f"{quality['candidates']} candidate edges survived schema validation.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
