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


def _tool_timeout_sec(timeout_ms: int) -> int:
    return max(5, min(int(timeout_ms / 1000) + 5, 600))


class _SandboxTool(Tool):
    def __init__(self, *, client: DockerSandboxClient, name: str, description: str) -> None:
        super().__init__()
        self.client = client
        self.name = name
        self.description = description

    async def _invoke(self, fn, **kwargs) -> str:
        try:
            payload = await asyncio.to_thread(fn, **kwargs)
            return json.dumps(payload, ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"error": str(exc)}, ensure_ascii=False)


class _CommandTool(_SandboxTool):
    async def _run_command(
        self,
        *,
        command: str,
        cwd: str | None,
        timeout_ms: int,
        env: dict[str, str] | None = None,
        shell_executable: str | None = None,
        persist_output: bool = False,
    ) -> str:
        payload = {
            "command": command,
            "cwd": cwd,
            "timeout_ms": max(1, min(int(timeout_ms), 3600000)),
            "env": env,
            "shell_executable": shell_executable,
            "persist_output": bool(persist_output),
        }
        return await self._invoke(
            self.client.api_post,
            sandbox_id=None,
            path="/tools/bash",
            payload=payload,
            timeout_sec=_tool_timeout_sec(timeout_ms),
        )


class BashTool(_CommandTool):
    def __init__(self, client: DockerSandboxClient) -> None:
        super().__init__(
            client=client,
            name="bash",
            description="Execute a shell command inside the managed docker sandbox via /tools/bash.",
        )
        self.requires_confirmation = True

    async def execute(
        self,
        command: str,
        cwd: str | None = None,
        timeout_ms: int = 120000,
        env: dict[str, str] | None = None,
        shell_executable: str | None = None,
        persist_output: bool = False,
        **kwargs,
    ) -> str:
        del kwargs
        return await self._run_command(
            command=command,
            cwd=cwd,
            timeout_ms=timeout_ms,
            env=env,
            shell_executable=shell_executable,
            persist_output=persist_output,
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
                "shell_executable": {"type": "string"},
                "persist_output": {"type": "boolean", "default": False},
            },
            required=["command"],
        )


class GlobTool(_SandboxTool):
    def __init__(self, client: DockerSandboxClient) -> None:
        super().__init__(
            client=client,
            name="glob",
            description="List files matching a glob pattern using ripgrep file listing.",
        )

    async def execute(
        self,
        pattern: str,
        path: str = ".",
        include_hidden: bool = False,
        cwd: str | None = None,
        timeout_ms: int = 120000,
        persist_output: bool = False,
        **kwargs,
    ) -> str:
        del kwargs
        timeout_ms = max(1, min(int(timeout_ms), 3600000))
        payload = {
            "pattern": pattern,
            "path": path,
            "include_hidden": bool(include_hidden),
            "cwd": cwd,
            "timeout_ms": timeout_ms,
            "persist_output": bool(persist_output),
        }
        return await self._invoke(
            self.client.api_post,
            sandbox_id=None,
            path="/tools/glob",
            payload=payload,
            timeout_sec=_tool_timeout_sec(timeout_ms),
        )

    def schema(self) -> dict[str, Any]:
        return _tool_schema(
            name=self.name,
            description=self.description,
            properties={
                "pattern": {"type": "string", "description": "Glob pattern like '*.py'."},
                "path": {"type": "string", "default": "."},
                "include_hidden": {"type": "boolean", "default": False},
                "cwd": {"type": "string"},
                "timeout_ms": {"type": "integer", "default": 120000, "minimum": 1, "maximum": 3600000},
                "persist_output": {"type": "boolean", "default": False},
            },
            required=["pattern"],
        )


class ReadTool(_SandboxTool):
    def __init__(self, client: DockerSandboxClient) -> None:
        super().__init__(
            client=client,
            name="read",
            description="Read file content by line range (read-only).",
        )

    async def execute(
        self,
        path: str,
        start_line: int = 1,
        end_line: int | None = None,
        cwd: str | None = None,
        timeout_ms: int = 120000,
        persist_output: bool = False,
        **kwargs,
    ) -> str:
        del kwargs
        timeout_ms = max(1, min(int(timeout_ms), 3600000))
        payload = {
            "path": path,
            "start_line": max(1, int(start_line)),
            "end_line": int(end_line) if end_line is not None else None,
            "cwd": cwd,
            "timeout_ms": timeout_ms,
            "persist_output": bool(persist_output),
        }
        return await self._invoke(
            self.client.api_post,
            sandbox_id=None,
            path="/tools/read",
            payload=payload,
            timeout_sec=_tool_timeout_sec(timeout_ms),
        )

    def schema(self) -> dict[str, Any]:
        return _tool_schema(
            name=self.name,
            description=self.description,
            properties={
                "path": {"type": "string"},
                "start_line": {"type": "integer", "default": 1, "minimum": 1},
                "end_line": {"type": "integer", "minimum": 1},
                "cwd": {"type": "string"},
                "timeout_ms": {"type": "integer", "default": 120000, "minimum": 1, "maximum": 3600000},
                "persist_output": {"type": "boolean", "default": False},
            },
            required=["path"],
        )


class GrepTool(_SandboxTool):
    def __init__(self, client: DockerSandboxClient) -> None:
        super().__init__(
            client=client,
            name="grep",
            description="Search text using grep (read-only).",
        )

    async def execute(
        self,
        pattern: str,
        path: str = ".",
        ignore_case: bool = False,
        recursive: bool = True,
        line_number: bool = True,
        max_count: int | None = None,
        cwd: str | None = None,
        timeout_ms: int = 120000,
        persist_output: bool = False,
        **kwargs,
    ) -> str:
        del kwargs
        timeout_ms = max(1, min(int(timeout_ms), 3600000))
        payload = {
            "pattern": pattern,
            "path": path,
            "ignore_case": bool(ignore_case),
            "recursive": bool(recursive),
            "line_number": bool(line_number),
            "max_count": int(max_count) if isinstance(max_count, int) else None,
            "cwd": cwd,
            "timeout_ms": timeout_ms,
            "persist_output": bool(persist_output),
        }
        return await self._invoke(
            self.client.api_post,
            sandbox_id=None,
            path="/tools/grep",
            payload=payload,
            timeout_sec=_tool_timeout_sec(timeout_ms),
        )

    def schema(self) -> dict[str, Any]:
        return _tool_schema(
            name=self.name,
            description=self.description,
            properties={
                "pattern": {"type": "string"},
                "path": {"type": "string", "default": "."},
                "ignore_case": {"type": "boolean", "default": False},
                "recursive": {"type": "boolean", "default": True},
                "line_number": {"type": "boolean", "default": True},
                "max_count": {"type": "integer", "minimum": 1},
                "cwd": {"type": "string"},
                "timeout_ms": {"type": "integer", "default": 120000, "minimum": 1, "maximum": 3600000},
                "persist_output": {"type": "boolean", "default": False},
            },
            required=["pattern"],
        )


class RgTool(_SandboxTool):
    def __init__(self, client: DockerSandboxClient) -> None:
        super().__init__(
            client=client,
            name="rg",
            description="Search text using ripgrep (read-only, fast).",
        )

    async def execute(
        self,
        pattern: str,
        path: str = ".",
        glob: str | None = None,
        ignore_case: bool = False,
        line_number: bool = True,
        max_count: int | None = None,
        cwd: str | None = None,
        timeout_ms: int = 120000,
        persist_output: bool = False,
        **kwargs,
    ) -> str:
        del kwargs
        timeout_ms = max(1, min(int(timeout_ms), 3600000))
        payload = {
            "pattern": pattern,
            "path": path,
            "glob": glob,
            "ignore_case": bool(ignore_case),
            "line_number": bool(line_number),
            "max_count": int(max_count) if isinstance(max_count, int) else None,
            "cwd": cwd,
            "timeout_ms": timeout_ms,
            "persist_output": bool(persist_output),
        }
        return await self._invoke(
            self.client.api_post,
            sandbox_id=None,
            path="/tools/rg",
            payload=payload,
            timeout_sec=_tool_timeout_sec(timeout_ms),
        )

    def schema(self) -> dict[str, Any]:
        return _tool_schema(
            name=self.name,
            description=self.description,
            properties={
                "pattern": {"type": "string"},
                "path": {"type": "string", "default": "."},
                "glob": {"type": "string"},
                "ignore_case": {"type": "boolean", "default": False},
                "line_number": {"type": "boolean", "default": True},
                "max_count": {"type": "integer", "minimum": 1},
                "cwd": {"type": "string"},
                "timeout_ms": {"type": "integer", "default": 120000, "minimum": 1, "maximum": 3600000},
                "persist_output": {"type": "boolean", "default": False},
            },
            required=["pattern"],
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

    registry.register(BashTool(client))
    registry.register(GlobTool(client))
    registry.register(ReadTool(client))
    registry.register(GrepTool(client))
    registry.register(RgTool(client))

    _register_browser_tool(
        registry,
        client,
        name="navigate_page",
        description="Navigate page by URL/back/forward/reload.",
        path="/browser/navigate_page",
        properties={
            "type": {"type": "string", "enum": ["url", "back", "forward", "reload"], "default": "url"},
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
        name="take_snapshot",
        description="Capture accessibility snapshot and return a11y lines only.",
        path="/browser/take_snapshot",
        properties={
            "verbose": {"type": "boolean", "default": False},
        },
        required=[],
    )
    _register_browser_tool(
        registry,
        client,
        name="wait_for",
        description="Wait for any target text to appear (text is required).",
        path="/browser/wait_for",
        properties={
            "text": {"type": "array", "items": {"type": "string"}, "minItems": 1},
            "timeout_ms": {"type": "integer", "default": 30000, "minimum": 1, "maximum": 300000},
        },
        required=["text"],
    )
    _register_browser_tool(
        registry,
        client,
        name="list_pages",
        description="List open pages.",
        path="/browser/list_pages",
        properties={},
        required=[],
    )
    _register_browser_tool(
        registry,
        client,
        name="new_page",
        description="Create a new page and navigate to the provided URL.",
        path="/browser/new_page",
        properties={
            "url": {"type": "string"},
            "background": {"type": "boolean", "default": False},
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
        name="select_page",
        description="Select one page as active by pageId.",
        path="/browser/select_page",
        properties={
            "pageId": {"type": "integer", "minimum": 0},
            "bringToFront": {"type": "boolean", "default": False},
        },
        required=["pageId"],
    )
    _register_browser_tool(
        registry,
        client,
        name="close_page",
        description="Close one page by pageId.",
        path="/browser/close_page",
        properties={
            "pageId": {"type": "integer", "minimum": 0},
        },
        required=["pageId"],
    )
    _register_browser_tool(
        registry,
        client,
        name="fill",
        description="Fill one input/textarea/select target with value.",
        path="/browser/fill",
        properties={
            "uid": {"type": "string"},
            "selector": {"type": "string"},
            "value": {"type": "string"},
            "timeout_ms": {"type": "integer", "default": 30000, "minimum": 1, "maximum": 300000},
        },
        required=["value"],
    )
    _register_browser_tool(
        registry,
        client,
        name="type_text",
        description="Type text into currently focused element; optionally press submitKey.",
        path="/browser/type_text",
        properties={
            "text": {"type": "string"},
            "submitKey": {"type": "string"},
            "delay_ms": {"type": "integer", "default": 0, "minimum": 0, "maximum": 2000},
        },
        required=["text"],
    )
    _register_browser_tool(
        registry,
        client,
        name="upload_file",
        description="Upload one file to a file-input target.",
        path="/browser/upload_file",
        properties={
            "uid": {"type": "string"},
            "selector": {"type": "string"},
            "filePath": {"type": "string"},
            "timeout_ms": {"type": "integer", "default": 30000, "minimum": 1, "maximum": 300000},
        },
        required=["filePath"],
    )

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
        name="browser_take_screenshot",
        description="Take a screenshot of page or target element.",
        path="/browser/take_screenshot",
        properties={
            "uid": {"type": "string"},
            "selector": {"type": "string"},
            "full_page": {"type": "boolean", "default": True},
            "format": {"type": "string", "enum": ["png", "jpeg", "webp"], "default": "png"},
            "quality": {"type": "integer", "minimum": 0, "maximum": 100},
        },
        required=[],
    )
    return SandboxToolRuntime(client)
