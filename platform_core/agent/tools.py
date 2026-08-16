"""Governed tool registry, approval gate, execution receipt, and invocation.

Every write tool is approval-gated by construction. Approvals are atomically
consumed against the exact tool, run, and canonical argument hash before an
external effect starts. A crash after consumption leaves a ``started`` receipt,
which is deliberately a reconciliation case rather than an automatic replay.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Literal

from sqlalchemy import text

from platform_core.adapters.postgres.checkpoint import get_checkpoint_store
from platform_core.correctness.cancellation import cancellation_point
from platform_core.db.engine import tenant_session
from platform_core.identity.capabilities import Capability, require
from platform_core.identity.principal import RequestContext
from platform_core.observability import audit
from platform_core.observability.telemetry import (
    bind_request_context,
    record_tool_execution,
    start_span,
)
from platform_core.settings import get_settings

SideEffect = Literal["none", "write"]
ToolHandler = Callable[[RequestContext, dict[str, Any]], dict[str, Any]]
ArgumentValidator = Callable[[dict[str, Any]], dict[str, Any]]
_NAME = re.compile(r"^[a-z][a-z0-9_.-]{1,127}$")


class UnknownTool(LookupError):
    pass


class ToolConflict(RuntimeError):
    pass


class ToolExecutionUnavailable(RuntimeError):
    pass


class ApprovalRequired(RuntimeError):
    def __init__(self, approval_id: uuid.UUID) -> None:
        self.approval_id = approval_id
        super().__init__(f"tool execution requires approval {approval_id}")


class ApprovalInvalid(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    description: str
    side_effect: SideEffect
    capability: Capability
    handler: ToolHandler
    requires_approval: bool = False
    timeout_seconds: float | None = None
    validate_arguments: ArgumentValidator | None = None

    def __post_init__(self) -> None:
        if not _NAME.fullmatch(self.name):
            raise ValueError(f"invalid tool name {self.name!r}")
        if self.side_effect == "write" and not self.requires_approval:
            raise ValueError("write tools must require approval")
        if self.timeout_seconds is not None and self.timeout_seconds <= 0:
            raise ValueError("tool timeout must be positive")


@dataclass(frozen=True, slots=True)
class ToolInvocation:
    execution_id: uuid.UUID
    tool_name: str
    status: str
    result: dict[str, Any] | None = None
    replayed: bool = False


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._tools:
            raise ValueError(f"tool {spec.name!r} is already registered")
        self._tools[spec.name] = spec

    def get(self, name: str) -> ToolSpec:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise UnknownTool(name) from exc

    def describe(self) -> list[dict[str, Any]]:
        return [
            {
                "name": spec.name,
                "description": spec.description,
                "side_effect": spec.side_effect,
                "requires_approval": spec.requires_approval,
                "capability": str(spec.capability),
            }
            for spec in sorted(self._tools.values(), key=lambda candidate: candidate.name)
        ]


registry = ToolRegistry()


def invoke(
    ctx: RequestContext,
    *,
    tool_name: str,
    arguments: dict[str, Any],
    run_id: uuid.UUID,
    idempotency_key: str,
    approval_id: uuid.UUID | None = None,
) -> ToolInvocation:
    cancellation_point(ctx)
    spec = registry.get(tool_name)
    require(ctx.principal, spec.capability, resource=tool_name)
    if ctx.run_id is not None and ctx.run_id != run_id:
        raise ToolConflict("invocation run does not match request context")
    if not 1 <= len(idempotency_key) <= 160:
        raise ValueError("idempotency_key must contain 1-160 characters")

    validated = spec.validate_arguments(arguments) if spec.validate_arguments else arguments
    encoded_arguments = _canonical(validated)
    settings = get_settings()
    if len(encoded_arguments) > settings.tool_max_arguments_bytes:
        raise ValueError("tool arguments exceed tool_max_arguments_bytes")
    arguments_sha = hashlib.sha256(encoded_arguments).hexdigest()
    # Keep the database key bounded and preserve the important semantic that a
    # caller cannot reuse one idempotency key for a different tool. The exact
    # tool and argument hash are checked against the receipt on replay.
    durable_key = f"invoke:v1:{hashlib.sha256(idempotency_key.encode()).hexdigest()}"

    existing = _existing(ctx, durable_key)
    if existing is not None:
        return _resolve_existing_with_metric(existing, spec, arguments_sha)

    if spec.requires_approval and approval_id is None:
        approval_id = _request_approval(
            ctx,
            spec=spec,
            run_id=run_id,
            arguments=validated,
            arguments_sha=arguments_sha,
        )
        get_checkpoint_store().append(
            ctx,
            f"run:{run_id}",
            {
                "phase": "awaiting_tool_approval",
                "tool_name": tool_name,
                "arguments_sha256": arguments_sha,
                "approval_id": str(approval_id),
            },
            awaiting=f"tool_approval:{approval_id}",
        )
        record_tool_execution(tool_name, spec.side_effect, "awaiting_approval")
        raise ApprovalRequired(approval_id)

    execution_id, raced = _start_execution(
        ctx,
        spec=spec,
        run_id=run_id,
        approval_id=approval_id,
        arguments_sha=arguments_sha,
        idempotency_key=durable_key,
    )
    if raced is not None:
        return _resolve_existing_with_metric(raced, spec, arguments_sha)

    timeout = spec.timeout_seconds or settings.tool_default_timeout_seconds
    execution_started = time.monotonic()
    try:
        with start_span(
            "gen_ai.tool.execute",
            attributes={
                "gen_ai.tool.name": tool_name,
                "platform.tool.side_effect": spec.side_effect,
                "platform.tool.arguments_sha256": arguments_sha,
                "platform.tool.execution_id": str(execution_id),
                "platform.run.id": str(run_id),
            },
        ) as span:
            bind_request_context(ctx)
            cancellation_point(ctx)
            executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"tool-{tool_name}")
            future = executor.submit(spec.handler, ctx, validated)
            try:
                result = future.result(timeout=timeout)
            finally:
                executor.shutdown(wait=False, cancel_futures=True)
            if not isinstance(result, dict):
                raise TypeError("tool handlers must return a JSON object")
            span.set_attribute("platform.outcome", "succeeded")
    except FutureTimeout as exc:
        status = "needs_reconciliation" if spec.side_effect == "write" else "failed"
        _finish_failure(ctx, execution_id, status, "tool timeout")
        record_tool_execution(
            tool_name,
            spec.side_effect,
            status,
            duration_ms=(time.monotonic() - execution_started) * 1000,
        )
        audit.record(
            ctx,
            action="tool.execute",
            outcome=audit.Outcome.FAILED,
            resource_type="tool_execution",
            resource_id=str(execution_id),
            detail={"tool": tool_name, "status": status, "reason": "timeout"},
        )
        raise ToolExecutionUnavailable(f"tool {tool_name!r} timed out") from exc
    except Exception as exc:
        status = "needs_reconciliation" if spec.side_effect == "write" else "failed"
        _finish_failure(ctx, execution_id, status, f"{type(exc).__name__}: {exc}")
        record_tool_execution(
            tool_name,
            spec.side_effect,
            status,
            duration_ms=(time.monotonic() - execution_started) * 1000,
        )
        audit.record(
            ctx,
            action="tool.execute",
            outcome=audit.Outcome.FAILED,
            resource_type="tool_execution",
            resource_id=str(execution_id),
            detail={"tool": tool_name, "status": status, "error_type": type(exc).__name__},
        )
        raise ToolExecutionUnavailable(f"tool {tool_name!r} failed") from exc

    encoded_result = _canonical(result)
    if len(encoded_result) > settings.checkpoint_max_state_bytes:
        _finish_failure(ctx, execution_id, "failed", "tool result exceeds retained result limit")
        record_tool_execution(
            tool_name,
            spec.side_effect,
            "failed",
            duration_ms=(time.monotonic() - execution_started) * 1000,
        )
        raise ToolExecutionUnavailable("tool result exceeds retained result limit")
    result_sha = hashlib.sha256(encoded_result).hexdigest()
    try:
        with tenant_session(ctx.tenant) as session:
            updated = session.execute(
                text(
                    "UPDATE tool_execution SET status = 'succeeded', "
                    "result = CAST(:result AS jsonb), result_sha256 = :sha, "
                    "finished_at = now() WHERE id = :id AND status = 'started' "
                    "RETURNING id"
                ),
                {
                    "result": encoded_result.decode("utf-8"),
                    "sha": result_sha,
                    "id": execution_id,
                },
            ).scalar_one_or_none()
            if updated is None:
                raise ToolConflict("tool execution receipt was no longer started")
    except Exception as exc:
        # The external effect may have happened but its durable result could not
        # be committed. Leaving the receipt at ``started`` forces reconciliation
        # and, critically, refuses an automatic replay of a possible write.
        record_tool_execution(
            tool_name,
            spec.side_effect,
            "needs_reconciliation",
            duration_ms=(time.monotonic() - execution_started) * 1000,
        )
        raise ToolExecutionUnavailable(
            f"tool {tool_name!r} completed but its result could not be committed"
        ) from exc

    record_tool_execution(
        tool_name,
        spec.side_effect,
        "succeeded",
        duration_ms=(time.monotonic() - execution_started) * 1000,
    )

    get_checkpoint_store().append(
        ctx,
        f"run:{run_id}",
        {
            "phase": "tool_succeeded",
            "tool_name": tool_name,
            "execution_id": str(execution_id),
            "result_sha256": result_sha,
        },
    )
    audit.record(
        ctx,
        action="tool.execute",
        outcome=audit.Outcome.SUCCEEDED,
        resource_type="tool_execution",
        resource_id=str(execution_id),
        detail={"tool": tool_name, "result_sha256": result_sha},
    )
    return ToolInvocation(execution_id, tool_name, "succeeded", result)


def _request_approval(
    ctx: RequestContext,
    *,
    spec: ToolSpec,
    run_id: uuid.UUID,
    arguments: dict[str, Any],
    arguments_sha: str,
) -> uuid.UUID:
    settings = get_settings()
    with tenant_session(ctx.tenant) as session:
        run_exists = session.execute(
            text("SELECT 1 FROM run WHERE id = :id"), {"id": run_id}
        ).scalar_one_or_none()
        if run_exists is None:
            raise ToolConflict("run not found")
        existing = session.execute(
            text(
                "SELECT id FROM tool_approval WHERE run_id = :run AND tool_name = :tool "
                "AND arguments_sha256 = :sha AND requested_by = :by "
                "AND status IN ('pending','approved') AND consumed_at IS NULL "
                "AND expires_at > now() ORDER BY requested_at DESC LIMIT 1"
            ),
            {"run": run_id, "tool": spec.name, "sha": arguments_sha, "by": ctx.principal.id},
        ).scalar_one_or_none()
        if existing is not None:
            return existing
        approval_id = session.execute(
            text(
                "INSERT INTO tool_approval "
                "(tenant_id, run_id, tool_name, side_effect, arguments, arguments_sha256, "
                " requested_by, expires_at) "
                "VALUES (:tenant, :run, :tool, :effect, CAST(:arguments AS jsonb), :sha, "
                " :by, now() + :ttl) RETURNING id"
            ),
            {
                "tenant": ctx.tenant.id,
                "run": run_id,
                "tool": spec.name,
                "effect": spec.side_effect,
                "arguments": _canonical(arguments).decode("utf-8"),
                "sha": arguments_sha,
                "by": ctx.principal.id,
                "ttl": timedelta(seconds=settings.tool_approval_ttl_seconds),
            },
        ).scalar_one()
    audit.record(
        ctx,
        action="tool.approval.request",
        outcome=audit.Outcome.SUCCEEDED,
        resource_type="tool_approval",
        resource_id=str(approval_id),
        detail={"tool": spec.name, "arguments_sha256": arguments_sha},
    )
    return approval_id


def _start_execution(
    ctx: RequestContext,
    *,
    spec: ToolSpec,
    run_id: uuid.UUID,
    approval_id: uuid.UUID | None,
    arguments_sha: str,
    idempotency_key: str,
):
    with tenant_session(ctx.tenant) as session:
        execution_id = session.execute(
            text(
                "INSERT INTO tool_execution "
                "(tenant_id, run_id, approval_id, tool_name, side_effect, arguments_sha256, "
                " idempotency_key, requested_by) "
                "SELECT :tenant, r.id, :approval, :tool, :effect, :sha, :key, :by "
                "FROM run r WHERE r.id = :run "
                "ON CONFLICT (tenant_id, idempotency_key) DO NOTHING RETURNING id"
            ),
            {
                "tenant": ctx.tenant.id,
                "run": run_id,
                "approval": approval_id,
                "tool": spec.name,
                "effect": spec.side_effect,
                "sha": arguments_sha,
                "key": idempotency_key,
                "by": ctx.principal.id,
            },
        ).scalar_one_or_none()
        if execution_id is None:
            raced = session.execute(
                text(
                    "SELECT id, tool_name, arguments_sha256, status, result "
                    "FROM tool_execution WHERE idempotency_key = :key"
                ),
                {"key": idempotency_key},
            ).one_or_none()
            if raced is None:
                raise ToolConflict("run not found")
            return None, raced

        if spec.requires_approval:
            consumed = session.execute(
                text(
                    "UPDATE tool_approval SET consumed_at = now() "
                    "WHERE id = :approval AND run_id = :run AND tool_name = :tool "
                    "AND arguments_sha256 = :sha AND status = 'approved' "
                    "AND consumed_at IS NULL AND expires_at > now() RETURNING id"
                ),
                {
                    "approval": approval_id,
                    "run": run_id,
                    "tool": spec.name,
                    "sha": arguments_sha,
                },
            ).scalar_one_or_none()
            if consumed is None:
                raise ApprovalInvalid(
                    "approval is absent, expired, consumed, or argument-mismatched"
                )
        return execution_id, None


def _existing(ctx: RequestContext, idempotency_key: str):
    with tenant_session(ctx.tenant) as session:
        return session.execute(
            text(
                "SELECT id, tool_name, arguments_sha256, status, result "
                "FROM tool_execution WHERE idempotency_key = :key"
            ),
            {"key": idempotency_key},
        ).one_or_none()


def _resolve_existing(row, tool_name: str, arguments_sha: str) -> ToolInvocation:
    if row.tool_name != tool_name or row.arguments_sha256 != arguments_sha:
        raise ToolConflict("idempotency key was already used for another tool invocation")
    if row.status == "succeeded":
        return ToolInvocation(row.id, tool_name, row.status, row.result, replayed=True)
    raise ToolExecutionUnavailable(
        f"existing execution {row.id} is {row.status}; automatic replay is refused"
    )


def _resolve_existing_with_metric(
    row, spec: ToolSpec, arguments_sha: str
) -> ToolInvocation:
    try:
        invocation = _resolve_existing(row, spec.name, arguments_sha)
    except ToolConflict:
        record_tool_execution(spec.name, spec.side_effect, "conflict")
        raise
    except ToolExecutionUnavailable:
        record_tool_execution(spec.name, spec.side_effect, str(row.status))
        raise
    record_tool_execution(spec.name, spec.side_effect, "replayed")
    return invocation


def _finish_failure(
    ctx: RequestContext, execution_id: uuid.UUID, status: str, error: str
) -> None:
    with tenant_session(ctx.tenant) as session:
        session.execute(
            text(
                "UPDATE tool_execution SET status = :status, error = :error, finished_at = now() "
                "WHERE id = :id AND status = 'started'"
            ),
            {"status": status, "error": error[:2000], "id": execution_id},
        )


def _canonical(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("tool values must be finite JSON") from exc


def _run_status(ctx: RequestContext, arguments: dict[str, Any]) -> dict[str, Any]:
    try:
        run_id = uuid.UUID(str(arguments["run_id"]))
    except (KeyError, ValueError) as exc:
        raise ValueError("run_id must be a UUID") from exc
    with tenant_session(ctx.tenant) as session:
        row = session.execute(
            text("SELECT id, workload, status, attempt, max_attempts FROM run WHERE id = :id"),
            {"id": run_id},
        ).one_or_none()
    if row is None:
        raise LookupError("run not found")
    return {
        "run_id": str(row.id),
        "workload": row.workload,
        "status": row.status,
        "attempt": row.attempt,
        "max_attempts": row.max_attempts,
    }


registry.register(
    ToolSpec(
        name="platform.run_status",
        description="Read the durable status of a run in the current tenant.",
        side_effect="none",
        capability=Capability.TOOL_INVOKE_READONLY,
        handler=_run_status,
    )
)
