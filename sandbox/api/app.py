from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import signal
import time
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)
from pydantic import BaseModel, Field

DEFAULT_TIMEOUT_MS = int(os.getenv("SANDBOX_EXEC_TIMEOUT_MS", "120000"))
MAX_TIMEOUT_MS = int(os.getenv("SANDBOX_EXEC_MAX_TIMEOUT_MS", "3600000"))
UPLOAD_ROOT = Path(os.getenv("SANDBOX_UPLOAD_ROOT", "/config/uploads")).resolve()
RESULT_ARTIFACT_ROOT = Path(
    os.getenv("SANDBOX_RESULT_ARTIFACT_ROOT", "/config/tool-artifacts/results")
).resolve()
RESULT_PREVIEW_MAX_LINES = max(1, int(os.getenv("SANDBOX_RESULT_PREVIEW_MAX_LINES", "256")))
RESULT_PREVIEW_MAX_BYTES = max(128, int(os.getenv("SANDBOX_RESULT_PREVIEW_MAX_BYTES", "10240")))
RESULT_PREVIEW_HEAD_LINES = max(1, int(os.getenv("SANDBOX_RESULT_PREVIEW_HEAD_LINES", "128")))
RESULT_PREVIEW_TAIL_LINES = max(1, int(os.getenv("SANDBOX_RESULT_PREVIEW_TAIL_LINES", "128")))
AUTO_GRANT_PERMISSIONS_ENV = "SANDBOX_BROWSER_AUTO_GRANT_PERMISSIONS"
AUTO_GRANT_PERMISSIONS_LIST_ENV = "SANDBOX_BROWSER_AUTO_GRANT_PERMISSION_LIST"
DEFAULT_AUTO_GRANT_PERMISSIONS = [
    "geolocation",
    "notifications",
    "camera",
    "microphone",
    "clipboard-read",
    "clipboard-write",
]
SUPPORTED_BROWSER_PERMISSIONS = {
    "geolocation",
    "midi",
    "midi-sysex",
    "notifications",
    "camera",
    "microphone",
    "background-sync",
    "ambient-light-sensor",
    "accelerometer",
    "gyroscope",
    "magnetometer",
    "accessibility-events",
    "clipboard-read",
    "clipboard-write",
    "payment-handler",
    "persistent-storage",
    "idle-detection",
}


def _safe_tool_name(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value or "").strip())
    normalized = normalized.strip("._-")
    return normalized or "tool"


def _artifact_path(tool_name: str, suffix: str) -> Path:
    folder = (RESULT_ARTIFACT_ROOT / _safe_tool_name(tool_name)).resolve()
    folder.mkdir(parents=True, exist_ok=True)
    filename = f"{time.time_ns()}_{os.getpid()}.{suffix}"
    return (folder / filename).resolve()


def _write_text_artifact(tool_name: str, text: str, *, suffix: str = "txt") -> str:
    out_path = _artifact_path(tool_name, suffix=suffix)
    out_path.write_text(text, encoding="utf-8")
    return str(out_path)


def _write_bytes_artifact(tool_name: str, data: bytes, *, suffix: str) -> str:
    out_path = _artifact_path(tool_name, suffix=suffix)
    out_path.write_bytes(data)
    return str(out_path)


def _preview_text(text: str) -> tuple[str, bool, str | None]:
    encoded = text.encode("utf-8", errors="replace")
    byte_count = len(encoded)
    lines = text.splitlines()
    line_count = len(lines)

    hit_line = line_count > RESULT_PREVIEW_MAX_LINES
    hit_byte = byte_count > RESULT_PREVIEW_MAX_BYTES
    if not hit_line and not hit_byte:
        return text, False, None

    line_hit_at = (RESULT_PREVIEW_MAX_LINES + 1) if hit_line else None
    byte_hit_at: int | None = None
    if hit_byte:
        if line_count == 0:
            byte_hit_at = 1
        else:
            running_bytes = 0
            for idx, line in enumerate(lines, start=1):
                running_bytes += len(line.encode("utf-8", errors="replace"))
                if idx < line_count:
                    running_bytes += 1  # account for newline separators
                if running_bytes > RESULT_PREVIEW_MAX_BYTES:
                    byte_hit_at = idx
                    break
            if byte_hit_at is None:
                byte_hit_at = line_count + 1

    prefer_line_mode = bool(
        hit_line
        and (
            not hit_byte
            or (line_hit_at is not None and byte_hit_at is not None and line_hit_at < byte_hit_at)
        )
    )

    if prefer_line_mode:
        head_n = min(RESULT_PREVIEW_HEAD_LINES, line_count)
        max_tail = max(0, line_count - head_n)
        tail_n = min(RESULT_PREVIEW_TAIL_LINES, max_tail)
        head_lines = lines[:head_n]
        tail_lines = lines[-tail_n:] if tail_n > 0 else []
        omitted_lines = max(0, line_count - head_n - tail_n)
        kept_text = "\n".join(head_lines + tail_lines)
        omitted_bytes = max(0, byte_count - len(kept_text.encode("utf-8", errors="replace")))
        marker = f"[... OMITTED MIDDLE: {omitted_lines} lines, {omitted_bytes} bytes ...]"
        merged = head_lines + [marker] + tail_lines
        return "\n".join(merged), True, "line"

    # Byte-only overflow (line count is within limit): keep byte head/tail.
    half = max(1, RESULT_PREVIEW_MAX_BYTES // 2)
    head_text = encoded[:half].decode("utf-8", errors="ignore")
    tail_text = encoded[-half:].decode("utf-8", errors="ignore")
    omitted_bytes = max(0, byte_count - len(encoded[:half]) - len(encoded[-half:]))
    marker = f"[... OMITTED MIDDLE: 0 lines, {omitted_bytes} bytes ...]"
    if tail_text:
        preview = "\n".join([head_text, marker, tail_text])
    else:
        preview = "\n".join([head_text, marker])
    return preview, True, "byte"


def _payload_to_text(payload: Any) -> str:
    if payload is None:
        return ""
    if isinstance(payload, str):
        return payload
    if isinstance(payload, bytes):
        return payload.decode("utf-8", errors="replace")
    if isinstance(payload, list) and all(isinstance(item, str) for item in payload):
        return "\n".join(payload)
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _stream_result_payload(
    tool_name: str,
    stream_name: str,
    content: str,
    *,
    persist_output: bool = False,
) -> dict[str, Any]:
    preview, truncated, by = _preview_text(content)
    payload: dict[str, Any] = {"preview": preview}
    if truncated or persist_output:
        payload["path"] = _write_text_artifact(f"{tool_name}_{stream_name}", content, suffix="txt")
    if truncated:
        payload["truncated"] = True
        payload["by"] = by
    return payload


def _ok_envelope(
    *,
    data: Any,
    artifacts: list[dict[str, Any]] | None = None,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "ok": True,
        "data": data,
        "artifacts": artifacts or [],
        "error": None,
    }
    if meta:
        out["meta"] = meta
    return out


def _subprocess_result_to_envelope(
    payload: dict[str, Any],
    *,
    engine_used: str | None = None,
    fallback_from: str | None = None,
) -> dict[str, Any]:
    data: dict[str, Any] = {
        "timed_out": bool(payload.get("timed_out")),
        "exit_code": int(payload.get("exit_code", -1)),
    }
    if engine_used:
        data["engine_used"] = engine_used
    if fallback_from:
        data["fallback_from"] = fallback_from

    artifacts: list[dict[str, Any]] = []
    for stream_name in ("stdout", "stderr"):
        stream_payload = payload.get(stream_name)
        if not isinstance(stream_payload, dict):
            continue
        artifact: dict[str, Any] = {
            "name": stream_name,
            "kind": "text",
            "preview": str(stream_payload.get("preview", "")),
        }
        path = stream_payload.get("path")
        if isinstance(path, str) and path.strip():
            artifact["path"] = path
        if stream_payload.get("truncated") is True:
            artifact["truncated"] = True
        if isinstance(stream_payload.get("by"), str):
            artifact["by"] = stream_payload.get("by")
        artifacts.append(artifact)

    out = _ok_envelope(data=data, artifacts=artifacts)
    if not bool(payload.get("success")):
        out["ok"] = False
        out["error"] = {
            "type": "subprocess_failed",
            "exit_code": data["exit_code"],
            "timed_out": data["timed_out"],
        }
    return out


def _subprocess_should_fallback_to_grep(payload: dict[str, Any]) -> bool:
    if bool(payload.get("timed_out")):
        return False
    if bool(payload.get("success")):
        return False
    exit_code = int(payload.get("exit_code", -1))
    if exit_code in {2, 127}:
        return True
    stderr = payload.get("stderr") if isinstance(payload.get("stderr"), dict) else {}
    stderr_preview = str((stderr or {}).get("preview", "")).lower()
    fallback_markers = (
        "regex parse error",
        "unsupported",
        "unrecognized flag",
        "pcre",
        "look-around",
        "backreferences",
    )
    return any(marker in stderr_preview for marker in fallback_markers)


def _maybe_externalize_payload(payload: Any, *, tool_name: str) -> Any:
    text = _payload_to_text(payload)
    preview, truncated, by = _preview_text(text)
    if not truncated:
        return payload

    response: dict[str, Any] = {
        "output": {
            "preview": preview,
            "path": _write_text_artifact(tool_name, text, suffix="txt"),
            "truncated": True,
        }
    }
    if by:
        response["output"]["by"] = by
    return response


def _resolve_any_path(raw_path: str | None, base_dir: Path) -> Path:
    if raw_path is None or not str(raw_path).strip():
        return base_dir
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        candidate = base_dir / candidate
    return candidate.resolve()


def _resolve_cwd(raw_cwd: str | None) -> Path:
    if raw_cwd is None or not str(raw_cwd).strip():
        return Path("/").resolve()
    candidate = Path(raw_cwd).expanduser()
    if not candidate.is_absolute():
        candidate = (Path.cwd() / candidate).resolve()
    return candidate.resolve()


def _ensure_cwd(raw_cwd: str | None) -> Path:
    cwd_path = _resolve_cwd(raw_cwd)
    if not cwd_path.exists() or not cwd_path.is_dir():
        raise HTTPException(status_code=400, detail=f"invalid cwd: {cwd_path}")
    return cwd_path


async def _run_subprocess_tool(
    *,
    tool_name: str,
    cwd_path: Path,
    timeout_ms: int,
    env: dict[str, str] | None,
    persist_output: bool,
    success_exit_codes: set[int],
    shell_command: str | None = None,
    argv: list[str] | None = None,
    shell_executable: str | None = None,
) -> dict[str, Any]:
    if bool(shell_command) == bool(argv):
        raise HTTPException(status_code=400, detail="invalid_subprocess_request")

    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)

    spawn_kwargs: dict[str, Any] = {}
    if os.name != "nt":
        spawn_kwargs["preexec_fn"] = os.setsid

    try:
        if shell_command is not None:
            proc = await asyncio.create_subprocess_shell(
                shell_command,
                cwd=str(cwd_path),
                env=merged_env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                executable=shell_executable,
                **spawn_kwargs,
            )
        else:
            assert argv is not None
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(cwd_path),
                env=merged_env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **spawn_kwargs,
            )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"spawn_failed: {exc}") from exc

    timed_out = False
    stdout_bytes = b""
    stderr_bytes = b""
    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(),
            timeout=max(1, int(timeout_ms)) / 1000.0,
        )
    except asyncio.TimeoutError:
        timed_out = True
        await _kill_process_tree(proc)
        stdout_bytes, stderr_bytes = await proc.communicate()

    exit_code = -1 if proc.returncode is None else int(proc.returncode)
    return {
        "success": (not timed_out and exit_code in success_exit_codes),
        "timed_out": timed_out,
        "exit_code": exit_code,
        "stdout": _stream_result_payload(
            tool_name,
            "stdout",
            stdout_bytes.decode("utf-8", errors="replace"),
            persist_output=persist_output,
        ),
        "stderr": _stream_result_payload(
            tool_name,
            "stderr",
            stderr_bytes.decode("utf-8", errors="replace"),
            persist_output=persist_output,
        ),
    }

def _escape_css_attr(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _compact_whitespace(value: str, *, limit: int = 240) -> str:
    compact = " ".join((value or "").split())
    if len(compact) <= limit:
        return compact
    return compact[: max(0, limit - 3)] + "..."


def _env_flag_enabled(name: str, *, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().lower() not in {"0", "false", "no", "off"}


def _normalize_permission_list(raw: str | None) -> list[str]:
    source = raw if raw is not None else ",".join(DEFAULT_AUTO_GRANT_PERMISSIONS)
    normalized: list[str] = []
    seen: set[str] = set()
    for item in str(source).split(","):
        value = item.strip().lower()
        if not value or value in seen:
            continue
        if value not in SUPPORTED_BROWSER_PERMISSIONS:
            continue
        seen.add(value)
        normalized.append(value)
    if normalized:
        return normalized
    return list(DEFAULT_AUTO_GRANT_PERMISSIONS)


def _origin_from_url(raw_url: str) -> str | None:
    parsed = urlsplit(str(raw_url or ""))
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return f"{parsed.scheme}://{parsed.netloc}"


def _flatten_a11y_tree(root: Any, *, max_nodes: int) -> tuple[list[str], int, bool]:
    lines: list[str] = []
    visited = 0
    truncated = False
    hard_limit = max(1, int(max_nodes))

    def _walk(node: Any, depth: int) -> None:
        nonlocal visited, truncated
        if truncated or not isinstance(node, dict):
            return

        visited += 1
        role = _compact_whitespace(str(node.get("role") or ""))
        name = _compact_whitespace(str(node.get("name") or ""))
        value = _compact_whitespace(str(node.get("value") or ""))
        description = _compact_whitespace(str(node.get("description") or ""))
        disabled = bool(node.get("disabled"))
        focused = bool(node.get("focused"))
        checked = node.get("checked")
        selected = node.get("selected")

        parts = [f"role={role or '-'}"]
        if name:
            parts.append(f"name={name}")
        if value:
            parts.append(f"value={value}")
        if description:
            parts.append(f"description={description}")
        if disabled:
            parts.append("disabled=true")
        if focused:
            parts.append("focused=true")
        if isinstance(checked, bool):
            parts.append(f"checked={str(checked).lower()}")
        if isinstance(selected, bool):
            parts.append(f"selected={str(selected).lower()}")

        lines.append(f"{'  ' * max(0, depth)}- " + " | ".join(parts))
        if len(lines) >= hard_limit:
            truncated = True
            return

        children = node.get("children")
        if isinstance(children, list):
            for child in children:
                _walk(child, depth + 1)
                if truncated:
                    break

    _walk(root, 0)
    return lines, visited, truncated


def _cdp_ax_field(node: dict[str, Any], field: str) -> str:
    raw = node.get(field)
    value: Any
    if isinstance(raw, dict):
        value = raw.get("value")
    else:
        value = raw
    if value is None:
        return ""
    return _compact_whitespace(str(value))


def _cdp_ax_properties(node: dict[str, Any]) -> dict[str, str]:
    props = node.get("properties")
    if not isinstance(props, list):
        return {}
    out: dict[str, str] = {}
    for item in props:
        if not isinstance(item, dict):
            continue
        key = str(item.get("name") or "").strip()
        if not key:
            continue
        value_raw = item.get("value")
        value: Any = value_raw
        if isinstance(value_raw, dict):
            value = value_raw.get("value")
        if value is None:
            continue
        out[key] = _compact_whitespace(str(value))
    return out


def _sanitize_a11y_url(url: str) -> str:
    if not url:
        return ""
    compact = _compact_whitespace(url, limit=320)
    if compact.startswith("data:image"):
        return "data:image(<redacted>)"
    return compact


def _format_cdp_a11y_line(
    *,
    uid: str,
    role: str,
    name: str,
    url: str,
    value: str,
    description: str,
    ignored: bool,
) -> str:
    role_token = "ignored" if ignored else (role or "unknown")
    parts = [f"uid={uid}", role_token]
    if name:
        parts.append(json.dumps(name, ensure_ascii=False))
    if url:
        parts.append(f"url={json.dumps(url, ensure_ascii=False)}")
    if value and value != name:
        parts.append(f"value={json.dumps(value, ensure_ascii=False)}")
    if description:
        parts.append(f"description={json.dumps(description, ensure_ascii=False)}")
    return " ".join(parts)


def _build_cdp_a11y_lines(
    nodes: list[dict[str, Any]],
    *,
    interesting_only: bool,
    max_nodes: int,
) -> tuple[list[str], list[dict[str, Any]], int, bool]:
    hard_limit = max(1, int(max_nodes))
    by_id: dict[str, dict[str, Any]] = {}
    for node in nodes:
        if not isinstance(node, dict):
            continue
        node_id = node.get("nodeId")
        if node_id is None:
            continue
        key = str(node_id)
        if key not in by_id:
            by_id[key] = node

    if not by_id:
        return [], [], 0, False

    root_ids: list[str] = []
    for node_id, node in by_id.items():
        parent_id = node.get("parentId")
        parent_key = str(parent_id) if parent_id is not None else ""
        if not parent_key or parent_key not in by_id:
            root_ids.append(node_id)
    if "1" in root_ids:
        root_ids = ["1"] + [item for item in root_ids if item != "1"]

    lines: list[str] = []
    items: list[dict[str, Any]] = []
    visited = 0
    truncated = False
    uid_counter = 0
    seen: set[str] = set()

    def _should_emit(*, role: str, ignored: bool, name: str, value: str, description: str, url: str) -> bool:
        if not interesting_only:
            return True
        role_lc = role.lower()
        if role_lc == "rootwebarea":
            return True
        if ignored:
            return False
        if role_lc in {"none", "generic"} and not any([name, value, description, url]):
            return False
        return True

    def _walk(node_id: str, depth: int, parent_uid: str | None) -> None:
        nonlocal visited, truncated, uid_counter
        if truncated or node_id in seen:
            return
        node = by_id.get(node_id)
        if node is None:
            return
        seen.add(node_id)
        visited += 1

        role = _cdp_ax_field(node, "role")
        name = _cdp_ax_field(node, "name")
        value = _cdp_ax_field(node, "value")
        description = _cdp_ax_field(node, "description")
        ignored = bool(node.get("ignored"))
        props = _cdp_ax_properties(node)
        url = _sanitize_a11y_url(props.get("url", ""))

        emit = _should_emit(
            role=role,
            ignored=ignored,
            name=name,
            value=value,
            description=description,
            url=url,
        )

        next_depth = depth
        current_parent_uid = parent_uid
        if emit:
            if len(items) >= hard_limit:
                truncated = True
                return
            uid = f"1_{uid_counter}"
            uid_counter += 1
            line = _format_cdp_a11y_line(
                uid=uid,
                role=role,
                name=name,
                url=url,
                value=value,
                description=description,
                ignored=ignored,
            )
            lines.append(f"{'  ' * max(0, depth)}{line}")
            items.append(
                {
                    "uid": uid,
                    "nodeId": node_id,
                    "parentUid": parent_uid,
                    "depth": depth,
                    "role": role,
                    "name": name,
                    "value": value,
                    "description": description,
                    "url": url,
                    "ignored": ignored,
                }
            )
            current_parent_uid = uid
            next_depth = depth + 1

        child_ids_raw = node.get("childIds")
        child_ids: list[str] = []
        if isinstance(child_ids_raw, list):
            for child in child_ids_raw:
                child_key = str(child)
                if child_key in by_id:
                    child_ids.append(child_key)

        for child_id in child_ids:
            _walk(child_id, next_depth, current_parent_uid)
            if truncated:
                return

    for root_id in root_ids:
        _walk(root_id, 0, None)
        if truncated:
            return lines, items, visited, True

    # Traverse disconnected nodes if any remain.
    for node_id in by_id.keys():
        if node_id in seen:
            continue
        _walk(node_id, 0, None)
        if truncated:
            return lines, items, visited, True

    return lines, items, visited, False


async def _kill_process_tree(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    if os.name != "nt":
        try:
            os.killpg(proc.pid, signal.SIGKILL)
            return
        except Exception:
            pass
    try:
        proc.kill()
    except Exception:
        pass


class BrowserManager:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._dialog_queue: asyncio.Queue[Any] = asyncio.Queue()
        self._dialog_listener_page: Page | None = None
        self._permission_probe_context_id: int | None = None
        self._auto_grant_permissions = _env_flag_enabled(AUTO_GRANT_PERMISSIONS_ENV, default=True)
        self._auto_grant_permission_list = _normalize_permission_list(
            os.getenv(AUTO_GRANT_PERMISSIONS_LIST_ENV)
        )
        self._permission_grant_context_id: int | None = None
        self._permission_granted_origins: set[str] = set()

        cdp_port = os.getenv("CHROME_REMOTE_DEBUGGING_PORT", "9222").strip() or "9222"
        self._cdp_url = os.getenv("SANDBOX_CHROME_CDP_URL", f"http://127.0.0.1:{cdp_port}")
        self._connect_timeout_sec = max(
            1.0,
            float(os.getenv("SANDBOX_CHROME_CONNECT_TIMEOUT_SEC", "20")),
        )
        self._connect_poll_sec = 0.25

    @staticmethod
    def _permission_probe_init_script() -> str:
        return """
(() => {
  const root = window;
  if (root.__SF_PERMISSION_PROBE_INSTALLED) return;
  root.__SF_PERMISSION_PROBE_INSTALLED = true;
  if (!Array.isArray(root.__SF_PERMISSION_EVENTS)) {
    root.__SF_PERMISSION_EVENTS = [];
  }
  const push = (kind, detail) => {
    try {
      root.__SF_PERMISSION_EVENTS.push({
        ts: Date.now(),
        kind: String(kind || ""),
        detail: String(detail || "").slice(0, 200),
        href: String(location.href || ""),
      });
      const cap = 200;
      if (root.__SF_PERMISSION_EVENTS.length > cap) {
        root.__SF_PERMISSION_EVENTS.splice(0, root.__SF_PERMISSION_EVENTS.length - cap);
      }
    } catch (_) {}
  };
  const wrap = (obj, key, kind) => {
    try {
      if (!obj) return;
      const original = obj[key];
      if (typeof original !== "function") return;
      if (original.__sf_permission_wrapped) return;
      const wrapped = function(...args) {
        push(kind, key);
        return original.apply(this, args);
      };
      wrapped.__sf_permission_wrapped = true;
      obj[key] = wrapped;
    } catch (_) {}
  };

  try {
    if (navigator.geolocation) {
      wrap(navigator.geolocation, "getCurrentPosition", "geolocation");
      wrap(navigator.geolocation, "watchPosition", "geolocation");
    }
  } catch (_) {}
  try {
    if (navigator.mediaDevices) {
      wrap(navigator.mediaDevices, "getUserMedia", "media");
    }
  } catch (_) {}
  try {
    if (window.Notification) {
      wrap(window.Notification, "requestPermission", "notification");
    }
  } catch (_) {}
  try {
    if (navigator.clipboard) {
      wrap(navigator.clipboard, "read", "clipboard");
      wrap(navigator.clipboard, "readText", "clipboard");
      wrap(navigator.clipboard, "write", "clipboard");
      wrap(navigator.clipboard, "writeText", "clipboard");
    }
  } catch (_) {}
})();
"""

    async def _install_permission_probe_locked(self, page: Page) -> None:
        if self._context is not None:
            ctx_id = id(self._context)
            if self._permission_probe_context_id != ctx_id:
                try:
                    await self._context.add_init_script(script=self._permission_probe_init_script())
                    self._permission_probe_context_id = ctx_id
                except Exception:
                    pass
        try:
            await page.evaluate(self._permission_probe_init_script())
        except Exception:
            pass

    async def _grant_auto_permissions_locked(self, page: Page | None = None) -> None:
        if not self._auto_grant_permissions or self._context is None:
            return
        permissions = list(self._auto_grant_permission_list)
        if not permissions:
            return

        ctx_id = id(self._context)
        if self._permission_grant_context_id != ctx_id:
            self._permission_grant_context_id = None
            self._permission_granted_origins = set()
            try:
                await self._context.grant_permissions(permissions)
                self._permission_grant_context_id = ctx_id
            except Exception:
                return

        if page is None:
            return
        origin = _origin_from_url(page.url)
        if not origin or origin in self._permission_granted_origins:
            return
        try:
            await self._context.grant_permissions(permissions, origin=origin)
            self._permission_granted_origins.add(origin)
        except Exception:
            pass

    async def _permission_marker_locked(self, page: Page) -> dict[str, Any]:
        try:
            payload = await page.evaluate(
                """
                async () => {
                  const permissionNames = [
                    "geolocation",
                    "notifications",
                    "camera",
                    "microphone",
                    "clipboard-read",
                    "clipboard-write",
                  ];
                  const states = {};
                  const canQuery = !!(navigator.permissions && navigator.permissions.query);
                  for (const name of permissionNames) {
                    if (!canQuery) {
                      states[name] = "unsupported";
                      continue;
                    }
                    try {
                      const status = await navigator.permissions.query({ name });
                      states[name] = status.state;
                    } catch (_) {
                      states[name] = "unsupported";
                    }
                  }

                  const rawEvents = Array.isArray(window.__SF_PERMISSION_EVENTS)
                    ? window.__SF_PERMISSION_EVENTS
                    : [];
                  const now = Date.now();
                  const recentRequests = rawEvents
                    .filter((item) => item && typeof item.ts === "number" && now - item.ts <= 180000)
                    .slice(-20);

                  const promptPermissions = Object.entries(states)
                    .filter(([_, state]) => state === "prompt")
                    .map(([name]) => name);
                  const deniedPermissions = Object.entries(states)
                    .filter(([_, state]) => state === "denied")
                    .map(([name]) => name);

                  const kinds = Array.from(
                    new Set(
                      recentRequests
                        .map((item) => String(item.kind || ""))
                        .filter((item) => item.length > 0),
                    ),
                  );
                  const expectedKinds = {
                    geolocation: ["geolocation"],
                    notifications: ["notification"],
                    camera: ["media"],
                    microphone: ["media"],
                    "clipboard-read": ["clipboard"],
                    "clipboard-write": ["clipboard"],
                  };

                  const detectedPermissions = [];
                  for (const perm of promptPermissions) {
                    const expected = expectedKinds[perm] || [];
                    if (expected.some((kind) => kinds.includes(kind))) {
                      detectedPermissions.push(perm);
                    }
                  }

                  return {
                    states,
                    prompt_permissions: promptPermissions,
                    denied_permissions: deniedPermissions,
                    recent_requests: recentRequests,
                    permission_prompt_detected: detectedPermissions.length > 0,
                    permission_prompt_permissions: detectedPermissions,
                  };
                }
                """
            )
            if isinstance(payload, dict):
                return payload
        except Exception:
            pass
        return {
            "states": {},
            "prompt_permissions": [],
            "denied_permissions": [],
            "recent_requests": [],
            "permission_prompt_detected": False,
            "permission_prompt_permissions": [],
        }

    async def permission_marker(self) -> dict[str, Any]:
        async with self._lock:
            await self._start_locked()
            page = self._require_page_locked()
            await self._install_permission_probe_locked(page)
            return await self._permission_marker_locked(page)

    async def _connect_browser_locked(self) -> None:
        if self._playwright is None:
            self._playwright = await async_playwright().start()

        deadline = asyncio.get_running_loop().time() + self._connect_timeout_sec
        last_error = "unknown"
        while True:
            try:
                # Connect to existing GUI Chrome. Do not launch any new browser process.
                self._browser = await self._playwright.chromium.connect_over_cdp(self._cdp_url)
                return
            except Exception as exc:
                last_error = str(exc)
                if asyncio.get_running_loop().time() >= deadline:
                    raise HTTPException(
                        status_code=503,
                        detail=f"chrome_cdp_unavailable: {last_error}",
                    ) from exc
                await asyncio.sleep(self._connect_poll_sec)

    @staticmethod
    def _normalize_wait_until(wait_until: str) -> Literal["load", "domcontentloaded", "networkidle", "commit"]:
        allowed = {"load", "domcontentloaded", "networkidle", "commit"}
        normalized = (wait_until or "domcontentloaded").strip().lower()
        if normalized not in allowed:
            return "domcontentloaded"
        return normalized  # type: ignore[return-value]

    async def _safe_page_title(self, page: Page) -> str:
        try:
            return await page.title()
        except Exception:
            return ""

    def _install_dialog_listener_locked(self, page: Page) -> None:
        if self._dialog_listener_page is page:
            return
        self._dialog_listener_page = page

        def _on_dialog(dialog: Any) -> None:
            try:
                self._dialog_queue.put_nowait(dialog)
            except Exception:
                pass

        page.on("dialog", _on_dialog)

    async def _ensure_single_context_locked(self) -> None:
        if self._browser is None:
            raise HTTPException(status_code=500, detail="browser_not_connected")

        contexts = list(self._browser.contexts)
        if not contexts:
            self._context = await self._browser.new_context(ignore_https_errors=True)
            self._page = await self._context.new_page()
            await self._grant_auto_permissions_locked(self._page)
            self._install_dialog_listener_locked(self._page)
            await self._install_permission_probe_locked(self._page)
            return

        primary = contexts[0]
        for extra in contexts[1:]:
            try:
                await extra.close()
            except Exception:
                pass
        self._context = primary

        pages = list(primary.pages)
        if pages:
            self._page = pages[0]
        else:
            self._page = await primary.new_page()
        await self._grant_auto_permissions_locked(self._page)
        self._install_dialog_listener_locked(self._page)
        await self._install_permission_probe_locked(self._page)

    async def _start_locked(self) -> None:
        if self._browser is None:
            await self._connect_browser_locked()

        try:
            await self._ensure_single_context_locked()
        except Exception:
            await self._disconnect_locked()
            await self._connect_browser_locked()
            await self._ensure_single_context_locked()

    def _require_page_locked(self) -> Page:
        if self._page is None:
            raise HTTPException(status_code=400, detail="browser_not_started")
        return self._page

    async def _disconnect_locked(self) -> None:
        self._context = None
        self._page = None
        self._browser = None
        self._dialog_listener_page = None
        self._permission_probe_context_id = None
        self._permission_grant_context_id = None
        self._permission_granted_origins = set()
        while not self._dialog_queue.empty():
            try:
                self._dialog_queue.get_nowait()
            except Exception:
                break
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None

    def _resolve_locator(self, page: Page, *, uid: str | None, selector: str | None):
        if uid and uid.strip():
            safe_uid = _escape_css_attr(uid.strip())
            resolved = f'[data-sf-uid="{safe_uid}"]'
            return page.locator(resolved).first, resolved
        if selector and selector.strip():
            resolved = selector.strip()
            return page.locator(resolved).first, resolved
        raise HTTPException(status_code=400, detail="target_required: provide uid or selector")

    async def _tabs_payload_locked(self) -> dict[str, Any]:
        if self._context is None:
            raise HTTPException(status_code=500, detail="browser_context_unavailable")

        pages = list(self._context.pages)
        active = self._page
        items: list[dict[str, Any]] = []
        active_index = 0 if pages else -1
        for idx, page in enumerate(pages):
            is_active = page is active
            if is_active:
                active_index = idx
            items.append(
                {
                    "index": idx,
                    "url": page.url,
                    "title": await self._safe_page_title(page),
                    "is_active": is_active,
                }
            )

        return {
            "active_index": active_index,
            "tabs": items,
        }

    @staticmethod
    def _cdp_pages_payload(tabs_payload: dict[str, Any]) -> dict[str, Any]:
        tabs = tabs_payload.get("tabs") if isinstance(tabs_payload, dict) else []
        pages: list[dict[str, Any]] = []
        if isinstance(tabs, list):
            for item in tabs:
                if not isinstance(item, dict):
                    continue
                page_id = item.get("index")
                try:
                    page_id = int(page_id)
                except Exception:
                    continue
                pages.append(
                    {
                        "pageId": page_id,
                        "url": item.get("url") or "",
                        "title": item.get("title") or "",
                        "isActive": bool(item.get("is_active")),
                    }
                )
        active_page_id = tabs_payload.get("active_index") if isinstance(tabs_payload, dict) else None
        try:
            active_page_id = int(active_page_id)
        except Exception:
            active_page_id = None
        return {
            "pageCount": len(pages),
            "activePageId": active_page_id,
            "pages": pages,
        }

    async def _ensure_snapshot_uids(self, page: Page, *, max_elements: int) -> dict[str, Any]:
        return await page.evaluate(
            """
            (maxElements) => {
              const root = window;
              if (!root.__SF_UID_SEQ || typeof root.__SF_UID_SEQ !== "number") {
                root.__SF_UID_SEQ = 1;
              }
              const selectors = [
                "a", "button", "input", "textarea", "select", "option",
                "label", "summary", "[role]", "[tabindex]",
                "[contenteditable='true']", "[onclick]"
              ];
              const nodes = Array.from(document.querySelectorAll(selectors.join(",")));
              const elements = [];
              for (const node of nodes) {
                if (!(node instanceof HTMLElement)) continue;
                const style = window.getComputedStyle(node);
                const rect = node.getBoundingClientRect();
                const visible = rect.width > 0 && rect.height > 0 &&
                  style.display !== "none" && style.visibility !== "hidden";
                if (!visible) continue;

                let uid = node.getAttribute("data-sf-uid");
                if (!uid) {
                  uid = `sf-${root.__SF_UID_SEQ++}`;
                  node.setAttribute("data-sf-uid", uid);
                }

                const text = ((node.innerText || node.textContent || "").trim()).replace(/\\\\s+/g, " ").slice(0, 200);
                const ariaLabel = (node.getAttribute("aria-label") || "").trim();
                const placeholder = (node.getAttribute("placeholder") || "").trim();
                const role = (node.getAttribute("role") || "").trim();
                const tag = node.tagName.toLowerCase();
                const href = tag === "a" ? (node.getAttribute("href") || "") : "";

                elements.push({
                  uid,
                  tag,
                  role,
                  text,
                  aria_label: ariaLabel,
                  placeholder,
                  id: node.id || "",
                  name: node.getAttribute("name") || "",
                  href,
                  x: Math.round(rect.x),
                  y: Math.round(rect.y),
                  width: Math.round(rect.width),
                  height: Math.round(rect.height),
                });
              }
              return {
                url: location.href,
                title: document.title || "",
                viewport: { width: window.innerWidth, height: window.innerHeight },
                element_count: elements.length,
                elements: elements.slice(0, Math.max(1, maxElements)),
              };
            }
            """,
            max(1, min(int(max_elements), 2000)),
        )

    async def _a11y_snapshot_payload(
        self,
        page: Page,
        *,
        interesting_only: bool,
        max_nodes: int,
    ) -> list[str]:
        # Prefer CDP AX tree so text output stays aligned with cdp-mcp style.
        if self._context is None:
            raise HTTPException(status_code=500, detail="browser_context_unavailable")

        try:
            cdp = await self._context.new_cdp_session(page)
            try:
                raw = await cdp.send("Accessibility.getFullAXTree")
            finally:
                with suppress(Exception):
                    await cdp.detach()
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"a11y_snapshot_unavailable: {exc}") from exc

        nodes_raw = raw.get("nodes") if isinstance(raw, dict) else []
        nodes: list[dict[str, Any]] = (
            [node for node in nodes_raw if isinstance(node, dict)] if isinstance(nodes_raw, list) else []
        )
        lines, _, _, _ = _build_cdp_a11y_lines(
            nodes,
            interesting_only=bool(interesting_only),
            max_nodes=max_nodes,
        )
        return lines

    async def state(self) -> dict[str, Any]:
        async with self._lock:
            await self._start_locked()
            return await self._tabs_payload_locked()

    async def navigate(self, *, url: str, timeout_ms: int, wait_until: str) -> dict[str, Any]:
        return await self.navigate_page(
            action="url",
            url=url,
            timeout_ms=timeout_ms,
            wait_until=wait_until,
        )

    async def navigate_back(self, *, timeout_ms: int, wait_until: str) -> dict[str, Any]:
        return await self.navigate_page(
            action="back",
            url=None,
            timeout_ms=timeout_ms,
            wait_until=wait_until,
        )

    async def navigate_page(
        self,
        *,
        action: Literal["url", "back", "forward", "reload"],
        url: str | None,
        timeout_ms: int,
        wait_until: str,
    ) -> dict[str, Any]:
        async with self._lock:
            await self._start_locked()
            page = self._require_page_locked()
            resolved_wait = self._normalize_wait_until(wait_until)

            if action == "url":
                target_url = str(url or "").strip()
                if not target_url:
                    raise HTTPException(status_code=400, detail="navigate_page_url_required")
                await page.goto(target_url, timeout=timeout_ms, wait_until=resolved_wait)
                await self._grant_auto_permissions_locked(page)
                return {
                    "action": "url",
                    "url": page.url,
                }

            if action == "back":
                response = await page.go_back(timeout=timeout_ms, wait_until=resolved_wait)
                await self._grant_auto_permissions_locked(page)
                return {
                    "action": "back",
                    "navigated": response is not None,
                    "url": page.url,
                }

            if action == "forward":
                response = await page.go_forward(timeout=timeout_ms, wait_until=resolved_wait)
                await self._grant_auto_permissions_locked(page)
                return {
                    "action": "forward",
                    "navigated": response is not None,
                    "url": page.url,
                }

            response = await page.reload(timeout=timeout_ms, wait_until=resolved_wait)
            await self._grant_auto_permissions_locked(page)
            return {
                "action": "reload",
                "reloaded": response is not None,
                "url": page.url,
            }

    async def list_pages(self) -> dict[str, Any]:
        async with self._lock:
            await self._start_locked()
            tabs_payload = await self._tabs_payload_locked()
            return self._cdp_pages_payload(tabs_payload)

    async def new_page(
        self,
        *,
        url: str,
        timeout_ms: int,
        wait_until: str,
        background: bool,
    ) -> dict[str, Any]:
        async with self._lock:
            await self._start_locked()
            if self._context is None:
                raise HTTPException(status_code=500, detail="browser_context_unavailable")

            target_url = str(url or "").strip()
            if not target_url:
                raise HTTPException(status_code=400, detail="new_page_url_required")

            previous_active = self._page
            resolved_wait = self._normalize_wait_until(wait_until)
            page = await self._context.new_page()
            self._install_dialog_listener_locked(page)
            await page.goto(target_url, timeout=timeout_ms, wait_until=resolved_wait)
            await self._grant_auto_permissions_locked(page)

            if not background:
                self._page = page
                await page.bring_to_front()
            elif previous_active is not None:
                self._page = previous_active

            pages_now = list(self._context.pages)
            created_page_id = pages_now.index(page) if page in pages_now else None
            tabs_payload = await self._tabs_payload_locked()
            return {
                "pageId": created_page_id,
                **self._cdp_pages_payload(tabs_payload),
            }

    async def select_page(self, *, page_id: int, bring_to_front: bool) -> dict[str, Any]:
        async with self._lock:
            await self._start_locked()
            if self._context is None:
                raise HTTPException(status_code=500, detail="browser_context_unavailable")

            pages = list(self._context.pages)
            if page_id < 0 or page_id >= len(pages):
                raise HTTPException(status_code=404, detail=f"page_id_out_of_range: {page_id}")
            page = pages[page_id]
            self._page = page
            self._install_dialog_listener_locked(page)
            await self._grant_auto_permissions_locked(page)
            if bring_to_front:
                await page.bring_to_front()

            tabs_payload = await self._tabs_payload_locked()
            return self._cdp_pages_payload(tabs_payload)

    async def close_page(self, *, page_id: int) -> dict[str, Any]:
        async with self._lock:
            await self._start_locked()
            if self._context is None:
                raise HTTPException(status_code=500, detail="browser_context_unavailable")

            pages = list(self._context.pages)
            if len(pages) <= 1:
                raise HTTPException(status_code=400, detail="cannot_close_last_page")
            if page_id < 0 or page_id >= len(pages):
                raise HTTPException(status_code=404, detail=f"page_id_out_of_range: {page_id}")

            target = pages[page_id]
            active_page = self._page
            await target.close()
            remaining = list(self._context.pages)
            if not remaining:
                self._page = await self._context.new_page()
            elif active_page is target or active_page not in remaining:
                fallback = min(page_id, len(remaining) - 1)
                self._page = remaining[fallback]
                await self._page.bring_to_front()
            else:
                self._page = active_page
            self._install_dialog_listener_locked(self._page)

            tabs_payload = await self._tabs_payload_locked()
            return self._cdp_pages_payload(tabs_payload)

    async def click(
        self,
        *,
        uid: str | None,
        selector: str | None,
        button: Literal["left", "right", "middle"],
        click_count: int,
        timeout_ms: int,
    ) -> dict[str, Any]:
        async with self._lock:
            await self._start_locked()
            page = self._require_page_locked()
            locator, target = self._resolve_locator(page, uid=uid, selector=selector)
            clicks = max(1, click_count)
            pending_before = self._dialog_queue.qsize()

            def _ok_response(**extra: Any) -> dict[str, Any]:
                payload = {
                    "target": target,
                    "dialog_pending": self._dialog_queue.qsize() > pending_before,
                }
                payload.update(extra)
                return payload

            # For alert/prompt/confirm flows, Playwright click can remain blocked until
            # the modal is handled. Run click as a task and short-circuit as soon as a
            # new dialog is observed.
            click_task = asyncio.create_task(
                locator.click(
                    button=button,
                    click_count=clicks,
                    timeout=timeout_ms,
                    no_wait_after=True,
                )
            )

            while not click_task.done():
                if self._dialog_queue.qsize() > pending_before:
                    click_task.cancel()
                    # Best-effort cleanup: avoid waiting for the full click timeout.
                    with suppress(asyncio.CancelledError, asyncio.TimeoutError, PlaywrightTimeoutError):
                        await asyncio.wait_for(click_task, timeout=0.2)
                    return _ok_response(
                        dialog_pending=True,
                        click_short_circuited=True,
                        reason="dialog_detected_early",
                    )
                await asyncio.sleep(0.02)

            try:
                await click_task
                return _ok_response()
            except PlaywrightTimeoutError:
                if self._dialog_queue.qsize() > pending_before:
                    return _ok_response(
                        dialog_pending=True,
                        click_timeout_ignored=True,
                        reason="dialog_blocked_click_completion",
                    )
                raise

    async def hover(self, *, uid: str | None, selector: str | None, timeout_ms: int) -> dict[str, Any]:
        async with self._lock:
            await self._start_locked()
            page = self._require_page_locked()
            locator, target = self._resolve_locator(page, uid=uid, selector=selector)
            await locator.hover(timeout=timeout_ms)
            return {"target": target}

    async def drag(
        self,
        *,
        from_uid: str | None,
        from_selector: str | None,
        to_uid: str | None,
        to_selector: str | None,
        timeout_ms: int,
    ) -> dict[str, Any]:
        async with self._lock:
            await self._start_locked()
            page = self._require_page_locked()
            source, source_target = self._resolve_locator(page, uid=from_uid, selector=from_selector)
            target, target_target = self._resolve_locator(page, uid=to_uid, selector=to_selector)
            await source.drag_to(target, timeout=timeout_ms)
            return {"from": source_target, "to": target_target}

    async def evaluate(self, *, expression: str, arg: Any) -> Any:
        async with self._lock:
            await self._start_locked()
            page = self._require_page_locked()
            return await page.evaluate(expression, arg)

    async def type_text(
        self,
        *,
        text: str,
        uid: str | None,
        selector: str | None,
        clear_before: bool,
        delay_ms: int,
        timeout_ms: int,
    ) -> dict[str, Any]:
        async with self._lock:
            await self._start_locked()
            page = self._require_page_locked()
            if (uid and uid.strip()) or (selector and selector.strip()):
                locator, target = self._resolve_locator(page, uid=uid, selector=selector)
                if clear_before:
                    await locator.fill("", timeout=timeout_ms)
                await locator.type(text, delay=max(0, delay_ms), timeout=timeout_ms)
                return {
                    "typed": len(text),
                    "target": target,
                }

            await page.keyboard.type(text, delay=max(0, delay_ms))
            return {
                "typed": len(text),
                "target": "active_element",
            }

    async def type_text_alias(
        self,
        *,
        text: str,
        submit_key: str | None,
        delay_ms: int,
    ) -> dict[str, Any]:
        async with self._lock:
            await self._start_locked()
            page = self._require_page_locked()
            await page.keyboard.type(text, delay=max(0, int(delay_ms)))
            submitted = None
            if isinstance(submit_key, str) and submit_key.strip():
                submitted = submit_key.strip()
                await page.keyboard.press(submitted)
            return {
                "typed": len(text),
                "submitKey": submitted,
            }

    async def fill(self, *, uid: str | None, selector: str | None, value: str, timeout_ms: int) -> dict[str, Any]:
        async with self._lock:
            await self._start_locked()
            page = self._require_page_locked()
            locator, target = self._resolve_locator(page, uid=uid, selector=selector)
            await locator.fill(value, timeout=timeout_ms)
            return {"target": target}

    async def fill_form(
        self,
        *,
        fields: list[dict[str, Any]],
        timeout_ms: int,
        submit: bool,
    ) -> dict[str, Any]:
        async with self._lock:
            await self._start_locked()
            page = self._require_page_locked()
            applied: list[dict[str, Any]] = []
            for item in fields:
                uid = str(item.get("uid") or "").strip() or None
                selector = str(item.get("selector") or "").strip() or None
                value = str(item.get("value") or "")
                locator, target = self._resolve_locator(page, uid=uid, selector=selector)
                await locator.fill(value, timeout=timeout_ms)
                applied.append({"target": target, "length": len(value)})
            if submit:
                await page.keyboard.press("Enter")
            return {
                "filled_count": len(applied),
                "submit": submit,
            }

    async def press_key(
        self,
        *,
        key: str,
        uid: str | None,
        selector: str | None,
        timeout_ms: int,
    ) -> dict[str, Any]:
        async with self._lock:
            await self._start_locked()
            page = self._require_page_locked()
            if (uid and uid.strip()) or (selector and selector.strip()):
                locator, target = self._resolve_locator(page, uid=uid, selector=selector)
                await locator.press(key, timeout=timeout_ms)
                return {"pressed": key, "target": target}
            await page.keyboard.press(key)
            return {"pressed": key, "target": "active_element"}

    async def select_option(
        self,
        *,
        uid: str | None,
        selector: str | None,
        values: list[str],
        labels: list[str],
        indexes: list[int],
        timeout_ms: int,
    ) -> dict[str, Any]:
        if not values and not labels and not indexes:
            raise HTTPException(status_code=400, detail="select_option_requires_values_labels_or_indexes")
        async with self._lock:
            await self._start_locked()
            page = self._require_page_locked()
            locator, target = self._resolve_locator(page, uid=uid, selector=selector)
            selected = await locator.select_option(
                value=values or None,
                label=labels or None,
                index=indexes or None,
                timeout=timeout_ms,
            )
            return {"target": target, "selected": selected}

    async def wait_for(
        self,
        *,
        texts: list[str],
        uid: str | None,
        selector: str | None,
        state: Literal["visible", "hidden", "attached", "detached"],
        timeout_ms: int,
    ) -> dict[str, Any]:
        async with self._lock:
            await self._start_locked()
            page = self._require_page_locked()

            if (uid and uid.strip()) or (selector and selector.strip()):
                locator, target = self._resolve_locator(page, uid=uid, selector=selector)
                await locator.wait_for(state=state, timeout=timeout_ms)
                return {"matched": target, "state": state}

            candidates = [item for item in texts if isinstance(item, str) and item.strip()]
            if candidates:
                await page.wait_for_function(
                    """
                    (needles) => {
                      const body = (document.body && document.body.innerText) || "";
                      return needles.some((needle) => body.includes(needle));
                    }
                    """,
                    arg=candidates,
                    timeout=timeout_ms,
                )
                matched = await page.evaluate(
                    """
                    (needles) => {
                      const body = (document.body && document.body.innerText) || "";
                      return needles.find((needle) => body.includes(needle)) || null;
                    }
                    """,
                    candidates,
                )
                return {"matched": matched}

            raise HTTPException(status_code=400, detail="wait_for_requires_text_or_target")

    async def take_snapshot(
        self,
        *,
        verbose: bool,
        file_path: str | None,
        preview_lines: int,
    ) -> list[str]:
        del file_path, preview_lines
        return await self.snapshot(
            mode="a11y",
            max_elements=200,
            max_nodes=3000 if verbose else 800,
            interesting_only=not bool(verbose),
            preview_lines=0,
            output_path=None,
        )

    async def snapshot(
        self,
        *,
        mode: Literal["a11y", "dom"],
        max_elements: int,
        max_nodes: int,
        interesting_only: bool,
        preview_lines: int,
        output_path: str | None,
    ) -> Any:
        del preview_lines, output_path
        async with self._lock:
            await self._start_locked()
            page = self._require_page_locked()
            normalized_mode = "dom" if mode == "dom" else "a11y"

            if normalized_mode == "dom":
                payload = await self._ensure_snapshot_uids(page, max_elements=max_elements)
                return payload.get("elements") or []

            return await self._a11y_snapshot_payload(
                page,
                interesting_only=interesting_only,
                max_nodes=max_nodes,
            )

    async def take_screenshot(
        self,
        *,
        uid: str | None,
        selector: str | None,
        output_path: str | None,
        full_page: bool,
        image_format: Literal["png", "jpeg", "webp"],
        quality: int | None,
    ) -> dict[str, Any]:
        del output_path
        suffix_map = {"png": "png", "jpeg": "jpg", "webp": "webp"}
        mime_map = {"png": "image/png", "jpeg": "image/jpeg", "webp": "image/webp"}
        async with self._lock:
            await self._start_locked()
            page = self._require_page_locked()
            kwargs: dict[str, Any] = {
                "type": image_format,
            }
            if quality is not None and image_format in {"jpeg", "webp"}:
                kwargs["quality"] = max(0, min(int(quality), 100))

            if (uid and uid.strip()) or (selector and selector.strip()):
                locator, _ = self._resolve_locator(page, uid=uid, selector=selector)
                image_bytes = await locator.screenshot(**kwargs)
            else:
                kwargs["full_page"] = bool(full_page)
                image_bytes = await page.screenshot(**kwargs)

            path = _write_bytes_artifact(
                "browser_take_screenshot",
                image_bytes,
                suffix=suffix_map.get(image_format, "bin"),
            )
            return {
                "path": path,
                "mime": mime_map.get(image_format, "application/octet-stream"),
                "bytes": len(image_bytes),
            }

    async def tabs(
        self,
        *,
        action: Literal["list", "new", "activate", "close", "close_others"],
        index: int | None,
        url: str | None,
        timeout_ms: int,
        wait_until: str,
    ) -> dict[str, Any]:
        async with self._lock:
            await self._start_locked()
            if self._context is None:
                raise HTTPException(status_code=500, detail="browser_context_unavailable")

            resolved_wait = self._normalize_wait_until(wait_until)
            pages = list(self._context.pages)
            active_page = self._page
            active_index = 0
            for idx, page in enumerate(pages):
                if page is active_page:
                    active_index = idx
                    break

            if action == "new":
                new_page = await self._context.new_page()
                self._page = new_page
                self._install_dialog_listener_locked(new_page)
                await new_page.bring_to_front()
                if url:
                    await new_page.goto(url, timeout=timeout_ms, wait_until=resolved_wait)

            elif action == "activate":
                if index is None:
                    raise HTTPException(status_code=400, detail="tabs_activate_requires_index")
                if index < 0 or index >= len(pages):
                    raise HTTPException(status_code=404, detail=f"tab_index_out_of_range: {index}")
                self._page = pages[index]
                self._install_dialog_listener_locked(self._page)
                await self._page.bring_to_front()

            elif action == "close":
                if not pages:
                    raise HTTPException(status_code=400, detail="no_tabs_to_close")
                target_index = index if index is not None else active_index
                if target_index < 0 or target_index >= len(pages):
                    raise HTTPException(status_code=404, detail=f"tab_index_out_of_range: {target_index}")
                target_page = pages[target_index]
                await target_page.close()
                remaining = list(self._context.pages)
                if not remaining:
                    self._page = await self._context.new_page()
                else:
                    fallback_index = min(target_index, len(remaining) - 1)
                    self._page = remaining[fallback_index]
                self._install_dialog_listener_locked(self._page)
                await self._page.bring_to_front()

            elif action == "close_others":
                if not pages:
                    raise HTTPException(status_code=400, detail="no_tabs_to_close")
                keep_index = index if index is not None else active_index
                if keep_index < 0 or keep_index >= len(pages):
                    raise HTTPException(status_code=404, detail=f"tab_index_out_of_range: {keep_index}")
                keep_page = pages[keep_index]
                for page in pages:
                    if page is keep_page:
                        continue
                    await page.close()
                self._page = keep_page
                self._install_dialog_listener_locked(self._page)
                await self._page.bring_to_front()

            elif action != "list":
                raise HTTPException(status_code=400, detail=f"unsupported_tabs_action: {action}")

            return await self._tabs_payload_locked()

    async def mouse_click_xy(
        self,
        *,
        x: float,
        y: float,
        button: Literal["left", "right", "middle"],
        click_count: int,
        delay_ms: int,
    ) -> dict[str, Any]:
        async with self._lock:
            await self._start_locked()
            page = self._require_page_locked()
            await page.mouse.click(
                x=float(x),
                y=float(y),
                button=button,
                click_count=max(1, int(click_count)),
                delay=max(0, int(delay_ms)),
            )
            return {"clicked": {"x": float(x), "y": float(y)}}

    async def mouse_down(self, *, button: Literal["left", "right", "middle"]) -> dict[str, Any]:
        async with self._lock:
            await self._start_locked()
            page = self._require_page_locked()
            await page.mouse.down(button=button)
            return {"mouse_down": True}

    async def mouse_move_xy(self, *, x: float, y: float, steps: int) -> dict[str, Any]:
        async with self._lock:
            await self._start_locked()
            page = self._require_page_locked()
            await page.mouse.move(x=float(x), y=float(y), steps=max(1, int(steps)))
            return {"moved_to": {"x": float(x), "y": float(y)}}

    async def mouse_drag_xy(
        self,
        *,
        start_x: float,
        start_y: float,
        end_x: float,
        end_y: float,
        steps: int,
        button: Literal["left", "right", "middle"],
    ) -> dict[str, Any]:
        async with self._lock:
            await self._start_locked()
            page = self._require_page_locked()
            await page.mouse.move(x=float(start_x), y=float(start_y))
            await page.mouse.down(button=button)
            await page.mouse.move(x=float(end_x), y=float(end_y), steps=max(1, int(steps)))
            await page.mouse.up(button=button)
            return {
                "dragged": {
                    "start": {"x": float(start_x), "y": float(start_y)},
                    "end": {"x": float(end_x), "y": float(end_y)},
                },
            }

    async def mouse_up(self, *, button: Literal["left", "right", "middle"]) -> dict[str, Any]:
        async with self._lock:
            await self._start_locked()
            page = self._require_page_locked()
            await page.mouse.up(button=button)
            return {"mouse_up": True}

    async def mouse_wheel(self, *, delta_x: float, delta_y: float) -> dict[str, Any]:
        async with self._lock:
            await self._start_locked()
            page = self._require_page_locked()
            await page.mouse.wheel(delta_x=float(delta_x), delta_y=float(delta_y))
            return {"wheel": {"delta_x": float(delta_x), "delta_y": float(delta_y)}}

    async def file_upload(
        self,
        *,
        uid: str | None,
        selector: str | None,
        files: list[str],
        timeout_ms: int,
    ) -> dict[str, Any]:
        normalized: list[str] = []
        for item in files:
            if not isinstance(item, str) or not item.strip():
                continue
            file_path = Path(item).expanduser().resolve()
            if not file_path.exists() or not file_path.is_file():
                raise HTTPException(status_code=400, detail=f"file_not_found: {file_path}")
            normalized.append(str(file_path))
        if not normalized:
            raise HTTPException(status_code=400, detail="files_required")

        async with self._lock:
            await self._start_locked()
            page = self._require_page_locked()
            locator, target = self._resolve_locator(page, uid=uid, selector=selector)
            await locator.set_input_files(normalized, timeout=timeout_ms)
            return {"target": target, "files": normalized}

    async def upload_file(
        self,
        *,
        uid: str | None,
        selector: str | None,
        file_path: str,
        timeout_ms: int,
    ) -> dict[str, Any]:
        return await self.file_upload(
            uid=uid,
            selector=selector,
            files=[file_path],
            timeout_ms=timeout_ms,
        )

    async def handle_dialog(
        self,
        *,
        action: Literal["accept", "dismiss"],
        prompt_text: str | None,
        timeout_ms: int,
    ) -> dict[str, Any]:
        async with self._lock:
            await self._start_locked()
            self._require_page_locked()

            dialog = None
            while not self._dialog_queue.empty():
                try:
                    dialog = self._dialog_queue.get_nowait()
                except Exception:
                    break

            if dialog is None:
                try:
                    dialog = await asyncio.wait_for(
                        self._dialog_queue.get(),
                        timeout=max(1, timeout_ms) / 1000.0,
                    )
                except asyncio.TimeoutError as exc:
                    raise HTTPException(status_code=408, detail="dialog_not_found") from exc

            payload = {
                "type": getattr(dialog, "type", "unknown"),
                "message": getattr(dialog, "message", ""),
                "default_value": getattr(dialog, "default_value", ""),
                "action": action,
            }
            if action == "accept":
                await dialog.accept(prompt_text if prompt_text is not None else "")
            else:
                await dialog.dismiss()
            return payload

    async def close(self) -> dict[str, Any]:
        async with self._lock:
            was_connected = self._browser is not None
            await self._disconnect_locked()
            return {"closed": was_connected}

    async def shutdown(self) -> None:
        async with self._lock:
            await self._disconnect_locked()


browser_manager = BrowserManager()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)
    RESULT_ARTIFACT_ROOT.mkdir(parents=True, exist_ok=True)
    yield
    await browser_manager.shutdown()


app = FastAPI(title="StewardFlow Sandbox API", version="0.3.0", lifespan=lifespan)


class BashRequest(BaseModel):
    command: str = Field(..., min_length=1, description="Shell command string.")
    cwd: str | None = Field(default=None, description="Working directory. Supports absolute or relative path.")
    timeout_ms: int = Field(default=DEFAULT_TIMEOUT_MS, ge=1, le=MAX_TIMEOUT_MS)
    env: dict[str, str] | None = Field(default=None, description="Extra environment variables.")
    shell_executable: str | None = Field(default=None, description="Optional shell executable path.")
    persist_output: bool = Field(default=False, description="Persist stdout/stderr to artifact path even if not truncated.")


class GlobRequest(BaseModel):
    pattern: str = Field(..., min_length=1)
    path: str = Field(default=".")
    include_hidden: bool = False
    cwd: str | None = Field(default=None, description="Working directory. Supports absolute or relative path.")
    timeout_ms: int = Field(default=DEFAULT_TIMEOUT_MS, ge=1, le=MAX_TIMEOUT_MS)
    persist_output: bool = Field(default=False, description="Persist stdout/stderr to artifact path even if not truncated.")


class ReadRequest(BaseModel):
    path: str = Field(..., min_length=1)
    start_line: int = Field(default=1, ge=1)
    end_line: int | None = Field(default=None, ge=1)
    cwd: str | None = Field(default=None, description="Working directory. Supports absolute or relative path.")
    timeout_ms: int = Field(default=DEFAULT_TIMEOUT_MS, ge=1, le=MAX_TIMEOUT_MS)
    persist_output: bool = Field(default=False, description="Persist stdout/stderr to artifact path even if not truncated.")


class SearchRequest(BaseModel):
    pattern: str = Field(..., min_length=1)
    path: str = Field(default=".")
    engine_hint: Literal["auto", "rg", "grep"] = "auto"
    glob: str | None = None
    pcre2: bool = False
    ignore_case: bool = False
    recursive: bool = True
    line_number: bool = True
    max_count: int | None = Field(default=None, ge=1)
    cwd: str | None = Field(default=None, description="Working directory. Supports absolute or relative path.")
    timeout_ms: int = Field(default=DEFAULT_TIMEOUT_MS, ge=1, le=MAX_TIMEOUT_MS)
    persist_output: bool = Field(default=False, description="Persist stdout/stderr to artifact path even if not truncated.")


class BrowserNavigateRequest(BaseModel):
    url: str
    timeout_ms: int = Field(default=30000, ge=1, le=300000)
    wait_until: str = Field(default="domcontentloaded")


class BrowserNavigateBackRequest(BaseModel):
    timeout_ms: int = Field(default=30000, ge=1, le=300000)
    wait_until: str = Field(default="domcontentloaded")


class BrowserNavigatePageRequest(BaseModel):
    type: Literal["url", "back", "forward", "reload"] = "url"
    url: str | None = None
    timeout_ms: int = Field(default=30000, ge=1, le=300000)
    wait_until: str = Field(default="domcontentloaded")


class BrowserListPagesRequest(BaseModel):
    pass


class BrowserNewPageRequest(BaseModel):
    url: str
    timeout_ms: int = Field(default=30000, ge=1, le=300000)
    wait_until: str = Field(default="domcontentloaded")
    background: bool = False


class BrowserSelectPageRequest(BaseModel):
    pageId: int = Field(ge=0)
    bringToFront: bool = False


class BrowserClosePageRequest(BaseModel):
    pageId: int = Field(ge=0)


class BrowserClickRequest(BaseModel):
    uid: str | None = None
    selector: str | None = None
    button: Literal["left", "right", "middle"] = "left"
    click_count: int = Field(default=1, ge=1, le=10)
    timeout_ms: int = Field(default=30000, ge=1, le=300000)


class BrowserHoverRequest(BaseModel):
    uid: str | None = None
    selector: str | None = None
    timeout_ms: int = Field(default=30000, ge=1, le=300000)


class BrowserDragRequest(BaseModel):
    from_uid: str | None = None
    from_selector: str | None = None
    to_uid: str | None = None
    to_selector: str | None = None
    timeout_ms: int = Field(default=30000, ge=1, le=300000)


class BrowserEvaluateRequest(BaseModel):
    expression: str
    arg: Any = None


class BrowserFileUploadRequest(BaseModel):
    uid: str | None = None
    selector: str | None = None
    files: list[str] = Field(default_factory=list, min_length=1)
    timeout_ms: int = Field(default=30000, ge=1, le=300000)


class BrowserFormField(BaseModel):
    uid: str | None = None
    selector: str | None = None
    value: str


class BrowserFillFormRequest(BaseModel):
    fields: list[BrowserFormField] = Field(default_factory=list, min_length=1)
    timeout_ms: int = Field(default=30000, ge=1, le=300000)
    submit: bool = False


class BrowserHandleDialogRequest(BaseModel):
    action: Literal["accept", "dismiss"] = "accept"
    prompt_text: str | None = None
    timeout_ms: int = Field(default=10000, ge=1, le=300000)


class BrowserPressKeyRequest(BaseModel):
    key: str
    uid: str | None = None
    selector: str | None = None
    timeout_ms: int = Field(default=30000, ge=1, le=300000)


class BrowserSelectOptionRequest(BaseModel):
    uid: str | None = None
    selector: str | None = None
    values: list[str] = Field(default_factory=list)
    labels: list[str] = Field(default_factory=list)
    indexes: list[int] = Field(default_factory=list)
    timeout_ms: int = Field(default=30000, ge=1, le=300000)


class BrowserSnapshotRequest(BaseModel):
    mode: Literal["a11y", "dom"] = "a11y"
    interesting_only: bool = True
    max_nodes: int = Field(default=800, ge=1, le=10000)
    preview_lines: int = Field(default=40, ge=0, le=200)
    max_elements: int = Field(default=200, ge=1, le=2000)


class BrowserTakeSnapshotRequest(BaseModel):
    verbose: bool = False
    preview_lines: int = Field(default=40, ge=0, le=200)


class BrowserTakeScreenshotRequest(BaseModel):
    uid: str | None = None
    selector: str | None = None
    full_page: bool = True
    format: Literal["png", "jpeg", "webp"] = "png"
    quality: int | None = Field(default=None, ge=0, le=100)


class BrowserTypeRequest(BaseModel):
    text: str
    uid: str | None = None
    selector: str | None = None
    clear_before: bool = False
    delay_ms: int = Field(default=0, ge=0, le=2000)
    timeout_ms: int = Field(default=30000, ge=1, le=300000)


class BrowserTypeTextRequest(BaseModel):
    text: str
    submitKey: str | None = None
    delay_ms: int = Field(default=0, ge=0, le=2000)


class BrowserFillRequest(BaseModel):
    uid: str | None = None
    selector: str | None = None
    value: str
    timeout_ms: int = Field(default=30000, ge=1, le=300000)


class BrowserUploadFileRequest(BaseModel):
    uid: str | None = None
    selector: str | None = None
    filePath: str
    timeout_ms: int = Field(default=30000, ge=1, le=300000)


class BrowserWaitForRequest(BaseModel):
    text: list[str] = Field(default_factory=list)
    uid: str | None = None
    selector: str | None = None
    state: Literal["visible", "hidden", "attached", "detached"] = "visible"
    timeout_ms: int = Field(default=30000, ge=1, le=300000)


class BrowserTabsRequest(BaseModel):
    action: Literal["list", "new", "activate", "close", "close_others"] = "list"
    index: int | None = Field(default=None, ge=0)
    url: str | None = None
    timeout_ms: int = Field(default=30000, ge=1, le=300000)
    wait_until: str = Field(default="domcontentloaded")


class BrowserMouseClickXYRequest(BaseModel):
    x: float
    y: float
    button: Literal["left", "right", "middle"] = "left"
    click_count: int = Field(default=1, ge=1, le=10)
    delay_ms: int = Field(default=0, ge=0, le=5000)


class BrowserMouseDownRequest(BaseModel):
    button: Literal["left", "right", "middle"] = "left"


class BrowserMouseDragXYRequest(BaseModel):
    start_x: float
    start_y: float
    end_x: float
    end_y: float
    steps: int = Field(default=20, ge=1, le=1000)
    button: Literal["left", "right", "middle"] = "left"


class BrowserMouseMoveXYRequest(BaseModel):
    x: float
    y: float
    steps: int = Field(default=1, ge=1, le=1000)


class BrowserMouseUpRequest(BaseModel):
    button: Literal["left", "right", "middle"] = "left"


class BrowserMouseWheelRequest(BaseModel):
    delta_x: float = 0
    delta_y: float = 0


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


def _build_grep_argv(req: SearchRequest) -> list[str]:
    argv = ["grep"]
    if req.recursive:
        argv.append("-R")
    if req.ignore_case:
        argv.append("-i")
    if req.line_number:
        argv.append("-n")
    if isinstance(req.max_count, int) and req.max_count > 0:
        argv.extend(["-m", str(int(req.max_count))])
    argv.extend(["--", req.pattern, req.path])
    return argv


def _build_rg_argv(req: SearchRequest) -> list[str]:
    argv = ["rg"]
    if req.ignore_case:
        argv.append("-i")
    if req.line_number:
        argv.append("-n")
    if req.pcre2:
        argv.append("-P")
    if isinstance(req.max_count, int) and req.max_count > 0:
        argv.extend(["-m", str(int(req.max_count))])
    if not req.recursive:
        argv.extend(["--max-depth", "1"])
    if isinstance(req.glob, str) and req.glob.strip():
        argv.extend(["-g", req.glob.strip()])
    argv.extend(["--", req.pattern, req.path])
    return argv


async def _run_search_with_routing(req: SearchRequest, *, forced_engine: str | None = None) -> dict[str, Any]:
    cwd_path = _ensure_cwd(req.cwd)
    chosen = (forced_engine or req.engine_hint or "auto").strip().lower()
    if chosen not in {"auto", "rg", "grep"}:
        raise HTTPException(status_code=400, detail=f"unsupported_engine_hint: {chosen}")

    if chosen == "grep":
        grep_result = await _run_subprocess_tool(
            tool_name="tools_search_grep",
            cwd_path=cwd_path,
            timeout_ms=req.timeout_ms,
            env=None,
            persist_output=req.persist_output,
            success_exit_codes={0, 1},
            argv=_build_grep_argv(req),
        )
        return _subprocess_result_to_envelope(grep_result, engine_used="grep")

    if chosen == "rg":
        rg_result = await _run_subprocess_tool(
            tool_name="tools_search_rg",
            cwd_path=cwd_path,
            timeout_ms=req.timeout_ms,
            env=None,
            persist_output=req.persist_output,
            success_exit_codes={0, 1},
            argv=_build_rg_argv(req),
        )
        return _subprocess_result_to_envelope(rg_result, engine_used="rg")

    rg_result = await _run_subprocess_tool(
        tool_name="tools_search_rg",
        cwd_path=cwd_path,
        timeout_ms=req.timeout_ms,
        env=None,
        persist_output=req.persist_output,
        success_exit_codes={0, 1},
        argv=_build_rg_argv(req),
    )
    if not _subprocess_should_fallback_to_grep(rg_result):
        return _subprocess_result_to_envelope(rg_result, engine_used="rg")

    grep_result = await _run_subprocess_tool(
        tool_name="tools_search_grep",
        cwd_path=cwd_path,
        timeout_ms=req.timeout_ms,
        env=None,
        persist_output=req.persist_output,
        success_exit_codes={0, 1},
        argv=_build_grep_argv(req),
    )
    return _subprocess_result_to_envelope(grep_result, engine_used="grep", fallback_from="rg")


@app.post("/tools/bash")
async def tools_bash(req: BashRequest) -> dict[str, Any]:
    cwd_path = _ensure_cwd(req.cwd)
    result = await _run_subprocess_tool(
        tool_name="tools_bash",
        cwd_path=cwd_path,
        timeout_ms=req.timeout_ms,
        env=req.env,
        persist_output=req.persist_output,
        success_exit_codes={0},
        shell_command=req.command,
        shell_executable=req.shell_executable,
    )
    return _subprocess_result_to_envelope(result)


@app.post("/tools/glob")
async def tools_glob(req: GlobRequest) -> dict[str, Any]:
    cwd_path = _ensure_cwd(req.cwd)
    argv = ["rg", "--files"]
    if req.include_hidden:
        argv.append("-uu")
    argv.extend(["-g", req.pattern, "--", req.path])
    result = await _run_subprocess_tool(
        tool_name="tools_glob",
        cwd_path=cwd_path,
        timeout_ms=req.timeout_ms,
        env=None,
        persist_output=req.persist_output,
        success_exit_codes={0, 1},
        argv=argv,
    )
    return _subprocess_result_to_envelope(result)


@app.post("/tools/read")
async def tools_read(req: ReadRequest) -> dict[str, Any]:
    cwd_path = _ensure_cwd(req.cwd)
    start = max(1, int(req.start_line))
    end = max(start, int(req.end_line)) if req.end_line is not None else (start + 255)
    argv = ["sed", "-n", f"{start},{end}p", "--", req.path]
    result = await _run_subprocess_tool(
        tool_name="tools_read",
        cwd_path=cwd_path,
        timeout_ms=req.timeout_ms,
        env=None,
        persist_output=req.persist_output,
        success_exit_codes={0},
        argv=argv,
    )
    return _subprocess_result_to_envelope(result)


@app.post("/tools/search")
async def tools_search(req: SearchRequest) -> dict[str, Any]:
    return await _run_search_with_routing(req)


@app.post("/tools/grep")
async def tools_grep(req: SearchRequest) -> dict[str, Any]:
    return await _run_search_with_routing(req, forced_engine="grep")


@app.post("/tools/rg")
async def tools_rg(req: SearchRequest) -> dict[str, Any]:
    return await _run_search_with_routing(req, forced_engine="rg")


@app.get("/browser/state")
async def browser_state() -> Any:
    payload = await browser_manager.state()
    return _maybe_externalize_payload(payload, tool_name="browser_state")


@app.post("/browser/click")
async def browser_click(req: BrowserClickRequest) -> Any:
    payload = await browser_manager.click(
        uid=req.uid,
        selector=req.selector,
        button=req.button,
        click_count=req.click_count,
        timeout_ms=req.timeout_ms,
    )
    return _maybe_externalize_payload(payload, tool_name="browser_click")


@app.post("/browser/close")
async def browser_close() -> Any:
    payload = await browser_manager.close()
    return _maybe_externalize_payload(payload, tool_name="browser_close")


@app.post("/browser/drag")
async def browser_drag(req: BrowserDragRequest) -> Any:
    payload = await browser_manager.drag(
        from_uid=req.from_uid,
        from_selector=req.from_selector,
        to_uid=req.to_uid,
        to_selector=req.to_selector,
        timeout_ms=req.timeout_ms,
    )
    return _maybe_externalize_payload(payload, tool_name="browser_drag")


@app.post("/browser/evaluate")
async def browser_evaluate(req: BrowserEvaluateRequest) -> Any:
    payload = await browser_manager.evaluate(expression=req.expression, arg=req.arg)
    return _maybe_externalize_payload(payload, tool_name="browser_evaluate")


@app.post("/browser/file_upload")
async def browser_file_upload(req: BrowserFileUploadRequest) -> Any:
    payload = await browser_manager.file_upload(
        uid=req.uid,
        selector=req.selector,
        files=req.files,
        timeout_ms=req.timeout_ms,
    )
    return _maybe_externalize_payload(payload, tool_name="browser_file_upload")


@app.post("/browser/fill_form")
async def browser_fill_form(req: BrowserFillFormRequest) -> Any:
    payload = await browser_manager.fill_form(
        fields=[item.model_dump() for item in req.fields],
        timeout_ms=req.timeout_ms,
        submit=req.submit,
    )
    return _maybe_externalize_payload(payload, tool_name="browser_fill_form")


@app.post("/browser/handle_dialog")
async def browser_handle_dialog(req: BrowserHandleDialogRequest) -> Any:
    payload = await browser_manager.handle_dialog(
        action=req.action,
        prompt_text=req.prompt_text,
        timeout_ms=req.timeout_ms,
    )
    return _maybe_externalize_payload(payload, tool_name="browser_handle_dialog")


@app.post("/browser/hover")
async def browser_hover(req: BrowserHoverRequest) -> Any:
    payload = await browser_manager.hover(uid=req.uid, selector=req.selector, timeout_ms=req.timeout_ms)
    return _maybe_externalize_payload(payload, tool_name="browser_hover")


@app.post("/browser/navigate")
async def browser_navigate(req: BrowserNavigateRequest) -> Any:
    payload = await browser_manager.navigate(
        url=req.url,
        timeout_ms=req.timeout_ms,
        wait_until=req.wait_until,
    )
    return _maybe_externalize_payload(payload, tool_name="browser_navigate")


@app.post("/browser/navigate_page")
async def browser_navigate_page(req: BrowserNavigatePageRequest) -> Any:
    payload = await browser_manager.navigate_page(
        action=req.type,
        url=req.url,
        timeout_ms=req.timeout_ms,
        wait_until=req.wait_until,
    )
    return _maybe_externalize_payload(payload, tool_name="browser_navigate_page")


@app.post("/browser/list_pages")
async def browser_list_pages(req: BrowserListPagesRequest) -> Any:
    del req
    payload = await browser_manager.list_pages()
    return _maybe_externalize_payload(payload, tool_name="browser_list_pages")


@app.post("/browser/new_page")
async def browser_new_page(req: BrowserNewPageRequest) -> Any:
    payload = await browser_manager.new_page(
        url=req.url,
        timeout_ms=req.timeout_ms,
        wait_until=req.wait_until,
        background=req.background,
    )
    return _maybe_externalize_payload(payload, tool_name="browser_new_page")


@app.post("/browser/select_page")
async def browser_select_page(req: BrowserSelectPageRequest) -> Any:
    payload = await browser_manager.select_page(
        page_id=req.pageId,
        bring_to_front=req.bringToFront,
    )
    return _maybe_externalize_payload(payload, tool_name="browser_select_page")


@app.post("/browser/close_page")
async def browser_close_page(req: BrowserClosePageRequest) -> Any:
    payload = await browser_manager.close_page(page_id=req.pageId)
    return _maybe_externalize_payload(payload, tool_name="browser_close_page")


@app.post("/browser/navigate_back")
async def browser_navigate_back(req: BrowserNavigateBackRequest) -> Any:
    payload = await browser_manager.navigate_back(
        timeout_ms=req.timeout_ms,
        wait_until=req.wait_until,
    )
    return _maybe_externalize_payload(payload, tool_name="browser_navigate_back")


@app.post("/browser/press_key")
async def browser_press_key(req: BrowserPressKeyRequest) -> Any:
    payload = await browser_manager.press_key(
        key=req.key,
        uid=req.uid,
        selector=req.selector,
        timeout_ms=req.timeout_ms,
    )
    return _maybe_externalize_payload(payload, tool_name="browser_press_key")


@app.post("/browser/select_option")
async def browser_select_option(req: BrowserSelectOptionRequest) -> Any:
    payload = await browser_manager.select_option(
        uid=req.uid,
        selector=req.selector,
        values=req.values,
        labels=req.labels,
        indexes=req.indexes,
        timeout_ms=req.timeout_ms,
    )
    return _maybe_externalize_payload(payload, tool_name="browser_select_option")


@app.post("/browser/snapshot")
async def browser_snapshot(req: BrowserSnapshotRequest) -> Any:
    payload = await browser_manager.snapshot(
        mode=req.mode,
        max_elements=req.max_elements,
        max_nodes=req.max_nodes,
        interesting_only=req.interesting_only,
        preview_lines=req.preview_lines,
        output_path=None,
    )
    return _maybe_externalize_payload(payload, tool_name="browser_snapshot")


@app.post("/browser/take_snapshot")
async def browser_take_snapshot(req: BrowserTakeSnapshotRequest) -> Any:
    payload = await browser_manager.take_snapshot(
        verbose=req.verbose,
        file_path=None,
        preview_lines=req.preview_lines,
    )
    return _maybe_externalize_payload(payload, tool_name="browser_take_snapshot")


@app.post("/browser/take_screenshot")
async def browser_take_screenshot(req: BrowserTakeScreenshotRequest) -> Any:
    payload = await browser_manager.take_screenshot(
        uid=req.uid,
        selector=req.selector,
        output_path=None,
        full_page=req.full_page,
        image_format=req.format,
        quality=req.quality,
    )
    return payload


@app.post("/browser/type_text")
async def browser_type_text(req: BrowserTypeTextRequest) -> Any:
    payload = await browser_manager.type_text_alias(
        text=req.text,
        submit_key=req.submitKey,
        delay_ms=req.delay_ms,
    )
    return _maybe_externalize_payload(payload, tool_name="browser_type_text")


@app.post("/browser/type")
async def browser_type(req: BrowserTypeRequest) -> Any:
    payload = await browser_manager.type_text(
        text=req.text,
        uid=req.uid,
        selector=req.selector,
        clear_before=req.clear_before,
        delay_ms=req.delay_ms,
        timeout_ms=req.timeout_ms,
    )
    return _maybe_externalize_payload(payload, tool_name="browser_type")


@app.post("/browser/fill")
async def browser_fill(req: BrowserFillRequest) -> Any:
    payload = await browser_manager.fill(
        uid=req.uid,
        selector=req.selector,
        value=req.value,
        timeout_ms=req.timeout_ms,
    )
    return _maybe_externalize_payload(payload, tool_name="browser_fill")


@app.post("/browser/wait_for")
async def browser_wait_for(req: BrowserWaitForRequest) -> Any:
    payload = await browser_manager.wait_for(
        texts=req.text,
        uid=req.uid,
        selector=req.selector,
        state=req.state,
        timeout_ms=req.timeout_ms,
    )
    return _maybe_externalize_payload(payload, tool_name="browser_wait_for")


@app.post("/browser/upload_file")
async def browser_upload_file(req: BrowserUploadFileRequest) -> Any:
    payload = await browser_manager.upload_file(
        uid=req.uid,
        selector=req.selector,
        file_path=req.filePath,
        timeout_ms=req.timeout_ms,
    )
    return _maybe_externalize_payload(payload, tool_name="browser_upload_file")


@app.post("/browser/tabs")
async def browser_tabs(req: BrowserTabsRequest) -> Any:
    payload = await browser_manager.tabs(
        action=req.action,
        index=req.index,
        url=req.url,
        timeout_ms=req.timeout_ms,
        wait_until=req.wait_until,
    )
    return _maybe_externalize_payload(payload, tool_name="browser_tabs")


@app.post("/browser/mouse_click_xy")
async def browser_mouse_click_xy(req: BrowserMouseClickXYRequest) -> Any:
    payload = await browser_manager.mouse_click_xy(
        x=req.x,
        y=req.y,
        button=req.button,
        click_count=req.click_count,
        delay_ms=req.delay_ms,
    )
    return _maybe_externalize_payload(payload, tool_name="browser_mouse_click_xy")


@app.post("/browser/mouse_down")
async def browser_mouse_down(req: BrowserMouseDownRequest) -> Any:
    payload = await browser_manager.mouse_down(button=req.button)
    return _maybe_externalize_payload(payload, tool_name="browser_mouse_down")


@app.post("/browser/mouse_drag_xy")
async def browser_mouse_drag_xy(req: BrowserMouseDragXYRequest) -> Any:
    payload = await browser_manager.mouse_drag_xy(
        start_x=req.start_x,
        start_y=req.start_y,
        end_x=req.end_x,
        end_y=req.end_y,
        steps=req.steps,
        button=req.button,
    )
    return _maybe_externalize_payload(payload, tool_name="browser_mouse_drag_xy")


@app.post("/browser/mouse_move_xy")
async def browser_mouse_move_xy(req: BrowserMouseMoveXYRequest) -> Any:
    payload = await browser_manager.mouse_move_xy(x=req.x, y=req.y, steps=req.steps)
    return _maybe_externalize_payload(payload, tool_name="browser_mouse_move_xy")


@app.post("/browser/mouse_up")
async def browser_mouse_up(req: BrowserMouseUpRequest) -> Any:
    payload = await browser_manager.mouse_up(button=req.button)
    return _maybe_externalize_payload(payload, tool_name="browser_mouse_up")


@app.post("/browser/mouse_wheel")
async def browser_mouse_wheel(req: BrowserMouseWheelRequest) -> Any:
    payload = await browser_manager.mouse_wheel(delta_x=req.delta_x, delta_y=req.delta_y)
    return _maybe_externalize_payload(payload, tool_name="browser_mouse_wheel")


@app.post("/files/upload")
async def files_upload(
    file: UploadFile = File(...),
    target_path: str | None = Form(default=None),
    destination_dir: str = Form(default="/config/uploads"),
    overwrite: bool = Form(default=True),
) -> Any:
    if target_path and target_path.strip():
        out_path = _resolve_any_path(target_path, UPLOAD_ROOT)
    else:
        if not file.filename:
            raise HTTPException(status_code=400, detail="filename_missing")
        out_path = (_resolve_any_path(destination_dir, UPLOAD_ROOT) / Path(file.filename).name).resolve()

    if out_path.exists() and not overwrite:
        raise HTTPException(status_code=409, detail=f"target_exists: {out_path}")

    out_path.parent.mkdir(parents=True, exist_ok=True)

    size = 0
    digest = hashlib.sha256()
    with out_path.open("wb") as handle:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            handle.write(chunk)
            digest.update(chunk)
            size += len(chunk)
    await file.close()

    payload = {
        "path": str(out_path),
        "size_bytes": size,
        "sha256": digest.hexdigest(),
    }
    return _maybe_externalize_payload(payload, tool_name="files_upload")


