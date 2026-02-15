from __future__ import annotations

import glob as globlib
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from .path_sandbox import resolve_allowed_path, tool_result_root, workspace_root
from .tool import Tool


DEFAULT_MAX_ITEMS = 200
DEFAULT_MAX_LINES = 200
DEFAULT_MAX_BYTES = 16384
DEFAULT_WRITE_MAX_BYTES = 1048576
DEFAULT_READ_LENGTH = 2000
HARD_MAX_READ_LENGTH = min(
    8000,
    max(2000, int(os.getenv("TOOL_RESULT_FS_READ_MAX_CHARS", "4000"))),
)


def _to_rel_display(path: Path) -> str:
    workspace = workspace_root()
    try:
        return path.resolve().relative_to(workspace).as_posix()
    except Exception:
        try:
            return path.resolve().relative_to(tool_result_root()).as_posix()
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


def _build_error(error: str) -> str:
    return json.dumps({"ok": False, "error": error}, ensure_ascii=False)


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
        self.description = "List files/directories using a workspace-safe API."

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
            target = resolve_allowed_path(path, field_name="path")
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

            payload = {
                "ok": True,
                "items": all_items[:limit],
                "truncated": len(all_items) > limit,
                "summary": {
                    "path": _to_rel_display(target),
                    "returned_items": min(len(all_items), limit),
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
                        "path": {"type": "string", "default": ".", "description": "Relative file or directory path."},
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
            if Path(pattern).is_absolute() or ".." in Path(pattern).parts:
                return _build_error("pattern_must_be_relative_and_without_parent_segments")

            root = workspace_root()
            limit = _safe_int(max_matches, DEFAULT_MAX_ITEMS)
            matches_rel = globlib.glob(pattern, root_dir=str(root), recursive=True)

            all_matches: List[Dict[str, Any]] = []
            for rel in matches_rel:
                candidate = resolve_allowed_path(rel, field_name="pattern_match")
                item = _item_from_path(candidate)
                if item:
                    all_matches.append(item)

            payload = {
                "ok": True,
                "matches": all_matches[:limit],
                "truncated": len(all_matches) > limit,
                "summary": {
                    "pattern": pattern,
                    "returned_matches": min(len(all_matches), limit),
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
                        "pattern": {"type": "string", "description": "Relative glob pattern like '**/*.py'."},
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
        self.description = "Read text from a relative file path with bounded offset/length."

    async def execute(
        self,
        path: str,
        offset: int = 0,
        length: int = DEFAULT_READ_LENGTH,
        start_line: Optional[int] = None,
        max_lines: int = DEFAULT_MAX_LINES,
        max_bytes: int = DEFAULT_MAX_BYTES,
        **kwargs,
    ) -> str:
        del kwargs
        if not path:
            return _build_error("path_required")

        try:
            file_path = resolve_allowed_path(path, field_name="path")
            if not file_path.exists() or not file_path.is_file():
                return _build_error(f"not_a_file: {path}")

            text = file_path.read_text(encoding="utf-8", errors="replace")
            hard_limit = HARD_MAX_READ_LENGTH

            if start_line is not None:
                start = _safe_int(start_line, 1)
                line_limit = _safe_int(max_lines, DEFAULT_MAX_LINES)
                byte_limit = _safe_int(max_bytes, DEFAULT_MAX_BYTES)

                lines = text.splitlines(keepends=True)
                selected = lines[start - 1: start - 1 + line_limit]
                chunk = "".join(selected)
                encoded = chunk.encode("utf-8")
                if len(encoded) > byte_limit:
                    chunk = encoded[:byte_limit].decode("utf-8", errors="ignore")
                if len(chunk) > hard_limit:
                    chunk = chunk[:hard_limit]

                returned_chars = len(chunk)
                total_chars_from_start = len("".join(lines[start - 1:])) if start - 1 < len(lines) else 0
                payload = {
                    "ok": True,
                    "mode": "line",
                    "text": chunk,
                    "truncated": returned_chars < total_chars_from_start,
                    "summary": {
                        "path": _to_rel_display(file_path),
                        "start_line": start,
                        "returned_chars": returned_chars,
                        "total_chars_from_start": total_chars_from_start,
                        "hard_limit_chars": hard_limit,
                    },
                }
                return json.dumps(payload, ensure_ascii=False)

            safe_offset = max(0, int(offset or 0))
            requested_length = _safe_int(length, DEFAULT_READ_LENGTH)
            safe_length = min(requested_length, hard_limit)

            if safe_offset >= len(text):
                chunk = ""
            else:
                chunk = text[safe_offset: safe_offset + safe_length]
            truncated = safe_offset + len(chunk) < len(text) or safe_length < requested_length

            payload = {
                "ok": True,
                "mode": "offset",
                "text": chunk,
                "truncated": truncated,
                "summary": {
                    "path": _to_rel_display(file_path),
                    "offset": safe_offset,
                    "requested_length": requested_length,
                    "returned_chars": len(chunk),
                    "total_chars": len(text),
                    "hard_limit_chars": hard_limit,
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
                        "path": {"type": "string", "description": "Relative target file path."},
                        "offset": {"type": "integer", "default": 0, "minimum": 0},
                        "length": {"type": "integer", "default": DEFAULT_READ_LENGTH, "minimum": 1, "maximum": 8000},
                        "start_line": {"type": "integer", "minimum": 1, "description": "Legacy line-based mode."},
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
        self.description = "Write text to a relative file path with workspace guardrails and size limits."

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

            file_path = resolve_allowed_path(path, field_name="path")
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
        self.description = "Get stat metadata for a relative path under workspace/tool_result root."

    async def execute(self, path: str, **kwargs) -> str:
        del kwargs
        if not path:
            return _build_error("path_required")
        try:
            target = resolve_allowed_path(path, field_name="path")
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
