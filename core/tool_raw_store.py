from __future__ import annotations

import gzip
import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


@dataclass
class ToolRawStoreConfig:
    root: str = os.getenv("STEWARDFLOW_TOOL_RAW_ROOT", "data/tool-raw")
    retention_days_ok: int = _env_int("STEWARDFLOW_TOOL_RAW_RETENTION_DAYS_OK", 7)
    retention_days_error: int = _env_int("STEWARDFLOW_TOOL_RAW_RETENTION_DAYS_ERROR", 30)
    max_total_bytes: int = _env_int("STEWARDFLOW_TOOL_RAW_MAX_TOTAL_BYTES", 2 * 1024 * 1024 * 1024)
    high_watermark_ratio: float = _env_float("STEWARDFLOW_TOOL_RAW_HIGH_WATERMARK_RATIO", 0.90)
    low_watermark_ratio: float = _env_float("STEWARDFLOW_TOOL_RAW_LOW_WATERMARK_RATIO", 0.70)
    gc_interval_sec: int = _env_int("STEWARDFLOW_TOOL_RAW_GC_INTERVAL_SEC", 3600)


class ToolRawStore:
    _last_gc_at: float = 0.0

    def __init__(self, config: ToolRawStoreConfig | None = None) -> None:
        self.config = config or ToolRawStoreConfig()
        self.root = Path(self.config.root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

        self.config.retention_days_ok = max(1, int(self.config.retention_days_ok))
        self.config.retention_days_error = max(1, int(self.config.retention_days_error))
        self.config.max_total_bytes = max(1, int(self.config.max_total_bytes))
        self.config.high_watermark_ratio = max(0.1, min(1.0, float(self.config.high_watermark_ratio)))
        self.config.low_watermark_ratio = max(0.05, min(self.config.high_watermark_ratio, float(self.config.low_watermark_ratio)))
        self.config.gc_interval_sec = max(30, int(self.config.gc_interval_sec))

    def write(
        self,
        *,
        trace_id: str,
        turn_id: str,
        step_id: str,
        action_id: str,
        tool_name: str,
        ok: bool,
        payload: Any,
    ) -> str:
        now = datetime.now(timezone.utc)
        day_path = self.root / f"{now.year:04d}" / f"{now.month:02d}" / f"{now.day:02d}"
        file_dir = day_path / str(trace_id) / str(turn_id) / str(step_id)
        file_dir.mkdir(parents=True, exist_ok=True)

        status = "ok" if ok else "error"
        safe_tool = "".join(ch if ch.isalnum() or ch in {"_", "-", "."} else "_" for ch in str(tool_name or "tool"))
        file_name = f"{int(time.time() * 1000)}_{action_id}_{safe_tool}_{status}.json.gz"
        out_path = (file_dir / file_name).resolve()

        record = {
            "trace_id": trace_id,
            "turn_id": turn_id,
            "step_id": step_id,
            "action_id": action_id,
            "tool_name": tool_name,
            "ok": bool(ok),
            "timestamp": now.isoformat(),
            "payload": payload,
        }
        with gzip.open(out_path, mode="wt", encoding="utf-8") as fp:
            json.dump(record, fp, ensure_ascii=False, separators=(",", ":"))

        self.maybe_gc()
        return str(out_path)

    def maybe_gc(self) -> None:
        now = time.time()
        if now - self._last_gc_at < float(self.config.gc_interval_sec):
            return
        self._last_gc_at = now
        self._gc_by_ttl(now)
        self._gc_by_size()

    def _gc_by_ttl(self, now_ts: float) -> None:
        ttl_ok = self.config.retention_days_ok * 86400
        ttl_err = self.config.retention_days_error * 86400
        for path in self.root.rglob("*.json.gz"):
            try:
                age = now_ts - path.stat().st_mtime
                name = path.name.lower()
                ttl = ttl_ok if "_ok." in name else ttl_err
                if age >= ttl:
                    path.unlink(missing_ok=True)
            except Exception:
                continue

    def _gc_by_size(self) -> None:
        files: list[tuple[Path, int, float]] = []
        total = 0
        for path in self.root.rglob("*.json.gz"):
            try:
                st = path.stat()
            except Exception:
                continue
            files.append((path, int(st.st_size), float(st.st_mtime)))
            total += int(st.st_size)

        high = int(self.config.max_total_bytes * self.config.high_watermark_ratio)
        if total <= high:
            return

        low = int(self.config.max_total_bytes * self.config.low_watermark_ratio)
        ok_files = [(p, s, m) for p, s, m in files if "_ok." in p.name.lower()]
        err_files = [(p, s, m) for p, s, m in files if "_ok." not in p.name.lower()]
        ok_files.sort(key=lambda item: item[2])   # oldest first
        err_files.sort(key=lambda item: item[2])  # oldest first

        for candidates in (ok_files, err_files):
            for path, size, _ in candidates:
                if total <= low:
                    return
                try:
                    path.unlink(missing_ok=True)
                    total -= size
                except Exception:
                    continue
