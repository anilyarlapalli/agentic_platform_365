"""End-to-end check of the real transport: outbox → relay → Redis → Celery worker.

The pytest suite runs Celery eagerly, which makes delivery synchronous and the
assertions deterministic — but it also means the broker is never actually
exercised. This script closes that gap by running the genuine path with a real
Redis broker and a real worker process.

Run it with the stack up:

    make up && .venv/bin/python -m scripts.e2e_transport

Exits non-zero if the run does not reach `succeeded` through the broker.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import uuid
from pathlib import Path

from sqlalchemy import text

ROOT = Path(__file__).resolve().parent.parent
PY = ROOT / ".venv" / "bin" / "python"


def main() -> int:
    os.environ.setdefault("SERVICE_ROLE", "test")
    os.environ.setdefault("ENVIRONMENT", "local")

    from platform_core.correctness import outbox
    from platform_core.db.engine import owner_session, tenant_session
    from platform_core.identity.principal import (
        ActorType,
        Principal,
        RequestContext,
        Role,
        Tenant,
    )

    slug = f"e2e-{uuid.uuid4().hex[:8]}"
    with owner_session() as s:
        tenant_id = s.execute(
            text("INSERT INTO tenant (slug, name) VALUES (:s, 'E2E') RETURNING id"),
            {"s": slug},
        ).scalar_one()
    tenant = Tenant(id=tenant_id, slug=slug)

    with tenant_session(tenant) as s:
        principal_id = s.execute(
            text(
                "INSERT INTO principal (tenant_id, subject, roles) "
                "VALUES (:t, 'e2e@example.com', ARRAY['operator']) RETURNING id"
            ),
            {"t": tenant.id},
        ).scalar_one()

    ctx = RequestContext(
        principal=Principal(
            id=principal_id, tenant=tenant, subject="e2e@example.com",
            roles=frozenset({Role.OPERATOR}), actor_type=ActorType.HUMAN,
        )
    )

    # 1. Admit work: run row + outbox row, one transaction.
    with tenant_session(tenant) as s:
        run_id, created = outbox.enqueue_run(
            s, ctx, workload="echo", payload={"message": "hello over redis"}
        )
    print(f"admitted run {run_id} (created={created})")

    depth, _ = outbox.backlog()
    print(f"outbox backlog before relay: {depth}")
    assert depth >= 1, "the outbox row was not written"

    # 2. Start a real Celery worker against the real broker.
    # Worker output goes to a FILE, not a PIPE. A Celery worker is chatty, and an
    # unread pipe fills its 64KB buffer — at which point the worker BLOCKS on
    # write and can never process anything. The first version of this script
    # used PIPE and hung indefinitely, which looked exactly like a broker fault.
    log_path = ROOT / "evidence" / "e2e_worker.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_file = log_path.open("w")
    worker = subprocess.Popen(
        [
            str(PY), "-m", "celery", "-A", "apps.worker.tasks", "worker",
            "--loglevel=INFO", "--concurrency=1", "--pool=solo",
            "-Q", "runs", "-n", f"e2e@{uuid.uuid4().hex[:6]}",
        ],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
        stdout=log_file, stderr=subprocess.STDOUT, text=True,
    )

    try:
        time.sleep(6)  # let the worker connect and register
        if worker.poll() is not None:
            print("worker died on startup:\n" + log_path.read_text()[-3000:])
            return 1

        # 3. Relay drains the outbox onto the broker.
        from apps.relay.main import run_once

        published = run_once()
        print(f"relay published: {published}")
        if published != 1:
            print("relay did not publish the row")
            return 1

        # 4. Wait for the worker to pick it up and finish.
        deadline = time.time() + 60
        status = None
        while time.time() < deadline:
            with tenant_session(tenant) as s:
                row = s.execute(
                    text("SELECT status, result, error FROM run WHERE id = :r"),
                    {"r": run_id},
                ).one()
            status = row.status
            if status in ("succeeded", "failed"):
                break
            time.sleep(1)

        print(f"final run status: {status}")
        if status != "succeeded":
            print(f"error: {row.error}")
            print("worker output:\n" + log_path.read_text()[-3000:])
            return 1

        print(f"result: {json.dumps(row.result)}")

        with tenant_session(tenant) as s:
            steps = s.execute(
                text(
                    "SELECT step, status, attempt FROM side_effect "
                    "WHERE run_id = :r ORDER BY step"
                ),
                {"r": run_id},
            ).all()
        for st in steps:
            print(f"  step {st.step}: {st.status} (attempt {st.attempt})")
        if not all(st.status == "completed" for st in steps):
            print("not every step completed")
            return 1

        remaining, _ = outbox.backlog()
        print(f"outbox backlog after: {remaining}")

        print("\nOK — outbox → relay → Redis → Celery worker → succeeded")
        return 0
    finally:
        worker.terminate()
        try:
            worker.wait(timeout=10)
        except subprocess.TimeoutExpired:
            worker.kill()
        log_file.close()
        with owner_session() as s:
            s.execute(text("DELETE FROM tenant WHERE slug = :s"), {"s": slug})


if __name__ == "__main__":
    raise SystemExit(main())
