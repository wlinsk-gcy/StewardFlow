import sys
from typing import Any, TextIO
import logging

from mcp import StdioServerParameters
from mcp.client.stdio import stdio_client

from core.mcp.manager.base import ConnectionManager

logger = logging.getLogger(__name__)


class StdioConnectionManager(ConnectionManager[tuple[Any, Any]]):
    """Connection manager for stdio-based MCP connections.

    This class handles the proper task isolation for stdio_client context managers
    to prevent the "cancel scope in different task" error. It runs the stdio_client
    in a dedicated task and manages its lifecycle.
    """

    def __init__(
        self,
        server_params: StdioServerParameters,
        errlog: TextIO = sys.stderr,
    ):
        """Initialize a new stdio connection manager.

        Args:
            server_params: The parameters for the stdio server
            errlog: The error log stream
        """
        super().__init__()
        self.server_params = server_params
        self.errlog = errlog
        self._stdio_ctx = None

    async def _establish_connection(self) -> tuple[Any, Any]:
        """Establish a stdio connection.

        Returns:
            A tuple of (read_stream, write_stream)

        Raises:
            Exception: If connection cannot be established.
        """
        # Create the context manager
        self._stdio_ctx = stdio_client(self.server_params, self.errlog)

        # Enter the context manager
        read_stream, write_stream = await self._stdio_ctx.__aenter__()

        # Return the streams
        return (read_stream, write_stream)

    async def _close_connection(self) -> None:
        """Close the stdio connection."""
        if self._stdio_ctx:
            # Exit the context manager
            try:
                await self._stdio_ctx.__aexit__(None, None, None)
            except Exception as e:
                logger.warning(f"Error closing stdio context: {e}")
            finally:
                self._stdio_ctx = None
