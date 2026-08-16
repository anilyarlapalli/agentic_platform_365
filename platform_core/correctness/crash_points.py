"""Deterministic crash injection, for proving recovery rather than hoping for it.

A recovery path that has never run is a plan, not a mechanism. The only way to
know that a crash between two writes is survivable is to crash between them, on
purpose, at every boundary, and check the invariants afterwards.

## Why SIGKILL and not an exception

An exception unwinds. `finally` blocks run, context managers exit, transactions
roll back cleanly, connections close. That is not what a dying process does — a
container OOM, a node eviction or a `docker kill` stops the process between two
instructions with a transaction open and a socket half-written.

So :func:`maybe_crash` sends ``SIGKILL`` to its own process. No handlers, no
unwinding, no cleanup. The state left behind is the state a real crash leaves.

## Why an environment variable

The crash point has to survive into a **subprocess**, because the test has to
outlive the thing it kills. An in-process patch cannot express "die halfway
through", so the harness sets ``PLATFORM_CRASH_AT`` and the worker runs in its
own process.

Disabled unless that variable is set, so this is inert in every normal run.
"""

from __future__ import annotations

import logging
import os
import signal

logger = logging.getLogger("platform.correctness.crash_points")

CRASH_ENV_VAR = "PLATFORM_CRASH_AT"


def armed_at() -> str | None:
    return os.environ.get(CRASH_ENV_VAR) or None


def maybe_crash(point: str) -> None:
    """Kill this process, hard, if ``point`` is the armed crash point.

    ``os.kill(os.getpid(), SIGKILL)`` rather than ``sys.exit`` or ``os._exit``:
    the first two are still cooperative enough to flush buffers, and a flushed
    buffer is a write that a real crash might not have made.
    """
    target = armed_at()
    if target is None or target != point:
        return

    # Written to stderr unbuffered, because the process is about to stop
    # existing and anything buffered will be lost — which is the point.
    os.write(2, f"[crash-point] dying at {point}\n".encode())
    os.kill(os.getpid(), signal.SIGKILL)


def crash_points_for(steps: list[str]) -> list[str]:
    """Every boundary around a list of steps.

    A crash *before* a step and *after* it are different failures: before leaves
    no claim, after leaves a claim with the effect applied but possibly no
    completion record. Both must be recoverable, so both are enumerated.
    """
    points: list[str] = []
    for step in steps:
        points.append(f"before:{step}")
        points.append(f"after:{step}")
    return points
