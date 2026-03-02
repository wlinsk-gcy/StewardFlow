from __future__ import annotations

from typing import Any


def _safe_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def _is_mcp_proxy_tool(tool_obj: Any) -> bool:
    # Avoid hard dependency import so this helper stays testable in lightweight envs.
    return tool_obj is not None and tool_obj.__class__.__name__ == "MCPToolProxy"


def _dedupe_and_sort_tools(raw_tools: list[dict[str, str]]) -> list[dict[str, str]]:
    by_name: dict[str, dict[str, str]] = {}
    for item in raw_tools:
        name = _safe_text(item.get("name")).strip()
        if not name:
            continue
        if name not in by_name:
            by_name[name] = {
                "name": name,
                "description": _safe_text(item.get("description")).strip(),
            }
    return sorted(by_name.values(), key=lambda item: item["name"])


def _extract_server_name(tool_name: str) -> str | None:
    if "_" not in tool_name:
        return None
    maybe_server, _ = tool_name.split("_", 1)
    maybe_server = maybe_server.strip()
    return maybe_server or None


async def build_registry_summary(tool_registry: Any, mcp_client: Any) -> dict[str, Any]:
    registry_tools = {}
    if tool_registry is not None and hasattr(tool_registry, "list_tools"):
        registry_tools = tool_registry.list_tools() or {}

    built_in_tools: list[dict[str, str]] = []
    proxy_tools: list[dict[str, str]] = []
    for tool_name, tool_obj in registry_tools.items():
        tool_item = {
            "name": _safe_text(tool_name),
            "description": _safe_text(getattr(tool_obj, "description", "")),
        }
        if _is_mcp_proxy_tool(tool_obj):
            proxy_tools.append(tool_item)
        else:
            built_in_tools.append(tool_item)
    built_in_tools = _dedupe_and_sort_tools(built_in_tools)

    configured_server_names: list[str] = []
    sessions: dict[str, Any] = {}
    if mcp_client is not None:
        if hasattr(mcp_client, "get_server_names"):
            configured_server_names = list(mcp_client.get_server_names() or [])
        sessions = dict(getattr(mcp_client, "sessions", {}) or {})

    server_names = set(configured_server_names)
    for item in proxy_tools:
        extracted = _extract_server_name(item["name"])
        if extracted:
            server_names.add(extracted)

    mcp_servers: list[dict[str, Any]] = []
    for server_name in sorted(server_names):
        session = sessions.get(server_name)
        connector = getattr(session, "connector", None) if session is not None else None
        connected = bool(getattr(connector, "is_connected", False))

        remote_tools: list[dict[str, str]] = []
        if connected and connector is not None and hasattr(connector, "list_tools"):
            try:
                listed = await connector.list_tools()
                for tool_obj in listed or []:
                    remote_tools.append(
                        {
                            "name": _safe_text(getattr(tool_obj, "name", "")).strip(),
                            "description": _safe_text(getattr(tool_obj, "description", "")).strip(),
                        }
                    )
            except Exception:
                remote_tools = []

        prefix = f"{server_name}_"
        fallback_proxy_tools = [
            {
                "name": item["name"][len(prefix):],
                "description": _safe_text(item.get("description")).strip(),
            }
            for item in proxy_tools
            if item["name"].startswith(prefix)
        ]

        merged_tools = _dedupe_and_sort_tools(remote_tools + fallback_proxy_tools)
        mcp_servers.append(
            {
                "name": server_name,
                "connected": connected,
                "tool_count": len(merged_tools),
                "tools": merged_tools,
            }
        )

    mcp_tool_count = sum(item["tool_count"] for item in mcp_servers)
    return {
        "built_in_tools": built_in_tools,
        "mcp_servers": mcp_servers,
        "counts": {
            "built_in_tools": len(built_in_tools),
            "mcp_servers": len(mcp_servers),
            "mcp_tools": mcp_tool_count,
            "all_tools": len(built_in_tools) + mcp_tool_count,
        },
    }
