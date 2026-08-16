"""Work distribution, with leases rather than bare claims.

The Azure build's ``jobs.claim`` is a single conditional UPDATE — correct as far
as it goes, because it stops two workers starting the same job. What it lacks is
an expiry, and the gap is observable: a worker that dies mid-run leaves the row
in ``running`` forever, the redelivered message finds the job un-claimable,
returns normally, and the message is then deleted. The job is orphaned with
nothing left to retry it.

A lease fixes the class rather than the instance. The holder must heartbeat to
keep it; when the lease expires the work is returned to ``pending`` by the
reaper, and because the reaper only ever acts on an *expired* lease, there is
never a moment when two live workers hold the same run.

Note what this port does **not** expose: an "enqueue" that is separate from a
state change. Publishing is the outbox relay's job, and the relay is the only
thing that calls :meth:`publish`. Application code records intent inside its
transaction; nothing else can put a message on the wire.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class QueueMessage:
    id: str
    run_id: uuid.UUID
    tenant_id: uuid.UUID
    workload: str
    payload: dict[str, Any]
    delivery_count: int
    enqueued_at: datetime
    # W3C traceparent so a request and the work it causes land in one trace.
    trace_context: dict[str, str]


@runtime_checkable
class JobQueue(Protocol):
    def publish(self, message: QueueMessage) -> None:
        """Put a message on the wire. **Relay-only.**

        Deliberately takes a fully-formed message and no context: by the time
        anything reaches here the tenant and run are already settled, recorded
        in the outbox row, and committed. Application code that wants work done
        writes an outbox entry inside its own transaction instead.
        """
        ...

    def acquire(
        self, *, worker_id: str, lease_duration: timedelta, workloads: list[str] | None = None
    ) -> QueueMessage | None:
        """Take the next available message and lease it. None when idle."""
        ...

    def heartbeat(self, message: QueueMessage, *, worker_id: str,
                  extend_by: timedelta) -> bool:
        """Extend the lease. False means it was lost — stop working immediately.

        A worker that ignores a lost heartbeat and finishes anyway is the one
        way two workers can both apply the same side effect. Returning a bool
        rather than raising makes the check cheap enough to do in a loop.
        """
        ...

    def complete(self, message: QueueMessage, *, worker_id: str) -> None:
        """Acknowledge. Only the lease holder may complete."""
        ...

    def fail(
        self,
        message: QueueMessage,
        *,
        worker_id: str,
        error: str,
        retryable: bool,
        retry_after: timedelta | None = None,
    ) -> None:
        """Release the message.

        ``retryable`` is the caller's classification, derived from the error
        *type* rather than guessed here — see :mod:`platform_core.ports.errors`
        for why that distinction cannot live in a comment.
        """
        ...

    def reap_expired(self, *, older_than: timedelta) -> int:
        """Return expired leases to pending. The recovery path, run by the relay.

        Returns how many were reaped, which is a metric worth alerting on: a
        non-zero steady state means workers are dying mid-run.
        """
        ...

    def depth(self, workload: str | None = None) -> tuple[int, float]:
        """``(pending count, age in seconds of the oldest pending message)``.

        Age matters more than depth: a queue of 3 that has not moved in an hour
        is a stall, and a queue of 3,000 draining steadily is not an incident.
        Azure's own queue metric has no age dimension, which is why the Azure
        build had to peek for it.
        """
        ...
