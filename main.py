"""
FastAPI ReAct + HITL Agent MVP
Entry point.
"""
import sys
import asyncio
import os
from pathlib import Path
from typing import Any, Tuple

if sys.platform.startswith("win"):
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
import logging
import yaml
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from context import request_id_ctx, trace_id_ctx

from utils.id_util import get_sonyflake
from utils.tool_artifacts_util import clear_tool_artifacts
from core.llm import Provider
from core.storage.checkpoint import CheckpointStore
from core.tools.tool import ToolRegistry
from core.tools.sandbox import register_sandbox_tools
from core.mcp.client import MCPClient
from core.mcp.startup import start_mcp_initialization, stop_mcp_initialization

from core.services.task_service import TaskService
from core.services.sandbox_manager import SandboxManager, SandboxManagerError
from api.routes import router as agent_router
from api.sandbox_routes import router as sandbox_router
from ws.connection_manager import ConnectionManager
from core.cache_manager import InMemoryCacheManager
from core.builder.build import build_system_prompt
from core.trace_event_logger import configure_trace_event_logger

PROJECT_ROOT = Path(__file__).resolve().parent

with (PROJECT_ROOT / "config.yaml").open("r", encoding="utf-8") as f:
    config = yaml.safe_load(f)

runtime_cfg = config.get("runtime") or {}
configure_trace_event_logger(
    mode=runtime_cfg.get("trace_event_log_mode"),
    preview_chars=runtime_cfg.get("trace_event_log_preview_chars"),
    rate_limit_sec=runtime_cfg.get("trace_event_log_rate_limit_sec"),
)


class RequestIdFilter(logging.Filter):
    def filter(self, record):
        # request
        record.request_id = request_id_ctx.get()
        # agent runtime
        record.trace_id = trace_id_ctx.get()
        return True


logger = logging.getLogger()
logger.setLevel(logging.INFO)

handler = logging.StreamHandler()
formatter = logging.Formatter(
    "%(asctime)s | req=%(request_id)s | trace=%(trace_id)s | "
    "%(levelname)s | %(filename)s:%(lineno)d | %(name)s | %(message)s"
)
handler.setFormatter(formatter)
handler.addFilter(RequestIdFilter())

default_log_file = PROJECT_ROOT / "data" / "logs" / "stewardflow.log"
log_file_path = Path(os.getenv("STEWARDFLOW_LOG_FILE", str(default_log_file)))
log_file_path.parent.mkdir(parents=True, exist_ok=True)
file_handler = logging.FileHandler(log_file_path, encoding="utf-8")
file_handler.setFormatter(formatter)
file_handler.addFilter(RequestIdFilter())

logger.handlers.clear()
logger.addHandler(handler)
logger.addHandler(file_handler)

def _is_within_root(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except Exception:
        return False


def _close_data_file_handlers(data_root: Path) -> None:
    root_logger = logging.getLogger()
    for log_handler in list(root_logger.handlers):
        file_name = getattr(log_handler, "baseFilename", None)
        if not file_name:
            continue
        if not _is_within_root(Path(file_name), data_root):
            continue
        root_logger.removeHandler(log_handler)
        try:
            log_handler.close()
        except Exception as exc:
            logger.warning("Failed to close log handler '%s': %s", file_name, exc)


def _clear_data_files(data_root: Path) -> Tuple[int, int]:
    target_root = data_root.resolve()
    if not target_root.exists():
        return 0, 0
    if target_root != (PROJECT_ROOT / "data").resolve():
        raise RuntimeError(f"Refusing to clear unexpected data root: {target_root}")

    deleted = 0
    failed = 0
    for target in target_root.rglob("*"):
        if not target.is_file() and not target.is_symlink():
            continue
        try:
            target.unlink()
            deleted += 1
        except Exception as exc:
            failed += 1
            logger.warning("Failed to delete data file '%s': %s", target, exc)
    return deleted, failed


def _prune_empty_data_dirs(data_root: Path) -> Tuple[int, int]:
    target_root = data_root.resolve()
    if not target_root.exists():
        return 0, 0
    if target_root != (PROJECT_ROOT / "data").resolve():
        raise RuntimeError(f"Refusing to prune unexpected data root: {target_root}")

    removed = 0
    failed = 0
    dirs = [p for p in target_root.rglob("*") if p.is_dir()]
    dirs.sort(key=lambda p: len(p.parts), reverse=True)
    for directory in dirs:
        if directory == target_root:
            continue
        try:
            # Remove empty directories only.
            next(directory.iterdir())
            continue
        except StopIteration:
            try:
                directory.rmdir()
                removed += 1
            except Exception as exc:
                failed += 1
                logger.warning("Failed to delete empty data dir '%s': %s", directory, exc)
        except Exception as exc:
            failed += 1
            logger.warning("Failed to inspect data dir '%s': %s", directory, exc)
    return removed, failed


def init_load_tools():
    registry = ToolRegistry()
    sandbox_runtime = register_sandbox_tools(registry, config.get("sandbox") or {})
    return registry, sandbox_runtime


async def _auto_create_sandbox(app: FastAPI, manager: SandboxManager, sandbox_cfg: dict[str, Any]) -> None:
    created = await asyncio.to_thread(
        manager.create,
        sandbox_id=None,
        image=sandbox_cfg.get("image"),
        start_url=str(sandbox_cfg.get("start_url", "https://www.baidu.com/")),
        display_width=int(sandbox_cfg.get("display_width", 1920)),
        display_height=int(sandbox_cfg.get("display_height", 1080)),
        user_id=1000,
        group_id=1000,
        keep_app_running=True,
        novnc_port=None,
        vnc_port=None,
        api_port=None,
        extra_env={},
        restart_policy="unless-stopped",
        wait_ready=False,
        ready_timeout_sec=1,
        public_host=sandbox_cfg.get("public_host"),
    )
    app.state.auto_sandbox_id = created.get("sandbox_id")
    app.state.auto_sandbox_created = True
    logger.info(
        "Auto-created sandbox on startup: id=%s novnc=%s api=%s ports=%s",
        app.state.auto_sandbox_id,
        created.get("urls", {}).get("novnc"),
        created.get("urls", {}).get("api"),
        created.get("ports"),
    )


async def _auto_delete_sandbox(app: FastAPI, manager: SandboxManager) -> None:
    sandbox_id = getattr(app.state, "auto_sandbox_id", None)
    if not sandbox_id:
        return

    try:
        await asyncio.to_thread(
            manager.delete,
            str(sandbox_id),
            force=True,
        )
        logger.info("Auto-deleted sandbox on shutdown: %s", sandbox_id)
    except SandboxManagerError as exc:
        if exc.status_code == 404:
            return
        logger.warning("Failed to auto-delete sandbox '%s': %s", sandbox_id, exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ===== startup =====
    ws_manager = ConnectionManager()
    checkpoint = CheckpointStore()
    tool_registry, browser_manager = init_load_tools()
    # Disable MCP tool injection for now: only local sandbox tools should be available.
    mcp_client = MCPClient(config={"mcpServers": {}})
    mcp_cfg = config.get("mcp") or {}
    startup_wait_seconds = float(mcp_cfg.get("startup_wait_seconds", 2.0))
    mcp_init_task = await start_mcp_initialization(
        mcp_client=mcp_client,
        tool_registry=tool_registry,
        startup_wait_seconds=startup_wait_seconds,
        logger=logger,
    )
    llm_config = config.get("llm")
    provider = Provider(llm_config.get("model"),
                        llm_config.get("api_key"),
                        llm_config.get("base_url"),
                        tool_registry,
                        ws_manager,
                        config.get("context"))
    cache_manager = InMemoryCacheManager(model=llm_config.get("model"), api_key=llm_config.get("api_key"),
                                         base_url=llm_config.get("base_url"),
                                         build_system_prompt_fn=build_system_prompt)

    task_service = TaskService(
        checkpoint,
        provider,
        tool_registry,
        ws_manager,
        cache_manager,
    )

    app.state.checkpoint = checkpoint
    app.state.tool_registry = tool_registry
    app.state.provider = provider
    app.state.ws_manager = ws_manager
    app.state.browser_manager = browser_manager
    app.state.task_service = task_service
    app.state.mcp_client = mcp_client
    app.state.mcp_init_task = mcp_init_task
    sandbox_cfg = config.get("sandbox") or {}
    sandbox_manager = SandboxManager(
        default_image=sandbox_cfg.get("image", "gui-sandbox:dev"),
        default_public_host=sandbox_cfg.get("public_host"),
        docker_base_url=sandbox_cfg.get("docker_base_url"),
        healthcheck_host=sandbox_cfg.get("healthcheck_host", "127.0.0.1"),
    )
    app.state.sandbox_manager = sandbox_manager
    try:
        await _auto_create_sandbox(app, sandbox_manager, sandbox_cfg)
        if browser_manager is not None and hasattr(browser_manager, "set_sandbox_id"):
            browser_manager.set_sandbox_id(getattr(app.state, "auto_sandbox_id", None))
    except Exception as exc:
        logger.warning("Sandbox auto-create failed during startup: %s", exc)
    yield

    # ===== shutdown =====
    if browser_manager is not None:
        await browser_manager.shutdown()
    await stop_mcp_initialization(getattr(app.state, "mcp_init_task", None), logger)
    app_mcp_client = getattr(app.state, "mcp_client", None)
    if app_mcp_client is not None:
        await app_mcp_client.close_all_sessions()
    app_sandbox_manager = getattr(app.state, "sandbox_manager", None)
    if app_sandbox_manager is not None:
        try:
            await _auto_delete_sandbox(app, app_sandbox_manager)
        except Exception as exc:
            logger.warning("Sandbox auto-delete failed during shutdown: %s", exc)
        app_sandbox_manager.close()
    clear_tool_artifacts()
    data_root = PROJECT_ROOT / "data"
    _close_data_file_handlers(data_root)
    deleted_count, failed_count = _clear_data_files(data_root)
    removed_dirs, failed_dirs = _prune_empty_data_dirs(data_root)
    logger.info(
        "Shutdown data cleanup completed: deleted_files=%s failed_files=%s "
        "removed_empty_dirs=%s failed_empty_dirs=%s root=%s",
        deleted_count,
        failed_count,
        removed_dirs,
        failed_dirs,
        data_root,
    )


# FastAPI application
app = FastAPI(
    title="StewardFlow",
    description="I do the work. You stay in control.",
    version="0.1.0",
    lifespan=lifespan,
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Allow all origins in MVP.
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    request_id = request.headers.get("X-Request-ID", get_sonyflake("log"))
    token = request_id_ctx.set(request_id)
    try:
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response
    finally:
        request_id_ctx.reset(token)


# Register routers
app.include_router(agent_router)
app.include_router(sandbox_router)


@app.websocket("/ws/{client_id}")
async def websocket_endpoint(websocket: WebSocket, client_id: str):
    """WebSocket endpoint."""
    ws_manager = app.state.ws_manager
    await ws_manager.connect(websocket, client_id)
    try:
        while True:
            # Keep connection alive.
            await websocket.receive_text()
    except WebSocketDisconnect:
        ws_manager.disconnect(client_id)
        logger.info(f"Disconnected from client: {client_id}")


@app.get("/")
async def root():
    """Root endpoint."""
    return {
        "name": "StewardFlow",
        "version": "0.1.0",
        "endpoints": {
            "POST /agent/run": "Start agent run",
            "GET /agent/health": "Agent health check",
        }
    }


@app.get("/health")
async def health():
    """Service health check."""
    return {
        "status": "healthy",
        "service": "StewardFlow healthy",
    }

if __name__ == "__main__":
    import uvicorn

    app_config = config.get("app")
    port = int(app_config.get("port")) or 8080
    log_config = config.get("log")
    log_level = log_config.get("level") or "info"
    # Development server
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=port,
        log_level=log_level
    )


