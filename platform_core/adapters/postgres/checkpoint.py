"""Immutable PostgreSQL checkpoints with tenant RLS and replay history."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from functools import lru_cache

from sqlalchemy import text

from platform_core.db.engine import system_session, tenant_session
from platform_core.identity.principal import RequestContext
from platform_core.ports.checkpoint import Checkpoint, Durability
from platform_core.settings import get_settings


class CheckpointConflict(RuntimeError):
    """The same thread step was reused for different state."""


class PostgresCheckpointStore:
    def durability(self) -> Durability:
        try:
            with system_session(reason="checkpoint durability probe") as session:
                exists = session.execute(
                    text("SELECT to_regclass('public.agent_checkpoint') IS NOT NULL")
                ).scalar_one()
            return Durability(
                durable=bool(exists),
                backend="postgresql",
                detail="immutable tenant-scoped checkpoints" if exists else "table missing",
            )
        except Exception as exc:
            return Durability(False, "postgresql", type(exc).__name__)

    def save(self, ctx: RequestContext, checkpoint: Checkpoint) -> None:
        if checkpoint.tenant_id != ctx.tenant.id:
            raise ValueError("checkpoint tenant does not match request context")
        _validate_metadata(checkpoint.thread_id, checkpoint.awaiting)
        if checkpoint.step < 0:
            raise ValueError("checkpoint step cannot be negative")

        encoded = _canonical(checkpoint.state)
        normalized = json.loads(encoded)
        if len(encoded) > get_settings().checkpoint_max_state_bytes:
            raise ValueError("checkpoint state exceeds checkpoint_max_state_bytes")
        digest = hashlib.sha256(encoded).hexdigest()

        with tenant_session(ctx.tenant) as session:
            inserted = session.execute(
                text(
                    "INSERT INTO agent_checkpoint "
                    "(tenant_id, thread_id, step, run_id, state, state_sha256, awaiting, "
                    " created_by, created_at) "
                    "VALUES (:tenant, :thread, :step, :run, CAST(:state AS jsonb), :sha, "
                    " :awaiting, :created_by, :created_at) "
                    "ON CONFLICT (tenant_id, thread_id, step) DO NOTHING "
                    "RETURNING state_sha256"
                ),
                {
                    "tenant": ctx.tenant.id,
                    "thread": checkpoint.thread_id,
                    "step": checkpoint.step,
                    "run": ctx.run_id,
                    "state": json.dumps(normalized, separators=(",", ":")),
                    "sha": digest,
                    "awaiting": checkpoint.awaiting,
                    "created_by": ctx.principal.id,
                    "created_at": checkpoint.created_at,
                },
            ).scalar_one_or_none()
            if inserted is not None:
                return
            existing = session.execute(
                text(
                    "SELECT state_sha256, awaiting, run_id FROM agent_checkpoint "
                    "WHERE thread_id = :thread AND step = :step"
                ),
                {"thread": checkpoint.thread_id, "step": checkpoint.step},
            ).one()
            if (
                existing.state_sha256 != digest
                or existing.awaiting != checkpoint.awaiting
                or existing.run_id != ctx.run_id
            ):
                raise CheckpointConflict(
                    f"checkpoint {checkpoint.thread_id!r} step {checkpoint.step} already "
                    "contains different state"
                )

    def append(
        self,
        ctx: RequestContext,
        thread_id: str,
        state: dict,
        *,
        awaiting: str | None = None,
    ) -> Checkpoint:
        """Append the next step under an advisory lock, safe across replicas."""
        _validate_metadata(thread_id, awaiting)
        encoded = _canonical(state)
        normalized = json.loads(encoded)
        if len(encoded) > get_settings().checkpoint_max_state_bytes:
            raise ValueError("checkpoint state exceeds checkpoint_max_state_bytes")
        digest = hashlib.sha256(encoded).hexdigest()
        with tenant_session(ctx.tenant) as session:
            session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:thread, 0))"),
                {"thread": f"{ctx.tenant.id}:{thread_id}"},
            )
            step = int(
                session.execute(
                    text(
                        "SELECT coalesce(max(step), -1) + 1 FROM agent_checkpoint "
                        "WHERE thread_id = :thread"
                    ),
                    {"thread": thread_id},
                ).scalar_one()
            )
            created_at = datetime.now(UTC)
            session.execute(
                text(
                    "INSERT INTO agent_checkpoint "
                    "(tenant_id, thread_id, step, run_id, state, state_sha256, awaiting, "
                    " created_by, created_at) "
                    "VALUES (:tenant, :thread, :step, :run, CAST(:state AS jsonb), :sha, "
                    " :awaiting, :created_by, :created_at)"
                ),
                {
                    "tenant": ctx.tenant.id,
                    "thread": thread_id,
                    "step": step,
                    "run": ctx.run_id,
                    "state": json.dumps(normalized, separators=(",", ":")),
                    "sha": digest,
                    "awaiting": awaiting,
                    "created_by": ctx.principal.id,
                    "created_at": created_at,
                },
            )
        return Checkpoint(
            thread_id=thread_id,
            tenant_id=ctx.tenant.id,
            step=step,
            state=normalized,
            awaiting=awaiting,
            created_at=created_at,
        )

    def load(
        self, ctx: RequestContext, thread_id: str, *, step: int | None = None
    ) -> Checkpoint | None:
        where = "thread_id = :thread"
        params: dict = {"thread": thread_id}
        if step is not None:
            where += " AND step = :step"
            params["step"] = step
        order = "" if step is not None else " ORDER BY step DESC"
        with tenant_session(ctx.tenant) as session:
            row = session.execute(
                text(
                    "SELECT thread_id, tenant_id, step, state, awaiting, created_at "
                    f"FROM agent_checkpoint WHERE {where}{order} LIMIT 1"
                ),
                params,
            ).one_or_none()
        return _checkpoint(row) if row else None

    def history(
        self, ctx: RequestContext, thread_id: str, *, limit: int = 50
    ) -> list[Checkpoint]:
        if not 1 <= limit <= 500:
            raise ValueError("checkpoint history limit must be between 1 and 500")
        with tenant_session(ctx.tenant) as session:
            rows = session.execute(
                text(
                    "SELECT thread_id, tenant_id, step, state, awaiting, created_at "
                    "FROM agent_checkpoint WHERE thread_id = :thread "
                    "ORDER BY step DESC LIMIT :limit"
                ),
                {"thread": thread_id, "limit": limit},
            ).all()
        return [_checkpoint(row) for row in rows]

    def delete_thread(self, ctx: RequestContext, thread_id: str) -> int:
        with tenant_session(ctx.tenant) as session:
            return int(
                session.execute(
                    text("DELETE FROM agent_checkpoint WHERE thread_id = :thread"),
                    {"thread": thread_id},
                ).rowcount
            )


def _canonical(state: dict) -> bytes:
    if not isinstance(state, dict):
        raise ValueError("checkpoint state must be a JSON object")
    try:
        return json.dumps(
            state,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("checkpoint state must contain finite JSON values") from exc


def _validate_metadata(thread_id: str, awaiting: str | None) -> None:
    if not thread_id or len(thread_id) > 200:
        raise ValueError("checkpoint thread_id must contain 1-200 characters")
    if awaiting is not None and (not awaiting or len(awaiting) > 128):
        raise ValueError("checkpoint awaiting must contain 1-128 characters")


def _checkpoint(row) -> Checkpoint:
    return Checkpoint(
        thread_id=row.thread_id,
        tenant_id=row.tenant_id,
        step=int(row.step),
        state=dict(row.state),
        awaiting=row.awaiting,
        created_at=row.created_at,
    )


@lru_cache(maxsize=1)
def get_checkpoint_store() -> PostgresCheckpointStore:
    return PostgresCheckpointStore()
