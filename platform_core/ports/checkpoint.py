"""Durable state for long-running, resumable work.

A graph that pauses for human approval may wait minutes or days, and it must
resume on whichever replica picks the work up next. That makes durability a
correctness property, not a convenience — and it is the one the Azure build had
to defend most carefully, because its upstream factory only recognises
``"sqlite"`` and **silently falls through to an in-memory saver** for any other
value, including the ``"postgres"`` you would naturally configure. The app
starts, answers questions, and loses interrupted threads on restart.

Two lessons from that, both encoded here:

:meth:`CheckpointStore.durability` is part of the interface, so "am I actually
durable" is answerable at runtime by a health endpoint rather than inferred from
a log line at startup. A degraded checkpointer is a silent correctness problem,
and silent correctness problems need a surface.

There is no in-memory fallback in this port's contract. An adapter that cannot
reach its backing store raises; it does not quietly substitute something that
loses data. Availability is the caller's decision to make with full knowledge,
not the adapter's to make on its behalf.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from platform_core.identity.principal import RequestContext


@dataclass(frozen=True, slots=True)
class Checkpoint:
    thread_id: str
    tenant_id: uuid.UUID
    step: int
    state: dict[str, Any]
    created_at: datetime
    # What the graph is waiting for, when it is paused. Surfaced so an operator
    # can see *why* a thread is idle without deserialising the state blob.
    awaiting: str | None = None


@dataclass(frozen=True, slots=True)
class Durability:
    durable: bool
    backend: str
    detail: str = ""


@runtime_checkable
class CheckpointStore(Protocol):
    def durability(self) -> Durability:
        """Whether checkpoints actually survive a restart, right now.

        Reported on the health endpoint. ``durable=False`` means interrupted
        work will be lost and must be treated as an incident, not a warning.
        """
        ...

    def save(self, ctx: RequestContext, checkpoint: Checkpoint) -> None:
        ...

    def load(self, ctx: RequestContext, thread_id: str,
             *, step: int | None = None) -> Checkpoint | None:
        """Latest checkpoint for a thread, or a specific step.

        Step addressing is what makes replay possible: a run that produced a bad
        result can be resumed from before the step that produced it, rather than
        restarted from nothing.
        """
        ...

    def history(self, ctx: RequestContext, thread_id: str,
                *, limit: int = 50) -> list[Checkpoint]:
        ...

    def delete_thread(self, ctx: RequestContext, thread_id: str) -> int:
        ...
