import asyncio
import logging
from contextlib import suppress
from typing import Any, Protocol


class _MCPClientLike(Protocol):
    async def initialize(self, registry: Any) -> None:
        ...


async def start_mcp_initialization(
    mcp_client: _MCPClientLike,
    tool_registry: Any,
    startup_wait_seconds: float,
    logger: logging.Logger,
) -> asyncio.Task[None]:
    async def _runner() -> None:
        try:
            await mcp_client.initialize(tool_registry)
        except asyncio.CancelledError:
            logger.info("MCP initialization task cancelled")
            raise
        except Exception as exc:
            logger.error("MCP initialization failed: %s", exc)

    task = asyncio.create_task(_runner(), name="mcp_initialize")

    if startup_wait_seconds > 0:
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=startup_wait_seconds)
        except asyncio.TimeoutError:
            logger.warning(
                "MCP initialization is still running after %.2fs; startup will continue.",
                startup_wait_seconds,
            )

    return task


async def stop_mcp_initialization(task: asyncio.Task[None] | None, logger: logging.Logger) -> None:
    if task is None or task.done():
        return

    task.cancel()
    with suppress(asyncio.CancelledError):
        await task
    logger.info("MCP initialization background task stopped")
