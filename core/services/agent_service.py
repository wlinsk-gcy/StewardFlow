"""
Agent 服务层
负责 Agent 的业务逻辑：创建、执行、查询、HITL 处理
"""

import asyncio
from typing import Optional

from core.protocol import (
    AgentState, NodeType, AgentStatus, HITLResult
)
from core.tools.tool import ToolRegistry
from core.executor import AgentExecutor
from core.llm import Provider
from core.storage.checkpoint import CheckpointStore
from ws.connection_manager import ConnectionManager


class AgentService:
    """Agent 业务逻辑服务"""

    def __init__(self, checkpoint: CheckpointStore, provider: Provider, tool_registry: ToolRegistry, ws_manager: ConnectionManager):
        self.checkpoint = checkpoint
        self.provider = provider
        self.tool_registry = tool_registry
        self.ws_manager = ws_manager
        self._executors: dict[str, AgentExecutor] = {}
        self._executor_lock = asyncio.Lock()

    async def create_agent(
        self,
        cliend_id: str,
        task: str,
        max_turns: int = 10,

    ) -> AgentState:
        agent = AgentState(
            client_id=cliend_id,
            status=AgentStatus.IDLE,
            current_node=NodeType.THINK,
            task=task,
            max_turns=max_turns,
        )
        # 保存
        self.checkpoint.save(agent)

        return agent

    async def execute_agent(self, agent_id: str):
        agent = self.checkpoint.load(agent_id) # 获取checkpoint
        if not agent:
            raise ValueError(f"Agent not found: {agent_id}")

        if agent.status != AgentStatus.IDLE:
            raise ValueError(f"Agent is not idle: {agent.status.value}")

        # 创建 Executor
        executor = AgentExecutor(
            llm=self.provider,
            tool_registry=self.tool_registry,
            agent_state=agent,
            checkpoint=self.checkpoint,
            ws_manager=self.ws_manager,
            verbose=True
        )

        # 存储 Executor
        # TODO 如果使用asyncio.create_task，需要确保executor被完全释放并GC，否则会导致协程任务堆积，内存泄露
        async with self._executor_lock:
            self._executors[agent_id] = executor

        # 执行
        try:
            await executor.run()
        except Exception as e:
            agent.status = AgentStatus.FAILED
            agent.error_message = str(e)
            self.checkpoint.save(agent)
        # TODO 不要finally去删除，在任务finish之后再删除，如果支持回放，就不要删除了
        # finally:
        #     # 清理 Executor
        #     async with self._executor_lock:
        #         if agent_id in self._executors:
        #             del self._executors[agent_id]

    def get_agent(self, agent_id: str) -> Optional[AgentState]:
        """查询 Agent 状态

        Args:
            agent_id: Agent ID

        Returns:
            AgentState 或 None
        """
        return self.checkpoint.load(agent_id)


    async def submit_hitl(self, agent_id: str, request_id: str, input_text: str) -> HITLResult:

        executor = self._executors.get(agent_id)
        if not executor:
            return HITLResult(success=False)

        result = executor.process_hitl(request_id, input_text)
        if result.success:
            await executor.run()

        return result
