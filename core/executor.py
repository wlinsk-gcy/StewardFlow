"""
ReAct 执行引擎
实现 Thought → Action → Observation 的循环逻辑
"""
import json
import logging
from datetime import datetime
from typing import Optional

from .protocol import (
    AgentStatus, NodeType, Thought, Action, Observation, Pending, HITLRequest,
    HITLResponse, HITLResult, AgentState, ActionType, HITLRequestType, Event, EventType
)
from .llm import Provider
from .tools.tool import ToolRegistry
from .storage.checkpoint import CheckpointStore
from context import agent_id_ctx
from utils.id_util import get_sonyflake
from .extractor import extract_json, normalize_llm_dict
from ws.connection_manager import ConnectionManager

logger = logging.getLogger(__name__)


class AgentExecutor:
    """ReAct 执行引擎

    负责：
    1. 执行 ReAct 循环（Thought → Action → Observation）
    2. 管理状态流转
    3. 处理 HITL 介入
    """

    def __init__(
            self,
            llm: Provider,
            tool_registry: ToolRegistry,
            agent_state: AgentState,
            checkpoint: CheckpointStore,
            ws_manager: ConnectionManager,
            verbose: bool = True
    ):
        self.llm = llm
        self.tool_registry = tool_registry
        self.agent = agent_state
        self.verbose = verbose
        self.checkpoint = checkpoint
        self.ws_manager = ws_manager
        self.turn_id: str = ''
        agent_id_ctx.set(self.agent.agent_id)
        # self.stream = True
        self.stream = False

    def log(self, message: str):
        """打印日志"""
        if self.verbose:
            print(message)

    def parse_llm_result(self, result: str) -> tuple[Thought, Action]:
        llm_dict = normalize_llm_dict(extract_json(result))
        thought = Thought(
            content=llm_dict["thought"],
            turn_id=self.turn_id
        )
        # 封装成 Action 对象
        action_dict = llm_dict["action"]
        action = Action(
            type=ActionType(action_dict["type"]),
            tool_name=action_dict.get("tool_name"),
            args=action_dict.get("args"),
            prompt=action_dict.get("prompt"),
            answer=action_dict.get("answer"),
            turn_id=self.turn_id
        )
        logger.info(f"[THOUGHT] {thought.content}")
        logger.info(f"[ACTION] {action}")
        return thought, action

    async def think_node(self):
        """THINK 节点"""
        logger.info(f"[TURN {self.agent.current_turn}] [THINK] {self.agent.current_node}")
        self.turn_id = get_sonyflake()  # 轮次id
        # 生成上下文
        context = {
            "client_id": self.agent.client_id,
            "agent_id": self.agent.agent_id,
            "turn_id": self.turn_id,
            "task": self.agent.task,
            "tao_trajectory": self.agent.tao_trajectory,
            "turn": self.agent.current_turn,
            "max_turns": self.agent.max_turns
        }
        if self.stream:
            result = await self.llm.stream_generate(context)
        else:
            result = self.llm.generate(context)
        thought, action = self.parse_llm_result(result)
        self.agent.pending = Pending(thought, action, False)
        event = Event(EventType.THOUGHT, self.agent.agent_id, self.turn_id, thought.to_dict())
        await self.ws_manager.send(event.to_dict(), client_id=self.agent.client_id)
        if not self.stream and action.type == ActionType.FINISH:
            event = Event(EventType.ANSWER, self.agent.agent_id, self.turn_id, {"content": action.answer})
            await self.ws_manager.send(event.to_dict(), client_id=self.agent.client_id)
            event = Event(EventType.END, self.agent.agent_id, self.turn_id, {"content": "done"})
            await self.ws_manager.send(event.to_dict(), client_id=self.agent.client_id)


    async def decide_next_node(self):
        """DECIDE（Router）,LangGraph 的灵魂
        next node = 函数返回值
        """
        logger.info(f"[TURN {self.agent.current_turn}] [DECIDE] {self.agent.current_node}")
        pending = self.agent.pending
        if not pending:
            self.agent.status = AgentStatus.FAILED
            self.agent.current_node = NodeType.END
            logger.error("任务运行到decide_next_node时，pending为空，任务异常")
            return

        if pending.action.type == ActionType.FINISH:
            self.agent.current_node = NodeType.END
        elif pending.action.type in [ActionType.REQUEST_CONFIRM, ActionType.REQUEST_INPUT]:
            self.agent.current_node = NodeType.HITL
        else:
            self.agent.current_node = NodeType.EXECUTE
            event = Event(EventType.ACTION, self.agent.agent_id, self.turn_id, self.agent.pending.action.to_dict())
            await self.ws_manager.send(event.to_dict(), client_id=self.agent.client_id)
        self.checkpoint.save(self.agent)


    async def execute_node(self):
        """EXECUTE 节点"""
        logger.info(f"[TURN {self.agent.current_turn}] [EXECUTE] {self.agent.current_node}")
        pending = self.agent.pending
        if not pending:
            self.agent.status = AgentStatus.FAILED
            self.agent.current_node = NodeType.END
            logger.error("任务运行到execute_node时，pending为空，任务异常")
            self.checkpoint.save(self.agent)
            return

        current_action = pending.action
        if current_action.type == ActionType.TOOL:
            tool = self.tool_registry.get(current_action.tool_name)
            if tool and tool.requires_confirmation and not getattr(pending, "confirmed", False):
                prompt = f"Confirm to execute tool '{current_action.tool_name}' with args: {current_action.args}"
                await self._execute_request_confirm(
                    self.turn_id,
                    prompt=prompt,
                    context=f"tool:{current_action.tool_name}",
                    tool_name=current_action.tool_name,
                    tool_args=current_action.args
                )
                self.agent.status = AgentStatus.WAITING
                self.agent.current_node = NodeType.HITL
                self.checkpoint.save(self.agent)
                return

            observation = await self._execute_tool(self.turn_id)
            self.agent.current_node = NodeType.OBSERVE
            self.append_tao_trajectory(observation)
        elif current_action.type == ActionType.FINISH:
            observation = Observation(content=self.agent.pending.action.answer, turn_id=self.turn_id, success=True)
            self.agent.current_node = NodeType.END  # 流程结束
            self.append_tao_trajectory(observation)
        else:
            observation = Observation(
                content=f"Unknown action type: {current_action.type}",
                turn_id=self.turn_id,
                success=False,
                error=f"Unknown action type: {current_action.type}"
            )
            self.agent.status = AgentStatus.FAILED
            self.agent.current_node = NodeType.END
            self.append_tao_trajectory(observation)
            logger.error("任务运行到execute_node时，current_action 不存在，任务异常")
        self.checkpoint.save(self.agent)  # 一轮 ReAct 结束后保存快照

    async def hitl_node(self):
        """HITL 节点（重点）, 没有await， 没有阻塞"""
        logger.info(f"[TURN {self.agent.current_turn}] [HITL] {self.agent.current_node}")
        if self.agent.pending.action.type not in [ActionType.REQUEST_INPUT, ActionType.REQUEST_CONFIRM]:
            self.agent.status = AgentStatus.FAILED
            self.agent.current_node = NodeType.END
            logger.error("任务运行到hitl_node时，ActionType有误，任务异常")
            self.checkpoint.save(self.agent)
            return
        self.agent.status = AgentStatus.WAITING
        self.agent.current_node = NodeType.HITL  # HITL 人类介入
        if self.agent.pending.action.type == ActionType.REQUEST_CONFIRM:
            await self._execute_request_confirm(
                self.turn_id,
                prompt=self.agent.pending.action.prompt,
                context="action_confirm"
            )
        else:
            self._execute_request_input(self.turn_id, self.agent.pending.action)
        self.checkpoint.save(self.agent)  # 进入HITL时需要Save Checkpoint
        return  # 🚨 停止执行

    def append_tao_trajectory(self, observation: Observation):
        logger.debug(f"[OBSERVATION] {observation.content}")
        # 拿到observation相当于一轮ReAct结束，保存到tao_trajectory中
        trajectory = {
            "turn_id": self.turn_id,
            "thought": self.agent.pending.thought.to_dict(),
            "action": self.agent.pending.action.to_dict(),
            "observation": observation.to_dict(),
            "timestamp": datetime.utcnow().isoformat(),
        }
        self.agent.tao_trajectory.append(trajectory)

    async def run(self):
        """执行 ReAct 循环"""
        logger.debug("=" * 60)
        logger.debug(f"[AGENT STARTED] Task: {self.agent.task}")
        logger.debug(f"[INFO] Max turns: {self.agent.max_turns}")
        logger.debug("=" * 60)

        # 更新状态
        self.agent.status = AgentStatus.RUNNING
        self.agent.started_at = datetime.utcnow()

        try:
            # ReAct 循环
            while (self.agent.current_turn < self.agent.max_turns
                   and self.agent.status not
                   in [AgentStatus.DONE, AgentStatus.FAILED,
                       AgentStatus.WAITING, AgentStatus.PAUSED]):

                if self.agent.current_node == NodeType.THINK:
                    # 1. 思考阶段
                    await self.think_node()
                    self.agent.current_node = NodeType.DECIDE
                    self.checkpoint.save(self.agent)
                elif self.agent.current_node == NodeType.DECIDE:
                    # 2. 检查是否需要人工确认 -- 返回下一个节点
                    await self.decide_next_node()
                elif self.agent.current_node == NodeType.EXECUTE:
                    # 3.1. 执行 Action
                    await self.execute_node()
                elif self.agent.current_node == NodeType.HITL:
                    # 3.2. 执行HITL -- Execute和Hitl是同级节点，
                    # DECIDE -> [EXECUTE,HITL] 二选一
                    await self.hitl_node()
                    return
                elif self.agent.current_node == NodeType.OBSERVE:
                    # 观察阶段 -- 开启下一轮思考
                    logger.info(f"[TURN {self.agent.current_turn}] [OBSERVE] {self.agent.current_node}")
                    event = Event(EventType.OBSERVATION,
                                  self.agent.agent_id,
                                  self.turn_id,
                                  self.agent.tao_trajectory[-1]['observation'])
                    await self.ws_manager.send(event.to_dict(), client_id=self.agent.client_id)
                    self.agent.pending = None
                    self.agent.current_turn += 1
                    self.agent.current_node = NodeType.THINK
                    self.checkpoint.save(self.agent)
                elif self.agent.current_node == NodeType.END:
                    # 4. 检查是否完成
                    logger.info(f"[TURN {self.agent.current_turn}] [END] {self.agent.current_node}")
                    self.agent.status = AgentStatus.DONE
                    self.agent.finished_at = datetime.utcnow()
                    self.checkpoint.save(self.agent)
                    content = self.agent.tao_trajectory[-1]['observation'][
                        'content'] if self.agent.tao_trajectory else self.agent.pending.action.answer
                    event = Event(EventType.FINAL,
                                  self.agent.agent_id,
                                  self.turn_id,
                                  {"content": content})
                    await self.ws_manager.send(event.to_dict(), client_id=self.agent.client_id)
                    return

            # 检查循环结束原因
            if self.agent.current_turn >= self.agent.max_turns:
                logger.error(f"[TURN {self.agent.current_turn}] AgentId: {self.agent.agent_id}, 达到最大轮次限制")
                self._fail("达到最大轮次限制")
        except Exception as e:
            event = Event(EventType.ERROR,
                          self.agent.agent_id,
                          self.turn_id,
                          {"content": str(e)})
            await self.ws_manager.send(event.to_dict(), client_id=self.agent.client_id)
            self._fail(f"[TURN {self.agent.current_turn}]  执行异常: {str(e)}")
            raise

    async def _execute_tool(self, turn_id: str) -> Observation:
        """执行 Tool"""
        tool_name = self.agent.pending.action.tool_name
        args = self.agent.pending.action.args

        logger.debug(f"[EXECUTING] Action: tool")
        logger.debug(f"Tool: {tool_name}")
        logger.debug(f"Args: {args}")

        tool = self.tool_registry.get(tool_name)
        if not tool:
            return Observation(
                role="tool",
                content=f"Tool '{tool_name}' not found",
                turn_id=turn_id,
                success=False,
                error=f"Tool not found: {tool_name}"
            )
        try:
            execute_result = tool.execute(**args)
            return Observation(
                role="tool",
                content= execute_result,
                turn_id=turn_id,
                success=True,
            )
        except RuntimeError as e:
            return Observation(
                role="tool",
                content=f"Tool '{tool_name}' executed error: {str(e)}",
                turn_id=turn_id,
                success=False,
                error=f"Tool '{tool_name}' executed error: {str(e)}"
            )

    def _execute_request_input(self, turn_id: str, action: Action):
        """执行请求输入（HITL）"""
        # 创建 HITL 请求
        hitl_request = HITLRequest(
            request_type=HITLRequestType.USER_INPUT,
            prompt=action.prompt or "请提供输入",
            turn_id=turn_id
        )
        self.agent.pending.requires_hitl = True
        self.agent.hitl = hitl_request
        # 增加 HITL 计数
        self.agent.hitl_count += 1
        logger.debug(f"[HITL] Action requests human input: {hitl_request.prompt}")
        logger.debug("Waiting for input...")

    async def _execute_request_confirm(
        self,
        turn_id: str,
        prompt: Optional[str],
        context: Optional[str] = None,
        tool_name: Optional[str] = None,
        tool_args: Optional[dict] = None
    ):
        prompt_text = prompt or "Please confirm the action."
        hitl_request = HITLRequest(
            request_type=HITLRequestType.CONFIRM_ACTION,
            prompt=prompt_text,
            context=context,
            turn_id=turn_id
        )
        self.agent.pending.requires_hitl = True
        self.agent.hitl = hitl_request
        self.agent.hitl_count += 1

        event = Event(
            EventType.HITL_CONFIRM,
            self.agent.agent_id,
            turn_id,
            {
                "request_id": hitl_request.request_id,
                "prompt": prompt_text,
                "context": context,
                "tool_name": tool_name,
                "args": tool_args or {}
            }
        )
        await self.ws_manager.send(event.to_dict(), client_id=self.agent.client_id)
        logger.debug(f"[HITL] Action requests confirmation: {hitl_request.prompt}")
        logger.debug("Waiting for confirmation...")

    def _parse_confirmation(self, input_text: str) -> bool:
        normalized = (input_text or "").strip().lower()
        return normalized in {"yes", "y", "confirm", "ok", "true", "1"}

    # ========================================================================
    # HITL 外部接口
    # ========================================================================

    def process_hitl(self, request_id: str, input_text: str) -> HITLResult:
        """处理 HITL 响应（由外部调用）"""
        if not self.agent.hitl:
            return HITLResult(success=False)

        if request_id != self.agent.hitl.request_id:
            return HITLResult(success=False)

        hitl_request = self.agent.hitl
        if not self.agent.pending:
            return HITLResult(success=False)

        if hitl_request.request_type == HITLRequestType.CONFIRM_ACTION:
            accepted = self._parse_confirmation(input_text)
            self.agent.pending.requires_hitl = False
            self.agent.hitl = None

            is_tool_confirm = bool(hitl_request.context and hitl_request.context.startswith("tool:"))
            if is_tool_confirm and accepted:
                setattr(self.agent.pending, "confirmed", True)
                self.agent.current_node = NodeType.EXECUTE
                self.checkpoint.save(self.agent)
                return HITLResult(success=True)

            result_text = "User confirmed." if accepted else "User rejected."
            observation = Observation(
                content=result_text,
                turn_id=self.agent.pending.thought.turn_id,
                success=accepted,
                error=None if accepted else "hitl_rejected"
            )
            self.agent.current_node = NodeType.OBSERVE
            self.append_tao_trajectory(observation)
            self.checkpoint.save(self.agent)
            return HITLResult(success=True)

        self.agent.hitl = None
        observation = Observation(
            content=input_text,
            turn_id=self.agent.pending.thought.turn_id,
            success=True
        )
        self.agent.current_node = NodeType.OBSERVE
        self.append_tao_trajectory(observation)
        self.checkpoint.save(self.agent)
        return HITLResult(success=True)


    def _finish(self, final_answer: str):
        """任务完成"""
        logger.info(f"[TURN {self.agent.current_turn}] [END] {self.agent.current_node}")
        self.agent.status = AgentStatus.DONE
        self.agent.finished_at = datetime.utcnow()
        self.checkpoint.save(self.agent)

        logger.info(f"[AGENT FINISHED] Final answer: {final_answer}")
        logger.info(f"[AGENT END] Total turns: {self.agent.current_turn}")
        logger.info("=" * 60)

    def _fail(self, error_message: str):
        """任务失败"""
        self.agent.status = AgentStatus.FAILED
        self.agent.finished_at = datetime.utcnow()
        self.agent.error_message = error_message

        logger.error(f"[AGENT FAILED] {error_message}")
        logger.error(f"[AGENT END] Total turns: {self.agent.current_turn}")
        logger.error("=" * 60)
