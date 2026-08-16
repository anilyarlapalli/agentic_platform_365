"""Ports: what the platform needs, stated without saying who provides it.

The Azure wrapper has no layer like this, and the cost is visible in its shape:
``blob_store`` speaks Blob, ``queue_client`` speaks Storage Queue, and
``search_store`` speaks AI Search, so moving substrate means rewriting each one.
A port that exists only in prose is not a port.

Every interface here is a ``Protocol``, not a base class. Adapters are checked
structurally, so an adapter can be written without importing the platform and a
test double is just an object with the right methods — no inheritance, no
registration, no import cycle between the thing and its abstraction.

Two rules that make these worth having:

**Ports take a** :class:`RequestContext`. Not a tenant id, not a string — the
context, which carries the tenant, the principal, the run and the idempotency
key. That is what makes it structurally impossible for an adapter to perform
work that cannot be attributed, which is the specific failure the Azure build
has: its chat and onboarding LLM spend both bill to the string ``"unknown"``
because no call site had a typed thing to pass.

**Ports raise port-level errors.** An adapter that lets ``botocore`` or
``psycopg`` exceptions escape has leaked its identity, and every caller then
handles the union of every adapter's exception tree.
"""

from __future__ import annotations

from platform_core.ports.checkpoint import Checkpoint, CheckpointStore, Durability
from platform_core.ports.errors import (
    BudgetExceededError,
    ConflictError,
    NotFoundError,
    PortError,
    TransientError,
)
from platform_core.ports.job_queue import JobQueue, QueueMessage
from platform_core.ports.ledger import BudgetStatus, Ledger, UsageRecord
from platform_core.ports.llm import ChatRequest, ChatResponse, LLMClient, TokenUsage
from platform_core.ports.object_store import ObjectStore, StoredObject
from platform_core.ports.vector_index import SearchHit, VectorIndex

__all__ = [
    "BudgetExceededError",
    "BudgetStatus",
    "ChatRequest",
    "ChatResponse",
    "Checkpoint",
    "CheckpointStore",
    "ConflictError",
    "Durability",
    "JobQueue",
    "Ledger",
    "LLMClient",
    "NotFoundError",
    "ObjectStore",
    "PortError",
    "QueueMessage",
    "SearchHit",
    "StoredObject",
    "TokenUsage",
    "TransientError",
    "UsageRecord",
    "VectorIndex",
]
