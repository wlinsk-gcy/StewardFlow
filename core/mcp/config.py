"""
Configuration loader for MCP session.

This module provides functionality to load MCP configuration from JSON files.
"""

import json
from typing import Any

from mcp.client.session import ElicitationFnT, ListRootsFnT, LoggingFnT, MessageHandlerFnT, SamplingFnT
from mcp.types import Root

from core.mcp.connectors.base import BaseConnector
from core.mcp.connectors.stdio import StdioConnector
from core.mcp.connectors.http import HttpConnector


def is_stdio_server(server_config: dict[str, Any]) -> bool:
    """Check if the server configuration is for a stdio server.

    Args:
        server_config: The server configuration section

    Returns:
        True if the server is a stdio server, False otherwise
    """
    return "command" in server_config and "args" in server_config


def load_config_file(filepath: str) -> dict[str, Any]:
    """Load a configuration file.

    Args:
        filepath: Path to the configuration file

    Returns:
        The parsed configuration
    """
    with open(filepath) as f:
        return json.load(f)


def create_connector_from_config(
        server_config: dict[str, Any],
        sampling_callback: SamplingFnT | None = None,
        elicitation_callback: ElicitationFnT | None = None,
        message_handler: MessageHandlerFnT | None = None,
        logging_callback: LoggingFnT | None = None,
        verify: bool | None = True,
        roots: list[Root] | None = None,
        list_roots_callback: ListRootsFnT | None = None,
) -> BaseConnector:
    """Create a connector based on server configuration.
    This function can be called with just the server_config parameter:
    create_connector_from_config(server_config)
    Args:
        server_config: The server configuration section
        sampling_callback: Optional sampling callback function.
    Returns:
        A configured connector instance
    """

    # Stdio connector (command-based)
    if is_stdio_server(server_config):
        return StdioConnector(
            command=server_config["command"],
            args=server_config["args"],
            env=server_config.get("env", None),
            sampling_callback=sampling_callback,
            elicitation_callback=elicitation_callback,
            message_handler=message_handler,
            logging_callback=logging_callback,
            roots=roots,
            list_roots_callback=list_roots_callback,
        )
    # HTTP connector
    elif "url" in server_config:
        return HttpConnector(
            base_url=server_config["url"],
            headers=server_config.get("headers", None),
            auth=server_config.get("auth"),
            timeout=server_config.get("timeout", 5),
            sse_read_timeout=server_config.get("sse_read_timeout", 60 * 5),
            sampling_callback=sampling_callback,
            elicitation_callback=elicitation_callback,
            message_handler=message_handler,
            logging_callback=logging_callback,
            verify=verify,
            roots=roots,
            list_roots_callback=list_roots_callback,
        )

    raise ValueError("Cannot determine connector type from config")
