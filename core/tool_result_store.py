from __future__ import annotations

import json
import re
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any

from core.tools.tool import Instance


DEFAULT_TOOL_RESULT_ROOT = "data/tool_results"


@dataclass
class StoredRef:
    id: str
    path: str
    mime: str
    bytes: int
    sha256: str
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class NormalizedToolResult:
    raw_bytes: bytes
    text: str
    mime: str
    ext: str
    is_binary: bool

    @property
    def bytes_size(self) -> int:
        return len(self.raw_bytes)

    @property
    def chars(self) -> int:
        return len(self.text)

    @property
    def lines(self) -> int:
        if not self.text:
            return 0
        return self.text.count("\n") + 1


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sanitize_segment(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", value or "")
    cleaned = cleaned.strip("._")
    return cleaned or "unknown"


def _is_safe_relative_path(raw_path: str) -> bool:
    p = Path(raw_path)
    if p.is_absolute():
        return False
    return not any(part == ".." for part in p.parts)


def _looks_like_json(text: str) -> bool:
    s = (text or "").strip()
    if not s:
        return False
    if not (s.startswith("{") and s.endswith("}")) and not (s.startswith("[") and s.endswith("]")):
        return False
    try:
        json.loads(s)
        return True
    except Exception:
        return False


class ToolResultStore:
    def __init__(self, root_dir: str = DEFAULT_TOOL_RESULT_ROOT):
        if not _is_safe_relative_path(root_dir):
            raise ValueError(f"unsafe_tool_result_root_dir: {root_dir}")
        workspace = Path(Instance.directory).resolve()
        resolved_root = (workspace / Path(root_dir)).resolve()
        if resolved_root != workspace and workspace not in resolved_root.parents:
            raise ValueError(f"tool_result_root_outside_workspace: {root_dir}")
        self._workspace = workspace
        self._root = resolved_root

    @property
    def root_dir(self) -> Path:
        return self._root

    def normalize(self, raw_result: Any) -> NormalizedToolResult:
        if isinstance(raw_result, bytes):
            try:
                text = raw_result.decode("utf-8")
                return NormalizedToolResult(
                    raw_bytes=raw_result,
                    text=text,
                    mime="text/plain; charset=utf-8",
                    ext="txt",
                    is_binary=False,
                )
            except UnicodeDecodeError:
                text = f"<binary {len(raw_result)} bytes>"
                return NormalizedToolResult(
                    raw_bytes=raw_result,
                    text=text,
                    mime="application/octet-stream",
                    ext="bin",
                    is_binary=True,
                )

        if isinstance(raw_result, (dict, list)):
            text = json.dumps(raw_result, ensure_ascii=False)
            return NormalizedToolResult(
                raw_bytes=text.encode("utf-8"),
                text=text,
                mime="application/json",
                ext="json",
                is_binary=False,
            )

        if raw_result is None:
            text = ""
        else:
            text = str(raw_result)

        mime = "application/json" if _looks_like_json(text) else "text/plain; charset=utf-8"
        ext = "json" if mime == "application/json" else "txt"
        return NormalizedToolResult(
            raw_bytes=text.encode("utf-8"),
            text=text,
            mime=mime,
            ext=ext,
            is_binary=False,
        )

    def preview(self, text: str, preview_limit: int) -> tuple[str, bool]:
        if preview_limit < 0:
            preview_limit = 0
        if len(text) <= preview_limit:
            return text, False
        return text[:preview_limit], True

    def persist(
        self,
        *,
        trace_id: str,
        turn_id: str,
        step_id: str,
        tool_call_id: str,
        normalized: NormalizedToolResult,
    ) -> StoredRef:
        trace_key = _sanitize_segment(trace_id)
        turn_key = _sanitize_segment(turn_id)
        step_key = _sanitize_segment(step_id)
        call_key = _sanitize_segment(tool_call_id)
        unique_suffix = uuid.uuid4().hex[:12]
        filename = f"{call_key}_{unique_suffix}.{normalized.ext}"
        file_path = self._root / trace_key / turn_key / step_key / filename
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_bytes(normalized.raw_bytes)

        file_hash = sha256(normalized.raw_bytes).hexdigest()
        created_at = _utc_now_iso()
        rel_path = file_path.resolve().relative_to(self._workspace).as_posix()

        return StoredRef(
            id=f"ref_{file_hash[:16]}",
            path=rel_path,
            mime=normalized.mime,
            bytes=normalized.bytes_size,
            sha256=file_hash,
            created_at=created_at,
        )
