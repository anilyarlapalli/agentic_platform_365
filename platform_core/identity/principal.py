"""Who is acting, and on whose behalf.

In the Azure build, identity is a free-text string: ``created_by`` on a job row,
``actor`` on a usage row. That is enough to print in a UI and not enough for
anything else — you cannot authorise against it, you cannot enforce a per-tenant
ceiling with it, and when spend has to be attributed there is nothing to join on.
The observable consequence there is that chat and onboarding LLM spend both bill
to the string ``"unknown"``, because no call site had a typed thing to pass.

So identity here is an object with a tenant in it, and it is required — not
defaulted — everywhere a decision or a charge depends on it.

Three distinct notions, deliberately not collapsed:

``Tenant``
    The isolation boundary. Every tenant-scoped row carries ``tenant_id``, and
    Postgres row-level security enforces it. Nothing above the database is
    trusted to filter.

``Principal``
    The authenticated actor: a human, a service, or a job acting on behalf of
    one. Carries the tenant it belongs to, so a principal can never be used
    against another tenant's data even by a caller that forgot to check.

``RequestContext``
    The unit of work: request id, run id, idempotency key, and the principal.
    This is what flows through the queue and into the LLM call, which is how
    spend becomes attributable and retries become safe.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Self


class Role(StrEnum):
    """Roles are coarse; authority is fine.

    A role answers "what kind of actor is this". It does not answer "may this
    actor do this to this object" — that is a capability check against the
    object's tenant and, for tools, against a capability grant. Conflating the
    two is how ``admin`` becomes the answer to every question and the boundary
    stops meaning anything.
    """

    OWNER = "owner"        # tenant administrator: manages members and budgets
    OPERATOR = "operator"  # may run workloads and ingest
    REVIEWER = "reviewer"  # may approve gated actions, may not initiate them
    VIEWER = "viewer"      # read-only
    SERVICE = "service"    # non-human; a worker acting on behalf of a tenant


class ActorType(StrEnum):
    HUMAN = "human"
    SERVICE = "service"
    JOB = "job"


@dataclass(frozen=True, slots=True)
class Tenant:
    id: uuid.UUID
    slug: str

    def __post_init__(self) -> None:
        if not self.slug or not self.slug.replace("-", "").replace("_", "").isalnum():
            raise ValueError(f"tenant slug {self.slug!r} must be alphanumeric with - or _")


@dataclass(frozen=True, slots=True)
class Principal:
    """An authenticated actor, permanently bound to one tenant.

    Frozen because a principal that can be mutated mid-request is a principal
    that can be escalated mid-request. Deriving a different one is explicit:
    :meth:`as_service` returns a new object rather than editing this one.
    """

    id: uuid.UUID
    tenant: Tenant
    subject: str                      # email for humans, name for services
    roles: frozenset[Role]
    actor_type: ActorType = ActorType.HUMAN

    def has_role(self, *roles: Role) -> bool:
        return bool(self.roles.intersection(roles))

    @property
    def is_owner(self) -> bool:
        return Role.OWNER in self.roles

    def as_service(self, name: str) -> Self:
        """Derive a service principal for the same tenant.

        Used when a worker acts on behalf of a tenant: the work keeps the
        tenant boundary but stops claiming to be the human who queued it. That
        distinction matters in the audit log — "alice ingested" and "the worker
        ingested for alice's tenant" are different events, and only one of them
        is a thing alice did.
        """
        return replace(
            self,
            subject=f"service:{name}",
            actor_type=ActorType.SERVICE,
            roles=frozenset({Role.SERVICE}),
        )

    def audit_ref(self) -> dict[str, str]:
        """Stable, non-sensitive identity for an audit row."""
        return {
            "principal_id": str(self.id),
            "tenant_id": str(self.tenant.id),
            "tenant_slug": self.tenant.slug,
            "actor_type": str(self.actor_type),
        }


@dataclass(frozen=True, slots=True)
class RequestContext:
    """One unit of work, from ingress through the queue to the LLM call.

    ``request_id``
        One inbound HTTP request. Changes on every retry from the client.

    ``run_id``
        One logical execution of a workload. **Stable across retries** — a
        redelivered queue message resumes the same run, so side effects keyed on
        ``(run_id, step)`` are naturally idempotent.

    ``idempotency_key``
        Client-supplied, scoped to the tenant. Two requests with the same key
        must produce one run. This is the control that makes a double-clicked
        upload or a retried POST safe, and it is the thing the Azure build has
        no equivalent of — there, two uploads to the same domain create two jobs
        that then race on a full index rebuild.

    ``causation_id`` / ``correlation_id``
        Which message caused this work, and which end-to-end flow it belongs to.
        The Azure build propagates W3C traceparent, which gets the spans into one
        trace; these get the *records* into one story, which is what an audit
        needs when the traces have aged out.
    """

    principal: Principal
    request_id: uuid.UUID = field(default_factory=uuid.uuid4)
    # HTTP requests are not durable runs unless they explicitly create one.
    # Keeping this nullable prevents synchronous model usage from carrying a
    # random UUID that cannot satisfy the usage ledger's run foreign key.
    run_id: uuid.UUID | None = None
    idempotency_key: str | None = None
    correlation_id: uuid.UUID | None = None
    causation_id: uuid.UUID | None = None
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    # Free-form, bounded, non-sensitive. Used for cost attribution dimensions
    # such as workload and task; never for authorisation.
    labels: dict[str, str] = field(default_factory=dict)

    @property
    def tenant(self) -> Tenant:
        return self.principal.tenant

    def child(self, *, step: str, **labels: str) -> Self:
        """Derive a context for a sub-step of the same run.

        Keeps ``run_id`` — the run is the idempotency scope, and a sub-step that
        invented a new one would make its own side effects un-deduplicable.
        """
        return replace(
            self,
            request_id=uuid.uuid4(),
            causation_id=self.request_id,
            correlation_id=self.correlation_id or self.request_id,
            labels={**self.labels, "step": step, **labels},
        )

    def cost_dimensions(self) -> dict[str, str]:
        """The labels every LLM charge is recorded against.

        ``tenant`` is mandatory and cannot be absent: the platform asserts in
        ``tests/properties/test_cost_attribution.py`` that no usage row is ever
        written with an unknown tenant. That assertion is only possible because
        this returns a tenant from a typed object rather than reading a
        context variable that some call paths forget to set.
        """
        return {
            "tenant_id": str(self.tenant.id),
            "tenant_slug": self.tenant.slug,
            "principal_id": str(self.principal.id),
            "run_id": str(self.run_id),
            **{k: v for k, v in self.labels.items() if v},
        }
