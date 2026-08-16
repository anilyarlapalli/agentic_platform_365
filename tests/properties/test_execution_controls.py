"""P1 execution controls: no hot retries, cooperative stops, fair batches, atomic caps."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from apps.worker import runner
from platform_core.api.routes.runs import cancel_run
from platform_core.correctness import leases, outbox
from platform_core.db.engine import tenant_session
from platform_core.identity.principal import RequestContext
from platform_core.observability.ledger import PostgresLedger
from platform_core.observability.llm import InstrumentedLLM
from platform_core.ports.errors import BudgetExceededError
from platform_core.ports.llm import ChatRequest

pytestmark = pytest.mark.property


def _admit(tenant, principal, *, workload: str = "echo", message: str = "work"):
    ctx = RequestContext(principal=principal)
    with tenant_session(tenant) as session:
        run_id, _ = outbox.enqueue_run(
            session,
            ctx,
            workload=workload,
            payload={"message": message},
        )
    return run_id


def test_transient_failures_are_delayed_before_the_next_lease(
    tenant_a, principal_a, record_evidence
) -> None:
    run_id = _admit(tenant_a, principal_a)
    leased = leases.acquire_specific(tenant_a, run_id, worker_id="retry-1")
    assert leased is not None
    assert leases.fail(leased, error="temporary", retryable=True)

    with tenant_session(tenant_a) as session:
        row = session.execute(
            text("SELECT status, available_at > now() AS delayed FROM run WHERE id = :id"),
            {"id": run_id},
        ).one()
    assert row.status == "pending"
    assert row.delayed is True
    assert leases.acquire_specific(tenant_a, run_id, worker_id="retry-too-soon") is None

    with tenant_session(tenant_a) as session:
        session.execute(
            text("UPDATE run SET available_at = now() WHERE id = :id"),
            {"id": run_id},
        )
    assert leases.acquire_specific(tenant_a, run_id, worker_id="retry-2") is not None

    record_evidence(
        "transient_retries_use_durable_backoff",
        holds=True,
        immediate_reacquire=False,
        due_reacquire=True,
    )


def test_a_leased_run_acknowledges_cancellation_at_a_safe_boundary(
    tenant_a, principal_a, record_evidence
) -> None:
    workload = "test-cancellable"
    entered = threading.Event()
    finish = threading.Event()

    def handler(_ctx, _payload):
        entered.set()
        assert finish.wait(5), "test handler was never released"
        return {"would_have_succeeded": True}

    runner.register(workload, handler)
    try:
        run_id = _admit(tenant_a, principal_a, workload=workload)
        leased = leases.acquire_specific(tenant_a, run_id, worker_id="cancel-worker")
        assert leased is not None
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(runner.execute_leased, leased, wid="cancel-worker")
            assert entered.wait(5), "worker did not enter the handler"
            response = cancel_run(run_id, RequestContext(principal=principal_a))
            assert response["status"] == "cancellation_requested"
            with tenant_session(tenant_a) as session:
                still_leased = session.execute(
                    text("SELECT status FROM run WHERE id = :id"), {"id": run_id}
                ).scalar_one()
            assert still_leased == "leased"
            finish.set()
            result = future.result(timeout=10)

        assert result["outcome"] == "cancelled"
        with tenant_session(tenant_a) as session:
            final = session.execute(
                text("SELECT status, leased_by FROM run WHERE id = :id"), {"id": run_id}
            ).one()
        assert final.status == "cancelled"
        assert final.leased_by is None
    finally:
        runner.WORKLOADS.pop(workload, None)

    record_evidence(
        "leased_cancellation_is_cooperative_and_fenced",
        holds=True,
        status_while_handler_active="leased",
        final_status=final.status,
    )


def test_relay_batch_interleaves_tenants(
    tenant_a, tenant_b, principal_a, principal_b, record_evidence
) -> None:
    for index in range(5):
        _admit(tenant_a, principal_a, message=f"acme-{index}")
    globex_run = _admit(tenant_b, principal_b, message="globex")

    delivered = []
    assert outbox.drain(delivered.append, batch_size=2) == 2
    assert {row.tenant_id for row in delivered} == {tenant_a.id, tenant_b.id}
    assert any(row.run_id == globex_run for row in delivered)

    record_evidence(
        "relay_batches_are_tenant_fair",
        holds=True,
        tenants_in_first_two=len({row.tenant_id for row in delivered}),
    )


def test_budget_reservations_are_atomic_under_concurrency(
    tenant_a, principal_a, record_evidence
) -> None:
    budget = PostgresLedger()
    budget.set_caps(tenant_a.id, daily_tokens=1000, monthly_cost_usd=100.0)
    barrier = threading.Barrier(2)

    def reserve(index: int):
        barrier.wait()
        ctx = RequestContext(principal=principal_a, labels={"task": "chat"})
        try:
            return budget.reserve(
                ctx,
                model="gpt-4o-mini",
                estimated_tokens=600,
                estimated_cost_usd=0.01,
            )
        except BudgetExceededError:
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        reservations = list(executor.map(reserve, range(2)))

    admitted = [reservation for reservation in reservations if reservation is not None]
    assert len(admitted) == 1
    status = budget.status(RequestContext(principal=principal_a, labels={"task": "chat"}))
    assert status.reserved_tokens == 600
    budget.release(admitted[0], reason="test complete")

    record_evidence(
        "budget_headroom_is_reserved_atomically",
        holds=True,
        concurrent_requests=2,
        admitted=len(admitted),
        committed_tokens=status.reserved_tokens,
    )


def test_synchronous_model_usage_settles_without_a_fake_run_id(
    tenant_a, principal_a, record_evidence
) -> None:
    class Completions:
        @staticmethod
        def create(**_kwargs):
            return SimpleNamespace(
                usage=SimpleNamespace(prompt_tokens=3, completion_tokens=2),
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content="ok", tool_calls=[]),
                        finish_reason="stop",
                    )
                ],
            )

    raw = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
    ctx = RequestContext(
        principal=principal_a,
        labels={"task": "chat", "workload": "chat"},
    )
    assert ctx.run_id is None
    response = InstrumentedLLM(raw).chat(
        ctx,
        ChatRequest(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": "hello"}],
            max_tokens=10,
        ),
    )
    assert response.content == "ok"

    with tenant_session(tenant_a) as session:
        usage_row = session.execute(
            text(
                "SELECT run_id, total_tokens FROM llm_usage "
                "WHERE principal_id = :principal ORDER BY id DESC LIMIT 1"
            ),
            {"principal": principal_a.id},
        ).one()
        reservation_status = session.execute(
            text(
                "SELECT status FROM budget_reservation "
                "WHERE request_id = :request ORDER BY created_at DESC LIMIT 1"
            ),
            {"request": ctx.request_id},
        ).scalar_one()
    assert usage_row.run_id is None
    assert usage_row.total_tokens == 5
    assert reservation_status == "settled"

    record_evidence(
        "synchronous_llm_usage_is_attributed_and_settled",
        holds=True,
        run_id=None,
        tokens=usage_row.total_tokens,
    )
