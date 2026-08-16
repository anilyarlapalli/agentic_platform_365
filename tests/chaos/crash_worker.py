"""Subprocess entry point for the chaos harness.

Runs exactly one leased run, in its own process, with ``PLATFORM_CRASH_AT``
armed. The test kills it and then inspects what survived.

It has to be a subprocess: the harness must outlive the thing it kills, and the
crash is a real ``SIGKILL`` rather than an exception, so there is no in-process
way to express "die halfway through".
"""

from __future__ import annotations

import json
import logging
import os
import sys
import uuid
from datetime import timedelta

from platform_core.identity.principal import Tenant


def main() -> int:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "WARNING"))

    tenant = Tenant(id=uuid.UUID(sys.argv[1]), slug=sys.argv[2])
    worker_name = sys.argv[3]
    lease_seconds = float(sys.argv[4]) if len(sys.argv) > 4 else 60.0

    from apps.worker.runner import _load_workloads, execute_one

    _load_workloads()
    summary = execute_one(
        tenant, wid=worker_name, lease=timedelta(seconds=lease_seconds)
    )

    # Only reached when the crash point was never hit. Printed so the harness
    # can distinguish "died as instructed" from "ran to completion" — a crash
    # point that silently never fires would make the whole test vacuous, which
    # is a failure mode this codebase has already hit twice.
    print(json.dumps(summary or {"outcome": "empty_queue"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
