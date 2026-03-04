from __future__ import annotations

import asyncio
import json
from typing import Any

from core.tools.tool import Tool, ToolRegistry

from .client import DockerSandboxClient


def _tool_schema(
    *,
    name: str,
    description: str,
    properties: dict[str, Any],
    required: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required or [],
                "additionalProperties": False,
            },
            "strict": True,
        },
    }


def _json_any_schema() -> dict[str, Any]:
    return {
        "anyOf": [
            {"type": "string"},
            {"type": "number"},
            {"type": "integer"},
            {"type": "boolean"},
            {"type": "object"},
            {"type": "array"},
            {"type": "null"},
        ]
    }


class _SandboxTool(Tool):
    def __init__(self, *, client: DockerSandboxClient, name: str, description: str) -> None:
        super().__init__()
        self.client = client
        self.name = name
        self.description = description

    async def _invoke(self, fn, **kwargs) -> str:
        try:
            payload = await asyncio.to_thread(fn, **kwargs)
            if isinstance(payload, dict):
                return json.dumps(payload, ensure_ascii=False)
            return json.dumps({"ok": True, "result": payload}, ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)


class ExecTool(_SandboxTool):
    def __init__(self, client: DockerSandboxClient) -> None:
        super().__init__(
            client=client,
            name="exec",
            description="Execute a shell command inside the managed docker sandbox via /tools/exec.",
        )
        self.requires_confirmation = True

    async def execute(
        self,
        command: str,
        cwd: str | None = None,
        timeout_ms: int = 120000,
        env: dict[str, str] | None = None,
        preview_bytes: int = 4096,
        capture_limit_bytes: int = 50 * 1024 * 1024,
        shell_executable: str | None = None,
        **kwargs,
    ) -> str:
        del kwargs
        payload = {
            "command": command,
            "cwd": cwd,
            "timeout_ms": max(1, min(int(timeout_ms), 3600000)),
            "env": env,
            "preview_bytes": max(128, min(int(preview_bytes), 65536)),
            "capture_limit_bytes": max(1024, min(int(capture_limit_bytes), 512 * 1024 * 1024)),
            "shell_executable": shell_executable,
        }
        return await self._invoke(
            self.client.api_post,
            sandbox_id=None,
            path="/tools/exec",
            payload=payload,
            timeout_sec=max(5, min(int(timeout_ms / 1000) + 5, 600)),
        )

    def schema(self) -> dict[str, Any]:
        return _tool_schema(
            name=self.name,
            description=self.description,
            properties={
                "command": {"type": "string", "description": "Shell command to run inside sandbox."},
                "cwd": {"type": "string"},
                "timeout_ms": {"type": "integer", "default": 120000, "minimum": 1, "maximum": 3600000},
                "env": {"type": "object", "additionalProperties": {"type": "string"}},
                "preview_bytes": {"type": "integer", "default": 4096, "minimum": 128, "maximum": 65536},
                "capture_limit_bytes": {
                    "type": "integer",
                    "default": 52428800,
                    "minimum": 1024,
                    "maximum": 536870912,
                },
                "shell_executable": {"type": "string"},
            },
            required=["command"],
        )


class ExecMetaTool(_SandboxTool):
    def __init__(self, client: DockerSandboxClient) -> None:
        super().__init__(
            client=client,
            name="exec_meta",
            description="Read metadata of a previous exec run by run_id.",
        )

    async def execute(self, run_id: str, **kwargs) -> str:
        del kwargs
        return await self._invoke(
            self.client.api_get,
            sandbox_id=None,
            path=f"/tools/exec/{run_id}",
            timeout_sec=30,
        )

    def schema(self) -> dict[str, Any]:
        return _tool_schema(
            name=self.name,
            description=self.description,
            properties={
                "run_id": {"type": "string", "description": "Run id returned by exec."},
            },
            required=["run_id"],
        )


class BrowserPostTool(_SandboxTool):
    def __init__(
        self,
        *,
        client: DockerSandboxClient,
        name: str,
        description: str,
        path: str,
        properties: dict[str, Any],
        required: list[str],
    ) -> None:
        super().__init__(client=client, name=name, description=description)
        self._path = path
        self._properties = properties
        self._required = required

    async def execute(self, **kwargs) -> str:
        payload = dict(kwargs)
        timeout_sec = 60
        timeout_ms = payload.get("timeout_ms")
        if isinstance(timeout_ms, int):
            timeout_sec = max(5, min(int(timeout_ms / 1000) + 5, 300))
        return await self._invoke(
            self.client.api_post,
            sandbox_id=None,
            path=self._path,
            payload=payload,
            timeout_sec=timeout_sec,
        )

    def schema(self) -> dict[str, Any]:
        return _tool_schema(
            name=self.name,
            description=self.description,
            properties=self._properties,
            required=list(self._required),
        )


class SandboxToolRuntime:
    def __init__(self, client: DockerSandboxClient) -> None:
        self._client = client

    def set_sandbox_id(self, sandbox_id: str | None) -> None:
        self._client.set_sandbox_id(sandbox_id)

    async def shutdown(self) -> None:
        await asyncio.to_thread(self._client.close)


def _register_browser_tool(
    registry: ToolRegistry,
    client: DockerSandboxClient,
    *,
    name: str,
    description: str,
    path: str,
    properties: dict[str, Any],
    required: list[str],
) -> None:
    registry.register(
        BrowserPostTool(
            client=client,
            name=name,
            description=description,
            path=path,
            properties=properties,
            required=required,
        )
    )


def register_sandbox_tools(registry: ToolRegistry, sandbox_cfg: dict[str, Any]) -> SandboxToolRuntime:
    client = DockerSandboxClient(
        default_image=str(sandbox_cfg.get("image", "gui-sandbox:dev")),
        docker_base_url=sandbox_cfg.get("docker_base_url"),
        public_host=sandbox_cfg.get("public_host"),
        healthcheck_host=str(sandbox_cfg.get("healthcheck_host", "127.0.0.1")),
    )

    registry.register(ExecTool(client))
    registry.register(ExecMetaTool(client))

    _register_browser_tool(
        registry,
        client,
        name="browser_click",
        description="Click an element in the sandbox browser.",
        path="/browser/click",
        properties={
            "uid": {"type": "string"},
            "selector": {"type": "string"},
            "button": {"type": "string", "enum": ["left", "right", "middle"], "default": "left"},
            "click_count": {"type": "integer", "default": 1, "minimum": 1, "maximum": 10},
            "timeout_ms": {"type": "integer", "default": 30000, "minimum": 1, "maximum": 300000},
        },
        required=[],
    )
    _register_browser_tool(
        registry,
        client,
        name="browser_close",
        description="Disconnect current CDP browser session (does not kill GUI Chrome process).",
        path="/browser/close",
        properties={},
        required=[],
    )
    _register_browser_tool(
        registry,
        client,
        name="browser_drag",
        description="Drag from one element to another.",
        path="/browser/drag",
        properties={
            "from_uid": {"type": "string"},
            "from_selector": {"type": "string"},
            "to_uid": {"type": "string"},
            "to_selector": {"type": "string"},
            "timeout_ms": {"type": "integer", "default": 30000, "minimum": 1, "maximum": 300000},
        },
        required=[],
    )
    _register_browser_tool(
        registry,
        client,
        name="browser_evaluate",
        description="Evaluate JavaScript expression in current page context.",
        path="/browser/evaluate",
        properties={
            "expression": {"type": "string"},
            "arg": _json_any_schema(),
        },
        required=["expression"],
    )
    _register_browser_tool(
        registry,
        client,
        name="browser_file_upload",
        description="Set files for a file input element (files are paths inside sandbox).",
        path="/browser/file_upload",
        properties={
            "uid": {"type": "string"},
            "selector": {"type": "string"},
            "files": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
            },
            "timeout_ms": {"type": "integer", "default": 30000, "minimum": 1, "maximum": 300000},
        },
        required=["files"],
    )
    _register_browser_tool(
        registry,
        client,
        name="browser_fill_form",
        description="Fill multiple form fields in one call.",
        path="/browser/fill_form",
        properties={
            "fields": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "properties": {
                        "uid": {"type": "string"},
                        "selector": {"type": "string"},
                        "value": {"type": "string"},
                    },
                    "required": ["value"],
                    "additionalProperties": False,
                },
            },
            "submit": {"type": "boolean", "default": False},
            "timeout_ms": {"type": "integer", "default": 30000, "minimum": 1, "maximum": 300000},
        },
        required=["fields"],
    )
    _register_browser_tool(
        registry,
        client,
        name="browser_handle_dialog",
        description="Handle next browser dialog by accepting or dismissing it.",
        path="/browser/handle_dialog",
        properties={
            "action": {"type": "string", "enum": ["accept", "dismiss"], "default": "accept"},
            "prompt_text": {"type": "string"},
            "timeout_ms": {"type": "integer", "default": 10000, "minimum": 1, "maximum": 300000},
        },
        required=[],
    )
    _register_browser_tool(
        registry,
        client,
        name="browser_hover",
        description="Hover over an element.",
        path="/browser/hover",
        properties={
            "uid": {"type": "string"},
            "selector": {"type": "string"},
            "timeout_ms": {"type": "integer", "default": 30000, "minimum": 1, "maximum": 300000},
        },
        required=[],
    )
    _register_browser_tool(
        registry,
        client,
        name="browser_navigate",
        description="Navigate current tab to URL.",
        path="/browser/navigate",
        properties={
            "url": {"type": "string"},
            "timeout_ms": {"type": "integer", "default": 30000, "minimum": 1, "maximum": 300000},
            "wait_until": {
                "type": "string",
                "default": "domcontentloaded",
                "enum": ["load", "domcontentloaded", "networkidle", "commit"],
            },
        },
        required=["url"],
    )
    _register_browser_tool(
        registry,
        client,
        name="browser_navigate_back",
        description="Navigate back in current tab history.",
        path="/browser/navigate_back",
        properties={
            "timeout_ms": {"type": "integer", "default": 30000, "minimum": 1, "maximum": 300000},
            "wait_until": {
                "type": "string",
                "default": "domcontentloaded",
                "enum": ["load", "domcontentloaded", "networkidle", "commit"],
            },
        },
        required=[],
    )
    _register_browser_tool(
        registry,
        client,
        name="browser_press_key",
        description="Press keyboard key globally or on a target element.",
        path="/browser/press_key",
        properties={
            "key": {"type": "string"},
            "uid": {"type": "string"},
            "selector": {"type": "string"},
            "timeout_ms": {"type": "integer", "default": 30000, "minimum": 1, "maximum": 300000},
        },
        required=["key"],
    )
    _register_browser_tool(
        registry,
        client,
        name="browser_select_option",
        description="Select options in a select element.",
        path="/browser/select_option",
        properties={
            "uid": {"type": "string"},
            "selector": {"type": "string"},
            "values": {"type": "array", "items": {"type": "string"}},
            "labels": {"type": "array", "items": {"type": "string"}},
            "indexes": {"type": "array", "items": {"type": "integer"}},
            "timeout_ms": {"type": "integer", "default": 30000, "minimum": 1, "maximum": 300000},
        },
        required=[],
    )
    _register_browser_tool(
        registry,
        client,
        name="browser_snapshot",
        description="Capture page snapshot (with generated element uids) to JSON artifact.",
        path="/browser/snapshot",
        properties={
            "max_elements": {"type": "integer", "default": 200, "minimum": 1, "maximum": 2000},
            "output_path": {"type": "string"},
        },
        required=[],
    )
    _register_browser_tool(
        registry,
        client,
        name="browser_take_screenshot",
        description="Take a screenshot of page or target element.",
        path="/browser/take_screenshot",
        properties={
            "uid": {"type": "string"},
            "selector": {"type": "string"},
            "output_path": {"type": "string"},
            "full_page": {"type": "boolean", "default": True},
            "format": {"type": "string", "enum": ["png", "jpeg", "webp"], "default": "png"},
            "quality": {"type": "integer", "minimum": 0, "maximum": 100},
        },
        required=[],
    )
    _register_browser_tool(
        registry,
        client,
        name="browser_type",
        description="Type text into a target element or active element.",
        path="/browser/type",
        properties={
            "text": {"type": "string"},
            "uid": {"type": "string"},
            "selector": {"type": "string"},
            "clear_before": {"type": "boolean", "default": False},
            "delay_ms": {"type": "integer", "default": 0, "minimum": 0, "maximum": 2000},
            "timeout_ms": {"type": "integer", "default": 30000, "minimum": 1, "maximum": 300000},
        },
        required=["text"],
    )
    _register_browser_tool(
        registry,
        client,
        name="browser_wait_for",
        description="Wait for text, target element state, or timeout.",
        path="/browser/wait_for",
        properties={
            "text": {"type": "array", "items": {"type": "string"}},
            "uid": {"type": "string"},
            "selector": {"type": "string"},
            "state": {"type": "string", "enum": ["visible", "hidden", "attached", "detached"], "default": "visible"},
            "timeout_ms": {"type": "integer", "default": 30000, "minimum": 1, "maximum": 300000},
        },
        required=[],
    )
    _register_browser_tool(
        registry,
        client,
        name="browser_tabs",
        description="Manage tabs: list/new/activate/close/close_others.",
        path="/browser/tabs",
        properties={
            "action": {
                "type": "string",
                "enum": ["list", "new", "activate", "close", "close_others"],
                "default": "list",
            },
            "index": {"type": "integer", "minimum": 0},
            "url": {"type": "string"},
            "timeout_ms": {"type": "integer", "default": 30000, "minimum": 1, "maximum": 300000},
            "wait_until": {
                "type": "string",
                "default": "domcontentloaded",
                "enum": ["load", "domcontentloaded", "networkidle", "commit"],
            },
        },
        required=[],
    )
    _register_browser_tool(
        registry,
        client,
        name="browser_mouse_click_xy",
        description="Mouse click at absolute viewport coordinates.",
        path="/browser/mouse_click_xy",
        properties={
            "x": {"type": "number"},
            "y": {"type": "number"},
            "button": {"type": "string", "enum": ["left", "right", "middle"], "default": "left"},
            "click_count": {"type": "integer", "default": 1, "minimum": 1, "maximum": 10},
            "delay_ms": {"type": "integer", "default": 0, "minimum": 0, "maximum": 5000},
        },
        required=["x", "y"],
    )
    _register_browser_tool(
        registry,
        client,
        name="browser_mouse_down",
        description="Press mouse button down.",
        path="/browser/mouse_down",
        properties={
            "button": {"type": "string", "enum": ["left", "right", "middle"], "default": "left"},
        },
        required=[],
    )
    _register_browser_tool(
        registry,
        client,
        name="browser_mouse_drag_xy",
        description="Drag mouse between absolute coordinates.",
        path="/browser/mouse_drag_xy",
        properties={
            "start_x": {"type": "number"},
            "start_y": {"type": "number"},
            "end_x": {"type": "number"},
            "end_y": {"type": "number"},
            "steps": {"type": "integer", "default": 20, "minimum": 1, "maximum": 1000},
            "button": {"type": "string", "enum": ["left", "right", "middle"], "default": "left"},
        },
        required=["start_x", "start_y", "end_x", "end_y"],
    )
    _register_browser_tool(
        registry,
        client,
        name="browser_mouse_move_xy",
        description="Move mouse to absolute coordinates.",
        path="/browser/mouse_move_xy",
        properties={
            "x": {"type": "number"},
            "y": {"type": "number"},
            "steps": {"type": "integer", "default": 1, "minimum": 1, "maximum": 1000},
        },
        required=["x", "y"],
    )
    _register_browser_tool(
        registry,
        client,
        name="browser_mouse_up",
        description="Release mouse button.",
        path="/browser/mouse_up",
        properties={
            "button": {"type": "string", "enum": ["left", "right", "middle"], "default": "left"},
        },
        required=[],
    )
    _register_browser_tool(
        registry,
        client,
        name="browser_mouse_wheel",
        description="Scroll mouse wheel with deltas.",
        path="/browser/mouse_wheel",
        properties={
            "delta_x": {"type": "number", "default": 0},
            "delta_y": {"type": "number", "default": 0},
        },
        required=[],
    )

    return SandboxToolRuntime(client)
