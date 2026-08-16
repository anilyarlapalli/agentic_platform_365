"""Schema. Every tenant-scoped table carries ``tenant_id`` and is RLS-protected.

Two conventions the migrations enforce and the property tests verify:

1. **A tenant-scoped table has a non-null ``tenant_id``, a policy, and
   ``FORCE ROW LEVEL SECURITY``.** ``tests/properties/test_tenant_isolation.py``
   enumerates the catalog and fails on any table that has the column but not the
   policy — so adding a table without protecting it breaks the build rather than
   quietly widening the boundary.

2. **Chunk identity is content-addressed and stable.** The Azure build has three
   id namespaces for the same chunk — a canonical ``c_<sha1:16>``, an ordinal
   position in a loaded list, and an adapter's ``md5(stem::index)[:12]`` — which
   is why its retrieval recall read 0.0 while retrieval worked perfectly, and
   why a rebuild silently invalidates every previously issued id. Here there is
   one id, derived from content, and ordinals never cross a boundary.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from platform_core.settings import get_settings


class Base(DeclarativeBase):
    pass


def _pk() -> Mapped[uuid.UUID]:
    return mapped_column(
        PgUUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )


def _tenant_fk() -> Mapped[uuid.UUID]:
    """The discriminator every policy reads. Never nullable, never defaulted.

    No default on purpose: a row that can be written without a tenant is a row
    that will be, and it would be invisible to every tenant-scoped query
    thereafter — orphaned rather than leaked, but still wrong and hard to find.
    """
    return mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("tenant.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )


def _created_at() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class Tenant(Base):
    """The isolation boundary. Deliberately not tenant-scoped itself."""

    __tablename__ = "tenant"

    id: Mapped[uuid.UUID] = _pk()
    slug: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = _created_at()

    # Per-tenant ceilings. Absent means the platform default applies, so a new
    # tenant is bounded from its first call rather than unbounded until somebody
    # remembers to set one.
    daily_token_cap: Mapped[int | None] = mapped_column(BigInteger)
    monthly_cost_cap_usd: Mapped[float | None] = mapped_column()

    __table_args__ = (
        CheckConstraint("slug ~ '^[a-z0-9][a-z0-9_-]{1,62}$'", name="tenant_slug_format"),
    )


class Principal(Base):
    __tablename__ = "principal"

    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    subject: Mapped[str] = mapped_column(String(320), nullable=False)
    actor_type: Mapped[str] = mapped_column(String(16), nullable=False, default="human")
    roles: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    password_hash: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        # Scoped to the tenant, not globally unique: the same email may belong
        # to different people in different tenants, and forcing global
        # uniqueness would leak the existence of an account across the boundary.
        UniqueConstraint("tenant_id", "subject", name="principal_tenant_subject_uniq"),
    )


class Document(Base):
    __tablename__ = "document"

    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    workload: Mapped[str] = mapped_column(String(64), nullable=False)
    collection: Mapped[str] = mapped_column(String(128), nullable=False)
    filename: Mapped[str] = mapped_column(Text, nullable=False)
    # Content hash. Two uploads of the same bytes to the same collection are one
    # document — deduplication is an idempotency property, not an optimisation.
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    storage_key: Mapped[str] = mapped_column(Text, nullable=False)
    uploaded_by: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("principal.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = _created_at()

    chunks: Mapped[list[Chunk]] = relationship(back_populates="document", cascade="all, delete")

    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "collection", "content_sha256", name="document_tenant_collection_sha_uniq"
        ),
    )


class Chunk(Base):
    """A retrievable unit, addressed by content hash and nothing else.

    ``ordinal`` exists because BM25 and graph retrievers need a stable position
    within a build, but it is scoped to ``(document_id, build_version)`` and
    never leaves the retrieval layer. Anything crossing a process, a queue or a
    stored artifact uses ``canonical_id``, which survives a rebuild.

    **Partitioned by hash of ``tenant_id`` into 16 partitions** (migration 0014).
    An HNSW index is a navigation graph, not a sorted list, so a query cannot
    enter it at one tenant's rows — it searches globally and the tenant
    predicate is applied to whatever comes back, which silently returns fewer
    rows than asked for once a tenant owns a small fraction of a large index.
    Partitioning gives each partition its own smaller graph so a pruned query
    searches far less foreign data, and ``hnsw.iterative_scan = strict_order``
    (set on the database in 0014) covers the residual mixing.

    Two consequences worth knowing before editing this model:

    * the primary key **must** contain the partition key, hence
      ``(id, tenant_id)`` rather than ``(id)``;
    * RLS is not inherited — every partition carries its own ENABLE + FORCE and
      its own policy, because ``SELECT * FROM chunk_p3`` is governed by
      ``chunk_p3``'s policies alone and ``platform_app`` can name it.
    """

    __tablename__ = "chunk"

    id: Mapped[uuid.UUID] = _pk()
    # Also part of the primary key: Postgres requires the partition key in it.
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("tenant.id", ondelete="CASCADE"),
        nullable=False,
        primary_key=True,
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("document.id", ondelete="CASCADE"), nullable=False
    )
    collection: Mapped[str] = mapped_column(String(128), nullable=False)

    # c_<sha1:16> of the normalised chunk text. One namespace, everywhere.
    canonical_id: Mapped[str] = mapped_column(String(32), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    build_version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1)

    text: Mapped[str] = mapped_column(Text, nullable=False)
    meta: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    # Dimension comes from the settings-validated model map, so an index can
    # never be built at a width the configured embedder does not produce.
    embedding: Mapped[list[float] | None] = mapped_column(
        Vector(get_settings().embedding_dimensions)
    )
    embedding_model: Mapped[str | None] = mapped_column(String(64))

    created_at: Mapped[datetime] = _created_at()

    document: Mapped[Document] = relationship(back_populates="chunks")

    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "collection", "canonical_id", "build_version",
            name="chunk_tenant_collection_canonical_build_uniq",
        ),
        Index("chunk_tenant_collection_idx", "tenant_id", "collection"),
        {"postgresql_partition_by": "HASH (tenant_id)"},
    )


class Run(Base):
    """One logical execution of a workload. The idempotency and audit anchor.

    Status flow, with a lease rather than a bare claim::

        pending ──► leased ──► succeeded
                      │  ▲
                      │  └── heartbeat extends lease_expires_at
                      └─────► failed

    The Azure build's ``jobs.claim`` is a single conditional UPDATE, which
    correctly prevents two workers starting the same job — but it has no lease,
    so a worker that dies mid-run leaves the row in ``running`` forever with
    nothing to recover it. A lease with an expiry lets a reaper return the work
    to ``pending`` without ever risking two live workers on one run.
    """

    __tablename__ = "run"

    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    workload: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")

    # Client-supplied, unique per tenant. Two requests carrying the same key
    # resolve to one run — the control the Azure build has no equivalent of.
    idempotency_key: Mapped[str | None] = mapped_column(String(200))

    requested_by: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("principal.id", ondelete="SET NULL")
    )
    input: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    result: Mapped[dict | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)

    # Lease, not claim.
    leased_by: Mapped[str | None] = mapped_column(String(128))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    available_at: Mapped[datetime] = _created_at()
    last_enqueued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_requested_by: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("principal.id", ondelete="SET NULL")
    )
    priority: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)

    release: Mapped[str | None] = mapped_column(String(64))
    correlation_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))

    created_at: Mapped[datetime] = _created_at()
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("tenant_id", "idempotency_key", name="run_tenant_idempotency_uniq"),
        CheckConstraint(
            "status IN ('pending','leased','succeeded','failed','cancelled')",
            name="run_status_valid",
        ),
        CheckConstraint("priority BETWEEN -10 AND 10", name="run_priority_bounded"),
        Index("run_lease_idx", "status", "lease_expires_at"),
        Index("run_tenant_created_idx", "tenant_id", "created_at"),
        Index(
            "run_claimable_v2_idx",
            "status",
            "available_at",
            priority.desc(),
            "created_at",
        ),
    )


class CapabilityGrant(Base):
    """A capability held by one principal, optionally scoped to one resource."""

    __tablename__ = "capability_grant"

    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    principal_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("principal.id", ondelete="CASCADE"), nullable=False
    )
    capability: Mapped[str] = mapped_column(String(64), nullable=False)
    resource: Mapped[str] = mapped_column(String(200), nullable=False, default="*")
    granted_by: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("principal.id", ondelete="SET NULL")
    )
    granted_at: Mapped[datetime] = _created_at()
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_by: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("principal.id", ondelete="SET NULL")
    )

    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "principal_id", "capability", "resource", name="capability_grant_uniq"
        ),
    )


class ToolApproval(Base):
    """A human decision on one gated tool call.

    ``arguments_sha256`` is re-checked at execution, so an approval cannot be
    replayed against arguments the reviewer never saw — the difference between
    approving *an* action and approving *this* action.
    """

    __tablename__ = "tool_approval"

    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    run_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("run.id", ondelete="CASCADE"), nullable=False
    )
    tool_name: Mapped[str] = mapped_column(String(128), nullable=False)
    side_effect: Mapped[str] = mapped_column(String(16), nullable=False)
    arguments: Mapped[dict] = mapped_column(JSONB, nullable=False)
    arguments_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    requested_by: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("principal.id", ondelete="SET NULL"), nullable=False
    )
    requested_at: Mapped[datetime] = _created_at()
    decided_by: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("principal.id", ondelete="SET NULL")
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decision_note: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','approved','rejected','expired')",
            name="tool_approval_status_valid",
        ),
        CheckConstraint(
            "decided_by IS NULL OR decided_by <> requested_by",
            name="tool_approval_no_self_approval",
        ),
    )


class AgentCheckpoint(Base):
    __tablename__ = "agent_checkpoint"

    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    thread_id: Mapped[str] = mapped_column(String(200), nullable=False)
    step: Mapped[int] = mapped_column(BigInteger, nullable=False)
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("run.id", ondelete="CASCADE")
    )
    state: Mapped[dict] = mapped_column(JSONB, nullable=False)
    state_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    awaiting: Mapped[str | None] = mapped_column(String(128))
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("principal.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = _created_at()

    __table_args__ = (
        CheckConstraint("step >= 0", name="agent_checkpoint_step_nonnegative"),
        UniqueConstraint(
            "tenant_id", "thread_id", "step", name="agent_checkpoint_thread_step_uniq"
        ),
        Index("agent_checkpoint_latest_idx", "tenant_id", "thread_id", step.desc()),
    )


class ToolExecution(Base):
    __tablename__ = "tool_execution"

    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    run_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("run.id", ondelete="CASCADE"), nullable=False
    )
    approval_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("tool_approval.id", ondelete="SET NULL")
    )
    tool_name: Mapped[str] = mapped_column(String(128), nullable=False)
    side_effect: Mapped[str] = mapped_column(String(16), nullable=False)
    arguments_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    requested_by: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("principal.id", ondelete="SET NULL")
    )
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="started")
    result: Mapped[dict | None] = mapped_column(JSONB)
    result_sha256: Mapped[str | None] = mapped_column(String(64))
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = _created_at()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "side_effect IN ('none','write')", name="tool_execution_side_effect_valid"
        ),
        CheckConstraint(
            "status IN ('started','succeeded','failed','needs_reconciliation')",
            name="tool_execution_status_valid",
        ),
        UniqueConstraint(
            "tenant_id", "idempotency_key", name="tool_execution_idempotency_uniq"
        ),
        Index("tool_execution_run_idx", "tenant_id", "run_id", "started_at"),
    )


class BudgetReservation(Base):
    """Headroom held atomically before an external model call is dispatched."""

    __tablename__ = "budget_reservation"

    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    principal_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("principal.id", ondelete="SET NULL")
    )
    request_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    task: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str] = mapped_column(String(96), nullable=False)
    estimated_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    estimated_cost_usd: Mapped[float] = mapped_column(Numeric(12, 6), nullable=False)
    actual_tokens: Mapped[int | None] = mapped_column(Integer)
    actual_cost_usd: Mapped[float | None] = mapped_column(Numeric(12, 6))
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="reserved")
    release_reason: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = _created_at()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "estimated_tokens >= 0 AND estimated_cost_usd >= 0",
            name="budget_reservation_estimate_nonnegative",
        ),
        CheckConstraint(
            "actual_tokens IS NULL OR actual_tokens >= 0",
            name="budget_reservation_actual_tokens_nonnegative",
        ),
        CheckConstraint(
            "actual_cost_usd IS NULL OR actual_cost_usd >= 0",
            name="budget_reservation_actual_cost_nonnegative",
        ),
        CheckConstraint(
            "status IN ('reserved','settled','released','expired')",
            name="budget_reservation_status_valid",
        ),
        Index("budget_reservation_active_idx", "tenant_id", "status", "expires_at"),
    )


class ContinuousEvalPolicy(Base):
    """Mandatory cadence for one tenant's named golden set."""

    __tablename__ = "continuous_eval_policy"

    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    dataset_name: Mapped[str] = mapped_column(String(128), nullable=False)
    interval_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=21_600)
    top_k: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=5)
    next_run_at: Mapped[datetime] = _created_at()
    last_scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_run_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("run.id", ondelete="SET NULL")
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("principal.id", ondelete="SET NULL")
    )
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("principal.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _created_at()

    __table_args__ = (
        CheckConstraint(
            "interval_seconds BETWEEN 900 AND 604800",
            name="continuous_eval_interval_bounded",
        ),
        CheckConstraint("top_k BETWEEN 1 AND 50", name="continuous_eval_top_k_bounded"),
        UniqueConstraint(
            "tenant_id", "dataset_name", name="continuous_eval_policy_dataset_uniq"
        ),
        Index("continuous_eval_due_idx", "next_run_at"),
    )


class AuditChainAnchor(Base):
    """Trusted predecessor retained when aged audit rows are purged."""

    __tablename__ = "audit_chain_anchor"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("tenant.id", ondelete="CASCADE"),
        primary_key=True,
    )
    through_event_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    through_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    events_anchored: Mapped[int] = mapped_column(BigInteger, nullable=False)
    reason: Mapped[str] = mapped_column(String(500), nullable=False)
    anchored_at: Mapped[datetime] = _created_at()


# Tables whose rows belong to exactly one tenant. The migration applies RLS to
# each, and the property test asserts this list matches the catalog — so the
# list cannot drift from reality in either direction. It has already caught one
# drift: adding 0002's two tables failed the suite until this tuple was updated,
# which is the guard doing its job rather than a nuisance.
TENANT_SCOPED_TABLES: tuple[str, ...] = (
    "principal",
    "document",
    "chunk",
    "run",
    "capability_grant",
    "tool_approval",
    "outbox",
    "side_effect",
    "llm_usage",
    "audit_event",
    "eval_dataset",
    "eval_run",
    "eval_result",
    "eval_baseline",
    "eval_item_label",
    "unanswered_question",
    "session",
    "onboarding_session",
    "onboarding_artifact",
    "collection_build",
    "agent_checkpoint",
    "tool_execution",
    "budget_reservation",
    "continuous_eval_policy",
    "audit_chain_anchor",
)

# Platform-wide tables, deliberately NOT tenant-scoped: a revision serves every
# tenant, so scoping it would be meaningless. Listed so the catalog check can
# tell "correctly unscoped" from "someone forgot".
PLATFORM_TABLES: frozenset[str] = frozenset({"tenant", "release", "release_observation"})

# Tables the relay role may read across tenants, and the only ones. Asserted
# against the catalog in the property tests, so widening the relay's reach
# requires changing this list in a reviewed diff rather than adding a grant in a
# migration nobody reads twice.
RELAY_ACCESSIBLE_TABLES: frozenset[str] = frozenset({"outbox", "side_effect", "run"})
