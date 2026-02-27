import logging
import json
import warnings
from typing import Any

from mcp.client.session import ElicitationFnT, ListRootsFnT, LoggingFnT, MessageHandlerFnT, SamplingFnT
from mcp.types import Root

from core.mcp.session import MCPSession
from core.mcp.config import create_connector_from_config, load_config_file
from core.tools.tool import ToolRegistry
from core.mcp.proxy import MCPToolProxy

logger = logging.getLogger(__name__)


class MCPClient:
    """Client for managing MCP servers and sessions.

        This class provides a unified interface for working with MCP servers,
        handling configuration, connector creation, and session management.
        """

    def __init__(
            self,
            config: str | dict[str, Any] | None = None,
            allowed_servers: list[str] | None = None,
            sampling_callback: SamplingFnT | None = None,
            elicitation_callback: ElicitationFnT | None = None,
            message_handler: MessageHandlerFnT | None = None,
            logging_callback: LoggingFnT | None = None,
            roots: list[Root] | None = None,
            list_roots_callback: ListRootsFnT | None = None,
            verify: bool | None = True,
    ) -> None:
        """Initialize a new MCP client.

        Args:
            config: Either a dict containing configuration or a path to a JSON config file.
                   If None, an empty configuration is used.
            roots: Optional list of Root objects to advertise to servers.
                Roots represent directories or files the client has access to.
            list_roots_callback: Optional custom callback for roots/list requests.
            sampling_callback: Optional sampling callback function.
            code_mode: Whether to enable code execution mode for tools.
        """
        self.config: dict[str, Any] = {}
        self.allowed_servers: list[str] = allowed_servers
        self.sessions: dict[str, MCPSession] = {}
        self.active_sessions: list[str] = []
        self.sampling_callback = sampling_callback
        self.elicitation_callback = elicitation_callback
        self.message_handler = message_handler
        self.logging_callback = logging_callback
        self.roots = roots
        self.list_roots_callback = list_roots_callback
        self.verify = verify
        # Load configuration if provided
        if config is not None:
            if isinstance(config, str):
                self.config = load_config_file(config)
            else:
                self.config = config

        # servers_list = list(self.config.get("mcpServers", {}).keys()) if self.config else []

    @classmethod
    def from_dict(
            cls,
            config: dict[str, Any],
            sampling_callback: SamplingFnT | None = None,
            elicitation_callback: ElicitationFnT | None = None,
            message_handler: MessageHandlerFnT | None = None,
            logging_callback: LoggingFnT | None = None,
            verify: bool | None = True,
            roots: list[Root] | None = None,
            list_roots_callback: ListRootsFnT | None = None,
    ) -> "MCPClient":
        """Create a MCPClient from a dictionary.

        Args:
            config: The configuration dictionary.
            sampling_callback: Optional sampling callback function.
            elicitation_callback: Optional elicitation callback function.
            roots: Optional list of Root objects to advertise to servers.
            list_roots_callback: Optional custom callback for roots/list requests.
        """
        return cls(
            config=config,
            sampling_callback=sampling_callback,
            elicitation_callback=elicitation_callback,
            message_handler=message_handler,
            logging_callback=logging_callback,
            verify=verify,
            roots=roots,
            list_roots_callback=list_roots_callback,
        )

    @classmethod
    def from_config_file(
            cls,
            filepath: str,
            sampling_callback: SamplingFnT | None = None,
            elicitation_callback: ElicitationFnT | None = None,
            message_handler: MessageHandlerFnT | None = None,
            logging_callback: LoggingFnT | None = None,
            verify: bool | None = True,
            roots: list[Root] | None = None,
            list_roots_callback: ListRootsFnT | None = None,
    ) -> "MCPClient":
        """Create a MCPClient from a configuration file.

        Args:
            filepath: The path to the configuration file.
            sampling_callback: Optional sampling callback function.
            elicitation_callback: Optional elicitation callback function.
            roots: Optional list of Root objects to advertise to servers.
            list_roots_callback: Optional custom callback for roots/list requests.
        """
        return cls(
            config=load_config_file(filepath),
            sampling_callback=sampling_callback,
            elicitation_callback=elicitation_callback,
            message_handler=message_handler,
            logging_callback=logging_callback,
            verify=verify,
            roots=roots,
            list_roots_callback=list_roots_callback,
        )

    async def initialize(self, registry: ToolRegistry) -> None:
        try:
            sessions = await self.create_all_sessions()
            for name, session in sessions.items():
                logger.info(f"Connected to {name} MCP server!")
                tools = await session.connector.list_tools()
                logger.debug(f"\nAvailable tools ({len(tools)}):")
                for tool in tools:
                    fq = f"{name}_{tool.name}"
                    proxy = MCPToolProxy(
                        fq_name=fq,
                        description=tool.description or "",
                        input_schema=tool.inputSchema or {},
                        call_fn=lambda args, _t=tool: session.call_tool(_t.name, args),
                    )
                    registry.register(proxy)
        except Exception as e:
            logger.error(f"MCP server initialize failed: {e}")
            await self.close_all_sessions()

    def add_server(
            self,
            name: str,
            server_config: dict[str, Any],
    ) -> None:
        """Add a server configuration.

        Args:
            name: The name to identify this server.
            server_config: The server configuration.
        """
        if "mcpServers" not in self.config:
            self.config["mcpServers"] = {}

        self.config["mcpServers"][name] = server_config

    def remove_server(self, name: str) -> None:
        """Remove a server configuration.

        Args:
            name: The name of the server to remove.
        """
        if "mcpServers" in self.config and name in self.config["mcpServers"]:
            del self.config["mcpServers"][name]

            # If we removed an active session, remove it from active_sessions
            if name in self.active_sessions:
                self.active_sessions.remove(name)

    def get_server_names(self) -> list[str]:
        """Get the list of configured server names.

        Returns:
            List of server names (excludes internal code mode server).
        """
        servers = list(self.config.get("mcpServers", {}).keys())
        # Don't expose internal code mode server in server list
        return servers

    def save_config(self, filepath: str) -> None:
        """Save the current configuration to a file.

        Args:
            filepath: The path to save the configuration to.
        """
        with open(filepath, "w") as f:
            json.dump(self.config, f, indent=2)

    async def create_session(self, server_name: str, auto_initialize: bool = True) -> MCPSession | None:
        """Create a session for the specified server.

        Args:
            server_name: The name of the server to create a session for.
            auto_initialize: Whether to automatically initialize the session.

        Returns:
            The created MCPSession.

        Raises:
            ValueError: If the specified server doesn't exist.
        """
        # Get server config
        servers = self.config.get("mcpServers", {})
        if not servers:
            warnings.warn("No MCP servers defined in config", UserWarning, stacklevel=2)
            return None

        if server_name not in servers:
            raise ValueError(f"Server '{server_name}' not found in config")

        server_config = servers[server_name]

        # Create connector with options and client-level auth
        connector = create_connector_from_config(
            server_config,
            sampling_callback=self.sampling_callback,
            elicitation_callback=self.elicitation_callback,
            message_handler=self.message_handler,
            logging_callback=self.logging_callback,
            roots=self.roots,
            list_roots_callback=self.list_roots_callback,
        )

        # Create the session
        session = MCPSession(connector)
        if auto_initialize:
            await session.initialize()
        self.sessions[server_name] = session

        # Add to active sessions
        if server_name not in self.active_sessions:
            self.active_sessions.append(server_name)

        return session

    async def create_all_sessions(
            self,
            auto_initialize: bool = True,
    ) -> dict[str, MCPSession]:
        """Create sessions for all configured servers.

        Args:
            auto_initialize: Whether to automatically initialize the sessions.

        Returns:
            Dictionary mapping server names to their MCPSession instances.

        Warns:
            UserWarning: If no servers are configured.
        """
        # Get server config
        servers = self.config.get("mcpServers", {})
        if not servers:
            warnings.warn("No MCP servers defined in config", UserWarning, stacklevel=2)
            return {}

        # Create sessions only for allowed servers if applicable else create for all servers
        for name in servers:
            if self.allowed_servers is None or name in self.allowed_servers:
                await self.create_session(name, auto_initialize)

        return self.sessions

    def get_session(self, server_name: str) -> MCPSession:
        """Get an existing session.

        Args:
            server_name: The name of the server to get the session for.
                        If None, uses the first active session.

        Returns:
            The MCPSession for the specified server.

        Raises:
            ValueError: If no active sessions exist or the specified session doesn't exist.
        """
        if server_name not in self.sessions:
            raise ValueError(f"No session exists for server '{server_name}'")

        return self.sessions[server_name]

    def get_all_active_sessions(self) -> dict[str, MCPSession]:
        """Get all active sessions.

        Returns:
            Dictionary mapping server names to their MCPSession instances.
        """

        return {name: self.sessions[name] for name in self.active_sessions if name in self.sessions}

    async def close_session(self, server_name: str) -> None:
        """Close a session.

        Args:
            server_name: The name of the server to close the session for.
                        If None, uses the first active session.

        Raises:
            ValueError: If no active sessions exist or the specified session doesn't exist.
        """
        # Check if the session exists
        if server_name not in self.sessions:
            logger.warning(f"No session exists for server '{server_name}', nothing to close")
            return

        # Get the session
        session = self.sessions[server_name]

        try:
            # Disconnect from the session
            logger.debug(f"Closing session for server '{server_name}'")
            await session.disconnect()
        except Exception as e:
            logger.error(f"Error closing session for server '{server_name}': {e}")
        finally:
            # Remove the session regardless of whether disconnect succeeded
            del self.sessions[server_name]

            # Remove from active_sessions
            if server_name in self.active_sessions:
                self.active_sessions.remove(server_name)

    async def close_all_sessions(self) -> None:
        """Close all active sessions.

        This method ensures all sessions are closed even if some fail.
        """
        # Get a list of all session names first to avoid modification during iteration
        server_names = list(self.sessions.keys())
        errors = []

        for server_name in server_names:
            try:
                logger.debug(f"Closing session for server '{server_name}'")
                await self.close_session(server_name)
            except Exception as e:
                error_msg = f"Failed to close session for server '{server_name}': {e}"
                logger.error(error_msg)
                errors.append(error_msg)

        # Log summary if there were errors
        if errors:
            logger.error(f"Encountered {len(errors)} errors while closing sessions")
        else:
            logger.debug("All sessions closed successfully")


if __name__ == '__main__':
    import asyncio


    async def test_session():
        config = {
            "mcpServers": {
                "context7": {
                    "command": "npx",
                    "args": ["-y", "@upstash/context7-mcp", "--api-key", "YOUR_API_KEY"]
                }
            }
        }
        client = MCPClient(config=config)
        try:
            session = await client.create_session("context7")
            print("Connected to context7 MCP server!")

            # List available tools
            tools = await session.connector.list_tools()
            print(f"\nAvailable tools ({len(tools)}):")
            for tool in tools[:5]:  # Show first 5
                print(f"  - {tool.name}")
            if len(tools) > 5:
                print(f"  ... and {len(tools) - 5} more")

        finally:
            await client.close_all_sessions()


    async def test_all_sessions():
        config = {
            "mcpServers": {
                "context7": {
                    "command": "npx",
                    "args": ["-y", "@upstash/context7-mcp", "--api-key", "YOUR_API_KEY"]
                }
            }
        }
        client = MCPClient(config=config)
        try:
            sessions = await client.create_all_sessions()
            for name, session in sessions.items():
                print(f"Connected to {name} MCP server!")
                tools = await session.connector.list_tools()
                print(f"\nAvailable tools ({len(tools)}):")
                for tool in tools[:5]:  # Show first 5
                    print(f"  - {tool.name}")
                if len(tools) > 5:
                    print(f"  ... and {len(tools) - 5} more")
        finally:
            await client.close_all_sessions()


    # asyncio.run(test_session())
    asyncio.run(test_all_sessions())
