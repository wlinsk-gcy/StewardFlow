"""
FastAPI route definitions for agent endpoints.
"""

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from core.protocol import AgentStatus, NodeType, RunAgentRequest, RunAgentResponse
from core.registry_summary import build_registry_summary
from core.services.task_service import TaskService
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
    def callback(task: asyncio.Task) -> None:
        try:
            exc = task.exception()
            if exc:
                logger.error("Captured exception: %s: %s", type(exc).__name__, exc)
        except asyncio.CancelledError:
            logger.warning("run_agent task was cancelled")

    if request.trace_id:
        trace = await task_service.get_trace(request.trace_id)
        if not trace:
            raise HTTPException(status_code=404, detail="Trace not found")
        if trace.status == AgentStatus.WAITING and trace.node == NodeType.HITL:
            hitl_task = asyncio.create_task(task_service.submit_hitl(trace, request.task))
            hitl_task.add_done_callback(callback)
            return RunAgentResponse(trace_id=request.trace_id)
        if trace.status == AgentStatus.DONE and trace.node == NodeType.END:
            await task_service.new_turn(trace, request.task)
            task = asyncio.create_task(task_service.start(trace))
            task.add_done_callback(callback)
            return RunAgentResponse(trace_id=request.trace_id)
        raise HTTPException(status_code=404, detail="Trace status is invalid")

    trace = await task_service.initialize(request.task, request.client_id)
    task = asyncio.create_task(task_service.start(trace))
    task.add_done_callback(callback)

    return RunAgentResponse(trace_id=trace.trace_id)


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


@router.get("/context")
async def context_report(
    trace_id: str = Query(..., min_length=1),
    cache_manager: Any = Depends(get_cache_manager),
) -> dict[str, Any]:
    report = await cache_manager.context_report(trace_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Context not found")
    return report


@router.get("/context/events")
async def context_events(
    trace_id: str = Query(..., min_length=1),
    limit: int = Query(default=200, ge=1, le=5000),
) -> dict[str, Any]:
    root = Path(os.getenv("STEWARDFLOW_AUDIT_ROOT", "data/audit")).resolve()
    events_path = (root / trace_id / "events.jsonl").resolve()
    try:
        events_path.relative_to(root)
    except Exception:
        raise HTTPException(status_code=400, detail="invalid trace_id")

    if not events_path.exists():
        return {
            "trace_id": trace_id,
            "count": 0,
            "items": [],
            "path": str(events_path),
        }

    items: list[dict[str, Any]] = []
    try:
        with events_path.open("r", encoding="utf-8") as fp:
            for line in fp:
                raw = line.strip()
                if not raw:
                    continue
                try:
                    obj = json.loads(raw)
                except Exception:
                    obj = {"raw": raw}
                items.append(obj)
    except Exception as exc:
        logger.exception("Failed to read audit events for trace=%s: %s", trace_id, exc)
        raise HTTPException(status_code=500, detail="Failed to read context events") from exc

    return {
        "trace_id": trace_id,
        "count": len(items),
        "items": items[-limit:],
        "path": str(events_path),
    }
