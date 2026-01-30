"""
ReAct 执行引擎
实现 Thought → Action → Observation 的循环逻辑
"""
import asyncio
import json
import logging
from datetime import datetime
from typing import Optional, cast

from .protocol import (
    AgentStatus, NodeType, Thought, Action, Observation, Pending, HITLRequest,
    HITLResponse, HITLResult, AgentState, ActionType, HITLRequestType, Event, EventType,
    Trace, Turn, Step, ActionStatus, ActionV2, ObservationV2, ObservationType, StepStatus, TurnStatus
)
from .builder.build import build_system_prompt_v2
from .llm import Provider
from .tools.tool import ToolRegistry
from .storage.checkpoint import CheckpointStore
from context import agent_id_ctx
from utils.id_util import get_sonyflake
from utils.screenshot_util import wait_and_emit_screenshot_event
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
        # llm_dict = normalize_llm_dict(extract_json(result))
        llm_dict = extract_json(result)
        thought = Thought(
            content=llm_dict["thought"],
            turn_id=self.turn_id
        )
        # 封装成 Action 对象
        action_dict = llm_dict["action"]
        try:
            action = Action(
                type=ActionType(action_dict["type"]),
                tool_name=action_dict.get("tool_name"),
                args=action_dict.get("args"),
                prompt=action_dict.get("prompt"),
                answer=action_dict.get("answer"),
                turn_id=self.turn_id
            )
        except Exception as e:
            if "is not a valid ActionType" in str(e):
                logger.error(str(e))
                action = Action(
                    type=ActionType.ERROR,
                    tool_name=action_dict.get("tool_name"),
                    args=action_dict.get("args"),
                    prompt=str(e),
                    answer=action_dict.get("answer"),
                    turn_id=self.turn_id
                )
            else:
                raise
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
            result, token_info = await self.llm.stream_generate(context)
        else:
            result, token_info = self.llm.generate(context)
        thought, action = self.parse_llm_result(result)
        self.agent.pending = Pending(thought, action, False)
        if self.agent.token_info:
            self.agent.token_info["prompt_tokens"] += token_info["prompt_tokens"]
            self.agent.token_info["completion_tokens"] += token_info["completion_tokens"]
            self.agent.token_info["total_tokens"] += token_info["total_tokens"]
        else:
            self.agent.token_info = token_info
        if len(thought.content) > 0:
            event = Event(EventType.THOUGHT, self.agent.agent_id, self.turn_id, thought.to_dict())
            await self.ws_manager.send(event.to_dict(), client_id=self.agent.client_id)
        event = Event(EventType.TOKEN_INFO, self.agent.agent_id, self.turn_id, self.agent.token_info)
        await self.ws_manager.send(event.to_dict(), client_id=self.agent.client_id)
        if not self.stream:
            if action.type == ActionType.FINISH:
                event = Event(EventType.ANSWER, self.agent.agent_id, self.turn_id, {"content": action.answer})
                await self.ws_manager.send(event.to_dict(), client_id=self.agent.client_id)
                event = Event(EventType.END, self.agent.agent_id, self.turn_id, {"content": "done"})
                await self.ws_manager.send(event.to_dict(), client_id=self.agent.client_id)
            elif action.type == ActionType.REQUEST_INPUT:
                event = Event(EventType.HITL_REQUEST, self.agent.agent_id, self.turn_id, {"content": action.prompt})
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

        tool = None
        if pending.action.type == ActionType.TOOL:
            tool = self.tool_registry.get(pending.action.tool_name)

        if pending.action.type == ActionType.FINISH:
            self.agent.current_node = NodeType.END
        elif pending.action.type == ActionType.ERROR:
            self.agent.current_node = NodeType.EXECUTE
        elif pending.action.type == ActionType.REQUEST_INPUT:
            self.agent.current_node = NodeType.HITL
        elif tool and tool.requires_confirmation and not getattr(pending, "confirmed", False):
            prompt = f"Confirm to execute tool '{pending.action.tool_name}' with args: {pending.action.args}"
            await self._execute_request_confirm(
                self.turn_id,
                prompt=prompt,
                context=f"tool:{pending.action.tool_name}",
                tool_name=pending.action.tool_name,
                tool_args=pending.action.args
            )
            event = Event(EventType.ACTION, self.agent.agent_id, self.turn_id, pending.action.to_dict())
            await self.ws_manager.send(event.to_dict(), client_id=self.agent.client_id)
            self.agent.status = AgentStatus.WAITING
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
            observation = await self._execute_tool(self.turn_id)
            self.agent.current_node = NodeType.OBSERVE
            self.append_tao_trajectory(observation)
        elif current_action.type == ActionType.FINISH:
            observation = Observation(content=self.agent.pending.action.answer, turn_id=self.turn_id, success=True)
            self.agent.current_node = NodeType.END  # 流程结束
            self.append_tao_trajectory(observation)
        else:
            if self.agent.error_count >= 2:
                raise RuntimeError
            observation = Observation(
                content=f"Unknown action type: {current_action.type}, Error Message: {current_action.prompt}",
                turn_id=self.turn_id,
                success=False
            )
            # self.agent.status = AgentStatus.FAILED
            # self.agent.current_node = NodeType.END
            self.agent.current_node = NodeType.OBSERVE
            self.append_tao_trajectory(observation)
            self.agent.error_count += 1
            logger.error("任务运行到execute_node时，current_actionType 不存在，任务异常, 开始重试")
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
                    answer_content = self.agent.pending.action.answer
                    tao_observation_content = self.agent.tao_trajectory[-1]['observation'][
                        'content'] if self.agent.tao_trajectory else None
                    event = Event(EventType.FINAL,
                                  self.agent.agent_id,
                                  self.turn_id,
                                  {"content": answer_content if answer_content else tao_observation_content})
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
        screenshot_tool = None
        screenshot_path = None

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
        if tool.name.startswith("chrome-devtools"):
            screenshot_tool = self.tool_registry.get("chrome-devtools_take_screenshot")
            screenshot_path = f"./.screenshots/{self.agent.agent_id}_screenshot.png"
        try:
            execute_result = await tool.execute(**args)
            if screenshot_tool:
                screenshot_result = await screenshot_tool.execute(filePath=screenshot_path)
                if self._is_tool_response_success(screenshot_result):
                    asyncio.create_task(
                        wait_and_emit_screenshot_event(
                            self.ws_manager,
                            client_id=self.agent.client_id,
                            agent_id=self.agent.agent_id,
                            turn_id=turn_id,
                            img_path=screenshot_path,
                            timeout_s=10.0,
                        )
                    )
            return Observation(
                role="tool",
                content=execute_result,
                turn_id=turn_id,
                success=True,
            )
        except Exception as e:
            return Observation(
                role="tool",
                content=f"Tool '{tool_name}' executed error: {str(e)}",
                turn_id=turn_id,
                success=False,
                error=f"Tool '{tool_name}' executed error: {str(e)}"
            )

    def _is_tool_response_success(self, response: Optional[str]) -> bool:
        if response is None:
            return True
        try:
            parsed = json.loads(response)
        except (TypeError, json.JSONDecodeError):
            return True
        if isinstance(parsed, dict):
            if "success" in parsed:
                return bool(parsed["success"])
            if "ok" in parsed:
                return bool(parsed["ok"])
            if "error" in parsed and parsed["error"]:
                return False
        return True

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
            # is_tool_confirm 的判断可以删除
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


class TaskExecutor:
    stream: bool = False

    def __init__(self, checkpoint: CheckpointStore, provider: Provider, tool_registry: ToolRegistry,
                 ws_manager: ConnectionManager):
        self.llm = provider
        self.tool_registry = tool_registry
        self.checkpoint = checkpoint
        self.ws_manager = ws_manager

    async def run(self, trace: Trace):
        trace.started_at = datetime.utcnow()
        turn = [turn for turn in trace.turns if trace.current_turn_id == turn.turn_id][0]
        step = None
        if trace.current_step_id:
            step = [step for step in turn.steps if step.step_id == trace.current_step_id][0]
        while (turn.index < trace.max_turns and
               trace.status not in [AgentStatus.DONE, AgentStatus.FAILED, AgentStatus.WAITING, AgentStatus.PAUSED]):
            match trace.node:
                case NodeType.THINK:
                    if trace.current_step_id:
                        trace.node = NodeType.DECIDE
                    else:
                        step = await self._think(trace, turn)
                case NodeType.DECIDE:
                    await self._decide(trace, turn, step)
                case NodeType.EXECUTE:
                    await self._action(trace, turn, step)
                case NodeType.HITL:
                    await self._hitl(trace, turn, step)
                case NodeType.OBSERVE:
                    await self._observe(trace, turn, step)
                case NodeType.END:
                    await self._end(trace, turn, step)
                case _:
                    trace.status = AgentStatus.FAILED
                    trace.error_message = f"Unknown node: {trace.node}"
                    self.checkpoint.save_v2(trace)
                    return

    async def _build_messages(self, trace_id: str) -> list:
        trace = self.checkpoint.load_v2(trace_id)
        messages = [
            {"role": "system", "content": build_system_prompt_v2()},
        ]
        for turn in trace.turns:
            turn = cast(dict, turn)
            messages.append({"role": "user", "content": turn.get("user_input")})
            for step in turn.get("steps"):
                observations_map = {observation.get("action_id"): observation for observation in
                                    step.get("observations")} if step.get("observations") else {}
                if step.get("tool_calls"):
                    messages.append({"role": "assistant",
                                     "content": json.dumps({"type": "tool", "message": "__tool_calls__"},
                                                           ensure_ascii=False), "tool_calls": step.get("tool_calls")})
                    for call in step.get("tool_calls"):
                        observation = observations_map.get(call.get("id"))
                        if not observation:
                            raise Exception(f"Miss observation, Step info: {step}")
                        messages.append(
                            {"role": "tool", "tool_call_id": call.get("id"), "content": observation.get("content")})
                else:
                    # 没有工具调用，要么就finish，要么就input / confirm，然后下一个Step, input / confirm 会接收用户的request_input 信息
                    for action in step.get("actions"):
                        if action.get("full_ref"):
                            messages.append({"role": "assistant", "content": action.get("full_ref")})
                        if action.get("request_input"):
                            messages.append({"role": "user", "content": action.get("request_input")})

        return messages

    async def _think(self, trace: Trace, turn: Turn) -> Step:
        # 一轮think创建新的Step
        step = Step(index=len(turn.steps) + 1)
        turn.steps.append(step)
        trace.current_step_id = step.step_id
        context = {
            "trace": trace,
            "step": step,
            "user_input": turn.user_input,
            "messages": await self._build_messages(trace.trace_id),
        }
        reasoning, actions, token_info = self.llm.generate_v2(context)
        if trace.token_info:
            trace.token_info["cache_tokens"] += token_info["cache_tokens"]
            trace.token_info["prompt_tokens"] += token_info["prompt_tokens"]
            trace.token_info["completion_tokens"] += token_info["completion_tokens"]
            trace.token_info["total_tokens"] += token_info["total_tokens"]
        else:
            trace.token_info = token_info
        event = Event(EventType.THOUGHT, trace.trace_id, turn.turn_id, {"content": reasoning})
        await self.ws_manager.send(event.to_dict(), client_id=trace.client_id)
        event = Event(EventType.TOKEN_INFO, trace.trace_id, turn.turn_id, trace.token_info)
        await self.ws_manager.send(event.to_dict(), client_id=trace.client_id)
        step.thought = reasoning
        step.actions = actions
        action = actions[0]
        # 这里是 not Stream才发送的
        if actions[0].type != ActionType.TOOL:
            type = actions[0].type
            if type == ActionType.FINISH:
                event = Event(EventType.ANSWER, trace.trace_id, step.step_id, {"content": action.message})
                await self.ws_manager.send(event.to_dict(), client_id=trace.client_id)
                event = Event(EventType.END, trace.trace_id, step.step_id, {"content": "done"})
                await self.ws_manager.send(event.to_dict(), client_id=trace.client_id)

        trace.node = NodeType.DECIDE
        self.checkpoint.save_v2(trace)
        return step

    async def _decide(self, trace: Trace, turn: Turn, step: Step):
        types = [action.type for action in step.actions]
        if ActionType.FINISH in types:
            action = step.actions[-1]
            action.status = ActionStatus.DONE
            trace.node = NodeType.END
        elif ActionType.REQUEST_INPUT in types or ActionType.REQUEST_CONFIRM in types:
            trace.node = NodeType.HITL  # LLM也只会返回一个
        else:
            # 否则就是只有工具执行，可能有多个工具需要确认，确认一个执行一个
            action_list = [action for action in step.actions if
                           action.status == ActionStatus.PLANNED and action.requires_confirm]
            if action_list:
                action = action_list[0]  # 每次只处理一个
                trace.pending_action_id = action.action_id
                action.status = ActionStatus.WAITING_CONFIRM
                step.status = StepStatus.WAITING_CONFIRM
                prompt = f"Confirm to execute tool '{action.tool_name}' with args: {action.args}"
                await self._request_confirm(
                    trace.client_id, trace.trace_id, turn.turn_id,
                    action.action_id,
                    prompt=prompt,
                    tool_name=action.tool_name,
                    tool_args=action.args
                )
                event = Event(EventType.ACTION, trace.trace_id, turn.turn_id, action.to_dict())
                await self.ws_manager.send(event.to_dict(), client_id=trace.client_id)
                trace.status = AgentStatus.WAITING
                trace.node = NodeType.HITL
            else:
                trace.node = NodeType.EXECUTE
                event = Event(EventType.ACTION, trace.trace_id, turn.turn_id, step.actions[-1].to_dict())  # TODO 先发送一个
                await self.ws_manager.send(event.to_dict(), client_id=trace.client_id)
        self.checkpoint.save_v2(trace)

    async def _action(self, trace: Trace, turn: Turn, step: Step):
        finish_action_list = [action for action in step.actions if action.type == ActionType.FINISH]
        if finish_action_list and ActionType.FINISH == finish_action_list[0].type:
            finish_action = finish_action_list[0]
            observation = ObservationV2(observation_id=get_sonyflake("observation_"),
                                        action_id=finish_action.action_id, type=ObservationType.INFO, ok=True,
                                        content=finish_action.message)
            finish_action.status = ActionStatus.DONE
            step.observations.append(observation)
            trace.node = NodeType.END  # 流程结束
        else:
            for action in step.actions:
                # 同一批Actions存在多个需要Confirm的工具，每次处理一个，执行一个, execute_hitl后requires_confirm = False
                if action.requires_confirm:
                    continue
                action.status = ActionStatus.RUNNING
                observation = await self._execute_tool(trace, turn, action)
                step.observations.append(observation)

            trace.node = NodeType.OBSERVE

        self.checkpoint.save_v2(trace)

    async def _hitl(self, trace: Trace, turn: Turn, step: Step):
        trace.status = AgentStatus.WAITING

        action = step.actions[-1]

        trace.pending_action_id = action.action_id

        if action.type == ActionType.REQUEST_CONFIRM:
            # 目前的逻辑，如果是Tool写死的requireConfirm，不会走这里
            # 只有LLM返回的request_confirm才走这里
            step.status = StepStatus.WAITING_CONFIRM
            action.status = ActionStatus.WAITING_CONFIRM
            await self._request_confirm(trace.client_id, trace.trace_id, turn.turn_id, action.action_id, action.message, action.tool_name, action.args)
            # if not tream
            event = Event(EventType.HITL_CONFIRM, trace.trace_id, turn.turn_id, {"content": action.message})
            await self.ws_manager.send(event.to_dict(), client_id=trace.client_id)
        elif action.type == ActionType.REQUEST_INPUT:
            step.status = StepStatus.WAITING_INPUT
            action.status = ActionStatus.WAITING_INPUT
            # if not tream
            event = Event(EventType.HITL_REQUEST, trace.trace_id, turn.turn_id, {"content": action.message})
            await self.ws_manager.send(event.to_dict(), client_id=trace.client_id)
        else:
            # error
            raise Exception(f"Hitl Unknown action: {action.type}")
        self.checkpoint.save_v2(trace)

    async def _observe(self, trace: Trace, turn: Turn, step: Step):

        # 如果当前Step还存在没有Done的Action，继续执行，可能是因为WaitConfirm
        actions = [action for action in step.actions if action.status != ActionStatus.DONE]
        if actions:
            trace.node = NodeType.THINK
        else:
            trace.node = NodeType.THINK
            event = Event(EventType.OBSERVATION,
                          trace.trace_id,
                          turn.turn_id,
                          step.observations[-1].to_dict())
            await self.ws_manager.send(event.to_dict(), client_id=trace.client_id)
            trace.current_step_id = None
        self.checkpoint.save_v2(trace)

    async def _end(self, trace: Trace, turn: Turn, step: Step):
        finished_at = datetime.utcnow()
        step.status = StepStatus.DONE
        step.finished_at = finished_at
        turn.status = TurnStatus.DONE
        turn.finished_at = finished_at
        trace.status = AgentStatus.DONE
        trace.finished_at = finished_at
        trace.current_turn_id = None
        trace.current_step_id = None
        trace.pending_action_id = None
        self.checkpoint.save_v2(trace)

        answer_content = step.actions[-1].message
        tao_observation_content = step.observations[-1].content if step.observations else None
        event = Event(EventType.FINAL,
                      trace.trace_id,
                      turn.turn_id,
                      {"content": answer_content if answer_content else tao_observation_content})
        await self.ws_manager.send(event.to_dict(), client_id=trace.client_id)


    async def _execute_tool(self, trace: Trace, turn: Turn, action: ActionV2):
        tool_name = action.tool_name
        args = action.args
        screenshot_tool = None
        screenshot_path = None
        observation_id = get_sonyflake("observation_")
        if action.confirm_status == "denied":
            # 用户拒绝执行
            action.status = ActionStatus.DONE
            return ObservationV2(observation_id=observation_id,
                                 action_id=action.action_id,
                                 type=ObservationType.HITL_DENIED,
                                 ok=True,
                                 content=f"The user refuses to perform the current tool call")
        tool = self.tool_registry.get(tool_name)
        if not tool:
            action.status = ActionStatus.FAILED
            return ObservationV2(observation_id=observation_id,
                                 action_id=action.action_id,
                                 type=ObservationType.TOOL_ERROR,
                                 ok=False,
                                 content=f"Tool '{tool_name}' not found")
        if tool.name != "chrome-devtools_take_screenshot" and tool.name.startswith("chrome-devtools"):
            screenshot_tool = self.tool_registry.get("chrome-devtools_take_screenshot")
            screenshot_path = f"./.screenshots/{trace.trace_id}_screenshot.png"
        try:
            execute_result = await tool.execute(**args)
            action.status = ActionStatus.DONE
            if screenshot_tool:
                screenshot_result = await screenshot_tool.execute(filePath=screenshot_path)
                if screenshot_result:
                    asyncio.create_task(
                        wait_and_emit_screenshot_event(
                            self.ws_manager,
                            client_id=trace.client_id,
                            agent_id=trace.trace_id,
                            turn_id=turn.turn_id,
                            img_path=screenshot_path,
                            timeout_s=10.0,
                        )
                    )
            return ObservationV2(observation_id=observation_id,
                                 action_id=action.action_id,
                                 type=ObservationType.TOOL_RESULT,
                                 ok=True,
                                 content=execute_result)
        except Exception as e:
            action.status = ActionStatus.FAILED
            return ObservationV2(
                observation_id=observation_id,
                action_id=action.action_id,
                type=ObservationType.TOOL_ERROR,
                ok=False,
                content=f"Tool '{tool_name}' executed error: {str(e)}",
            )

    async def _request_confirm(self, client_id: str, trace_id: str, turn_id: str, pending_action_id:str, prompt: str,
                               tool_name: Optional[str] = None,
                               tool_args: Optional[dict] = None):
        prompt_text = prompt or "Please confirm the action."
        event = Event(
            EventType.HITL_CONFIRM,
            trace_id,
            turn_id,
            {
                "request_id": pending_action_id,
                "prompt": prompt_text,
                "tool_name": tool_name,
                "args": tool_args or {}
            }
        )
        await self.ws_manager.send(event.to_dict(), client_id=client_id)

    async def execute_hitl(self, trace: Trace, request_input: str):
        turn = [turn for turn in trace.turns if turn.turn_id == trace.current_turn_id][0]
        step = [step for step in turn.steps if step.step_id == trace.current_step_id][0]
        action = [action for action in step.actions if action.action_id == trace.pending_action_id][0]
        if action.type == ActionType.REQUEST_INPUT:
            # 当前状态 AgentStatus.WAITING  NodeType.HITL
            action.request_input = request_input
            action.status = ActionStatus.DONE

            # 开启下一轮Step
            trace.status = AgentStatus.RUNNING
            trace.node = NodeType.THINK

            step.status = StepStatus.DONE
            step.finished_at = datetime.utcnow()

            trace.pending_action_id = None
            trace.current_step_id = None

        elif action.type == ActionType.REQUEST_CONFIRM:
            # 需要判断两种，一种是LLM的Confirm，一种是工具的Confirm
            accepted = self._parse_confirmation(request_input)
            action.request_input = "I've followed your instructions to complete the operation, please start the next step" if accepted else "I refuse to do the current action, skip it, and if you can't skip it, terminate the process"

            # 开启下一轮Step
            trace.status = AgentStatus.RUNNING
            trace.node = NodeType.THINK

            step.status = StepStatus.DONE
            step.finished_at = datetime.utcnow()

            trace.pending_action_id = None
            trace.current_step_id = None

        elif action.type == ActionType.TOOL:
            accepted = self._parse_confirmation(request_input)
            action.confirm_status = "approved" if accepted else "denied"
            action.requires_confirm = False
            action.status = ActionStatus.PLANNED

            trace.status = AgentStatus.RUNNING
            trace.node = NodeType.EXECUTE

            step.status = StepStatus.RUNNING
        else:
            raise NotImplementedError
        self.checkpoint.save_v2(trace)

    def _parse_confirmation(self, input_text: str) -> bool:
        normalized = (input_text or "").strip().lower()
        return normalized in {"yes", "y", "confirm", "ok", "true", "1"}

