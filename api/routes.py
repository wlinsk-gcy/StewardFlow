"""
FastAPI route definitions for agent endpoints.
"""

import json
import logging
import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from core.protocol import AgentStatus, NodeType, RunAgentRequest, RunAgentResponse
from core.registry_summary import build_registry_summary
from core.services.task_service import QueueRejectedError, TaskService
from core.storage.checkpoint import CheckpointStore

logger = logging.getLogger(__name__)


def get_task_service() -> TaskService:
    from main import app

    return app.state.task_service


def get_cache_manager() -> Any:
    service = get_task_service()
    return service.cache_manager


def get_checkpoint() -> CheckpointStore:
    from main import app

    return app.state.checkpoint


def get_tool_registry() -> Any:
    from main import app

    return app.state.tool_registry


def get_mcp_client() -> Any:
    from main import app

    return app.state.mcp_client


router = APIRouter(prefix="/agent", tags=["Agent"])


@router.post("/run", response_model=RunAgentResponse, status_code=201)
async def run_agent(
    request: RunAgentRequest,
    task_service: TaskService = Depends(get_task_service),
):
    try:
        if request.trace_id:
            trace = await task_service.get_trace(request.trace_id)
            if not trace:
                raise HTTPException(status_code=404, detail="Trace not found")
            if trace.status == AgentStatus.WAITING and trace.node == NodeType.HITL:
                admission = await task_service.dispatch_hitl(trace, request.task)
                return RunAgentResponse(
                    trace_id=request.trace_id,
                    status="accepted",
                    message=f"queued wait_ms={admission.wait_ms} queue_length={admission.queue_length}",
                )
            if trace.status in {AgentStatus.DONE, AgentStatus.FAILED} and trace.node == NodeType.END:
                await task_service.new_turn(trace, request.task)
                admission = await task_service.dispatch_start(trace)
                return RunAgentResponse(
                    trace_id=request.trace_id,
                    status="accepted",
                    message=f"queued wait_ms={admission.wait_ms} queue_length={admission.queue_length}",
                )
            raise HTTPException(status_code=404, detail="Trace status is invalid")

        trace = await task_service.initialize(request.task, request.client_id)
        admission = await task_service.dispatch_start(trace)
        return RunAgentResponse(
            trace_id=trace.trace_id,
            status="accepted",
            message=f"queued wait_ms={admission.wait_ms} queue_length={admission.queue_length}",
        )
    except QueueRejectedError as exc:
        trace_id = request.trace_id or "-"
        raise HTTPException(
            status_code=429,
            detail={
                "reason": exc.reason,
                "queue_length": exc.queue_length,
                "wait_ms": exc.wait_ms,
            },
        ) from exc


@router.get("/health")
async def health_check() -> dict[str, str]:
    return {"status": "healthy", "service": "agent"}


@router.get("/registry-summary")
async def registry_summary(
    tool_registry: Any = Depends(get_tool_registry),
    mcp_client: Any = Depends(get_mcp_client),
) -> dict[str, Any]:
    try:
        return await build_registry_summary(tool_registry, mcp_client)
    except Exception as exc:
        logger.exception("Failed to build registry summary: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to load registry summary") from exc
