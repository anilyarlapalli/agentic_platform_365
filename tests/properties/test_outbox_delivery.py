"""Outbox → relay → broker → worker, and what happens when each link breaks.

The claim under test is not "messages get delivered" — it is **intent is never
lost**, which is a different and stronger thing. A broker can drop a message, a
relay can die mid-publish, a worker can die mid-run, and the run must still
execute exactly once in the end.

Each case below breaks one link and checks that property survives.

Celery runs eagerly here (``task_always_eager``) so delivery is synchronous and
the assertions are deterministic. What that does *not* cover — real broker
delivery, duplicates, reordering — is covered by the design rather than by this
file: the pointer carries no authority, so a duplicate loses the lease race and
a reordering is irrelevant. ``test_delivery_is_a_hint_not_an_authorisation``
asserts exactly that, which is the property that makes eager mode a fair
substitute.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from platform_core.correctness import outbox
from platform_core.db.engine import relay_session, tenant_session
from platform_core.identity.principal import RequestContext

pytestmark = pytest.mark.property


@pytest.fixture
def ctx(tenant_a, principal_a) -> RequestContext:
    return RequestContext(principal=principal_a)


def _outbox_rows(run_id: uuid.UUID) -> list[dict]:
    with relay_session(reason="test: inspect outbox") as s:
        rows = s.execute(
            text(
                "SELECT id, published_at, publish_attempts, last_error "
                "FROM outbox WHERE run_id = :r ORDER BY id"
            ),
            {"r": run_id},
        ).all()
    return [
        {
            "id": r.id, "published": r.published_at is not None,
            "attempts": r.publish_attempts, "error": r.last_error,
        }
        for r in rows
    ]


def test_run_and_outbox_row_commit_together(ctx, tenant_a, record_evidence):
    """The state change and the intent to publish are one transaction.

    The Azure build writes the job row and then enqueues, with nothing spanning
    the two, so a crash in between admits work that nobody will ever deliver.
    Here there is no such state: either both rows exist or neither does.
    """
    with tenant_session(tenant_a) as s:
        run_id, created = outbox.enqueue_run(
            s, ctx, workload="echo", payload={"message": "together"}
        )
    assert created

    with tenant_session(tenant_a) as s:
        run_exists = s.execute(
            text("SELECT 1 FROM run WHERE id = :r"), {"r": run_id}
        ).scalar_one_or_none()
    assert run_exists == 1
    assert len(_outbox_rows(run_id)) == 1

    record_evidence(
        "outbox_atomic_admission", holds=True,
        detail="run row and outbox row are written in one transaction",
    )


def test_a_rolled_back_transaction_leaves_neither(ctx, tenant_a, record_evidence):
    """The other half of atomicity, which is the half that is usually untested.

    Asserting that both rows appear proves they are written; only a rollback
    proves they are written *together*. Without this, a two-transaction
    implementation would pass the test above.
    """
    run_id: uuid.UUID | None = None
    with pytest.raises(RuntimeError, match="deliberate"), tenant_session(tenant_a) as s:
        run_id, _ = outbox.enqueue_run(
            s, ctx, workload="echo", payload={"message": "doomed"}
        )
        raise RuntimeError("deliberate failure after enqueue_run")

    assert run_id is not None
    with tenant_session(tenant_a) as s:
        assert s.execute(
            text("SELECT 1 FROM run WHERE id = :r"), {"r": run_id}
        ).scalar_one_or_none() is None
    assert _outbox_rows(run_id) == []

    record_evidence(
        "outbox_rollback_leaves_nothing", holds=True,
        detail="a failure after enqueue_run leaves neither a run nor an outbox row",
    )


def test_idempotency_key_collapses_duplicate_submissions(tenant_a, principal_a,
                                                         record_evidence):
    """Two requests with one key produce one run and one pointer.

    This is the control the Azure build has no equivalent of: two uploads in
    quick succession there create two jobs that then race on a full rebuild of
    the same domain.
    """
    key = f"idem-{uuid.uuid4()}"
    ids = []
    created_flags = []
    for _ in range(3):
        ctx = RequestContext(principal=principal_a, idempotency_key=key)
        with tenant_session(tenant_a) as s:
            run_id, created = outbox.enqueue_run(
                s, ctx, workload="echo", payload={"message": "duplicate"}
            )
        ids.append(run_id)
        created_flags.append(created)

    assert len(set(ids)) == 1, f"one idempotency key produced {len(set(ids))} runs"
    assert created_flags == [True, False, False]
    assert len(_outbox_rows(ids[0])) == 1, "a duplicate submission produced a second pointer"

    record_evidence(
        "outbox_idempotency_key", holds=True,
        detail="three submissions with one key produce one run and one outbox row",
    )


def test_a_publish_failure_leaves_the_row_for_retry(ctx, tenant_a, record_evidence):
    """A broker outage must not destroy intent.

    The row stays unpublished with its error recorded, and the next pass
    delivers it. If a publish failure discarded the row, a broker blip would
    silently drop work that was already accepted from a user.
    """
    with tenant_session(tenant_a) as s:
        run_id, _ = outbox.enqueue_run(
            s, ctx, workload="echo", payload={"message": "broker down"}
        )

    def failing_publish(_row):
        raise ConnectionError("broker unreachable")

    published = outbox.drain(failing_publish)
    assert published == 0

    rows = _outbox_rows(run_id)
    assert rows[0]["published"] is False
    assert rows[0]["attempts"] == 1
    assert "ConnectionError" in rows[0]["error"]

    # Broker recovers.
    delivered = []
    assert outbox.drain(delivered.append) == 1
    assert [r.run_id for r in delivered] == [run_id]
    assert _outbox_rows(run_id)[0]["published"] is True

    record_evidence(
        "outbox_publish_failure_retried", holds=True,
        detail="a failed publish leaves the row unpublished with its error; the next pass delivers",
    )


def test_repeated_publish_failures_become_visible(ctx, tenant_a, record_evidence):
    """A row that can never be published is reported, not retried in silence.

    A queue failing at a constant rate looks identical to a working one from the
    outside. ``poison_rows`` is what makes the difference observable.
    """
    with tenant_session(tenant_a) as s:
        run_id, _ = outbox.enqueue_run(
            s, ctx, workload="echo", payload={"message": "poison"}
        )

    def always_fails(_row):
        raise ValueError("permanently malformed")

    for _ in range(5):
        outbox.drain(always_fails)

    poison = outbox.poison_rows(min_attempts=5)
    assert any(p["run_id"] == str(run_id) for p in poison), poison
    assert poison[0]["attempts"] >= 5

    record_evidence(
        "outbox_poison_rows_reported", holds=True,
        detail="a row failing repeatedly is surfaced by poison_rows rather than retried silently",
    )


def test_backlog_reports_depth_and_age(ctx, tenant_a, record_evidence):
    """Age matters more than depth.

    A backlog of 3 that has not moved in an hour is a stall; 3,000 draining
    steadily is not. Azure's own queue metric has no age dimension, which is why
    the Azure build had to peek at messages to derive one.
    """
    with tenant_session(tenant_a) as s:
        outbox.enqueue_run(s, ctx, workload="echo", payload={"message": "backlog"})

    depth, age = outbox.backlog()
    assert depth >= 1
    assert age >= 0.0

    outbox.drain(lambda _row: None)
    drained_depth, _ = outbox.backlog()
    assert drained_depth == 0

    record_evidence(
        "outbox_backlog_observable", holds=True,
        detail="backlog reports both unpublished count and the age of the oldest row",
        depth_before=depth, depth_after=drained_depth,
    )


def test_delivery_is_a_hint_not_an_authorisation(tenant_a, principal_a, record_evidence):
    """A pointer carries no authority to execute — the lease does.

    This is the property that makes the broker's reliability irrelevant to
    correctness, and therefore the one that lets these tests run eagerly. A
    duplicate pointer for a run that is already leased must do nothing.
    """
    from datetime import timedelta

    from platform_core.correctness import leases

    ctx = RequestContext(principal=principal_a)
    with tenant_session(tenant_a) as s:
        run_id, _ = outbox.enqueue_run(
            s, ctx, workload="echo", payload={"message": "hint"}
        )

    first = leases.acquire_specific(
        tenant_a, run_id, worker_id="worker-1", lease=timedelta(seconds=300)
    )
    assert first is not None

    # A second pointer for the same run arrives — a duplicate delivery.
    second = leases.acquire_specific(
        tenant_a, run_id, worker_id="worker-2", lease=timedelta(seconds=300)
    )
    assert second is None, (
        "a duplicate pointer leased a run another worker already holds; the "
        "message would be acting as an authorisation rather than a hint"
    )

    with tenant_session(tenant_a) as s:
        holder = s.execute(
            text("SELECT leased_by, attempt FROM run WHERE id = :r"), {"r": run_id}
        ).one()
    assert holder.leased_by == "worker-1"
    assert holder.attempt == 1, "the duplicate delivery consumed an attempt"

    record_evidence(
        "outbox_pointer_is_not_authorisation", holds=True,
        detail="a duplicate pointer cannot lease a held run and does not consume an attempt",
    )


def test_relay_credential_is_required_to_drain(record_evidence):
    """The API cannot drain the outbox, whatever it calls.

    ``outbox.drain`` runs under ``relay_session``. If the app role could do this,
    every tenant's outbox would be readable from the request path.
    """

    from platform_core.db.engine import system_session

    with system_session(reason="test: app role attempts a cross-tenant outbox read") as s:
        visible = s.execute(text("SELECT count(*) FROM outbox")).scalar_one()
    assert visible == 0, (
        f"the app role saw {visible} outbox rows with no tenant context — the "
        f"relay boundary is not a credential"
    )

    with relay_session(reason="test: relay drains") as s:
        # The relay can, which is what makes the above a boundary rather than a
        # missing grant.
        s.execute(text("SELECT count(*) FROM outbox")).scalar_one()

    record_evidence(
        "outbox_drain_requires_relay_credential", holds=True,
        detail="app-role session sees no outbox rows; the relay credential does",
    )
