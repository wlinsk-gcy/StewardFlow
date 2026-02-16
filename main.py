"""
FastAPI ReAct + HITL Agent MVP
入口文件
"""
import sys
import asyncio
import os
from pathlib import Path

if sys.platform.startswith("win"):
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
import logging
import yaml
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from context import request_id_ctx, trace_id_ctx

from utils.id_util import get_sonyflake
from utils.screenshot_util import clean_screenshot
from utils.tool_artifacts_util import clear_tool_artifacts
from core.llm import Provider
from core.runtime_settings import RuntimeSettings, configure_runtime_settings
from core.storage.checkpoint import CheckpointStore
from core.tools.tool import ToolRegistry
from core.tools.proc_run import ProcRunTool
from core.tools.fs_tools import FsListTool, FsGlobTool, FsReadTool, FsWriteTool, FsStatTool
from core.tools.text_search import TextSearchTool
from core.tools.rg_loader import ensure_rg
from core.mcp.client import MCPClient

from core.services.task_service import TaskService
from api.routes import router as agent_router
from ws.connection_manager import ConnectionManager
from core.cache_manager import InMemoryCacheManager
from core.builder.build import build_system_prompt

PROJECT_ROOT = Path(__file__).resolve().parent

with (PROJECT_ROOT / "config.yaml").open("r", encoding="utf-8") as f:
    config = yaml.safe_load(f)

runtime_settings = configure_runtime_settings(
    raw_tool_result=config.get("tool_result") or {},
    env=os.environ,
    workspace_root=PROJECT_ROOT,
    allow_env_override=True,
)
# Optional compatibility mirror for legacy scripts.
os.environ["TOOL_RESULT_ROOT_DIR"] = runtime_settings.tool_result_root_dir
os.environ["TOOL_RESULT_FS_READ_MAX_CHARS"] = str(runtime_settings.fs_read_max_chars)


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


def init_load_tools(settings: RuntimeSettings):
    registry = ToolRegistry()
    from core.tools.web_search_use_exa import WebSearch
    registry.register(WebSearch())
    registry.register(FsListTool(settings=settings))
    registry.register(FsGlobTool(settings=settings))
    registry.register(FsReadTool(settings=settings))
    registry.register(FsWriteTool(settings=settings))
    registry.register(FsStatTool(settings=settings))
    registry.register(TextSearchTool(settings=settings))
    registry.register(ProcRunTool())
    return registry


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ===== startup =====
    rg_path, installed_now = ensure_rg(project_root=PROJECT_ROOT)
    app.state.rg_path = str(rg_path)
    app.state.rg_installed_now = installed_now
    os.environ["TOOL_RESULT_RG_PATH"] = str(rg_path)

    ws_manager = ConnectionManager()
    checkpoint = CheckpointStore()
    tool_registry = init_load_tools(runtime_settings)
    mcp_client = MCPClient(config="./mcp_config.json")
    await mcp_client.initialize(tool_registry)
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
        runtime_settings=runtime_settings,
    )

    app.state.checkpoint = checkpoint
    app.state.tool_registry = tool_registry
    app.state.provider = provider
    app.state.ws_manager = ws_manager

    app.state.task_service = task_service
    yield

    # ===== shutdown =====
    await mcp_client.close_all_sessions()
    clean_screenshot()
    clear_tool_artifacts()


# 创建 FastAPI 应用
app = FastAPI(
    title="StewardFlow",
    description="I do the work. You stay in control.",
    version="0.1.0",
    lifespan=lifespan,
)

# 添加 CORS 中间件
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # MVP 允许所有来源
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


# 注册路由
app.include_router(agent_router)


@app.websocket("/ws/{client_id}")
async def websocket_endpoint(websocket: WebSocket, client_id: str):
    """WebSocket 端点 (保持长连接)"""
    ws_manager = app.state.ws_manager
    await ws_manager.connect(websocket, client_id)
    try:
        while True:
            # 一般不需要前端发消息，只 keep alive, 保持连接活跃，也可以处理前端通过 WS 发来的心跳
            await websocket.receive_text()
    except WebSocketDisconnect:
        ws_manager.disconnect(client_id)
        logger.info(f"Disconnected from client: {client_id}")


# 根路径
@app.get("/")
async def root():
    """根路径"""
    return {
        "name": "StewardFlow",
        "version": "0.1.0",
        "endpoints": {
            "POST /agent/run": "启动 Agent",
            "GET /agent/health": "健康检查"
        }
    }


# 健康检查
@app.get("/health")
async def health():
    """健康检查"""
    return {
        "status": "healthy",
        "service": "StewardFlow healthy",
        "rg_path": getattr(app.state, "rg_path", None),
        "installed_now": getattr(app.state, "rg_installed_now", None),
    }


if __name__ == "__main__":
    import uvicorn

    app_config = config.get("app")
    port = int(app_config.get("port")) or 8080
    log_config = config.get("log")
    log_level = log_config.get("level") or "info"
    # 开发服务器
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=port,
        log_level=log_level
    )
