"""Kill the worker at every side-effect boundary; assert the invariants hold.

This is the Phase 2 acceptance test, and the reason the correctness layer exists
at all. The claim being tested is *recoverable under failure*, and the only way
to know is to fail on purpose, at every point where failing is interesting, and
check afterwards.

## The invariants, in order of severity

**I1 — no orphans.** After a crash and a reap, no run is stuck in a state
nothing will act on. Every run is `pending` (will be retried), `succeeded`, or
`failed` (terminal, with a reason). This is exactly what the Azure build loses:
a crash between `publish_domain` and `jobs.finish` leaves the job `running`
forever with the redelivered message deleted.

**I2 — no double application.** A step whose effect is not naturally idempotent
must not be applied twice, however many times the run is retried.

**I3 — completed work is not redone.** A step that completed before the crash
returns its stored result on retry rather than executing again.

**I4 — the crash actually happened.** Asserted explicitly, because a crash point
that silently never fires turns every assertion above into a tautology. This
codebase has already shipped two checks whose pass state was indistinguishable
from their nothing-to-check state; this is the guard against a third.
"""

from __future__ import annotations

import json
import os
import subprocess
import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import text

from platform_core.correctness import leases
from platform_core.db.engine import tenant_session
from platform_core.identity.principal import Tenant
from workloads.echo import workload as echo

pytestmark = [pytest.mark.chaos, pytest.mark.property]

ROOT = Path(__file__).resolve().parent.parent.parent
PY = ROOT / ".venv" / "bin" / "python"

# Every boundary the worker can die at.
#
# `mid:<step>` is the one that matters and the one an outside-in harness misses:
# it fires *inside* `perform_once`, after the effect has landed and before the
# completion record is written. `before:` and `after:` bracket the whole call, so
# they only ever produce "not claimed" or "claimed and completed" — both trivially
# recoverable. Without `mid:`, the NEEDS_RECONCILIATION path never executes and
# every "no double application" assertion passes for the wrong reason.
CRASH_POINTS = [
    "after:lease",
    *[
        f"{when}:{step}"
        for step in echo.STEPS
        for when in ("before", "mid", "after")
    ],
    "before:complete",
    "after:complete",
]

# Steps whose effect cannot be safely repeated. A crash at `mid:` on one of
# these must end terminal-with-a-reason, never retried into a duplicate.
NON_IDEMPOTENT_STEPS = {"announce"}


def _queue_run(tenant: Tenant, principal_id: uuid.UUID, message: str) -> uuid.UUID:
    with tenant_session(tenant) as s:
        return s.execute(
            text(
                "INSERT INTO run (tenant_id, workload, status, requested_by, input) "
                "VALUES (:t, 'echo', 'pending', :p, :input) RETURNING id"
            ),
            {"t": tenant.id, "p": principal_id, "input": json.dumps({"message": message})},
        ).scalar_one()


def _run_worker_until_crash(tenant: Tenant, crash_at: str, *, lease_s: float = 60.0):
    """Run one leased run in a subprocess armed to die at ``crash_at``."""
    env = {
        **os.environ,
        "PLATFORM_CRASH_AT": crash_at,
        "PYTHONPATH": str(ROOT),
        "SERVICE_ROLE": "test",
        "ENVIRONMENT": "local",
    }
    return subprocess.run(
        [
            str(PY), "-m", "tests.chaos.crash_worker",
            str(tenant.id), tenant.slug, f"crash-worker-{crash_at}", str(lease_s),
        ],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=120,
    )


def _run_state(tenant: Tenant, run_id: uuid.UUID) -> dict:
    with tenant_session(tenant) as s:
        run = s.execute(
            text(
                "SELECT status, attempt, error, result, leased_by FROM run WHERE id = :id"
            ),
            {"id": run_id},
        ).one()
        effects = s.execute(
            text(
                "SELECT step, status, attempt FROM side_effect WHERE run_id = :id "
                "ORDER BY step"
            ),
            {"id": run_id},
        ).all()
        notifications = s.execute(
            text(
                "SELECT count(*) FROM chunk WHERE collection = 'echo-notifications' "
                "AND canonical_id = :cid"
            ),
            {"cid": f"c_{str(run_id).replace('-', '')[:14]}"},
        ).scalar_one()
    return {
        "status": run.status,
        "attempt": run.attempt,
        "error": run.error,
        "result": run.result,
        "leased_by": run.leased_by,
        "effects": {e.step: {"status": e.status, "attempt": e.attempt} for e in effects},
        "notifications": notifications,
    }


def _advance_retry_clock(tenant: Tenant, run_id: uuid.UUID) -> None:
    """Advance only this run to its persisted retry time without a slow sleep."""
    with tenant_session(tenant) as session:
        delayed = session.execute(
            text("SELECT available_at > now() FROM run WHERE id = :id"),
            {"id": run_id},
        ).scalar_one()
        assert delayed, "reaped run did not receive durable retry backoff"
        session.execute(
            text("UPDATE run SET available_at = now() WHERE id = :id"),
            {"id": run_id},
        )


@pytest.mark.parametrize("crash_at", CRASH_POINTS)
def test_crash_at_every_boundary_is_recoverable(
    crash_at, tenant_a, principal_a, record_evidence
):
    """Kill the worker at one boundary, recover, and check the invariants."""
    run_id = _queue_run(tenant_a, principal_a.id, f"hello from {crash_at}")

    # ── crash ─────────────────────────────────────────────────────────────
    # A short lease so the reaper has something to do without the test sleeping
    # for a realistic lease duration.
    proc = _run_worker_until_crash(tenant_a, crash_at, lease_s=2.0)

    # I4: the crash must actually have happened. -9 is SIGKILL.
    assert proc.returncode == -9, (
        f"worker did not die at {crash_at!r} (returncode={proc.returncode}). "
        f"A crash point that never fires makes every assertion below vacuous.\n"
        f"stdout: {proc.stdout[:400]}\nstderr: {proc.stderr[-400:]}"
    )
    assert f"dying at {crash_at}" in proc.stderr

    after_crash = _run_state(tenant_a, run_id)

    # ── recover ───────────────────────────────────────────────────────────
    # The lease is 2s and the process is gone, so it is already expired or about
    # to be. The reaper is the whole recovery mechanism: recovery is something
    # that happens *to* the dead run, not something the dead process does.
    import time

    time.sleep(2.1)
    reaped = leases.reap_expired()

    after_reap = _run_state(tenant_a, run_id)

    # I1: nothing is stranded. Either the run is retryable or it is terminal.
    assert after_reap["status"] in ("pending", "succeeded", "failed"), (
        f"run stranded in {after_reap['status']!r} after crash at {crash_at} — "
        f"nothing will ever act on it"
    )
    assert after_reap["leased_by"] is None, "a dead worker still holds the lease"
    if after_reap["status"] == "pending":
        _advance_retry_clock(tenant_a, run_id)

    # ── retry to completion ───────────────────────────────────────────────
    outcomes = []
    for _ in range(3):
        state = _run_state(tenant_a, run_id)
        if state["status"] in ("succeeded", "failed"):
            break
        result = None
        from apps.worker.runner import _load_workloads, execute_one

        _load_workloads()
        result = execute_one(tenant_a, wid="recovery-worker", lease=timedelta(seconds=60))
        outcomes.append(result["outcome"] if result else "empty")

    final = _run_state(tenant_a, run_id)

    # I2: the non-idempotent step never applied twice.
    assert final["notifications"] <= 1, (
        f"the announce step applied {final['notifications']} times after a crash at "
        f"{crash_at} — a non-idempotent effect was duplicated"
    )

    # A crash after `announce` completed leaves a run that can finish; a crash
    # while `announce` was in flight is deliberately terminal, because repeating
    # it would double the notification. Both are acceptable; being stuck is not.
    assert final["status"] in ("succeeded", "failed"), final

    crashed_mid_step = crash_at.startswith("mid:")
    step_name = crash_at.split(":", 1)[1] if crashed_mid_step else None

    if crashed_mid_step and step_name in NON_IDEMPOTENT_STEPS:
        # The whole point of the retry policy. The effect landed but was never
        # recorded complete, and it cannot be safely repeated — so the run must
        # stop and say so, rather than retry and double the notification.
        assert final["status"] == "failed", (
            f"a crash inside the non-idempotent step {step_name!r} recovered to "
            f"{final['status']!r}. Either the effect was repeated, or the "
            f"NEEDS_RECONCILIATION policy did not apply."
        )
        assert "reconcil" in (final["error"] or "").lower(), (
            f"terminal, but not for the reconciliation reason: {final['error']}"
        )
        # The effect did land — that is what makes it need reconciliation rather
        # than a retry. Asserting it proves the crash happened after the write.
        assert final["notifications"] == 1, (
            f"expected the announce effect to have landed exactly once before the "
            f"crash, saw {final['notifications']}"
        )
        assert final["effects"][step_name]["status"] == "started", (
            "the step should remain claimed-but-incomplete, which is the precise "
            "description of what an operator must reconcile"
        )
    elif final["status"] == "failed":
        assert "reconcil" in (final["error"] or "").lower(), (
            f"terminal for an unexpected reason: {final['error']}"
        )

    record_evidence(
        f"chaos_crash_{crash_at.replace(':', '_')}",
        holds=True,
        crash_point=crash_at,
        died_as_instructed=True,
        state_after_crash=after_crash["status"],
        reaped=len(reaped),
        state_after_reap=after_reap["status"],
        recovery_outcomes=outcomes,
        final_status=final["status"],
        notifications_applied=final["notifications"],
        effects=final["effects"],
    )


def test_completed_steps_are_not_re_executed(tenant_a, principal_a, record_evidence):
    """I3: a step that completed before the crash returns its stored result.

    Crashing immediately after `reserve` completes, then retrying, must not
    execute `reserve` again — it must read the recorded result. Verified by
    document count, since a re-execution that happened to be idempotent would
    otherwise be indistinguishable from a skip.
    """
    run_id = _queue_run(tenant_a, principal_a.id, "no-double-reserve")

    proc = _run_worker_until_crash(tenant_a, "after:reserve", lease_s=2.0)
    assert proc.returncode == -9, proc.stderr[-400:]

    with tenant_session(tenant_a) as s:
        reserved = s.execute(
            text(
                "SELECT status, attempt FROM side_effect WHERE run_id = :r AND step = 'reserve'"
            ),
            {"r": run_id},
        ).one()
    assert reserved.status == "completed", (
        "the crash landed before `reserve` was recorded complete; the test is "
        "not exercising what it claims"
    )

    import time

    time.sleep(2.1)
    leases.reap_expired()
    _advance_retry_clock(tenant_a, run_id)

    from apps.worker.runner import _load_workloads, execute_one

    _load_workloads()
    execute_one(tenant_a, wid="recovery-worker", lease=timedelta(seconds=60))

    with tenant_session(tenant_a) as s:
        attempt_after = s.execute(
            text("SELECT attempt FROM side_effect WHERE run_id = :r AND step = 'reserve'"),
            {"r": run_id},
        ).scalar_one()

    assert attempt_after == reserved.attempt, (
        f"`reserve` was re-executed (attempt {reserved.attempt} -> {attempt_after}) "
        f"despite having completed before the crash"
    )

    record_evidence(
        "chaos_completed_steps_not_repeated",
        holds=True,
        detail="a completed step returns its stored result on retry rather than re-running",
        attempt_before=reserved.attempt,
        attempt_after=attempt_after,
    )


def test_reaper_does_not_touch_live_leases(tenant_a, principal_a, record_evidence):
    """The reaper must never race a healthy worker.

    It acts only on leases that have **already expired**, so by the time a run
    is a candidate its holder has failed to heartbeat for longer than the entire
    lease. A reaper that returned live work to `pending` would manufacture the
    double-execution the leases exist to prevent.
    """
    _queue_run(tenant_a, principal_a.id, "long-lease")

    held = leases.acquire(tenant_a, worker_id="healthy-worker", lease=timedelta(seconds=300))
    assert held is not None

    reaped = leases.reap_expired()
    assert not any(r["run_id"] == str(held.run_id) for r in reaped), (
        "the reaper took a live lease"
    )

    assert leases.heartbeat(held), "a live worker lost its lease to the reaper"

    with tenant_session(tenant_a) as s:
        status = s.execute(
            text("SELECT status, leased_by FROM run WHERE id = :id"), {"id": held.run_id}
        ).one()
    assert status.status == "leased" and status.leased_by == "healthy-worker"

    record_evidence(
        "chaos_reaper_respects_live_leases",
        holds=True,
        detail="a 300s lease is untouched by the reaper and heartbeats successfully",
    )


def test_fence_rejects_a_woken_zombie(tenant_a, principal_a, record_evidence):
    """A worker whose lease was reaped cannot complete the run afterwards.

    The scenario: worker A stalls past its deadline, the reaper returns the run,
    worker B takes it — then A wakes and tries to finish. Without a fence on
    `leased_by`, A silently overwrites B's outcome.
    """
    _queue_run(tenant_a, principal_a.id, "zombie")

    stalled = leases.acquire(tenant_a, worker_id="worker-a", lease=timedelta(seconds=-1))
    assert stalled is not None

    reaped = leases.reap_expired()
    assert any(r["run_id"] == str(stalled.run_id) for r in reaped)
    _advance_retry_clock(tenant_a, stalled.run_id)

    fresh = leases.acquire(tenant_a, worker_id="worker-b", lease=timedelta(seconds=60))
    assert fresh is not None and fresh.run_id == stalled.run_id

    # The zombie wakes up and tries to finish.
    assert leases.complete(stalled, result={"from": "zombie"}) is False, (
        "a worker completed a lease it no longer held — the fence is not applied"
    )
    assert leases.heartbeat(stalled) is False

    # The rightful holder still can.
    assert leases.complete(fresh, result={"from": "worker-b"}) is True

    with tenant_session(tenant_a) as s:
        result = s.execute(
            text("SELECT result FROM run WHERE id = :id"), {"id": fresh.run_id}
        ).scalar_one()
    assert result["from"] == "worker-b", f"the zombie's result won: {result}"

    record_evidence(
        "chaos_fence_rejects_zombie",
        holds=True,
        detail="complete() and heartbeat() both reject a worker that lost its lease",
    )
