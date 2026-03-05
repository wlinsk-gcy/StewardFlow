from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any


_counter_lock = threading.Lock()
_trace_counters: dict[str, int] = {}


def _audit_root() -> Path:
    root = Path(os.getenv("STEWARDFLOW_AUDIT_ROOT", "data/audit")).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _events_path(trace_id: str) -> Path:
    path = (_audit_root() / str(trace_id) / "events.jsonl").resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _bootstrap_counter(trace_id: str, path: Path) -> int:
    if not path.exists():
        return 0
    try:
        with path.open("r", encoding="utf-8") as fp:
            return sum(1 for _ in fp)
    except Exception:
        return 0


def _next_event_id(trace_id: str, path: Path) -> int:
    with _counter_lock:
        if trace_id not in _trace_counters:
            _trace_counters[trace_id] = _bootstrap_counter(trace_id, path)
        _trace_counters[trace_id] += 1
        return _trace_counters[trace_id]


def append_context_event(
    *,
    trace_id: str,
    event_type: str,
    turn_id: str | None = None,
    step_id: str | None = None,
    reason_code: str | None = None,
    metrics_before: dict[str, Any] | None = None,
    metrics_after: dict[str, Any] | None = None,
    changes: dict[str, Any] | None = None,
    refs: dict[str, Any] | None = None,
) -> str:
    path = _events_path(trace_id)
    event_id = _next_event_id(trace_id, path)
    payload: dict[str, Any] = {
        "event_id": event_id,
        "trace_id": trace_id,
        "turn_id": turn_id,
        "step_id": step_id,
        "timestamp": time.time(),
        "event_type": event_type,
    }
    if reason_code is not None:
        payload["reason_code"] = reason_code
    if metrics_before is not None:
        payload["metrics_before"] = metrics_before
    if metrics_after is not None:
        payload["metrics_after"] = metrics_after
    if changes is not None:
        payload["changes"] = changes
    if refs is not None:
        payload["refs"] = refs

    with path.open("a", encoding="utf-8") as fp:
        fp.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
    return str(path)
