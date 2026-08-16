"""Load: measure, record, and assert only what is genuinely a budget.

Marked ``load`` and excluded from the default run — these take seconds, not
milliseconds, and a suite people skip because it is slow stops being run at all.

## What is asserted and what is only recorded

Most of this **measures**. Absolute latency on a developer laptop, sharing a
Postgres with an unrelated stack, is not a number to gate on — asserting it would
produce a test that fails for reasons unrelated to the code.

What *is* asserted are the properties that must hold at any speed:

* the queue drains completely under concurrency, losing nothing;
* concurrent workers never double-execute a step;
* per-tenant isolation holds while several tenants are loaded simultaneously;
* the observed p95 is *recorded* so a regression is visible as a trend.

Numbers land in ``evidence/load/`` so "is it slower than last week" is a diff
rather than a memory.
"""

from __future__ import annotations

import json
import statistics
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text

from platform_core.correctness import leases, outbox
from platform_core.db.engine import tenant_session
from platform_core.identity.principal import RequestContext

pytestmark = [pytest.mark.load, pytest.mark.property]

EVIDENCE = Path(__file__).resolve().parent.parent.parent / "evidence" / "load"


def _record(name: str, **numbers) -> None:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    (EVIDENCE / f"{name}.json").write_text(
        json.dumps(
            {**numbers, "recorded_at": datetime.now(UTC).isoformat(timespec="seconds")},
            indent=2, default=str,
        )
    )


def _percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(len(ordered) * p))
    return ordered[index]


def test_the_queue_drains_completely_under_concurrency(tenant_a, principal_a):
    """200 runs, 8 workers. Every run finishes exactly once, and none is lost.

    The assertion is completeness, not speed. A queue that drains fast but drops
    one job in two hundred is worse than a slow one that drops none — and the
    only way to see it is to run enough work that a rare race becomes likely.
    """
    total = 200
    started = time.monotonic()
    with tenant_session(tenant_a) as s:
        for i in range(total):
            outbox.enqueue_run(
                s, RequestContext(principal=principal_a, idempotency_key=f"load-{i}"),
                workload="echo", payload={"message": f"message {i}"},
            )
    admitted = time.monotonic() - started

    from apps.worker.runner import _load_workloads, execute_one

    _load_workloads()

    latencies: list[float] = []
    outcomes: list[str] = []

    def drain_one() -> None:
        while True:
            call_started = time.monotonic()
            result = execute_one(tenant_a, lease=timedelta(seconds=120))
            if result is None:
                return
            latencies.append((time.monotonic() - call_started) * 1000)
            outcomes.append(result["outcome"])

    drain_started = time.monotonic()
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: drain_one(), range(8)))
    drain_elapsed = time.monotonic() - drain_started

    with tenant_session(tenant_a) as s:
        counts = dict(
            s.execute(
                text("SELECT status, count(*) FROM run GROUP BY status")
            ).all()
        )
        # Exactly one completed side-effect row per step per run: the proof that
        # concurrency did not double-apply anything.
        duplicate_steps = s.execute(
            text(
                "SELECT count(*) FROM (SELECT run_id, step, count(*) AS n "
                "FROM side_effect GROUP BY run_id, step HAVING count(*) > 1) d"
            )
        ).scalar_one()

    assert counts.get("succeeded", 0) == total, f"expected {total} succeeded, got {counts}"
    assert counts.get("pending", 0) == 0, "runs left stranded in pending"
    assert counts.get("leased", 0) == 0, "runs left leased after the drain"
    assert duplicate_steps == 0, f"{duplicate_steps} (run_id, step) pairs executed twice"

    _record(
        "queue_drain",
        runs=total, workers=8,
        admitted_s=round(admitted, 3),
        drained_s=round(drain_elapsed, 3),
        throughput_runs_per_s=round(total / drain_elapsed, 1),
        p50_ms=round(_percentile(latencies, 0.50), 1),
        p95_ms=round(_percentile(latencies, 0.95), 1),
        p99_ms=round(_percentile(latencies, 0.99), 1),
        mean_ms=round(statistics.mean(latencies), 1) if latencies else 0,
        duplicate_steps=duplicate_steps,
        outcomes={o: outcomes.count(o) for o in set(outcomes)},
    )


def test_isolation_holds_while_several_tenants_are_loaded(tenant_a, tenant_b,
                                                          principal_a, principal_b):
    """Concurrency does not weaken the boundary.

    Every isolation test elsewhere runs a quiet database. This one hammers two
    tenants through one connection pool simultaneously — the arrangement in which
    a pooled-connection GUC leak actually manifests.
    """
    per_tenant = 40

    def load_tenant(tenant, principal, marker: str) -> list[str]:
        seen: list[str] = []
        for i in range(per_tenant):
            with tenant_session(tenant) as s:
                s.execute(
                    text(
                        "INSERT INTO run (tenant_id, workload, status, requested_by, input) "
                        "VALUES (:t, 'echo', 'pending', :p, :input)"
                    ),
                    {"t": tenant.id, "p": principal.id,
                     "input": json.dumps({"marker": marker, "i": i})},
                )
                rows = s.execute(
                    text("SELECT DISTINCT input->>'marker' FROM run WHERE input ? 'marker'")
                ).scalars().all()
            seen.extend(r for r in rows if r)
        return seen

    with ThreadPoolExecutor(max_workers=2) as pool:
        a_future = pool.submit(load_tenant, tenant_a, principal_a, "acme")
        b_future = pool.submit(load_tenant, tenant_b, principal_b, "globex")
        a_seen, b_seen = a_future.result(), b_future.result()

    assert set(a_seen) == {"acme"}, f"tenant A saw {set(a_seen)} under concurrent load"
    assert set(b_seen) == {"globex"}, f"tenant B saw {set(b_seen)} under concurrent load"

    _record(
        "concurrent_isolation",
        operations=per_tenant * 2, tenants=2,
        cross_tenant_reads=0,
        detail="two tenants interleaved through one pool; neither observed the other",
    )


def test_lease_contention_never_double_executes(tenant_a, principal_a):
    """16 workers racing for 50 runs. Each run is executed by exactly one.

    Lease acquisition uses `FOR UPDATE SKIP LOCKED`, so contention should spread
    workers across rows rather than serialising them behind one lock. The
    recorded throughput is what shows whether that is actually happening.
    """
    total = 50
    with tenant_session(tenant_a) as s:
        for i in range(total):
            s.execute(
                text(
                    "INSERT INTO run (tenant_id, workload, status, requested_by, input) "
                    "VALUES (:t, 'echo', 'pending', :p, :input)"
                ),
                {"t": tenant_a.id, "p": principal_a.id,
                 "input": json.dumps({"message": f"contended {i}"})},
            )

    acquired: list[str] = []

    def grab() -> None:
        while True:
            run = leases.acquire(
                tenant_a, worker_id=f"w-{uuid.uuid4().hex[:6]}",
                lease=timedelta(seconds=120),
            )
            if run is None:
                return
            acquired.append(str(run.run_id))

    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(lambda _: grab(), range(16)))
    elapsed = time.monotonic() - started

    assert len(acquired) == total, f"acquired {len(acquired)} leases for {total} runs"
    assert len(set(acquired)) == total, (
        f"{len(acquired) - len(set(acquired))} runs were leased more than once"
    )

    _record(
        "lease_contention",
        runs=total, workers=16,
        elapsed_s=round(elapsed, 3),
        acquisitions_per_s=round(total / elapsed, 1),
        duplicate_leases=len(acquired) - len(set(acquired)),
    )


def test_reaper_recovers_a_large_backlog(tenant_a, principal_a):
    """100 abandoned leases return to pending in one pass.

    Recovery time matters as much as recovery: a reaper that needs a hundred
    passes to clear a hundred stranded runs turns a brief worker outage into a
    long one.
    """
    total = 100
    with tenant_session(tenant_a) as s:
        for _i in range(total):
            s.execute(
                text(
                    "INSERT INTO run (tenant_id, workload, status, requested_by, "
                    "  leased_by, lease_expires_at, attempt) "
                    "VALUES (:t, 'echo', 'leased', :p, 'dead-worker', "
                    "  now() - interval '1 minute', 1)"
                ),
                {"t": tenant_a.id, "p": principal_a.id},
            )

    started = time.monotonic()
    reaped = leases.reap_expired()
    elapsed = time.monotonic() - started

    assert len(reaped) == total, f"reaped {len(reaped)} of {total} in one pass"

    with tenant_session(tenant_a) as s:
        pending = s.execute(
            text("SELECT count(*) FROM run WHERE status = 'pending'")
        ).scalar_one()
    assert pending == total

    _record(
        "reaper_throughput",
        stranded_runs=total, reaped_in_one_pass=len(reaped),
        elapsed_s=round(elapsed, 3),
        runs_per_s=round(total / elapsed, 1) if elapsed else None,
    )
