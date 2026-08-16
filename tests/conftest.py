"""Shared fixtures. Two tenants, always, because one tenant proves nothing.

A single-tenant test suite cannot detect the failure mode that matters: it never
puts another tenant's rows in the database for a query to accidentally return.
Every fixture here creates at least two, with overlapping data — same collection
names, same document filenames, same chunk text — so that a boundary failure
produces a *wrong answer* rather than an empty one.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import text

# Settings are read once and cached; the test profile must be in place first.
os.environ.setdefault("ENVIRONMENT", "local")
os.environ.setdefault("SERVICE_ROLE", "test")

from platform_core.db.engine import owner_session, tenant_session  # noqa: E402
from platform_core.identity.principal import (  # noqa: E402
    ActorType,
    Principal,
    Role,
    Tenant,
)

EVIDENCE_DIR = Path(__file__).resolve().parent.parent / "evidence"


@pytest.fixture(scope="session")
def evidence_dir() -> Path:
    d = EVIDENCE_DIR / "properties"
    d.mkdir(parents=True, exist_ok=True)
    return d


@pytest.fixture
def record_evidence(evidence_dir: Path):
    """Write a machine-readable record of what a property test actually proved.

    A passing test that leaves no trace is indistinguishable from a test that
    was skipped. Each record names the property, the assertions, and the
    timestamp, so `evidence/` answers "what is known to hold" without re-running
    anything.
    """
    written: list[dict] = []

    def _record(property_name: str, **facts) -> None:
        written.append({"property": property_name, **facts})

    yield _record

    for entry in written:
        path = evidence_dir / f"{entry['property']}.json"
        path.write_text(
            json.dumps(
                {**entry, "recorded_at": datetime.now(UTC).isoformat(timespec="seconds")},
                indent=2,
                default=str,
            )
        )


def _make_tenant(slug: str) -> Tenant:
    """Create a tenant. Requires the owner role — tenants are a platform object.

    That the app role *cannot* do this is itself part of the boundary: a
    compromised application credential must not be able to mint a new isolation
    scope for itself.
    """
    with owner_session() as s:
        row = s.execute(
            text(
                "INSERT INTO tenant (slug, name) VALUES (:slug, :name) "
                "ON CONFLICT (slug) DO UPDATE SET name = EXCLUDED.name "
                "RETURNING id"
            ),
            {"slug": slug, "name": slug.replace("-", " ").title()},
        ).scalar_one()
    return Tenant(id=row, slug=slug)


@pytest.fixture
def tenant_a() -> Tenant:
    return _make_tenant("acme-industrial")


@pytest.fixture
def tenant_b() -> Tenant:
    return _make_tenant("globex-motors")


def _principal_for(tenant: Tenant, subject: str, roles: set[Role]) -> Principal:
    with tenant_session(tenant) as s:
        pid = s.execute(
            text(
                "INSERT INTO principal (tenant_id, subject, actor_type, roles) "
                "VALUES (:t, :s, 'human', :r) "
                "ON CONFLICT (tenant_id, subject) DO UPDATE SET roles = EXCLUDED.roles "
                "RETURNING id"
            ),
            {"t": tenant.id, "s": subject, "r": [str(r) for r in roles]},
        ).scalar_one()
    return Principal(
        id=pid, tenant=tenant, subject=subject, roles=frozenset(roles), actor_type=ActorType.HUMAN
    )


@pytest.fixture
def principal_a(tenant_a: Tenant) -> Principal:
    return _principal_for(tenant_a, "operator@acme.example", {Role.OPERATOR})


@pytest.fixture
def principal_b(tenant_b: Tenant) -> Principal:
    return _principal_for(tenant_b, "operator@globex.example", {Role.OPERATOR})


@pytest.fixture
def seeded_corpus(tenant_a: Tenant, tenant_b: Tenant) -> dict[str, dict]:
    """Identical-looking data in both tenants.

    Same collection name, same filename, same chunk text, near-identical
    embeddings. Deliberate: if isolation is broken, a similarity search from
    tenant A will rank tenant B's chunk highly and the test fails with a wrong
    answer rather than a missing one. Distinct data would let a broken boundary
    pass by coincidence.
    """
    out: dict[str, dict] = {}
    shared_text = "Torque calibration procedure for the spindle assembly."

    for tenant, marker in ((tenant_a, "acme"), (tenant_b, "globex")):
        with tenant_session(tenant) as s:
            doc_id = s.execute(
                text(
                    "INSERT INTO document (tenant_id, workload, collection, filename, "
                    "content_sha256, byte_size, storage_key) "
                    "VALUES (:t, 'echo', 'maintenance', 'procedure.pdf', :sha, 1024, :key) "
                    "ON CONFLICT (tenant_id, collection, filename) "
                    "WHERE superseded_at IS NULL DO UPDATE "
                    "SET content_sha256 = EXCLUDED.content_sha256 RETURNING id"
                ),
                {"t": tenant.id, "sha": f"{marker:{'0'}<64}", "key": f"{marker}/procedure.pdf"},
            ).scalar_one()

            # Embeddings differ only in the first component, so cosine distance
            # between them is small — a leak surfaces as a top-ranked foreign row.
            vec = [0.9 if marker == "acme" else 0.91] + [0.1] * 1535
            chunk_id = s.execute(
                text(
                    "INSERT INTO chunk (tenant_id, document_id, collection, canonical_id, "
                    "ordinal, text, embedding, embedding_model) "
                    "VALUES (:t, :d, 'maintenance', :cid, 0, :txt, :vec, 'test') "
                    "ON CONFLICT (tenant_id, collection, canonical_id, build_version) "
                    "DO UPDATE SET text = EXCLUDED.text RETURNING id"
                ),
                {
                    "t": tenant.id,
                    "d": doc_id,
                    "cid": f"c_{marker}0000000000",
                    "txt": f"{shared_text} [{marker}]",
                    "vec": str(vec),
                },
            ).scalar_one()

        out[marker] = {"tenant": tenant, "document_id": doc_id, "chunk_id": chunk_id}
    return out


@pytest.fixture
def populated_every_table(tenant_a: Tenant, tenant_b: Tenant, seeded_corpus) -> dict[str, int]:
    """Seed at least one row per tenant in **every** tenant-scoped table.

    Exists because a fail-closed assertion over an empty table proves nothing:
    ``count(*) == 0`` is true whether the policy blocked the read or there was
    simply nothing to read. That gap let a genuine cross-tenant hole in ``run``,
    ``outbox`` and ``side_effect`` pass the isolation suite unnoticed.

    Returns the row count written per table, so a test can assert it was
    actually working against populated tables.
    """
    from platform_core.db.models import TENANT_SCOPED_TABLES

    counts: dict[str, int] = {}
    for tenant in (tenant_a, tenant_b):
        with tenant_session(tenant) as s:
            principal_id = s.execute(
                text(
                    "INSERT INTO principal (tenant_id, subject, roles) "
                    "VALUES (:t, 'seed@example.com', ARRAY['viewer']) "
                    "ON CONFLICT (tenant_id, subject) DO UPDATE SET roles = EXCLUDED.roles "
                    "RETURNING id"
                ),
                {"t": tenant.id},
            ).scalar_one()

            run_id = s.execute(
                text(
                    "INSERT INTO run (tenant_id, workload, status, requested_by) "
                    "VALUES (:t, 'echo', 'pending', :p) RETURNING id"
                ),
                {"t": tenant.id, "p": principal_id},
            ).scalar_one()

            s.execute(
                text(
                    "INSERT INTO outbox (tenant_id, run_id, workload, payload) "
                    "VALUES (:t, :r, 'echo', '{}'::jsonb)"
                ),
                {"t": tenant.id, "r": run_id},
            )
            s.execute(
                text(
                    "INSERT INTO side_effect (tenant_id, run_id, step) "
                    "VALUES (:t, :r, 'seed-step')"
                ),
                {"t": tenant.id, "r": run_id},
            )
            s.execute(
                text(
                    "INSERT INTO capability_grant (tenant_id, principal_id, capability) "
                    "VALUES (:t, :p, 'query:read') ON CONFLICT DO NOTHING"
                ),
                {"t": tenant.id, "p": principal_id},
            )
            approval_id = s.execute(
                text(
                    "INSERT INTO tool_approval (tenant_id, run_id, tool_name, side_effect, "
                    "arguments, arguments_sha256, requested_by, expires_at) "
                    "VALUES (:t, :r, 'seed_tool', 'write', '{}'::jsonb, :sha, :p, "
                    "now() + interval '1 hour') RETURNING id"
                ),
                {"t": tenant.id, "r": run_id, "sha": "c" * 64, "p": principal_id},
            ).scalar_one()
            s.execute(
                text(
                    "INSERT INTO agent_checkpoint "
                    "(tenant_id, thread_id, step, run_id, state, state_sha256, created_by) "
                    "VALUES (:t, :thread, 0, :r, '{}'::jsonb, :sha, :p)"
                ),
                {
                    "t": tenant.id,
                    "thread": f"seed:{tenant.slug}",
                    "r": run_id,
                    "sha": "e" * 64,
                    "p": principal_id,
                },
            )
            s.execute(
                text(
                    "INSERT INTO tool_execution "
                    "(tenant_id, run_id, approval_id, tool_name, side_effect, "
                    " arguments_sha256, idempotency_key, requested_by, status, result, "
                    " result_sha256, finished_at) "
                    "VALUES (:t, :r, :approval, 'seed_tool', 'write', :args_sha, "
                    " :key, :p, 'succeeded', '{}'::jsonb, :result_sha, now())"
                ),
                {
                    "t": tenant.id,
                    "r": run_id,
                    "approval": approval_id,
                    "args_sha": "c" * 64,
                    "key": f"seed:{tenant.slug}",
                    "p": principal_id,
                    "result_sha": "f" * 64,
                },
            )
            s.execute(
                text(
                    "INSERT INTO budget_reservation "
                    "(tenant_id, principal_id, request_id, task, model, estimated_tokens, "
                    " estimated_cost_usd, status, expires_at) "
                    "VALUES (:t, :p, :request, 'chat', 'gpt-4o-mini', 10, 0.001, "
                    " 'reserved', now() + interval '1 hour')"
                ),
                {"t": tenant.id, "p": principal_id, "request": run_id},
            )
            s.execute(
                text(
                    "INSERT INTO llm_usage (tenant_id, principal_id, run_id, workload, "
                    "task, model, input_tokens, output_tokens, cost_usd) "
                    "VALUES (:t, :p, :r, 'echo', 'chat', 'gpt-4o-mini', 10, 5, 0.0001)"
                ),
                {"t": tenant.id, "p": principal_id, "r": run_id},
            )
            s.execute(
                text(
                    "INSERT INTO audit_event (tenant_id, principal_id, actor_subject, "
                    "action, outcome, hash) "
                    "VALUES (:t, :p, 'seed@example.com', 'seed.event', 'succeeded', :h)"
                ),
                {"t": tenant.id, "p": principal_id, "h": "0" * 64},
            )

            dataset_id = s.execute(
                text(
                    "INSERT INTO eval_dataset (tenant_id, name, collection, "
                    "content_sha256, items, item_count, created_by) "
                    "VALUES (:t, 'seed-set', 'maintenance', :sha, '[]'::jsonb, 0, :p) "
                    "RETURNING id"
                ),
                {"t": tenant.id, "sha": "d" * 64, "p": principal_id},
            ).scalar_one()
            s.execute(
                text(
                    "INSERT INTO continuous_eval_policy "
                    "(tenant_id, dataset_name, created_by, updated_by) "
                    "VALUES (:t, 'seed-set', :p, :p) "
                    "ON CONFLICT (tenant_id, dataset_name) DO NOTHING"
                ),
                {"t": tenant.id, "p": principal_id},
            )
            eval_run_id = s.execute(
                text(
                    "INSERT INTO eval_run (tenant_id, dataset_id, dataset_sha, "
                    "code_rev, model_id, status) "
                    "VALUES (:t, :d, :sha, 'seed', 'gpt-4o-mini', 'completed') "
                    "RETURNING id"
                ),
                {"t": tenant.id, "d": dataset_id, "sha": "d" * 64},
            ).scalar_one()
            s.execute(
                text(
                    "INSERT INTO eval_result (tenant_id, eval_run_id, item_id, question) "
                    "VALUES (:t, :r, 'seed-item', 'seed question?')"
                ),
                {"t": tenant.id, "r": eval_run_id},
            )
            s.execute(
                text(
                    "INSERT INTO eval_baseline (tenant_id, dataset_name, eval_run_id, "
                    "promoted_by) VALUES (:t, 'seed-set', :r, :p)"
                ),
                {"t": tenant.id, "r": eval_run_id, "p": principal_id},
            )
            # The corpus-gap backlog. Seeded here for the same reason as
            # everything else in this fixture: an isolation assertion over an
            # empty table proves nothing.
            s.execute(
                text(
                    "INSERT INTO unanswered_question (tenant_id, collection, "
                    "  question, question_key, mode) "
                    "VALUES (:t, 'maintenance', 'seed gap question?', "
                    "        'seed gap question', 'dense') "
                    "ON CONFLICT (tenant_id, collection, question_key) DO NOTHING"
                ),
                {"t": tenant.id},
            )
            # Review state lives beside the dataset rather than inside it, so it
            # needs its own row here — an isolation assertion over an empty
            # table proves nothing, which is what this fixture exists to prevent.
            s.execute(
                text(
                    "INSERT INTO eval_item_label (tenant_id, dataset_name, item_id, "
                    "answer_source) VALUES (:t, 'seed-set', 'seed-item', 'llm_drafted') "
                    "ON CONFLICT (tenant_id, dataset_name, item_id) DO NOTHING"
                ),
                {"t": tenant.id},
            )
            s.execute(
                text(
                    "INSERT INTO session (id, tenant_id, principal_id, workload, "
                    "expires_at) VALUES (:sid, :t, :p, 'echo', now() + interval '1 hour')"
                ),
                {"sid": f"seed-{tenant.slug}", "t": tenant.id, "p": principal_id},
            )
            # Reads resolve through this since 0016 — a collection with chunks
            # but no live build returns nothing, so seeding chunks without a
            # build would make every retrieval assertion vacuous.
            s.execute(
                text(
                    "INSERT INTO collection_build (tenant_id, collection, "
                    "  build_version, status, chunk_count, promoted_at) "
                    "VALUES (:t, 'maintenance', 1, 'live', 1, now()) "
                    "ON CONFLICT (tenant_id, collection, build_version) DO NOTHING"
                ),
                {"t": tenant.id},
            )
            onboarding_id = s.execute(
                text(
                    "INSERT INTO onboarding_session (tenant_id, domain, collection, "
                    "status, created_by) "
                    "VALUES (:t, 'seed-domain', 'maintenance', 'draft_ready', :p) "
                    "RETURNING id"
                ),
                {"t": tenant.id, "p": principal_id},
            ).scalar_one()
            s.execute(
                text(
                    "INSERT INTO onboarding_artifact (tenant_id, session_id, kind, "
                    "name, payload) "
                    "VALUES (:t, :s, 'predicate_map', 'predicate_map', "
                    "        CAST(:payload AS jsonb))"
                ),
                {
                    "t": tenant.id, "s": onboarding_id,
                    "payload": '{"predicate_map": {"seed": "SEED"}}',
                },
            )

    # Anchors are writable only by the retention function/table owner. Seed one
    # through the owner after tenant transactions commit so RLS assertions over
    # the table are not vacuous without widening the app role's grants.
    with owner_session() as s:
        for tenant in (tenant_a, tenant_b):
            s.execute(
                text(
                    "INSERT INTO audit_chain_anchor "
                    "(tenant_id, through_event_id, through_hash, events_anchored, reason) "
                    "VALUES (:t, 0, :hash, 0, 'isolation fixture') "
                    "ON CONFLICT (tenant_id) DO NOTHING"
                ),
                {"t": tenant.id, "hash": "f" * 64},
            )

    # Verify the seeding actually worked, per tenant, before any test relies on
    # it. A fixture that silently seeds nothing recreates the exact problem it
    # was written to close.
    with tenant_session(tenant_a) as s:
        for table in TENANT_SCOPED_TABLES:
            n = s.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()
            assert n > 0, f"fixture failed to seed {table}; the assertion would be vacuous"
            counts[table] = n
    return counts


@pytest.fixture(autouse=True)
def _clean_slate(request):
    """Remove test rows between cases, as the owner, so RLS cannot hide leftovers.

    Cleaning through a tenant session would only delete rows that session can
    see — which is precisely the thing under test, so it would be circular.

    ``app.audit_purge_reason`` is required because ``audit_event`` is
    append-only: deleting a tenant cascades into it, and the trigger refuses an
    undeclared DELETE. Declaring the reason is exactly the workflow a real
    erasure request would follow, so the tests exercise it rather than working
    around it.
    """
    yield
    if request.node.get_closest_marker("unit"):
        return
    with owner_session() as s:
        s.execute(
            text("SELECT set_config('app.audit_purge_reason', :why, true)"),
            {"why": "test teardown: removing fixture tenants"},
        )
        s.execute(
            text("DELETE FROM tenant WHERE slug IN ('acme-industrial','globex-motors')")
        )
