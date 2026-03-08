from __future__ import annotations

import asyncio
import copy
import json
from dataclasses import dataclass
from typing import Any

from core.tools.tool import Tool, ToolRegistry

from .client import DockerSandboxClient

DEFAULT_SANDBOX_IMAGE = "gui-sandbox:dev"
DEFAULT_HEALTHCHECK_HOST = "127.0.0.1"
DEFAULT_HTTP_TIMEOUT_SEC = 180

_WAIT_UNTIL_VALUES = ["load", "domcontentloaded", "networkidle", "commit"]
_SCREENSHOT_FORMAT_VALUES = ["png", "jpeg", "jpg", "webp"]


def _schema(
    *,
    properties: dict[str, Any],
    required: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": copy.deepcopy(properties),
        "required": list(required or []),
    }


@dataclass(frozen=True)
class SandboxToolSpec:
    name: str
    path: str
    description: str
    parameters: dict[str, Any]
    requires_confirmation: bool = False


class SandboxToolRuntime:
    def __init__(self, sandbox_cfg: dict[str, Any] | None = None) -> None:
        cfg = dict(sandbox_cfg or {})
        self.sandbox_id = str(cfg.get("sandbox_id") or "").strip() or None
        self.default_http_timeout_sec = max(15, int(cfg.get("tool_http_timeout_sec", DEFAULT_HTTP_TIMEOUT_SEC)))
        self.client = DockerSandboxClient(
            default_image=str(cfg.get("image") or DEFAULT_SANDBOX_IMAGE),
            docker_base_url=cfg.get("docker_base_url"),
            public_host=cfg.get("public_host"),
            healthcheck_host=str(cfg.get("healthcheck_host") or DEFAULT_HEALTHCHECK_HOST),
            sandbox_id=self.sandbox_id,
        )

    def set_sandbox_id(self, sandbox_id: str | None) -> None:
        self.sandbox_id = str(sandbox_id or "").strip() or None
        self.client.set_sandbox_id(self.sandbox_id)

    def _http_timeout_for(self, payload: dict[str, Any]) -> int:
        timeout_sec = self.default_http_timeout_sec
        raw_timeout = payload.get("timeout")
        if isinstance(raw_timeout, (int, float)) and raw_timeout > 0:
            timeout_sec = max(timeout_sec, int(raw_timeout / 1000) + 15)
        return timeout_sec

    async def invoke(self, *, path: str, payload: dict[str, Any]) -> Any:
        timeout_sec = self._http_timeout_for(payload)
        return await asyncio.to_thread(
            self.client.api_post,
            sandbox_id=self.sandbox_id,
            path=path,
            payload=payload,
            timeout_sec=timeout_sec,
        )

    async def shutdown(self) -> None:
        await asyncio.to_thread(self.client.close)


class SandboxHttpTool(Tool):
    def __init__(self, runtime: SandboxToolRuntime, spec: SandboxToolSpec) -> None:
        super().__init__()
        self._runtime = runtime
        self._spec = spec
        self.name = spec.name
        self.description = spec.description
        self.requires_confirmation = bool(spec.requires_confirmation)

    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": copy.deepcopy(self._spec.parameters),
                "strict": True,
            },
        }

    async def execute(self, **kwargs) -> str:
        result = await self._runtime.invoke(path=self._spec.path, payload=dict(kwargs))
        if isinstance(result, str):
            return result
        return json.dumps(result, ensure_ascii=False)


TOOL_SPECS: tuple[SandboxToolSpec, ...] = (
    SandboxToolSpec(
        name="bash",
        path="/tools/bash",
        description=(
            "Execute a shell command inside the sandbox and return merged stdout/stderr. "
            "Use this for command execution, not for structured file reads or edits when dedicated tools fit better."
        ),
        parameters=_schema(
            properties={
                "command": {
                    "type": "string",
                    "description": "Shell command to execute.",
                    "minLength": 1,
                },
                "timeout": {
                    "type": "integer",
                    "description": "Maximum execution time in milliseconds.",
                    "minimum": 1,
                },
                "workdir": {
                    "type": "string",
                    "description": "Working directory, absolute or relative to the sandbox root.",
                },
                "description": {
                    "type": "string",
                    "description": "Optional short note describing the command intent.",
                },
            },
            required=["command"],
        ),
        requires_confirmation=True,
    ),
    SandboxToolSpec(
        name="glob",
        path="/tools/glob",
        description=(
            "Find files by glob pattern under a sandbox path. "
            "This is a fast path-discovery tool and returns matching paths only."
        ),
        parameters=_schema(
            properties={
                "pattern": {
                    "type": "string",
                    "description": "Glob pattern to match, such as `**/*.py` or `*.md`.",
                    "minLength": 1,
                },
                "path": {
                    "type": "string",
                    "description": "Base path to search from. Defaults to the sandbox working directory.",
                },
            },
            required=["pattern"],
        ),
    ),
    SandboxToolSpec(
        name="read",
        path="/tools/read",
        description=(
            "Read a file or list a directory from the sandbox. "
            "Supports offset/limit pagination and returns line-numbered text for files."
        ),
        parameters=_schema(
            properties={
                "filePath": {
                    "type": "string",
                    "description": "Target file or directory path.",
                    "minLength": 1,
                },
                "offset": {
                    "type": "integer",
                    "description": "1-based line or entry offset.",
                    "minimum": 1,
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of lines or directory entries to return.",
                    "minimum": 1,
                },
            },
            required=["filePath"],
        ),
    ),
    SandboxToolSpec(
        name="grep",
        path="/tools/grep",
        description=(
            "Search file contents with a regular expression under a sandbox path. "
            "Returns grouped matches with file paths and line snippets."
        ),
        parameters=_schema(
            properties={
                "pattern": {
                    "type": "string",
                    "description": "Regular expression to search for.",
                    "minLength": 1,
                },
                "path": {
                    "type": "string",
                    "description": "File or directory to search. Defaults to the sandbox working directory.",
                },
                "glob": {
                    "type": "string",
                    "description": "Optional glob filter to restrict searched files.",
                },
            },
            required=["pattern"],
        ),
    ),
    SandboxToolSpec(
        name="edit",
        path="/tools/edit",
        description=(
            "Edit a file by replacing `oldString` with `newString`. "
            "Use `replaceAll` only when the match is intentionally repeated."
        ),
        parameters=_schema(
            properties={
                "filePath": {
                    "type": "string",
                    "description": "Target file path.",
                    "minLength": 1,
                },
                "oldString": {
                    "type": "string",
                    "description": "Exact text to replace.",
                },
                "newString": {
                    "type": "string",
                    "description": "Replacement text.",
                },
                "replaceAll": {
                    "type": "boolean",
                    "description": "Replace every match instead of requiring a unique match.",
                },
            },
            required=["filePath", "oldString", "newString"],
        ),
    ),
    SandboxToolSpec(
        name="write",
        path="/tools/write",
        description=(
            "Create or overwrite a file with the provided content. "
            "Parent directories are created automatically when needed."
        ),
        parameters=_schema(
            properties={
                "content": {
                    "type": "string",
                    "description": "Full file content to write.",
                },
                "filePath": {
                    "type": "string",
                    "description": "Target file path.",
                    "minLength": 1,
                },
            },
            required=["content", "filePath"],
        ),
    ),
    SandboxToolSpec(
        name="navigate_page",
        path="/tools/navigate_page",
        description=(
            "Navigate the active page to a URL, or move the page back, forward, or reload it."
        ),
        parameters=_schema(
            properties={
                "type": {
                    "type": "string",
                    "description": "Navigation mode.",
                    "enum": ["url", "back", "forward", "reload"],
                },
                "url": {
                    "type": "string",
                    "description": "Destination URL. Required when `type` is `url`.",
                },
                "timeout": {
                    "type": "integer",
                    "description": "Navigation timeout in milliseconds.",
                    "minimum": 1,
                },
                "waitUntil": {
                    "type": "string",
                    "description": "Page lifecycle event to wait for before returning.",
                    "enum": _WAIT_UNTIL_VALUES,
                },
            },
            required=["type"],
        ),
    ),
    SandboxToolSpec(
        name="take_snapshot",
        path="/tools/take_snapshot",
        description=(
            "Capture a text snapshot of the current page that is optimized for subsequent interactions. "
            "Use the returned element `uid` values with tools like `click`, `fill`, `hover`, and `upload_file`."
        ),
        parameters=_schema(
            properties={
                "verbose": {
                    "type": "boolean",
                    "description": "Include a more verbose snapshot when true.",
                },
                "filePath": {
                    "type": "string",
                    "description": "Optional path to persist the snapshot text inside the sandbox.",
                },
            }
        ),
    ),
    SandboxToolSpec(
        name="click",
        path="/tools/click",
        description=(
            "Click an element from the latest page snapshot by `uid`."
        ),
        parameters=_schema(
            properties={
                "uid": {
                    "type": "string",
                    "description": "Element uid from the latest `take_snapshot` result.",
                    "minLength": 1,
                },
                "dblClick": {
                    "type": "boolean",
                    "description": "Perform a double click instead of a single click.",
                },
                "includeSnapshot": {
                    "type": "boolean",
                    "description": "Append a fresh page snapshot to the result.",
                },
            },
            required=["uid"],
        ),
    ),
    SandboxToolSpec(
        name="fill",
        path="/tools/fill",
        description=(
            "Fill an input, textarea, or select element from the latest page snapshot by `uid`."
        ),
        parameters=_schema(
            properties={
                "uid": {
                    "type": "string",
                    "description": "Element uid from the latest `take_snapshot` result.",
                    "minLength": 1,
                },
                "value": {
                    "type": "string",
                    "description": "Text or option value to apply.",
                },
                "includeSnapshot": {
                    "type": "boolean",
                    "description": "Append a fresh page snapshot to the result.",
                },
            },
            required=["uid", "value"],
        ),
    ),
    SandboxToolSpec(
        name="wait_for",
        path="/tools/wait_for",
        description=(
            "Wait until any expected text appears on the active page."
        ),
        parameters=_schema(
            properties={
                "text": {
                    "type": "array",
                    "description": "One or more text fragments to wait for.",
                    "items": {"type": "string"},
                    "minItems": 1,
                },
                "timeout": {
                    "type": "integer",
                    "description": "Maximum wait time in milliseconds.",
                    "minimum": 1,
                },
            },
            required=["text"],
        ),
    ),
    SandboxToolSpec(
        name="take_screenshot",
        path="/tools/take_screenshot",
        description=(
            "Capture a screenshot of the active page or of a snapshot-referenced element."
        ),
        parameters=_schema(
            properties={
                "uid": {
                    "type": "string",
                    "description": "Optional element uid from the latest `take_snapshot` result.",
                },
                "filePath": {
                    "type": "string",
                    "description": "Optional sandbox path where the screenshot should be written.",
                },
                "format": {
                    "type": "string",
                    "description": "Image format for the screenshot.",
                    "enum": _SCREENSHOT_FORMAT_VALUES,
                },
                "fullPage": {
                    "type": "boolean",
                    "description": "Capture the full page instead of the viewport when supported.",
                },
                "quality": {
                    "type": "integer",
                    "description": "Image quality for lossy formats such as jpeg or webp.",
                    "minimum": 0,
                    "maximum": 100,
                },
            }
        ),
    ),
    SandboxToolSpec(
        name="press_key",
        path="/tools/press_key",
        description=(
            "Press a keyboard key on the active page or focused element."
        ),
        parameters=_schema(
            properties={
                "key": {
                    "type": "string",
                    "description": "Keyboard key name, such as `Enter`, `Tab`, or `Control+A`.",
                    "minLength": 1,
                },
                "includeSnapshot": {
                    "type": "boolean",
                    "description": "Append a fresh page snapshot to the result.",
                },
            },
            required=["key"],
        ),
    ),
    SandboxToolSpec(
        name="handle_dialog",
        path="/tools/handle_dialog",
        description=(
            "Accept or dismiss the currently open JavaScript dialog."
        ),
        parameters=_schema(
            properties={
                "action": {
                    "type": "string",
                    "description": "Dialog action to perform.",
                    "enum": ["accept", "dismiss"],
                },
                "promptText": {
                    "type": "string",
                    "description": "Prompt text to submit when accepting a prompt dialog.",
                },
            },
            required=["action"],
        ),
    ),
    SandboxToolSpec(
        name="hover",
        path="/tools/hover",
        description=(
            "Hover over an element from the latest page snapshot by `uid`."
        ),
        parameters=_schema(
            properties={
                "uid": {
                    "type": "string",
                    "description": "Element uid from the latest `take_snapshot` result.",
                    "minLength": 1,
                },
                "includeSnapshot": {
                    "type": "boolean",
                    "description": "Append a fresh page snapshot to the result.",
                },
            },
            required=["uid"],
        ),
    ),
    SandboxToolSpec(
        name="upload_file",
        path="/tools/upload_file",
        description=(
            "Upload a sandbox file to a file input from the latest page snapshot by `uid`."
        ),
        parameters=_schema(
            properties={
                "uid": {
                    "type": "string",
                    "description": "Element uid from the latest `take_snapshot` result.",
                    "minLength": 1,
                },
                "filePath": {
                    "type": "string",
                    "description": "Sandbox file path to upload.",
                    "minLength": 1,
                },
                "includeSnapshot": {
                    "type": "boolean",
                    "description": "Append a fresh page snapshot to the result.",
                },
            },
            required=["uid", "filePath"],
        ),
    ),
    SandboxToolSpec(
        name="select_page",
        path="/tools/select_page",
        description=(
            "Switch the active browser page by `pageId`."
        ),
        parameters=_schema(
            properties={
                "pageId": {
                    "type": "integer",
                    "description": "Stable page identifier returned in browser tool metadata.",
                    "minimum": 0,
                },
                "bringToFront": {
                    "type": "boolean",
                    "description": "Bring the selected page to the foreground before returning.",
                },
            },
            required=["pageId"],
        ),
    ),
)


def register_sandbox_tools(registry: ToolRegistry, sandbox_cfg: dict[str, Any] | None = None) -> SandboxToolRuntime:
    runtime = SandboxToolRuntime(sandbox_cfg)
    for spec in TOOL_SPECS:
        registry.register(SandboxHttpTool(runtime, spec))
    return runtime
