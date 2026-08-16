"""Durable checkpoints and governed tools survive retries without widening authority."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from platform_core.adapters.postgres.checkpoint import (
    CheckpointConflict,
    PostgresCheckpointStore,
)
from platform_core.agent.tools import (
    ApprovalInvalid,
    ApprovalRequired,
    ToolConflict,
    ToolSpec,
    invoke,
    registry,
)
from platform_core.api.routes.approvals import Decision, decide
from platform_core.db.engine import tenant_session
from platform_core.identity.capabilities import Capability
from platform_core.identity.principal import ActorType, Principal, RequestContext, Role, Tenant
from platform_core.ports.checkpoint import Checkpoint

pytestmark = pytest.mark.property


def _principal(tenant: Tenant, subject: str, role: Role) -> Principal:
    with tenant_session(tenant) as session:
        principal_id = session.execute(
            text(
                "INSERT INTO principal (tenant_id, subject, actor_type, roles) "
                "VALUES (:tenant, :subject, 'human', :roles) RETURNING id"
            ),
            {"tenant": tenant.id, "subject": subject, "roles": [str(role)]},
        ).scalar_one()
    return Principal(
        id=principal_id,
        tenant=tenant,
        subject=subject,
        roles=frozenset({role}),
        actor_type=ActorType.HUMAN,
    )


def _run(tenant: Tenant, requested_by: uuid.UUID) -> uuid.UUID:
    with tenant_session(tenant) as session:
        return session.execute(
            text(
                "INSERT INTO run (tenant_id, workload, status, requested_by) "
                "VALUES (:tenant, 'echo', 'pending', :principal) RETURNING id"
            ),
            {"tenant": tenant.id, "principal": requested_by},
        ).scalar_one()


def test_checkpoints_are_immutable_ordered_and_tenant_isolated(
    tenant_a, tenant_b, principal_a, principal_b, record_evidence
) -> None:
    run_id = _run(tenant_a, principal_a.id)
    ctx_a = RequestContext(principal=principal_a, run_id=run_id)
    ctx_b = RequestContext(principal=principal_b, run_id=run_id)
    store = PostgresCheckpointStore()

    mutable_state = {"phase": "started", "nested": {"attempt": 1}}
    first = store.append(ctx_a, f"run:{run_id}", mutable_state)
    mutable_state["nested"]["attempt"] = 99
    second = store.append(
        ctx_a,
        f"run:{run_id}",
        {"phase": "awaiting", "approval": "required"},
        awaiting="tool_approval",
    )

    assert first.step == 0
    assert first.state["nested"]["attempt"] == 1
    assert second.step == 1
    assert store.load(ctx_a, f"run:{run_id}") == second
    assert [checkpoint.step for checkpoint in store.history(ctx_a, f"run:{run_id}")] == [
        1,
        0,
    ]
    assert store.load(ctx_b, f"run:{run_id}") is None

    replay = Checkpoint(
        thread_id=f"run:{run_id}",
        tenant_id=tenant_a.id,
        step=1,
        state={"approval": "required", "phase": "awaiting"},
        awaiting="tool_approval",
        created_at=datetime.now(UTC),
    )
    store.save(ctx_a, replay)
    with pytest.raises(CheckpointConflict):
        store.save(
            ctx_a,
            Checkpoint(
                thread_id=f"run:{run_id}",
                tenant_id=tenant_a.id,
                step=1,
                state={"phase": "changed"},
                awaiting="tool_approval",
                created_at=datetime.now(UTC),
            ),
        )

    record_evidence(
        "agent_checkpoints_are_durable_and_isolated",
        holds=True,
        steps=2,
        cross_tenant_visible=False,
        conflicting_rewrite_refused=True,
    )


def test_write_tool_approval_is_exact_single_use_and_idempotent(
    tenant_a, record_evidence
) -> None:
    requester = _principal(tenant_a, "owner-tools@acme.example", Role.OWNER)
    reviewer = _principal(tenant_a, "reviewer-tools@acme.example", Role.REVIEWER)
    run_id = _run(tenant_a, requester.id)
    requester_ctx = RequestContext(principal=requester, run_id=run_id)
    reviewer_ctx = RequestContext(principal=reviewer, run_id=run_id)
    calls: list[dict] = []

    def effect(_ctx, arguments):
        calls.append(dict(arguments))
        return {"written": arguments["value"]}

    spec = ToolSpec(
        name="test.write_exactly_once",
        description="exercise the durable approval gate",
        side_effect="write",
        capability=Capability.TOOL_INVOKE_WRITE,
        handler=effect,
        requires_approval=True,
    )
    registry.register(spec)
    try:
        with pytest.raises(ApprovalRequired) as requested:
            invoke(
                requester_ctx,
                tool_name=spec.name,
                arguments={"value": 7},
                run_id=run_id,
                idempotency_key="write-once",
            )
        approval_id = requested.value.approval_id
        assert decide(approval_id, Decision(approved=True), reviewer_ctx)["status"] == "approved"

        completed = invoke(
            requester_ctx,
            tool_name=spec.name,
            arguments={"value": 7},
            run_id=run_id,
            idempotency_key="write-once",
            approval_id=approval_id,
        )
        replayed = invoke(
            requester_ctx,
            tool_name=spec.name,
            arguments={"value": 7},
            run_id=run_id,
            idempotency_key="write-once",
            approval_id=approval_id,
        )

        assert completed.status == "succeeded"
        assert replayed.replayed is True
        assert replayed.execution_id == completed.execution_id
        assert calls == [{"value": 7}]

        with pytest.raises(ToolConflict):
            invoke(
                requester_ctx,
                tool_name=spec.name,
                arguments={"value": 8},
                run_id=run_id,
                idempotency_key="write-once",
                approval_id=approval_id,
            )
        with pytest.raises(ApprovalInvalid):
            invoke(
                requester_ctx,
                tool_name=spec.name,
                arguments={"value": 8},
                run_id=run_id,
                idempotency_key="altered-arguments",
                approval_id=approval_id,
            )

        with tenant_session(tenant_a) as session:
            consumed_at = session.execute(
                text("SELECT consumed_at FROM tool_approval WHERE id = :id"),
                {"id": approval_id},
            ).scalar_one()
            receipts = session.execute(
                text("SELECT count(*) FROM tool_execution WHERE run_id = :run"),
                {"run": run_id},
            ).scalar_one()
        assert consumed_at is not None
        assert receipts == 1
    finally:
        registry._tools.pop(spec.name, None)

    record_evidence(
        "write_tools_require_exact_single_use_approval",
        holds=True,
        handler_executions=len(calls),
        durable_receipts=receipts,
        replay_returned_stored_result=True,
    )


def test_a_consumed_approval_cannot_authorise_a_second_execution(
    tenant_a, record_evidence
) -> None:
    """One signature buys exactly one write.

    Neither existing case reaches this. Replaying the *same* idempotency key
    short-circuits on the execution receipt and never touches the approval;
    the reuse attempt above changes the arguments, so it is refused by the
    argument hash whether or not the approval was already spent. The gap left
    ``consumed_at`` — the single-use control itself — provably untested: a
    mutation removing it kept the whole suite green.

    So: same tool, same arguments, a *fresh* idempotency key. The only thing
    that can refuse this is the approval having already been consumed. Without
    it, one reviewer signature authorises the write repeatedly until the
    approval expires, which is a standing permission wearing an approval's name.
    """
    requester = _principal(tenant_a, "owner-single@acme.example", Role.OWNER)
    reviewer = _principal(tenant_a, "reviewer-single@acme.example", Role.REVIEWER)
    run_id = _run(tenant_a, requester.id)
    requester_ctx = RequestContext(principal=requester, run_id=run_id)
    reviewer_ctx = RequestContext(principal=reviewer, run_id=run_id)
    executed: list[dict] = []

    def effect(_ctx, arguments):
        executed.append(dict(arguments))
        return {"written": arguments["value"]}

    spec = ToolSpec(
        name="test.write_single_use",
        description="exercise single use on an identical repeat call",
        side_effect="write",
        capability=Capability.TOOL_INVOKE_WRITE,
        handler=effect,
        requires_approval=True,
    )
    registry.register(spec)
    try:
        with pytest.raises(ApprovalRequired) as requested:
            invoke(
                requester_ctx,
                tool_name=spec.name,
                arguments={"value": 7},
                run_id=run_id,
                idempotency_key="single-first",
            )
        approval_id = requested.value.approval_id
        assert decide(approval_id, Decision(approved=True), reviewer_ctx)["status"] == "approved"

        first = invoke(
            requester_ctx,
            tool_name=spec.name,
            arguments={"value": 7},
            run_id=run_id,
            idempotency_key="single-first",
            approval_id=approval_id,
        )
        assert first.status == "succeeded"

        # Identical arguments, new idempotency key: the receipt cannot answer
        # this and the argument hash still matches, so only consumed_at stands
        # between one approval and a second write.
        with pytest.raises(ApprovalInvalid):
            invoke(
                requester_ctx,
                tool_name=spec.name,
                arguments={"value": 7},
                run_id=run_id,
                idempotency_key="single-second",
                approval_id=approval_id,
            )

        with tenant_session(tenant_a) as session:
            receipts = session.execute(
                text("SELECT count(*) FROM tool_execution WHERE run_id = :run"),
                {"run": run_id},
            ).scalar_one()
        assert executed == [{"value": 7}], "the approved write ran more than once"
        assert receipts == 1
    finally:
        registry._tools.pop(spec.name, None)

    record_evidence(
        "an_approval_is_spent_by_its_first_execution",
        holds=True,
        handler_executions=len(executed),
        durable_receipts=receipts,
        second_execution_refused=True,
    )


def test_an_approval_authorises_only_the_arguments_it_named(
    tenant_a, record_evidence
) -> None:
    """A live approval must not carry over to different arguments.

    The single-use test above consumes the approval before trying to reuse it,
    so ``consumed_at IS NULL`` alone is enough to refuse there — which means the
    argument binding itself is never exercised. This case keeps the approval
    *unconsumed* and changes only the arguments, so the only thing that can
    refuse it is the argument hash. Without that, "approve this tool for this
    run" would silently mean "approve this tool for this run with any payload",
    which is the whole difference between an approval and a permission.
    """
    requester = _principal(tenant_a, "owner-args@acme.example", Role.OWNER)
    reviewer = _principal(tenant_a, "reviewer-args@acme.example", Role.REVIEWER)
    run_id = _run(tenant_a, requester.id)
    requester_ctx = RequestContext(principal=requester, run_id=run_id)
    reviewer_ctx = RequestContext(principal=reviewer, run_id=run_id)
    executed: list[dict] = []

    def effect(_ctx, arguments):
        executed.append(dict(arguments))
        return {"written": arguments["value"]}

    spec = ToolSpec(
        name="test.write_bound_arguments",
        description="exercise the argument binding on an unconsumed approval",
        side_effect="write",
        capability=Capability.TOOL_INVOKE_WRITE,
        handler=effect,
        requires_approval=True,
    )
    registry.register(spec)
    try:
        with pytest.raises(ApprovalRequired) as requested:
            invoke(
                requester_ctx,
                tool_name=spec.name,
                arguments={"value": 7},
                run_id=run_id,
                idempotency_key="bound-original",
            )
        approval_id = requested.value.approval_id
        assert decide(approval_id, Decision(approved=True), reviewer_ctx)["status"] == "approved"

        # Different arguments, fresh idempotency key: nothing has been consumed,
        # so only the argument hash stands between this and an unapproved write.
        with pytest.raises(ApprovalInvalid):
            invoke(
                requester_ctx,
                tool_name=spec.name,
                arguments={"value": 8},
                run_id=run_id,
                idempotency_key="bound-substituted",
                approval_id=approval_id,
            )

        assert executed == [], "the substituted payload must never reach the handler"
        with tenant_session(tenant_a) as session:
            consumed_at = session.execute(
                text("SELECT consumed_at FROM tool_approval WHERE id = :id"),
                {"id": approval_id},
            ).scalar_one()
            receipts = session.execute(
                text("SELECT count(*) FROM tool_execution WHERE run_id = :run"),
                {"run": run_id},
            ).scalar_one()
        # A refusal must not burn the approval, or a mistyped argument would
        # cost the reviewer a second signature.
        assert consumed_at is None
        assert receipts == 0

        # The approval is still good for what it actually named. Without this,
        # the assertions above would also pass if approvals never worked at all.
        completed = invoke(
            requester_ctx,
            tool_name=spec.name,
            arguments={"value": 7},
            run_id=run_id,
            idempotency_key="bound-original",
            approval_id=approval_id,
        )
        assert completed.status == "succeeded"
        assert executed == [{"value": 7}]
    finally:
        registry._tools.pop(spec.name, None)

    record_evidence(
        "approvals_bind_to_their_arguments",
        holds=True,
        substituted_payload_executed=False,
        approval_survived_refusal=True,
        named_payload_succeeded=True,
    )
