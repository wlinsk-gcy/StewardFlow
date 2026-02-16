from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator


MAX_PREVIEW_CHARS = 120
NO_CLIP_KEYS = {
    "trace_id",
    "turn_id",
    "step_id",
    "event",
    "tool_call_id",
    "tool_name",
    "ref_path",
    "path",
    "allowed_roots",
}

_trace_id_ctx: ContextVar[str] = ContextVar("event_trace_id", default="-")
_turn_id_ctx: ContextVar[str] = ContextVar("event_turn_id", default="-")
_step_id_ctx: ContextVar[str] = ContextVar("event_step_id", default="-")
_tool_call_id_ctx: ContextVar[str] = ContextVar("event_tool_call_id", default="-")
_tool_name_ctx: ContextVar[str] = ContextVar("event_tool_name", default="-")


def _clip_text(value: str) -> str:
    if len(value) <= MAX_PREVIEW_CHARS:
        return value
    return value[:MAX_PREVIEW_CHARS]


def _sanitize(value: Any, *, key: str | None = None) -> Any:
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

    logger.info(
        "event_log %s",
        json.dumps(_sanitize(payload), ensure_ascii=False, separators=(",", ":")),
    )
