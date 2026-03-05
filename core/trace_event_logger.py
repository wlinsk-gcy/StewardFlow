from __future__ import annotations

import json
import logging
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator


DEFAULT_PREVIEW_CHARS = 80
DEFAULT_MODE = "human"
DEFAULT_RATE_LIMIT_SEC = 1.0
SENSITIVE_KEYWORDS = {
    "authorization",
    "cookie",
    "api_key",
    "token",
    "password",
    "secret",
}
NO_CLIP_KEYS = {
    "trace_id",
    "turn_id",
    "step_id",
    "event",
    "tool_call_id",
    "tool_name",
    "ok",
    "elapsed_ms",
    "action_count",
    "reason_code",
    "ref_path",
    "path",
    "allowed_roots",
}
HUMAN_FIELDS = (
    "event",
    "trace_id",
    "turn_id",
    "step_id",
    "tool_call_id",
    "tool_name",
    "ok",
    "elapsed_ms",
    "action_count",
    "reason_code",
)
VERBOSE_OMIT_KEYS = {
    "raw",
    "raw_ref",
    "payload",
    "full_payload",
    "stdout",
    "stderr",
    "content",
}
ALWAYS_EMIT_EVENTS = {"tool_start", "tool_end"}

_mode = DEFAULT_MODE
_preview_chars = DEFAULT_PREVIEW_CHARS
_rate_limit_sec = DEFAULT_RATE_LIMIT_SEC
_last_emit_monotonic: dict[tuple[str, str, str, str, str], float] = {}
_last_emit_lock = threading.Lock()

_trace_id_ctx: ContextVar[str] = ContextVar("event_trace_id", default="-")
_turn_id_ctx: ContextVar[str] = ContextVar("event_turn_id", default="-")
_step_id_ctx: ContextVar[str] = ContextVar("event_step_id", default="-")
_tool_call_id_ctx: ContextVar[str] = ContextVar("event_tool_call_id", default="-")
_tool_name_ctx: ContextVar[str] = ContextVar("event_tool_name", default="-")


def configure_trace_event_logger(
    *,
    mode: Any = None,
    preview_chars: Any = None,
    rate_limit_sec: Any = None,
) -> None:
    global _mode, _preview_chars, _rate_limit_sec

    mode_value = str(mode or DEFAULT_MODE).strip().lower()
    _mode = "verbose" if mode_value == "verbose" else "human"

    try:
        preview_value = int(preview_chars if preview_chars is not None else DEFAULT_PREVIEW_CHARS)
        _preview_chars = max(1, preview_value)
    except (TypeError, ValueError):
        _preview_chars = DEFAULT_PREVIEW_CHARS

    try:
        rate_value = float(rate_limit_sec if rate_limit_sec is not None else DEFAULT_RATE_LIMIT_SEC)
        _rate_limit_sec = max(0.0, rate_value)
    except (TypeError, ValueError):
        _rate_limit_sec = DEFAULT_RATE_LIMIT_SEC

    with _last_emit_lock:
        _last_emit_monotonic.clear()


def _clip_text(value: str) -> str:
    if len(value) <= _preview_chars:
        return value
    return value[:_preview_chars] + "..."


def _is_sensitive_key(key: str | None) -> bool:
    if not key:
        return False
    lowered = key.lower()
    return any(marker in lowered for marker in SENSITIVE_KEYWORDS)


def _sanitize(value: Any, *, key: str | None = None) -> Any:
    if _is_sensitive_key(key):
        return "[REDACTED]"
    if isinstance(value, str):
        if key in NO_CLIP_KEYS:
            return value
        return _clip_text(value)
    if isinstance(value, dict):
        return {str(k): _sanitize(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_sanitize(item, key=key) for item in value]
    if isinstance(value, tuple):
        return [_sanitize(item, key=key) for item in value]
    return value


@contextmanager
def bind_event_context(
    *,
    trace_id: str | None = None,
    turn_id: str | None = None,
    step_id: str | None = None,
    tool_call_id: str | None = None,
    tool_name: str | None = None,
) -> Iterator[None]:
    tokens = []
    try:
        if trace_id is not None:
            tokens.append((_trace_id_ctx, _trace_id_ctx.set(str(trace_id))))
        if turn_id is not None:
            tokens.append((_turn_id_ctx, _turn_id_ctx.set(str(turn_id))))
        if step_id is not None:
            tokens.append((_step_id_ctx, _step_id_ctx.set(str(step_id))))
        if tool_call_id is not None:
            tokens.append((_tool_call_id_ctx, _tool_call_id_ctx.set(str(tool_call_id))))
        if tool_name is not None:
            tokens.append((_tool_name_ctx, _tool_name_ctx.set(str(tool_name))))
        yield
    finally:
        for ctx_var, token in reversed(tokens):
            ctx_var.reset(token)


def _filter_fields_for_mode(payload: dict[str, Any]) -> dict[str, Any]:
    if _mode == "human":
        result: dict[str, Any] = {}
        for key in HUMAN_FIELDS:
            if key not in payload:
                continue
            value = payload[key]
            if value is None:
                continue
            result[key] = value
        return result

    result = {}
    for key, value in payload.items():
        if key in VERBOSE_OMIT_KEYS:
            continue
        result[key] = value
    return result


def _should_skip_by_rate_limit(payload: dict[str, Any]) -> bool:
    if _rate_limit_sec <= 0:
        return False

    event = str(payload.get("event") or "")
    if event in ALWAYS_EMIT_EVENTS:
        return False

    dedup_key = (
        str(payload.get("trace_id") or "-"),
        str(payload.get("turn_id") or "-"),
        str(payload.get("step_id") or "-"),
        str(payload.get("tool_call_id") or "-"),
        event,
    )
    now = time.monotonic()
    with _last_emit_lock:
        last = _last_emit_monotonic.get(dedup_key)
        if last is not None and (now - last) < _rate_limit_sec:
            return True
        _last_emit_monotonic[dedup_key] = now
    return False


def emit_trace_event(logger: logging.Logger, *, event: str, **fields: Any) -> None:
    trace_id = str(fields.pop("trace_id", _trace_id_ctx.get()) or "-")
    turn_id = str(fields.pop("turn_id", _turn_id_ctx.get()) or "-")
    step_id = str(fields.pop("step_id", _step_id_ctx.get()) or "-")
    tool_call_id = str(fields.pop("tool_call_id", _tool_call_id_ctx.get()) or "-")
    tool_name = str(fields.pop("tool_name", _tool_name_ctx.get()) or "-")

    payload: dict[str, Any] = {
        "trace_id": trace_id,
        "turn_id": turn_id,
        "step_id": step_id,
        "event": event,
    }
    if tool_call_id != "-":
        payload["tool_call_id"] = tool_call_id
    if tool_name != "-":
        payload["tool_name"] = tool_name
    payload.update(fields)
    payload = _filter_fields_for_mode(payload)
    if _should_skip_by_rate_limit(payload):
        return

    logger.info(
        "event_log %s",
        json.dumps(_sanitize(payload), ensure_ascii=False, separators=(",", ":")),
    )


# Default boot configuration for direct module users (tests can override via configure function).
configure_trace_event_logger()
