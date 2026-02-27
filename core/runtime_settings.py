from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from core.tools.tool import Instance


DEFAULT_TOOL_RESULT_ROOT_DIR = "data/tool_results"
DEFAULT_INLINE_LIMIT = 500
DEFAULT_PREVIEW_LIMIT = 500
DEFAULT_FS_READ_MAX_CHARS = 4000
DEFAULT_ALWAYS_EXTERNALIZE_TOOLS = {
    "browser_html_content",
}


def _safe_int(value: Any, default: int, minimum: int = 1) -> int:
    try:
        parsed = int(value)
        if parsed < minimum:
            return default
        return parsed
    except Exception:
        return default


def _is_safe_relative_path(raw_path: str) -> bool:
    p = Path(raw_path)
    if p.is_absolute():
        return False
    return not any(part == ".." for part in p.parts)


@dataclass
class RuntimeSettings:
    workspace_root: Path
    tool_result_root_dir: str = DEFAULT_TOOL_RESULT_ROOT_DIR
    inline_limit: int = DEFAULT_INLINE_LIMIT
    preview_limit: int = DEFAULT_PREVIEW_LIMIT
    fs_read_max_chars: int = DEFAULT_FS_READ_MAX_CHARS
    always_externalize_tools: set[str] = field(
        default_factory=lambda: set(DEFAULT_ALWAYS_EXTERNALIZE_TOOLS)
    )

    def __post_init__(self):
        workspace = Path(self.workspace_root).resolve()
        root_dir = str(self.tool_result_root_dir or DEFAULT_TOOL_RESULT_ROOT_DIR).strip() or DEFAULT_TOOL_RESULT_ROOT_DIR
        if not _is_safe_relative_path(root_dir):
            raise ValueError(f"unsafe_tool_result_root_dir: {root_dir}")

        resolved_root = (workspace / Path(root_dir)).resolve()
        if resolved_root != workspace and workspace not in resolved_root.parents:
            raise ValueError(f"tool_result_root_outside_workspace: {root_dir}")

        self.workspace_root = workspace
        self.tool_result_root_dir = root_dir
        self.inline_limit = _safe_int(self.inline_limit, DEFAULT_INLINE_LIMIT, minimum=1)
        self.preview_limit = _safe_int(self.preview_limit, DEFAULT_PREVIEW_LIMIT, minimum=1)
        self.fs_read_max_chars = _safe_int(
            self.fs_read_max_chars,
            DEFAULT_FS_READ_MAX_CHARS,
            minimum=1,
        )
        self.always_externalize_tools = {
            str(item)
            for item in (self.always_externalize_tools or set())
            if isinstance(item, str)
        } or set(DEFAULT_ALWAYS_EXTERNALIZE_TOOLS)

    @property
    def tool_result_root(self) -> Path:
        return (self.workspace_root / Path(self.tool_result_root_dir)).resolve()

    @property
    def allowed_roots(self) -> tuple[Path, Path]:
        return (self.workspace_root, self.tool_result_root)

    @property
    def hard_fs_read_max_chars(self) -> int:
        return min(8000, max(2000, int(self.fs_read_max_chars)))

    @classmethod
    def from_sources(
        cls,
        *,
        raw_tool_result: Mapping[str, Any] | None = None,
        env: Mapping[str, str] | None = None,
        workspace_root: Path | None = None,
        allow_env_override: bool = True,
    ) -> "RuntimeSettings":
        raw = dict(raw_tool_result or {})
        env_map = env if env is not None else os.environ

        def pick(name: str, default: Any, env_key: str | None = None) -> Any:
            key = env_key or name.upper()
            if allow_env_override:
                env_val = env_map.get(key)
                if env_val is not None and str(env_val).strip() != "":
                    return env_val
            return raw.get(name, default)

        always_externalize_tools: set[str]
        always_env = env_map.get("TOOL_RESULT_ALWAYS_EXTERNALIZE_TOOLS", "") if allow_env_override else ""
        if always_env.strip():
            always_externalize_tools = {
                part.strip()
                for part in always_env.split(",")
                if part and part.strip()
            }
        else:
            always_externalize_tools = {
                str(item)
                for item in (raw.get("always_externalize_tools") or DEFAULT_ALWAYS_EXTERNALIZE_TOOLS)
                if isinstance(item, str)
            }

        workspace = Path(workspace_root) if workspace_root is not None else Path(Instance.directory)
        return cls(
            workspace_root=workspace,
            tool_result_root_dir=str(
                pick("root_dir", DEFAULT_TOOL_RESULT_ROOT_DIR, env_key="TOOL_RESULT_ROOT_DIR")
            ),
            inline_limit=_safe_int(
                pick("inline_limit", DEFAULT_INLINE_LIMIT, env_key="TOOL_RESULT_INLINE_LIMIT"),
                DEFAULT_INLINE_LIMIT,
                minimum=1,
            ),
            preview_limit=_safe_int(
                pick("preview_limit", DEFAULT_PREVIEW_LIMIT, env_key="TOOL_RESULT_PREVIEW_LIMIT"),
                DEFAULT_PREVIEW_LIMIT,
                minimum=1,
            ),
            fs_read_max_chars=_safe_int(
                pick("fs_read_max_chars", DEFAULT_FS_READ_MAX_CHARS, env_key="TOOL_RESULT_FS_READ_MAX_CHARS"),
                DEFAULT_FS_READ_MAX_CHARS,
                minimum=1,
            ),
            always_externalize_tools=always_externalize_tools,
        )


_runtime_settings: RuntimeSettings | None = None


def get_runtime_settings() -> RuntimeSettings:
    global _runtime_settings
    if _runtime_settings is None:
        _runtime_settings = RuntimeSettings.from_sources()
    return _runtime_settings


def set_runtime_settings(settings: RuntimeSettings) -> RuntimeSettings:
    global _runtime_settings
    _runtime_settings = settings
    return _runtime_settings


def configure_runtime_settings(
    *,
    raw_tool_result: Mapping[str, Any] | None = None,
    env: Mapping[str, str] | None = None,
    workspace_root: Path | None = None,
    allow_env_override: bool = True,
) -> RuntimeSettings:
    settings = RuntimeSettings.from_sources(
        raw_tool_result=raw_tool_result,
        env=env,
        workspace_root=workspace_root,
        allow_env_override=allow_env_override,
    )
    return set_runtime_settings(settings)
