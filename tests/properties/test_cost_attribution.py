"""All spend is attributable, budgets bind before the call, and audit is tamper-evident.

The headline assertion is that **unattributed spend is exactly zero**. In the
Azure build the equivalent number is not zero and cannot be: `token_budget` is a
ContextVar defaulting to the string `"unknown"`, set on the ingest and eval paths
only, so chat and onboarding — the two paths its own docstring calls most
important — both bill to `"unknown"`. And because the ContextVar is never reset,
a worker that ran an ingest for one domain bills a later onboarding step to it.

Here it is zero *structurally*: `llm_usage.tenant_id` is NOT NULL with a foreign
key, and every call takes a `RequestContext`. There is no value to default to.
These tests assert that the structure holds under a mixed workload rather than
trusting the schema in isolation.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from platform_core.db.engine import owner_session
from platform_core.identity.principal import RequestContext
from platform_core.observability import audit
from platform_core.observability.ledger import (
    INTERACTIVE_TASKS,
    PostgresLedger,
    fails_open,
)
from platform_core.observability.llm import (
    CHAIN_ORDER,
    InstrumentedLLM,
    UnattributableCall,
    price,
)
from platform_core.ports.errors import BudgetExceededError
from platform_core.ports.ledger import UsageRecord

pytestmark = pytest.mark.property


@pytest.fixture
def ledger() -> PostgresLedger:
    return PostgresLedger()


def _charge(ledger, tenant, principal, *, task: str, tokens: int, cost: float = 0.0,
            run_id=None) -> None:
    ledger.record(
        UsageRecord(
            tenant_id=tenant.id, principal_id=principal.id, run_id=run_id,
            workload="echo", task=task, model="gpt-4o-mini",
            input_tokens=tokens, output_tokens=0, cost_usd=cost,
            at=datetime.now(UTC),
        )
    )


# ── attribution ──────────────────────────────────────────────────────────


def test_unattributed_spend_is_zero_under_a_mixed_workload(
    ledger, tenant_a, tenant_b, principal_a, principal_b, record_evidence
):
    """Every charge belongs to a tenant, across every task."""
    for task in ("chat", "query", "ingest", "onboard", "eval"):
        _charge(ledger, tenant_a, principal_a, task=task, tokens=100, cost=0.001)
        _charge(ledger, tenant_b, principal_b, task=task, tokens=50, cost=0.0005)

    assert ledger.unattributed_spend() == 0

    # Scoped to these two tenants and read as the owner: a platform-wide count
    # would be a cross-tenant read, which the relay credential deliberately does
    # not grant on llm_usage — spend is not a delivery concern.
    with owner_session() as s:
        total, distinct = s.execute(
            text(
                "SELECT count(*), count(DISTINCT tenant_id) FROM llm_usage "
                "WHERE tenant_id = ANY(:ids)"
            ),
            {"ids": [tenant_a.id, tenant_b.id]},
        ).one()
    assert total == 10 and distinct == 2, f"{total} rows across {distinct} tenants"

    # And each tenant sees only its own spend.
    ctx_a = RequestContext(principal=principal_a, labels={"task": "chat"})
    ctx_b = RequestContext(principal=principal_b, labels={"task": "chat"})
    assert ledger.status(ctx_a).tokens_today == 500
    assert ledger.status(ctx_b).tokens_today == 250

    record_evidence(
        "cost_attribution_complete", holds=True,
        unattributed_rows=0, tasks_covered=["chat", "query", "ingest", "onboard", "eval"],
        detail="tenant_id is NOT NULL, so an unattributable charge cannot be written",
    )


def test_a_call_without_a_tenant_is_refused_not_charged(record_evidence):
    """The first link in the chain. No context, no call.

    This is the difference from the build this platform was derived from: there
    is no path that produces spend nobody owns, because the call does not happen.
    """
    llm = InstrumentedLLM(raw_client=object())

    with pytest.raises(UnattributableCall):
        llm._call(None, None, kind="chat")

    record_evidence(
        "cost_attribution_refuses_anonymous_calls", holds=True,
        detail="an LLM call with no RequestContext raises rather than spending",
    )


def test_the_chain_order_is_fixed(record_evidence):
    """Order is a value that can be asserted, not an import-order accident.

    In the Azure build, telemetry and the budget each patch
    `chat.completions.create` independently and `llm_retry` adds a third layer,
    so which is outermost depends on the order `bootstrap.init` runs them in.
    """
    assert CHAIN_ORDER == (
        "identity",
        "cancellation",
        "budget_reservation",
        "cache",
        "trace",
        "retry",
        "dispatch",
        "meter",
        "budget_settlement",
    )
    assert CHAIN_ORDER.index("budget_reservation") < CHAIN_ORDER.index("cache"), (
        "cache before budget lets a stampede of misses bypass the ceiling"
    )
    assert CHAIN_ORDER.index("budget_reservation") < CHAIN_ORDER.index("retry"), (
        "retry outside the budget check re-authorises each attempt"
    )
    assert CHAIN_ORDER.index("dispatch") < CHAIN_ORDER.index("meter"), (
        "usage is only knowable from the response"
    )
    assert CHAIN_ORDER.index("meter") < CHAIN_ORDER.index("budget_settlement")

    record_evidence("cost_chain_order_fixed", holds=True, order=list(CHAIN_ORDER))


# ── budget enforcement ───────────────────────────────────────────────────


def test_the_budget_refuses_before_dispatch(ledger, tenant_a, principal_a,
                                            record_evidence):
    """A ceiling that reports after the tokens are gone is a receipt, not a control."""
    ledger.set_caps(tenant_a.id, daily_tokens=1000, monthly_cost_usd=100.0)
    ledger.invalidate(tenant_a.id)
    ctx = RequestContext(principal=principal_a, labels={"task": "chat"})

    _charge(ledger, tenant_a, principal_a, task="chat", tokens=999)
    ledger.invalidate(tenant_a.id)
    ledger.check(ctx)  # still under

    _charge(ledger, tenant_a, principal_a, task="chat", tokens=2)
    ledger.invalidate(tenant_a.id)
    with pytest.raises(BudgetExceededError, match="daily cap"):
        ledger.check(ctx)

    record_evidence(
        "budget_refuses_before_dispatch", holds=True,
        detail="check() raises once the tenant is over its daily cap",
    )


def test_a_single_oversized_call_is_refused_on_estimate(ledger, tenant_a, principal_a,
                                                        record_evidence):
    """A large batch must not slip through because the ceiling had room for one more."""
    ledger.set_caps(tenant_a.id, daily_tokens=1000, monthly_cost_usd=100.0)
    ledger.invalidate(tenant_a.id)
    ctx = RequestContext(principal=principal_a, labels={"task": "ingest"})

    ledger.check(ctx)  # nothing spent yet
    with pytest.raises(BudgetExceededError):
        ledger.check(ctx, estimated_tokens=5000)

    record_evidence(
        "budget_refuses_on_estimate", holds=True,
        detail="a call whose estimated size alone breaches the cap is refused before dispatch",
    )


def test_budget_policy_is_per_task(record_evidence):
    """Interactive fails open; background fails closed. Decided 2026-08-12.

    The asymmetry is deliberate: refusing an interactive call costs one answer,
    permitting a background one can cost a month of budget.
    """
    for task in ("chat", "query"):
        assert fails_open(task) is True, f"{task} should fail open"
    for task in ("ingest", "onboard", "eval"):
        assert fails_open(task) is False, f"{task} should fail closed"

    # An unclassified task fails closed: a new task is more likely a background
    # job, and being wrong that way is the cheaper error.
    assert fails_open("some_new_task") is False
    assert fails_open("") is False
    assert {"chat", "query"} == INTERACTIVE_TASKS

    record_evidence(
        "budget_policy_per_task", holds=True,
        fail_open=sorted(INTERACTIVE_TASKS),
        detail="unclassified tasks fail closed",
    )


def test_unpriced_models_are_not_free(record_evidence):
    """An unknown model must not silently cost zero — that reads as thrift."""
    known = price("gpt-4o-mini", 1000, 1000)
    unknown = price("some-model-shipped-tomorrow", 1000, 1000)

    assert unknown > 0, "an unpriced model was charged nothing"
    assert unknown >= known, "an unpriced model should be charged conservatively"

    record_evidence(
        "cost_unpriced_model_conservative", holds=True,
        known_cost=known, unknown_cost=unknown,
    )


# ── audit ────────────────────────────────────────────────────────────────


def test_audit_chain_links_and_verifies(tenant_a, principal_a, record_evidence):
    """Each event links to its predecessor and the whole chain re-derives."""
    ctx = RequestContext(principal=principal_a)

    hashes = [
        audit.record(ctx, action=f"test.action.{i}", outcome=audit.Outcome.SUCCEEDED,
                     resource_type="run", resource_id=str(uuid.uuid4()),
                     detail={"i": i})
        for i in range(5)
    ]
    assert all(h is not None for h in hashes)

    events = audit.recent(ctx, limit=10)
    assert len(events) == 5
    # recent() is newest-first; walking backwards, each prev_hash is the next
    # older event's hash.
    for newer, older in zip(events, events[1:], strict=False):
        assert newer.prev_hash == older.hash

    result = audit.verify_chain(tenant_a.id, tenant_a.slug)
    assert result.intact, result.reason
    assert result.events_checked == 5

    record_evidence(
        "audit_chain_intact", holds=True, events=result.events_checked,
        detail="every digest re-derives and every link matches its predecessor",
    )


def test_audit_rows_cannot_be_updated_or_deleted(tenant_a, principal_a, record_evidence):
    """Append-only, enforced by a trigger rather than by convention.

    A revoked grant would stop the app role but not the owner. The point is that
    *nothing* rewrites history — a log the application can alter proves nothing
    about the application.
    """
    ctx = RequestContext(principal=principal_a)
    audit.record(ctx, action="test.immutable", outcome=audit.Outcome.ALLOWED)

    # Attempted as the OWNER — a superuser — because that is the strongest
    # caller in the system. If it cannot rewrite the log, nothing can.
    with pytest.raises(Exception) as update_err, owner_session() as s:
        s.execute(text("UPDATE audit_event SET action = 'tampered'"))
    assert "append-only" in str(update_err.value).lower()

    with pytest.raises(Exception) as delete_err, owner_session() as s:
        s.execute(text("DELETE FROM audit_event"))
    assert "append-only" in str(delete_err.value).lower()

    record_evidence(
        "audit_append_only_enforced", holds=True,
        detail="UPDATE and DELETE are rejected even for the superuser owner role",
    )


def test_audit_tampering_is_detected(tenant_a, principal_a, record_evidence):
    """Verification must actually catch an altered row.

    The trigger blocks UPDATE, so tampering is simulated the only way it could
    really happen — by dropping the trigger first, which is itself a privileged
    and auditable act. Without this case, `verify_chain` could return `intact`
    unconditionally and every other audit test would still pass.
    """
    ctx = RequestContext(principal=principal_a)
    for i in range(3):
        audit.record(ctx, action=f"legit.{i}", outcome=audit.Outcome.SUCCEEDED)

    assert audit.verify_chain(tenant_a.id).intact

    with owner_session() as s:
        s.execute(text("ALTER TABLE audit_event DISABLE TRIGGER audit_event_no_update"))
    try:
        with owner_session() as s:
            target = s.execute(
                text(
                    "SELECT id FROM audit_event WHERE tenant_id = :t "
                    "ORDER BY id LIMIT 1"
                ),
                {"t": tenant_a.id},
            ).scalar_one()
            s.execute(
                text("UPDATE audit_event SET action = 'tampered' WHERE id = :id"),
                {"id": target},
            )

        result = audit.verify_chain(tenant_a.id, tenant_a.slug)
        assert not result.intact, "an altered row passed verification"
        assert result.first_break_id == target
        assert "altered" in (result.reason or "")
    finally:
        with owner_session() as s:
            s.execute(text("ALTER TABLE audit_event ENABLE TRIGGER audit_event_no_update"))

    record_evidence(
        "audit_tampering_detected", holds=True,
        broken_at=result.first_break_id,
        detail="altering a row invalidates its digest and verify_chain locates it",
    )


def test_audit_is_tenant_scoped(tenant_a, tenant_b, principal_a, principal_b,
                                record_evidence):
    """One tenant's audit log is invisible to another, and chains independently."""
    audit.record(RequestContext(principal=principal_a), action="a.only",
                 outcome=audit.Outcome.SUCCEEDED)
    audit.record(RequestContext(principal=principal_b), action="b.only",
                 outcome=audit.Outcome.SUCCEEDED)

    a_events = audit.recent(RequestContext(principal=principal_a))
    b_events = audit.recent(RequestContext(principal=principal_b))

    assert [e.action for e in a_events] == ["a.only"]
    assert [e.action for e in b_events] == ["b.only"]
    # Chained per tenant, so neither is the other's predecessor — a global chain
    # would serialise every tenant's audit writes behind the busiest one.
    assert a_events[0].prev_hash is None and b_events[0].prev_hash is None

    record_evidence(
        "audit_tenant_scoped", holds=True,
        detail="per-tenant chains; neither tenant sees or links to the other",
    )
