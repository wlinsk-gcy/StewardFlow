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
from core.storage.checkpoint import CheckpointStore

logger = logging.getLogger(__name__)


# 全局依赖注入（通过 app.state 传递）
def get_agent_service() -> AgentService:
    """获取 AgentService 实例"""
    from main import app
    return app.state.agent_service


def get_checkpoint() -> CheckpointStore:
    """获取 Storage 实例"""
    from main import app
    return app.state.checkpoint


# 创建路由
router = APIRouter(prefix="/agent", tags=["Agent"])


@router.post("/run", response_model=RunAgentResponse, status_code=201)
async def run_agent(
        request: RunAgentRequest,
        agent_service: AgentService = Depends(get_agent_service)
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

    if request.agent_id:
        agent = agent_service.get_agent(request.agent_id)
        if not agent:
            raise HTTPException(status_code=404, detail="Agent not found")
        if (agent.status == AgentStatus.WAITING
                and agent.current_node == NodeType.HITL and agent.hitl):
            # 提交到服务层处理
            hitl_task = asyncio.create_task(
                agent_service.submit_hitl(request.agent_id, request_id=agent.hitl['request_id'],
                                          input_text=request.task))
            hitl_task.add_done_callback(callback)
            return RunAgentResponse(
                agent_id=request.agent_id,
            )

    # 创建 Agent
    agent = await agent_service.create_agent(
        cliend_id=request.client_id,
        task=request.task,
        max_turns=50,
    )
    task = asyncio.create_task(agent_service.execute_agent(agent.agent_id))
    task.add_done_callback(callback)

    # 返回
    return RunAgentResponse(
        agent_id=agent.agent_id
    )


@router.get("/health")
async def health_check():
    """健康检查"""
    return {"status": "healthy", "service": "agent"}
