"""
FastAPI ReAct + HITL Agent MVP
入口文件
"""
import sys
import asyncio
if sys.platform.startswith("win"):
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
import logging
import yaml
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from context import request_id_ctx, agent_id_ctx

from utils.id_util import get_sonyflake
from utils.screenshot_util import clean_screenshot
from core.llm import Provider
from core.storage.checkpoint import CheckpointStore
from core.tools.tool import ToolRegistry
from core.tools.bash import BashTool
from core.mcp.client import MCPClient

from core.services.agent_service import AgentService
from api.routes import router as agent_router
from ws.connection_manager import ConnectionManager

with open("config.yaml", "r", encoding="utf-8") as f:
    config = yaml.safe_load(f)


class RequestIdFilter(logging.Filter):
    def filter(self, record):
        # request
        record.request_id = request_id_ctx.get()
        # agent runtime
        record.agent_id = agent_id_ctx.get()
        return True


logger = logging.getLogger()
logger.setLevel(logging.INFO)

handler = logging.StreamHandler()
formatter = logging.Formatter(
    "%(asctime)s | req=%(request_id)s | agent=%(agent_id)s | "
    "%(levelname)s | %(filename)s:%(lineno)d | %(name)s | %(message)s"
)
handler.setFormatter(formatter)
handler.addFilter(RequestIdFilter())

logger.handlers.clear()
logger.addHandler(handler)


def init_load_tools(tools_config: dict | None = None):
    registry = ToolRegistry()
    search_config = tools_config.get("web_search") if tools_config else None
    if search_config:
        from core.tools.web_search_use_serpapi import WebSearch
        registry.register(WebSearch(search_config.get("api_key"), search_config.get("paywall_keywords")))
    else:
        from core.tools.web_search_use_exa import WebSearch
        registry.register(WebSearch())
    registry.register(BashTool())
    return registry


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ===== startup =====
    ws_manager = ConnectionManager() # ws管理器
    checkpoint = CheckpointStore() # AgentState快照储存器
    tool_registry = init_load_tools(config.get("tools")) # 工具管理器
    mcp_client = MCPClient(config = "./mcp_config.json")
    await mcp_client.initialize(tool_registry)
    llm_config = config.get("llm")
    provider = Provider(llm_config.get("model"),
                        llm_config.get("api_key"),
                        llm_config.get("base_url"),
                        tool_registry,
                        ws_manager)


    agent_service = AgentService(checkpoint, provider, tool_registry, ws_manager)

    app.state.checkpoint = checkpoint
    app.state.provider = provider
    app.state.agent_service = agent_service
    app.state.ws_manager = ws_manager

    yield

    # ===== shutdown =====
    await mcp_client.close_all_sessions()
    clean_screenshot()


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
        "service": "StewardFlow healthy"
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
