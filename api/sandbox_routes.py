"""
Sandbox orchestrator routes backed by Docker.
"""

from __future__ import annotations

import asyncio
from typing import Any, Literal

import requests
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from core.services.sandbox_manager import SandboxManager, SandboxManagerError


def get_sandbox_manager() -> SandboxManager:
    from main import app

    manager = getattr(app.state, "sandbox_manager", None)
    if manager is None:
        raise HTTPException(status_code=503, detail="sandbox manager unavailable")
    return manager


class SandboxCreateRequest(BaseModel):
    sandbox_id: str | None = Field(default=None, description="Container name. Auto-generated if omitted.")
    image: str | None = Field(default=None, description="Sandbox image. Defaults to manager configured image.")
    start_url: str = Field(default="https://www.baidu.com/")
    display_width: int = Field(default=1920, ge=320, le=8192)
    display_height: int = Field(default=1080, ge=240, le=8192)
    user_id: int = Field(default=1000, ge=0)
    group_id: int = Field(default=1000, ge=0)
    keep_app_running: bool = True

    novnc_port: int | None = Field(default=None, ge=1, le=65535, description="Host port for container 5800.")
    vnc_port: int | None = Field(default=None, ge=1, le=65535, description="Host port for container 5900.")
    api_port: int | None = Field(default=None, ge=1, le=65535, description="Host port for container 8899.")

    env: dict[str, str] | None = Field(default=None, description="Extra env vars.")
    restart_policy: Literal["no", "unless-stopped", "always"] = "unless-stopped"

    wait_ready: bool = True
    ready_timeout_sec: int = Field(default=30, ge=1, le=300)
    public_host: str | None = Field(default=None, description="Override host/IP used for returned URLs.")


router = APIRouter(prefix="/sandboxes", tags=["Sandboxes"])


def _select_running_sandbox(manager: SandboxManager) -> dict[str, Any] | None:
    items = manager.list(include_exited=False)
    running = [item for item in items if item.get("status") == "running"]
    if not running:
        return None
    running.sort(key=lambda item: str(item.get("created") or ""), reverse=True)
    return running[0]


def _select_running_sandbox_id(manager: SandboxManager) -> str:
    target = _select_running_sandbox(manager)
    if target is None:
        raise SandboxManagerError("No running sandbox found", status_code=404)
    sandbox_id = str(target.get("sandbox_id") or "").strip()
    if not sandbox_id:
        raise SandboxManagerError(
            "Running sandbox payload is missing sandbox_id",
            status_code=500,
        )
    return sandbox_id


def _reset_sandbox_browser(manager: SandboxManager, sandbox_id: str) -> dict[str, Any]:
    payload = manager.get(sandbox_id)
    if payload.get("status") != "running":
        raise SandboxManagerError(f"Sandbox not running: {sandbox_id}", status_code=409)

    api_port = payload.get("ports", {}).get("api")
    if not api_port:
        raise SandboxManagerError(
            f"Sandbox API port unavailable: {sandbox_id}",
            status_code=503,
        )

    url = f"http://{manager.healthcheck_host}:{int(api_port)}/browser/reset"
    try:
        response = requests.post(url, json={}, timeout=30)
    except Exception as exc:
        raise SandboxManagerError(
            f"Failed to reset sandbox browser: {exc}",
            status_code=502,
        ) from exc

    try:
        data = response.json()
    except Exception:
        data = {"raw": response.text}

    if response.status_code >= 400:
        raise SandboxManagerError(
            f"Sandbox browser reset failed: {data}",
            status_code=response.status_code,
        )
    if isinstance(data, dict) and str(data.get("output") or "").startswith("ErrorInfo:\n"):
        raise SandboxManagerError(
            f"Sandbox browser reset failed: {data['output']}",
            status_code=502,
        )

    return {
        "status": "ok",
        "sandbox_id": sandbox_id,
        "result": data,
    }


@router.post("", status_code=201)
async def create_sandbox(
    payload: SandboxCreateRequest,
    request: Request,
    manager: SandboxManager = Depends(get_sandbox_manager),
) -> dict[str, Any]:
    public_host = payload.public_host
    if not public_host:
        host_header = request.headers.get("host") or ""
        public_host = host_header.split(":")[0] if host_header else None
    try:
        result = await asyncio.to_thread(
            manager.create,
            sandbox_id=payload.sandbox_id,
            image=payload.image,
            start_url=payload.start_url,
            display_width=payload.display_width,
            display_height=payload.display_height,
            user_id=payload.user_id,
            group_id=payload.group_id,
            keep_app_running=payload.keep_app_running,
            novnc_port=payload.novnc_port,
            vnc_port=payload.vnc_port,
            api_port=payload.api_port,
            extra_env=payload.env,
            restart_policy=payload.restart_policy,
            wait_ready=payload.wait_ready,
            ready_timeout_sec=payload.ready_timeout_sec,
            public_host=public_host,
        )
        return result
    except SandboxManagerError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


@router.get("")
async def list_sandboxes(
    include_exited: bool = True,
    manager: SandboxManager = Depends(get_sandbox_manager),
) -> dict[str, Any]:
    try:
        items = await asyncio.to_thread(manager.list, include_exited=include_exited)
        return {"count": len(items), "items": items}
    except SandboxManagerError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


@router.get("/health")
async def sandbox_health(
    sandbox_id: str | None = Query(default=None),
    timeout_sec: int = Query(default=3, ge=1, le=30),
    manager: SandboxManager = Depends(get_sandbox_manager),
) -> dict[str, Any]:
    try:
        resolved_sandbox_id = sandbox_id.strip() if sandbox_id else ""
        if not resolved_sandbox_id:
            resolved_sandbox_id = await asyncio.to_thread(_select_running_sandbox_id, manager)
        return await asyncio.to_thread(
            manager.health,
            resolved_sandbox_id,
            timeout_sec=timeout_sec,
        )
    except SandboxManagerError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


@router.post("/browser/reset")
async def reset_running_sandbox_browser(
    manager: SandboxManager = Depends(get_sandbox_manager),
) -> dict[str, Any]:
    try:
        target = await asyncio.to_thread(_select_running_sandbox, manager)
        if target is None:
            return {
                "status": "noop",
                "sandbox_id": None,
                "reason": "no_running_sandbox",
            }
        sandbox_id = str(target.get("sandbox_id") or "").strip()
        if not sandbox_id:
            return {
                "status": "noop",
                "sandbox_id": None,
                "reason": "invalid_sandbox_payload_missing_sandbox_id",
            }
        return await asyncio.to_thread(_reset_sandbox_browser, manager, sandbox_id)
    except SandboxManagerError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


@router.get("/{sandbox_id}")
async def get_sandbox(
    sandbox_id: str,
    manager: SandboxManager = Depends(get_sandbox_manager),
) -> dict[str, Any]:
    try:
        return await asyncio.to_thread(manager.get, sandbox_id)
    except SandboxManagerError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


@router.post("/{sandbox_id}/start")
async def start_sandbox(
    sandbox_id: str,
    manager: SandboxManager = Depends(get_sandbox_manager),
) -> dict[str, Any]:
    try:
        return await asyncio.to_thread(manager.start, sandbox_id)
    except SandboxManagerError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


@router.post("/{sandbox_id}/stop")
async def stop_sandbox(
    sandbox_id: str,
    timeout_sec: int = Query(default=10, ge=1, le=300),
    manager: SandboxManager = Depends(get_sandbox_manager),
) -> dict[str, Any]:
    try:
        return await asyncio.to_thread(manager.stop, sandbox_id, timeout_sec=timeout_sec)
    except SandboxManagerError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


@router.delete("/{sandbox_id}")
async def delete_sandbox(
    sandbox_id: str,
    force: bool = Query(default=True),
    manager: SandboxManager = Depends(get_sandbox_manager),
) -> dict[str, Any]:
    try:
        return await asyncio.to_thread(
            manager.delete,
            sandbox_id,
            force=force,
        )
    except SandboxManagerError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


@router.get("/{sandbox_id}/logs")
async def sandbox_logs(
    sandbox_id: str,
    tail: int = Query(default=200, ge=1, le=5000),
    manager: SandboxManager = Depends(get_sandbox_manager),
) -> dict[str, Any]:
    try:
        return await asyncio.to_thread(manager.logs, sandbox_id, tail=tail)
    except SandboxManagerError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

