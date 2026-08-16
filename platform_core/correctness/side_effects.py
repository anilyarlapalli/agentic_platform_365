"""Idempotent side effects, keyed on ``(run_id, step)``.

## The failure this closes

From the Azure worker: artifacts are published to Blob, then ``jobs.finish``
marks the job succeeded. A crash between those two leaves the manifest version
bumped — so every API replica reloads it — while the job row sits ``running``
forever. On redelivery ``claim()`` returns False, the handler returns normally,
and the message is deleted. The work is orphaned with nothing left to retry it.

The general shape: **a side effect and the record that it happened are two
writes to two systems.** Whatever order you choose, a crash in the gap leaves
them disagreeing.

## What this does instead

Record the *intent* to perform a step, in Postgres, before performing it. The
``UNIQUE (run_id, step)`` constraint means the INSERT **is** the claim — a
second attempt gets a unique violation rather than a second execution.

    ┌ INSERT side_effect (started) ─── unique violation? ─► already claimed
    │
    ├ perform the effect
    │
    └ UPDATE side_effect (completed, result)

Four outcomes on retry, all handled:

``completed``
    Return the stored result. The effect is not repeated — which matters for
    effects that cannot be undone, like sending a message or charging a card.

``started``, claim **live**
    Another attempt is executing this step right now. Back off; do not run.

``started``, claim **expired**
    A previous attempt died between claiming and completing. Whether it is safe
    to retry depends on the effect, so the caller declares that with
    ``retry_policy``: an effect that is naturally idempotent at the far end (an
    S3 PUT to a content-addressed key) can simply re-run; one that is not must
    be reconciled.

``failed``
    Retry if attempts remain.

Those middle two were **one** case in the first version, and collapsing them was
a real bug rather than a simplification. Every ``started`` row read as "the
previous attempt died", so a concurrent second attempt re-ran an effect that was
still in flight. ``tests/properties/test_idempotency.py`` observed the effect
body execute twice for one ``(run_id, step)``; migration 0004 added the claim
deadline that tells the cases apart.

Note what the unique constraint does and does not buy: it makes the *row*
unique, not the *execution*. Uniqueness alone cannot distinguish a live claimant
from a dead one — that needs a deadline, exactly as the run lease does.

## What this is not

It is not a distributed transaction. The effect can still land while the
completing UPDATE is lost — the gap is narrowed, not eliminated. What it buys is
that the gap is *detectable*: a ``started`` row with an expired claim is a
precise description of what to reconcile, which is exactly what the orphaned
``running`` job in the Azure build lacks.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import threading
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from platform_core.correctness.cancellation import cancellation_point
from platform_core.db.engine import tenant_session
from platform_core.identity.principal import RequestContext

logger = logging.getLogger("platform.correctness.side_effects")


class RetryPolicy(StrEnum):
    """What to do when a previous attempt claimed a step but never completed."""

    # The far end is idempotent: re-running produces the same state. A PUT to a
    # content-addressed key, an UPSERT, a DELETE. Safe to just do it again.
    SAFE_TO_REPEAT = "safe_to_repeat"

    # The effect may have landed and repeating it would double it: sending an
    # email, appending to a log, incrementing a counter. Must not auto-retry;
    # surfaced for reconciliation instead.
    NEEDS_RECONCILIATION = "needs_reconciliation"


class EffectAlreadyRunning(RuntimeError):
    """Another live attempt holds this step. Not an error — back off and let it finish."""


class NeedsReconciliation(RuntimeError):
    """A previous attempt died mid-effect and the effect cannot be safely repeated.

    Carries the step so an operator, or a reconciler, knows precisely what to
    inspect. This is the state the Azure build has no representation for: there,
    the equivalent is a job stuck in ``running`` with no indication of how far it
    got.
    """

    def __init__(self, run_id: uuid.UUID, step: str, attempt: int) -> None:
        self.run_id, self.step, self.attempt = run_id, step, attempt
        super().__init__(
            f"run {run_id} step {step!r} was claimed by attempt {attempt} which did not "
            f"complete, and the effect is not safe to repeat — reconcile it"
        )


@dataclass(frozen=True, slots=True)
class EffectOutcome:
    result: Any
    repeated: bool          # True when a prior attempt had already completed it
    attempt: int


DEFAULT_CLAIM_TTL = timedelta(minutes=5)


def perform_once(
    ctx: RequestContext,
    step: str,
    effect: Callable[[], Any],
    *,
    retry_policy: RetryPolicy = RetryPolicy.SAFE_TO_REPEAT,
    claim_ttl: timedelta = DEFAULT_CLAIM_TTL,
) -> EffectOutcome:
    """Run ``effect`` at most once for ``(ctx.run_id, step)``.

    ``effect`` must return something JSON-serialisable, because the result is
    stored and returned verbatim to a later retry. If it cannot, have it return
    a reference — a storage key, an id — rather than the payload.

    ``claim_ttl`` bounds how long this attempt may hold the step. It must exceed
    the effect's realistic worst-case duration: a claim that lapses while the
    effect is still running lets a concurrent attempt in, which is the exact
    double-execution this function exists to prevent.
    """
    run_id = ctx.run_id
    if run_id is None:
        raise ValueError("perform_once requires a RequestContext with a durable run_id")
    cancellation_point(ctx)
    claimant = f"{socket.gethostname()}:{os.getpid()}:{threading.get_ident()}"

    # ── claim ─────────────────────────────────────────────────────────────
    with tenant_session(ctx.tenant) as s:
        # FOR UPDATE serialises two concurrent attempts through this read, so
        # the second sees the first's committed claim rather than racing it.
        existing = s.execute(
            text(
                "SELECT status, result, attempt, claimed_by, "
                "  (claim_expires_at IS NOT NULL AND claim_expires_at > now()) AS claim_live "
                "FROM side_effect WHERE run_id = :r AND step = :s FOR UPDATE"
            ),
            {"r": run_id, "s": step},
        ).one_or_none()

        if existing is None:
            try:
                s.execute(
                    text(
                        "INSERT INTO side_effect (tenant_id, run_id, step, status, "
                        "  claimed_by, claim_expires_at) "
                        "VALUES (:t, :r, :s, 'started', :by, now() + :ttl)"
                    ),
                    {"t": ctx.tenant.id, "r": run_id, "s": step,
                     "by": claimant, "ttl": claim_ttl},
                )
                attempt = 1
            except IntegrityError:
                # Lost the race between the SELECT and the INSERT. The unique
                # constraint is what makes that a rejection rather than a second
                # execution.
                raise EffectAlreadyRunning(
                    f"run {run_id} step {step!r} claimed concurrently"
                ) from None
        elif existing.status == "completed":
            logger.info("run %s step %s already completed — returning stored result",
                        run_id, step)
            return EffectOutcome(
                result=existing.result, repeated=True, attempt=existing.attempt
            )
        elif existing.status == "started":
            # The distinction migration 0004 exists for. A `started` row means
            # either "someone is running this right now" or "someone died
            # running this", and those need opposite handling. Without the claim
            # deadline both read as the second case, and a concurrent attempt
            # re-runs an effect that is still in flight — observed directly:
            # the effect body executed twice for one (run_id, step).
            if existing.claim_live:
                raise EffectAlreadyRunning(
                    f"run {run_id} step {step!r} is held by {existing.claimed_by} "
                    f"whose claim has not expired"
                )
            if retry_policy is RetryPolicy.NEEDS_RECONCILIATION:
                raise NeedsReconciliation(run_id, step, existing.attempt)
            attempt = existing.attempt + 1
            s.execute(
                text(
                    "UPDATE side_effect SET attempt = :a, started_at = now(), "
                    "  error = NULL, claimed_by = :by, claim_expires_at = now() + :ttl "
                    "WHERE run_id = :r AND step = :s"
                ),
                {"a": attempt, "r": run_id, "s": step, "by": claimant, "ttl": claim_ttl},
            )
            logger.warning(
                "run %s step %s retrying after an abandoned attempt (attempt %d) — "
                "the previous claim expired and the policy permits repeating",
                run_id, step, attempt,
            )
        else:  # failed
            attempt = existing.attempt + 1
            s.execute(
                text(
                    "UPDATE side_effect SET status = 'started', attempt = :a, "
                    "  started_at = now(), error = NULL, claimed_by = :by, "
                    "  claim_expires_at = now() + :ttl "
                    "WHERE run_id = :r AND step = :s"
                ),
                {"a": attempt, "r": run_id, "s": step, "by": claimant, "ttl": claim_ttl},
            )

    # ── perform, outside the transaction ──────────────────────────────────
    #
    # Deliberately not inside the claim transaction. Holding a Postgres
    # transaction open across a network call to S3 or an LLM means a slow
    # dependency pins a connection and an idle-in-transaction timeout kills the
    # claim mid-effect. The claim is committed first precisely so the effect can
    # take as long as it takes.
    try:
        cancellation_point(ctx)
        result = effect()

        # THE window. The effect has landed; the record that it landed has not.
        # This is exactly the Azure gap — artifacts published, `jobs.finish` not
        # yet reached — and it is the only boundary where the retry policy
        # actually decides anything. A chaos harness that brackets `perform_once`
        # from outside never lands here, so `NEEDS_RECONCILIATION` would never
        # execute and any "no double application" assertion would pass
        # vacuously. Named so `tests/chaos` can crash precisely here.
        from platform_core.correctness import crash_points

        crash_points.maybe_crash(f"mid:{step}")
    except Exception as exc:
        with tenant_session(ctx.tenant) as s:
            s.execute(
                text(
                    "UPDATE side_effect SET status = 'failed', error = :e, "
                    "  claim_expires_at = NULL WHERE run_id = :r AND step = :s"
                ),
                {"e": f"{type(exc).__name__}: {exc}"[:2000], "r": run_id, "s": step},
            )
        raise

    # ── complete ──────────────────────────────────────────────────────────
    with tenant_session(ctx.tenant) as s:
        s.execute(
            text(
                "UPDATE side_effect SET status = 'completed', completed_at = now(), "
                "  result = :res, claim_expires_at = NULL WHERE run_id = :r AND step = :s"
            ),
            {"res": json.dumps(result), "r": run_id, "s": step},
        )

    return EffectOutcome(result=result, repeated=False, attempt=attempt)


@contextmanager
def effect_boundary(ctx: RequestContext, step: str):
    """Marker for the chaos harness to crash at.

    Production code does not use this — :func:`perform_once` is the real
    mechanism. This exists so ``tests/chaos`` can name a boundary and kill the
    process exactly there, which is how "recoverable under failure" gets
    measured rather than asserted.
    """
    from platform_core.correctness import crash_points

    crash_points.maybe_crash(f"before:{step}")
    yield
    crash_points.maybe_crash(f"after:{step}")


def incomplete_effects(ctx: RequestContext, run_id: uuid.UUID) -> list[dict]:
    """Steps claimed but not completed — the precise reconciliation worklist."""
    with tenant_session(ctx.tenant) as s:
        rows = s.execute(
            text(
                "SELECT step, status, attempt, started_at, error FROM side_effect "
                "WHERE run_id = :r AND status <> 'completed' ORDER BY started_at"
            ),
            {"r": run_id},
        ).all()
    return [
        {
            "step": r.step, "status": r.status, "attempt": r.attempt,
            "started_at": r.started_at.isoformat(), "error": r.error,
        }
        for r in rows
    ]
