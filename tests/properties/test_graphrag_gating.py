"""The GraphRAG feature flag disables the feature, not just its startup check.

`GRAPHRAG_ENABLED` used to appear in exactly two places: its declaration and one
coherence branch that checked the engine tree existed. No request path consulted
it, so `POST /api/query` with `mode:"graph"` imported the borrowed engine
regardless. The flag's effect was inverted — setting it *false*, as the
production manifests do, removed the startup warning while leaving the feature
live, so a deployment without the tree failed at request time on whatever the
missing directory happened to raise.

These tests pin the corrected behaviour: one enforcement point, a clean refusal,
and a configuration that cannot claim to be enabled without saying where.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from platform_core.api.app import app
from platform_core.db.engine import tenant_session
from platform_core.identity.auth import issue_token
from platform_core.identity.principal import ActorType, Principal, Role
from platform_core.settings import Settings
from workloads.graphrag import engine

pytestmark = pytest.mark.property


def _operator(tenant):
    with tenant_session(tenant) as session:
        principal_id = session.execute(
            text(
                "INSERT INTO principal (tenant_id, subject, actor_type, roles) "
                "VALUES (:t, 'graph-operator@acme.example', 'human', ARRAY['operator']) "
                "ON CONFLICT (tenant_id, subject) DO UPDATE SET roles = EXCLUDED.roles "
                "RETURNING id"
            ),
            {"t": tenant.id},
        ).scalar_one()
    return Principal(
        id=principal_id,
        tenant=tenant,
        subject="graph-operator@acme.example",
        roles=frozenset({Role.OPERATOR}),
        actor_type=ActorType.HUMAN,
    )


def test_graph_mode_refuses_cleanly_when_the_engine_is_disabled(
    tenant_a, record_evidence
) -> None:
    """A disabled deployment answers 501, not 500 and not an import traceback.

    The status matters. A 5xx that reads as a crash sends a client into retry;
    501 says the platform is working and this deployment simply does not carry
    the engine, so the client can fall back to dense mode.
    """
    principal = _operator(tenant_a)
    with TestClient(app) as client:
        response = client.post(
            "/api/query",
            json={
                "question": "What causes vibration fault F-207?",
                "collection": "maintenance",
                "mode": "graph",
            },
            headers={"Authorization": f"Bearer {issue_token(principal)}"},
        )

    assert response.status_code == 501, response.text
    detail = response.json()["detail"]
    assert "GRAPHRAG_ENABLED" in detail
    assert "GRAPHRAG_ENGINE_ROOT" in detail

    record_evidence(
        "graph_mode_refuses_when_disabled",
        holds=True,
        status=response.status_code,
        names_both_settings=True,
    )


def test_every_engine_entry_point_is_gated_by_the_same_check(record_evidence) -> None:
    """Graph chat, onboarding drafts and schema validation share one gate.

    All three reach the borrowed engine through `prepare_imports`, which is why
    the flag is enforced there rather than at each call site. A second entry
    point that resolved the tree itself would reopen the hole silently, so this
    asserts the chokepoint is the only resolver.
    """
    with pytest.raises(engine.GraphRAGDisabled) as refused:
        engine.prepare_imports()
    assert "not enabled" in str(refused.value)

    # Scan for a second resolver. `graphrag_engine_root` may be read in exactly
    # two places: the settings declaration, and the chokepoint that enforces the
    # flag. Anything else is a path to the engine that skips the gate.
    repo = Path(__file__).resolve().parent.parent.parent
    readers: dict[str, int] = {}
    for path in sorted((repo / "platform_core").rglob("*.py")) + sorted(
        (repo / "workloads").rglob("*.py")
    ):
        hits = path.read_text().count("graphrag_engine_root")
        if hits:
            readers[path.relative_to(repo).as_posix()] = hits

    assert readers, "scan found no readers at all — the assertion would be vacuous"
    assert set(readers) == {
        "platform_core/settings.py",
        "workloads/graphrag/engine.py",
    }, f"a second place resolves the engine root and would bypass the gate: {readers}"

    record_evidence(
        "graphrag_gate_has_one_enforcement_point",
        holds=True,
        entry_points=["graph chat", "onboarding draft", "schema validation"],
        chokepoint="workloads.graphrag.engine.prepare_imports",
        root_readers=sorted(readers),
    )


def test_enabling_the_engine_without_a_root_is_refused_at_startup(
    record_evidence,
) -> None:
    """`enabled` and a root are one decision, not two independent settings.

    There is deliberately no default root — it lives outside this repository and
    its location is per-machine. The previous default was an absolute path under
    one developer's home directory, which shipped into the production manifests.
    """
    settings = Settings(
        environment="local",
        llm_provider="ollama",
        graphrag_enabled=True,
        graphrag_engine_root=None,
    )
    problems = settings.check_coherence()
    assert any("graphrag_engine_root is unset" in problem for problem in problems), problems

    missing = Settings(
        environment="local",
        llm_provider="ollama",
        graphrag_enabled=True,
        graphrag_engine_root="/nonexistent/engine/tree",
    )
    assert any("is missing" in problem for problem in missing.check_coherence())

    # The default configuration is off, and being off is coherent on its own.
    off = Settings(environment="local", llm_provider="ollama")
    assert off.graphrag_engine_root is None
    assert not any("graphrag" in problem for problem in off.check_coherence())

    record_evidence(
        "graphrag_enabled_requires_an_explicit_root",
        holds=True,
        default_root=None,
        unset_root_refused=True,
        missing_root_refused=True,
    )
