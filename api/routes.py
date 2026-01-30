"""
FastAPI 路由定义
Agent REST API 端点
"""
import asyncio
import logging
from fastapi import APIRouter, HTTPException, Depends

from core.protocol import (
    AgentStatus, NodeType, HITLResponse, RunAgentRequest, RunAgentResponse
)

from core.services.agent_service import AgentService
from core.services.task_service import TaskService
from core.storage.checkpoint import CheckpointStore

logger = logging.getLogger(__name__)


# 全局依赖注入（通过 app.state 传递）
def get_agent_service() -> AgentService:
    """获取 AgentService 实例"""
    from main import app
    return app.state.agent_service

def get_task_service() -> TaskService:
    from main import app
    return app.state.task_service


def get_checkpoint() -> CheckpointStore:
    """获取 Storage 实例"""
    from main import app
    return app.state.checkpoint


# 创建路由
router = APIRouter(prefix="/agent", tags=["Agent"])


@router.post("/run", response_model=RunAgentResponse, status_code=201)
async def run_agent(
        request: RunAgentRequest,
        task_service: TaskService = Depends(get_task_service)
        # agent_service: AgentService = Depends(get_agent_service)
):
    """
    启动一次 Agent 执行

    流程：
    1. 创建 Agent 实例
    2. 启动后台任务执行 ReAct 循环
    3. 立即返回 agent_id
    """

    def callback(task: asyncio.Task):
        try:
            exc = task.exception()
            if exc:
                logger.error(f"捕获异常: {type(exc).__name__}, {exc}")
        except asyncio.CancelledError:
            logger.warning("run_agent任务被取消")

    if request.trace_id:
        trace = await task_service.get_trace(request.trace_id)
        if not trace:
            raise HTTPException(status_code=404, detail="Trace not found")
        if trace.status == AgentStatus.WAITING and trace.node == NodeType.HITL:
            hitl_task = asyncio.create_task(task_service.submit_hitl(trace,request.task))
            hitl_task.add_done_callback(callback)
            return RunAgentResponse(trace_id=request.trace_id)
        elif trace.status == AgentStatus.DONE and trace.node == NodeType.END:
            await task_service.new_turn(trace, request.task)
            task = asyncio.create_task(task_service.start(trace))
            task.add_done_callback(callback)
            return RunAgentResponse(trace_id=request.trace_id)
        else:
            raise HTTPException(status_code=404, detail="Trace Status is invalid")

    trace = await task_service.initialize(request.task, request.client_id)
    task = asyncio.create_task(task_service.start(trace))
    task.add_done_callback(callback)

    # 返回
    return RunAgentResponse(trace_id=trace.trace_id)


@router.get("/health")
async def health_check():
    """健康检查"""
    return {"status": "healthy", "service": "agent"}
