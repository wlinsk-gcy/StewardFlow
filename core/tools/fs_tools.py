from __future__ import annotations

import glob as globlib
import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .tool import Instance, Tool


DEFAULT_MAX_ITEMS = 200
DEFAULT_MAX_LINES = 200
DEFAULT_MAX_BYTES = 16384
DEFAULT_WRITE_MAX_BYTES = 1048576
ARTIFACT_DIR = Path("data") / "tool_artifacts"


def _workspace_root() -> Path:
    return Path(Instance.directory).resolve()


def _resolve_workspace_path(raw_path: str) -> Path:
    root = _workspace_root()
    candidate = Path(raw_path or ".")
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve()
    if not Instance.contains_path(str(resolved)):
        raise PermissionError(f"path_outside_workspace: {raw_path}")
    if resolved != root and root not in resolved.parents:
        raise PermissionError(f"path_outside_workspace: {raw_path}")
    return resolved


def _to_rel_display(path: Path) -> str:
    root = _workspace_root()
    try:
        return path.resolve().relative_to(root).as_posix()
    except Exception:
        return path.resolve().as_posix()


def _safe_int(value: Any, default: int, min_value: int = 1) -> int:
    try:
        parsed = int(value)
        if parsed < min_value:
            return default
        return parsed
    except Exception:
        return default


def _write_artifact(tool_name: str, payload: Any, extension: str = "json") -> str:
    root = _workspace_root()
    folder = root / ARTIFACT_DIR
    folder.mkdir(parents=True, exist_ok=True)
    stamp = int(time.time() * 1000)
    file_path = folder / f"{tool_name}_{stamp}.{extension}"
    if extension == "json":
        file_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    else:
        text_payload = payload if isinstance(payload, str) else str(payload)
        file_path.write_text(text_payload, encoding="utf-8")
    return _to_rel_display(file_path)


def _build_error(error: str) -> str:
    return json.dumps(
        {"ok": False, "truncated": False, "artifact_path": None, "error": error},
        ensure_ascii=False,
    )


def _item_from_path(path: Path) -> Optional[Dict[str, Any]]:
    try:
        stat = path.stat()
    except OSError:
        return None
    if path.is_dir():
        item_type = "dir"
    elif path.is_file():
        item_type = "file"
    else:
        item_type = "other"
    return {"path": _to_rel_display(path), "type": item_type, "size": stat.st_size}


def _iter_children(target: Path, recursive: bool) -> List[Path]:
    if target.is_file():
        return [target]
    if not target.exists() or not target.is_dir():
        return []
    out: List[Path] = []
    if recursive:
        for dirpath, dirnames, filenames in os.walk(target):
            base = Path(dirpath)
            for name in dirnames:
                out.append(base / name)
            for name in filenames:
                out.append(base / name)
    else:
        try:
            for child in target.iterdir():
                out.append(child)
        except OSError:
            return []
    return out


class FsListTool(Tool):
    def __init__(self):
        super().__init__()
        self.name = "fs_list"
        self.description = "List files/directories using a workspace-safe, OS-agnostic API."

    async def execute(
        self,
        path: str = ".",
        recursive: bool = False,
        max_items: int = DEFAULT_MAX_ITEMS,
        include_dirs: bool = True,
        include_files: bool = True,
        **kwargs,
    ) -> str:
        del kwargs
        try:
            target = _resolve_workspace_path(path)
            limit = _safe_int(max_items, DEFAULT_MAX_ITEMS)

            all_items: List[Dict[str, Any]] = []
            for child in _iter_children(target, recursive=bool(recursive)):
                if child.is_dir() and not include_dirs:
                    continue
                if child.is_file() and not include_files:
                    continue
                item = _item_from_path(child)
                if item:
                    all_items.append(item)

            truncated = len(all_items) > limit
            preview_items = all_items[:limit]
            artifact_path = None
            if truncated:
                artifact_path = _write_artifact(
                    "fs_list",
                    {"path": _to_rel_display(target), "items": all_items},
                )

            payload = {
                "ok": True,
                "items": preview_items,
                "truncated": truncated,
                "artifact_path": artifact_path,
                "summary": {
                    "path": _to_rel_display(target),
                    "returned_items": len(preview_items),
                    "total_items": len(all_items),
                },
            }
            return json.dumps(payload, ensure_ascii=False)
        except Exception as exc:
            return _build_error(str(exc))

    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "default": ".", "description": "File or directory path."},
                        "recursive": {"type": "boolean", "default": False},
                        "max_items": {"type": "integer", "default": DEFAULT_MAX_ITEMS, "minimum": 1, "maximum": 5000},
                        "include_dirs": {"type": "boolean", "default": True},
                        "include_files": {"type": "boolean", "default": True},
                    },
                    "additionalProperties": False,
                },
                "strict": True,
            },
        }


class FsGlobTool(Tool):
    def __init__(self):
        super().__init__()
        self.name = "fs_glob"
        self.description = "Find workspace files/directories by glob pattern."

    async def execute(self, pattern: str, max_matches: int = DEFAULT_MAX_ITEMS, **kwargs) -> str:
        del kwargs
        if not pattern:
            return _build_error("pattern_required")
        try:
            root = _workspace_root()
            limit = _safe_int(max_matches, DEFAULT_MAX_ITEMS)
            matches_rel = globlib.glob(pattern, root_dir=str(root), recursive=True)

            all_matches: List[Dict[str, Any]] = []
            for rel in matches_rel:
                candidate = (root / rel).resolve()
                if not Instance.contains_path(str(candidate)):
                    continue
                item = _item_from_path(candidate)
                if item:
                    all_matches.append(item)

            truncated = len(all_matches) > limit
            preview_matches = all_matches[:limit]
            artifact_path = None
            if truncated:
                artifact_path = _write_artifact(
                    "fs_glob",
                    {"pattern": pattern, "matches": all_matches},
                )

            payload = {
                "ok": True,
                "matches": preview_matches,
                "truncated": truncated,
                "artifact_path": artifact_path,
                "summary": {
                    "pattern": pattern,
                    "returned_matches": len(preview_matches),
                    "total_matches": len(all_matches),
                },
            }
            return json.dumps(payload, ensure_ascii=False)
        except Exception as exc:
            return _build_error(str(exc))

    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "pattern": {"type": "string", "description": "Glob pattern like '**/*.py'."},
                        "max_matches": {"type": "integer", "default": DEFAULT_MAX_ITEMS, "minimum": 1, "maximum": 5000},
                    },
                    "required": ["pattern"],
                    "additionalProperties": False,
                },
                "strict": True,
            },
        }


class FsReadTool(Tool):
    def __init__(self):
        super().__init__()
        self.name = "fs_read"
        self.description = "Read text from a file with bounded lines/bytes and safe truncation."

    async def execute(
        self,
        path: str,
        start_line: int = 1,
        max_lines: int = DEFAULT_MAX_LINES,
        max_bytes: int = DEFAULT_MAX_BYTES,
        **kwargs,
    ) -> str:
        del kwargs
        if not path:
            return _build_error("path_required")
        try:
            file_path = _resolve_workspace_path(path)
            if not file_path.exists() or not file_path.is_file():
                return _build_error(f"not_a_file: {path}")

            start = _safe_int(start_line, 1)
            line_limit = _safe_int(max_lines, DEFAULT_MAX_LINES)
            byte_limit = _safe_int(max_bytes, DEFAULT_MAX_BYTES)

            with file_path.open("r", encoding="utf-8", errors="replace") as f:
                all_lines = f.readlines()

            selected_lines = all_lines[start - 1 :] if start - 1 < len(all_lines) else []
            full_text = "".join(selected_lines)
            preview_text = "".join(selected_lines[:line_limit])

            truncated = len(selected_lines) > line_limit
            encoded = preview_text.encode("utf-8")
            if len(encoded) > byte_limit:
                preview_text = encoded[:byte_limit].decode("utf-8", errors="ignore")
                truncated = True
            if len(full_text.encode("utf-8")) > byte_limit:
                truncated = True

            artifact_path = None
            if truncated:
                artifact_path = _write_artifact("fs_read", full_text, extension="txt")

            payload = {
                "ok": True,
                "text": preview_text,
                "truncated": truncated,
                "artifact_path": artifact_path,
                "summary": {
                    "path": _to_rel_display(file_path),
                    "start_line": start,
                    "returned_lines": min(len(selected_lines), line_limit),
                    "total_lines_from_start": len(selected_lines),
                },
            }
            return json.dumps(payload, ensure_ascii=False)
        except Exception as exc:
            return _build_error(str(exc))

    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Target file path."},
                        "start_line": {"type": "integer", "default": 1, "minimum": 1},
                        "max_lines": {"type": "integer", "default": DEFAULT_MAX_LINES, "minimum": 1, "maximum": 5000},
                        "max_bytes": {"type": "integer", "default": DEFAULT_MAX_BYTES, "minimum": 1, "maximum": 1048576},
                    },
                    "required": ["path"],
                    "additionalProperties": False,
                },
                "strict": True,
            },
        }


class FsWriteTool(Tool):
    def __init__(self):
        super().__init__()
        self.name = "fs_write"
        self.description = "Write text to a file with workspace guardrails and size limits."

    async def execute(
        self,
        path: str,
        content: str,
        mode: str = "overwrite",
        create_dirs: bool = True,
        max_bytes: int = DEFAULT_WRITE_MAX_BYTES,
        **kwargs,
    ) -> str:
        del kwargs
        if not path:
            return _build_error("path_required")
        try:
            byte_limit = _safe_int(max_bytes, DEFAULT_WRITE_MAX_BYTES)
            content_bytes = (content or "").encode("utf-8")
            if len(content_bytes) > byte_limit:
                return _build_error(f"content_exceeds_max_bytes: {len(content_bytes)} > {byte_limit}")

            file_path = _resolve_workspace_path(path)
            if create_dirs:
                file_path.parent.mkdir(parents=True, exist_ok=True)
            write_mode = "a" if mode == "append" else "w"
            with file_path.open(write_mode, encoding="utf-8") as f:
                written = f.write(content or "")

            payload = {
                "ok": True,
                "bytes_written": len((content or "").encode("utf-8")),
                "chars_written": written,
                "path": _to_rel_display(file_path),
                "truncated": False,
                "artifact_path": None,
            }
            return json.dumps(payload, ensure_ascii=False)
        except Exception as exc:
            return _build_error(str(exc))

    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "content": {"type": "string"},
                        "mode": {"type": "string", "enum": ["overwrite", "append"], "default": "overwrite"},
                        "create_dirs": {"type": "boolean", "default": True},
                        "max_bytes": {"type": "integer", "default": DEFAULT_WRITE_MAX_BYTES, "minimum": 1, "maximum": 16777216},
                    },
                    "required": ["path", "content"],
                    "additionalProperties": False,
                },
                "strict": True,
            },
        }


class FsStatTool(Tool):
    def __init__(self):
        super().__init__()
        self.name = "fs_stat"
        self.description = "Get stat metadata for a workspace path."

    async def execute(self, path: str, **kwargs) -> str:
        del kwargs
        if not path:
            return _build_error("path_required")
        try:
            target = _resolve_workspace_path(path)
            exists = target.exists()
            size = None
            mtime = None
            item_type = None
            if exists:
                stat = target.stat()
                size = stat.st_size
                mtime = stat.st_mtime
                if target.is_file():
                    item_type = "file"
                elif target.is_dir():
                    item_type = "dir"
                else:
                    item_type = "other"
            payload = {
                "ok": True,
                "exists": exists,
                "size": size,
                "mtime": mtime,
                "type": item_type,
                "path": _to_rel_display(target),
                "truncated": False,
                "artifact_path": None,
            }
            return json.dumps(payload, ensure_ascii=False)
        except Exception as exc:
            return _build_error(str(exc))

    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                    "additionalProperties": False,
                },
                "strict": True,
            },
        }

