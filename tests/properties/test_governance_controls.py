"""Continuous evaluation, retention, and audit availability are controls, not docs."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from platform_core.api.app import app
from platform_core.db.engine import owner_session, tenant_session
from platform_core.gates import datasets
from platform_core.governance import continuous_eval, retention
from platform_core.identity.auth import issue_token
from platform_core.identity.principal import RequestContext
from platform_core.observability import audit

pytestmark = pytest.mark.property


def _dataset(ctx: RequestContext, name: str = "continuous-set"):
    return datasets.save(
        ctx,
        name=name,
        collection="maintenance",
        items=[
            {
                "id": "q1",
                "question": "What is the inspection interval?",
                "expected_answer": "Every 30 days.",
                "must_cite": [],
            }
        ],
    )


def test_every_dataset_gets_a_mandatory_schedule_and_atomic_audit(
    tenant_a, principal_a, record_evidence
):
    ctx = RequestContext(principal=principal_a)
    saved = _dataset(ctx)

    with tenant_session(tenant_a) as session:
        policy = session.execute(
            text(
                "SELECT interval_seconds, top_k, next_run_at "
                "FROM continuous_eval_policy WHERE dataset_name = 'continuous-set'"
            )
        ).one()
    assert policy.interval_seconds >= 900
    assert 1 <= policy.top_k <= 50
    assert any(
        event.action == "eval.dataset.version.saved"
        and event.resource_id == "continuous-set"
        for event in audit.recent(ctx)
    )

    record_evidence(
        "continuous_eval_policy_is_mandatory",
        holds=True,
        dataset_sha=saved.content_sha256,
        interval_seconds=policy.interval_seconds,
    )


def test_due_schedule_atomically_creates_one_pinned_run_and_outbox(
    tenant_a, principal_a, record_evidence
):
    ctx = RequestContext(principal=principal_a)
    saved = _dataset(ctx)
    with tenant_session(tenant_a) as session:
        session.execute(
            text(
                "UPDATE continuous_eval_policy SET next_run_at = now() - interval '1 minute' "
                "WHERE dataset_name = 'continuous-set'"
            )
        )

    assert continuous_eval.schedule_due(limit=10) == 1
    assert continuous_eval.schedule_due(limit=10) == 0

    with tenant_session(tenant_a) as session:
        run = session.execute(
            text(
                "SELECT id, input, priority, status FROM run "
                "WHERE idempotency_key LIKE 'continuous-eval:%'"
            )
        ).one()
        outbox = session.execute(
            text("SELECT run_id, payload FROM outbox WHERE run_id = :run"),
            {"run": run.id},
        ).one()
    assert run.status == "pending"
    assert run.priority == -1
    assert run.input["continuous"] is True
    assert run.input["content_sha256"] == saved.content_sha256
    assert outbox.run_id == run.id
    assert outbox.payload == run.input

    record_evidence(
        "continuous_eval_is_durable_and_idempotent",
        holds=True,
        run_id=str(run.id),
        dataset_sha=saved.content_sha256,
    )


def test_retention_advances_anchor_and_preserves_audit_verification(
    tenant_a, principal_a, record_evidence
):
    ctx = RequestContext(principal=principal_a)
    old_at = (datetime.now(UTC) - timedelta(days=3000)).replace(microsecond=0)
    first_hash = audit._digest(
        tenant_id=tenant_a.id,
        actor_subject=principal_a.subject,
        action="old.one",
        resource_type=None,
        resource_id=None,
        outcome="succeeded",
        detail={},
        at=old_at.isoformat(),
        prev_hash=None,
    )
    second_at = old_at + timedelta(seconds=1)
    second_hash = audit._digest(
        tenant_id=tenant_a.id,
        actor_subject=principal_a.subject,
        action="old.two",
        resource_type=None,
        resource_id=None,
        outcome="succeeded",
        detail={},
        at=second_at.isoformat(),
        prev_hash=first_hash,
    )
    with owner_session() as session:
        session.execute(
            text(
                "INSERT INTO audit_event "
                "(tenant_id, principal_id, actor_subject, action, outcome, detail, at, "
                " prev_hash, hash) VALUES "
                "(:t, :p, :actor, 'old.one', 'succeeded', '{}'::jsonb, :at1, NULL, :h1), "
                "(:t, :p, :actor, 'old.two', 'succeeded', '{}'::jsonb, :at2, :h1, :h2)"
            ),
            {
                "t": tenant_a.id,
                "p": principal_a.id,
                "actor": principal_a.subject,
                "at1": old_at,
                "at2": second_at,
                "h1": first_hash,
                "h2": second_hash,
            },
        )

    audit.record(ctx, action="current.event", outcome=audit.Outcome.SUCCEEDED, required=True)
    counts = retention.enforce()
    verification = audit.verify_chain(tenant_a.id, tenant_a.slug)

    assert counts["audit_events"] == 2
    assert verification.intact
    assert verification.events_anchored == 2
    assert verification.anchor_event_id is not None
    assert verification.events_checked == 1

    record_evidence(
        "audit_retention_preserves_chain",
        holds=True,
        anchored_events=verification.events_anchored,
        remaining_events=verification.events_checked,
    )


def test_privileged_http_admission_fails_closed_when_audit_is_down(
    tenant_a, monkeypatch, record_evidence
):
    from platform_core.identity.principal import ActorType, Principal, Role

    with tenant_session(tenant_a) as session:
        principal_id = session.execute(
            text(
                "INSERT INTO principal (tenant_id, subject, roles) "
                "VALUES (:t, 'audit-owner@example.com', ARRAY['owner']) RETURNING id"
            ),
            {"t": tenant_a.id},
        ).scalar_one()
    principal = Principal(
        id=principal_id,
        tenant=tenant_a,
        subject="audit-owner@example.com",
        roles=frozenset({Role.OWNER}),
        actor_type=ActorType.HUMAN,
    )
    real_record = audit.record

    def unavailable(*args, **kwargs):
        if kwargs.get("required"):
            raise audit.AuditUnavailable("test outage")
        return real_record(*args, **kwargs)

    monkeypatch.setattr(audit, "record", unavailable)
    with TestClient(app) as client:
        response = client.put(
            "/api/usage/caps",
            json={"daily_token_cap": 1000},
            headers={"Authorization": f"Bearer {issue_token(principal)}"},
        )
    assert response.status_code == 503
    assert response.json()["code"] == "audit_unavailable"

    record_evidence(
        "privileged_actions_fail_closed_without_audit",
        holds=True,
        status=response.status_code,
    )
