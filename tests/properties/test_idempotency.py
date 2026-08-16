"""Concurrent attempts on the same step: exactly one executes.

Leases are the *primary* defence against two workers running one run. This file
tests the second one — the ``UNIQUE (run_id, step)`` constraint — which matters
precisely when the lease has already failed.

That is not hypothetical. The zombie scenario in ``tests/chaos`` is exactly it: a
worker stalls past its deadline, the run is reaped and re-leased, and the stalled
worker wakes up still believing it holds the lease. Its completion write is
fenced, but between waking and being fenced it is executing steps alongside the
new holder. The constraint is what stops both of them applying the same effect.

The mutation harness found this file missing. Dropping the unique constraint
left the whole suite green, because every existing test drives one worker at a
time — so a real control was unverified and the suite could not tell.
"""

from __future__ import annotations

import threading
import time
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from platform_core.correctness.side_effects import (
    EffectAlreadyRunning,
    RetryPolicy,
    perform_once,
)
from platform_core.db.engine import tenant_session
from platform_core.identity.principal import RequestContext

pytestmark = pytest.mark.property


def _new_run(tenant, principal) -> uuid.UUID:
    """A run to hang side effects from.

    Created ``pending`` rather than ``leased``: the ``run_lease_complete`` CHECK
    constraint requires a leased run to carry both a holder and an expiry, and
    it rejected the first version of this helper. These tests are about
    ``(run_id, step)`` idempotency, not about leases, so there is no holder to
    name — which is exactly what the constraint was pointing out.
    """
    with tenant_session(tenant) as s:
        return s.execute(
            text(
                "INSERT INTO run (tenant_id, workload, status, requested_by) "
                "VALUES (:t, 'echo', 'pending', :p) RETURNING id"
            ),
            {"t": tenant.id, "p": principal.id},
        ).scalar_one()


def test_concurrent_attempts_execute_the_effect_once(tenant_a, principal_a, record_evidence):
    """Two threads, one step. The effect body must run exactly once.

    The effect sleeps briefly so both threads are genuinely inside the claim
    window at the same time — without that, the second thread would simply
    observe a completed row and the race would never occur.
    """
    run_id = _new_run(tenant_a, principal_a)
    ctx = RequestContext(principal=principal_a, run_id=run_id)

    executions: list[str] = []
    outcomes: list[str] = []
    barrier = threading.Barrier(2)

    def effect() -> dict:
        executions.append(threading.current_thread().name)
        time.sleep(0.25)
        return {"ran_in": threading.current_thread().name}

    def attempt() -> None:
        barrier.wait(timeout=5)
        try:
            result = perform_once(ctx, "race", effect, retry_policy=RetryPolicy.SAFE_TO_REPEAT)
            outcomes.append("repeated" if result.repeated else "executed")
        except EffectAlreadyRunning:
            outcomes.append("already_running")

    threads = [threading.Thread(target=attempt, name=f"racer-{i}") for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    assert len(executions) == 1, (
        f"the effect body ran {len(executions)} times ({executions}) for one "
        f"(run_id, step). The UNIQUE (run_id, step) constraint is what makes the "
        f"INSERT the claim; without it both attempts proceed."
    )
    assert sorted(outcomes) == ["already_running", "executed"], outcomes

    with tenant_session(tenant_a) as s:
        rows = s.execute(
            text("SELECT count(*) FROM side_effect WHERE run_id = :r AND step = 'race'"),
            {"r": run_id},
        ).scalar_one()
    assert rows == 1, f"{rows} side_effect rows for one (run_id, step)"

    record_evidence(
        "idempotency_concurrent_attempts",
        holds=True,
        detail="two concurrent attempts on one (run_id, step): effect body executed once",
        executions=len(executions),
        outcomes=sorted(outcomes),
    )


def test_one_side_effect_row_per_run_and_step_is_enforced_by_the_database(
    tenant_a, principal_a, record_evidence
):
    """The UNIQUE (run_id, step) constraint exists and rejects a second row.

    Tested directly rather than by racing two threads. ``perform_once`` reads
    the row before inserting, and with the claim deadline in place the second
    caller almost always *sees* the first one's committed claim and backs off —
    so the interleaving in which the constraint is the deciding mechanism is
    real but narrow, and a race-based test would only catch a regression when
    the scheduler cooperated.

    The mutation harness made this concrete: dropping the constraint left the
    race test green. A control whose test only fails on a lucky interleaving is
    not verified, so this asserts the constraint itself — deterministically, in
    the layer that actually holds the guarantee.
    """
    run_id = _new_run(tenant_a, principal_a)

    with tenant_session(tenant_a) as s:
        s.execute(
            text(
                "INSERT INTO side_effect (tenant_id, run_id, step, status) "
                "VALUES (:t, :r, 'dup', 'started')"
            ),
            {"t": tenant_a.id, "r": run_id},
        )

    with pytest.raises(IntegrityError) as err, tenant_session(tenant_a) as s:
        s.execute(
            text(
                "INSERT INTO side_effect (tenant_id, run_id, step, status) "
                "VALUES (:t, :r, 'dup', 'started')"
            ),
            {"t": tenant_a.id, "r": run_id},
        )
    assert "side_effect_run_step_uniq" in str(err.value), (
        f"a second row for one (run_id, step) was rejected, but not by the "
        f"uniqueness constraint: {err.value}"
    )

    record_evidence(
        "idempotency_unique_constraint_enforced",
        holds=True,
        detail="UNIQUE (run_id, step) rejects a duplicate claim at the database level",
    )


def test_the_same_step_across_different_runs_is_independent(
    tenant_a, principal_a, record_evidence
):
    """Idempotency is scoped to the run, not to the step name.

    A step called ``transform`` in run A and run B are different units of work.
    Scoping on the step name alone would make the second run silently return the
    first one's result — a far worse bug than a duplicate execution, because it
    is a wrong answer rather than repeated work.
    """
    run_one = _new_run(tenant_a, principal_a)
    run_two = _new_run(tenant_a, principal_a)

    results = []
    for run_id, marker in ((run_one, "first"), (run_two, "second")):
        ctx = RequestContext(principal=principal_a, run_id=run_id)
        outcome = perform_once(ctx, "transform", lambda m=marker: {"marker": m})
        results.append(outcome)

    assert results[0].result["marker"] == "first"
    assert results[1].result["marker"] == "second", (
        "the second run received the first run's stored result — idempotency is "
        "keyed on the step name alone rather than on (run_id, step)"
    )
    assert not any(r.repeated for r in results)

    record_evidence(
        "idempotency_scoped_to_run",
        holds=True,
        detail="the same step name in two runs executes independently",
    )


def test_a_completed_step_returns_its_stored_result(tenant_a, principal_a, record_evidence):
    """A second call returns the recorded result without re-running the body."""
    run_id = _new_run(tenant_a, principal_a)
    ctx = RequestContext(principal=principal_a, run_id=run_id)

    calls = []

    def effect() -> dict:
        calls.append(1)
        return {"value": len(calls)}

    first = perform_once(ctx, "once", effect)
    second = perform_once(ctx, "once", effect)

    assert len(calls) == 1, f"the effect body ran {len(calls)} times"
    assert first.repeated is False and second.repeated is True
    assert second.result == first.result == {"value": 1}

    record_evidence(
        "idempotency_completed_step_returns_result",
        holds=True,
        detail="a repeated call returns the stored result and does not re-execute",
    )
