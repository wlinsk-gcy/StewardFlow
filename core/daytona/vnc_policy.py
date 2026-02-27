import json
from typing import Any

_EMIT_VNC_VIEW_TOOL_NAMES = frozenset(
    {
        "daytona_computer_start",
        "daytona_computer_status",
        "daytona_browser_navigate",
    }
)


def extract_vnc_payload(raw_result: Any) -> dict[str, Any] | None:
    payload = raw_result
    if isinstance(raw_result, str):
        try:
            payload = json.loads(raw_result)
        except Exception:
            return None

    if not isinstance(payload, dict):
        return None

    vnc_url = payload.get("vnc_url") or payload.get("url")
    if not isinstance(vnc_url, str) or not vnc_url.strip():
        return None

    data: dict[str, Any] = {
        "vnc_url": vnc_url.strip(),
    }
    if isinstance(payload.get("sandbox_id"), str):
        data["sandbox_id"] = payload["sandbox_id"]
    if payload.get("vnc_port") is not None:
        data["vnc_port"] = payload.get("vnc_port")
    if payload.get("vnc_url_ttl_seconds") is not None:
        data["vnc_url_ttl_seconds"] = payload.get("vnc_url_ttl_seconds")
    return data


def should_emit_vnc_view_event(tool_name: str | None, raw_result: Any) -> bool:
    if (tool_name or "").strip() not in _EMIT_VNC_VIEW_TOOL_NAMES:
        return False
    return extract_vnc_payload(raw_result) is not None
