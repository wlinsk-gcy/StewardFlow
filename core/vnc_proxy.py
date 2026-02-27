from __future__ import annotations

import inspect
from typing import Any, Mapping


def _pick_non_empty(*values: Any) -> str | None:
    for val in values:
        if val is None:
            continue
        text = str(val).strip()
        if text:
            return text
    return None


def build_vnc_proxy_headers(
    *,
    raw_cfg: Mapping[str, Any] | None,
    env: Mapping[str, str] | None,
) -> list[tuple[str, str]]:
    raw = dict(raw_cfg or {})
    env_map = env or {}
    api_key = _pick_non_empty(
        raw.get("vnc_api_key"),
        env_map.get("AGENTRUN_VNC_API_KEY"),
        env_map.get("VNC_API_KEY"),
    )
    if not api_key:
        return []
    return [
        ("X-API-Key", api_key),
        ("X-API-KEY", api_key),
    ]


def build_ws_connect_kwargs(
    ws_connect: Any,
    *,
    headers: list[tuple[str, str]] | None,
    max_size: int | None,
    open_timeout: int | float | None,
    close_timeout: int | float | None,
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "max_size": max_size,
        "open_timeout": open_timeout,
        "close_timeout": close_timeout,
    }
    if not headers:
        return kwargs

    header_key = "additional_headers"
    try:
        sig = inspect.signature(ws_connect)
        param_names = set(sig.parameters.keys())
        if "extra_headers" in param_names:
            header_key = "extra_headers"
        elif "additional_headers" in param_names:
            header_key = "additional_headers"
        elif "headers" in param_names:
            header_key = "headers"
    except Exception:
        # Default to new websockets kwarg naming.
        header_key = "additional_headers"

    kwargs[header_key] = headers
    return kwargs

