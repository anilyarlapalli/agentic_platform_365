"""HTTP exposure for the governed tool runtime."""

from __future__ import annotations

import uuid
from dataclasses import replace
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from platform_core.agent.tools import (
    ApprovalInvalid,
    ApprovalRequired,
    ToolConflict,
    ToolExecutionUnavailable,
    UnknownTool,
    invoke,
    registry,
)
from platform_core.api.deps import get_context
from platform_core.correctness.cancellation import RunCancelled
from platform_core.identity.capabilities import NotAuthorized
from platform_core.identity.principal import RequestContext

router = APIRouter(prefix="/api", tags=["tools"])


class InvocationRequest(BaseModel):
    run_id: uuid.UUID
    arguments: dict[str, Any] = Field(default_factory=dict)
    approval_id: uuid.UUID | None = None


@router.get("/tools")
def list_tools(_ctx: Annotated[RequestContext, Depends(get_context)]) -> dict:
    return {"tools": registry.describe()}


@router.post("/tools/{tool_name}/invoke")
def invoke_tool(
    tool_name: str,
    payload: InvocationRequest,
    request: Request,
    ctx: Annotated[RequestContext, Depends(get_context)],
):
    idempotency_key = request.headers.get("idempotency-key")
    if not idempotency_key:
        raise HTTPException(status_code=400, detail="Idempotency-Key header is required.")
    run_ctx = replace(
        ctx,
        run_id=payload.run_id,
        idempotency_key=idempotency_key,
        labels={**ctx.labels, "task": "tool", "tool": tool_name},
    )
    try:
        result = invoke(
            run_ctx,
            tool_name=tool_name,
            arguments=payload.arguments,
            run_id=payload.run_id,
            idempotency_key=idempotency_key,
            approval_id=payload.approval_id,
        )
    except UnknownTool:
        raise HTTPException(status_code=404, detail="Tool not found.") from None
    except NotAuthorized as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from None
    except ApprovalRequired as exc:
        return JSONResponse(
            status_code=202,
            content={
                "status": "awaiting_approval",
                "approval_id": str(exc.approval_id),
                "tool_name": tool_name,
            },
        )
    except (ApprovalInvalid, ToolConflict) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except RunCancelled as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except ToolExecutionUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None

    return {
        "execution_id": str(result.execution_id),
        "tool_name": result.tool_name,
        "status": result.status,
        "result": result.result,
        "replayed": result.replayed,
    }
