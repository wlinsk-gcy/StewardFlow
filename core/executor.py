"""
ReAct 执行引擎
实现 Thought → Action → Observation 的循环逻辑
"""
import asyncio
import json
import logging
from datetime import datetime
from typing import Optional

from .protocol import (
    AgentStatus, NodeType,
    ActionType, Event, EventType,
    Trace, Turn, Step, ActionStatus, Action, Observation, ObservationType, StepStatus, TurnStatus
)
from .builder.build import llm_response_schema
from .llm import Provider
from .runtime_settings import RuntimeSettings, get_runtime_settings
from .tool_result_externalizer import (
    ToolResultExternalizerConfig,
    ToolResultExternalizerMiddleware,
)
from .tools.tool import ToolRegistry
from .storage.checkpoint import CheckpointStore
from utils.id_util import get_sonyflake
from utils.screenshot_util import wait_and_emit_screenshot_event
from ws.connection_manager import ConnectionManager
from core.cache_manager import CacheManager

logger = logging.getLogger(__name__)

DEFAULT_SCREENSHOT_FLAG = ["chrome-devtools_click",
                           "chrome-devtools_drag",
                           "chrome-devtools_fill",
                           "chrome-devtools_fill_form",
                           "chrome-devtools_handle_dialog",
                           "chrome-devtools_hover",
                           "chrome-devtools_press_key",
                           "chrome-devtools_upload_file",
                           "chrome-devtools_close_page",
                           "chrome-devtools_list_pages",
                           "chrome-devtools_navigate_page",
                           "chrome-devtools_new_page",
                           "chrome-devtools_select_page"]

class TaskExecutor:
    stream: bool = False

    def __init__(self, checkpoint: CheckpointStore, provider: Provider, tool_registry: ToolRegistry,
                 ws_manager: ConnectionManager, cache_manager: CacheManager,
                 tool_result_config: Optional[ToolResultExternalizerConfig] = None,
                 runtime_settings: RuntimeSettings | None = None):
        self.llm = provider
        self.tool_registry = tool_registry
        self.checkpoint = checkpoint
        self.ws_manager = ws_manager
        self.cache_manager = cache_manager
        if runtime_settings is not None:
            resolved_settings = runtime_settings
        elif tool_result_config is not None:
            resolved_settings = tool_result_config.to_runtime_settings()
        else:
            resolved_settings = get_runtime_settings()
        self.tool_result_externalizer = ToolResultExternalizerMiddleware(
            settings=resolved_settings
        )

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
                    self.checkpoint.save(trace)
                    return

    async def _think(self, trace: Trace, turn: Turn) -> Step:
        step = Step(index=len(turn.steps) + 1)
        turn.steps.append(step)
        trace.current_step_id = step.step_id
        schemas = self.tool_registry.get_all_schemas(
            excludes=["chrome-devtools_take_screenshot", "chrome-devtools_evaluate_script"])
        messages = await self.cache_manager.build_messages(trace, schemas, toolset_version="tools_v1", response_schema_version="resp_v1")
        context = {
            "trace": trace,
            "step": step,
            "user_input": turn.user_input,
            "messages": messages,
        }
        reasoning, actions, token_info = self.llm.generate(context)
        await self.cache_manager.update_calibration(trace.trace_id, token_info["prompt_tokens"], schemas, toolset_version="tools_v1", response_schema_version="resp_v1")
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
        self.checkpoint.save(trace)
        return step

    async def _emit_action_batch(self, trace: Trace, turn: Turn, step: Step) -> None:
        actions = [a.to_dict() for a in (step.actions or [])]
        if not actions:
            return
        data = dict(actions[0])
        data["actions"] = actions
        data["count"] = len(actions)
        event = Event(EventType.ACTION, trace.trace_id, turn.turn_id, data)
        await self.ws_manager.send(event.to_dict(), client_id=trace.client_id)

    async def _emit_observation_batch(self, trace: Trace, turn: Turn, step: Step) -> None:
        observations = [o.to_dict() for o in (step.observations or [])]
        if not observations:
            return
        data = dict(observations[-1])
        data["observations"] = observations
        data["count"] = len(observations)
        event = Event(EventType.OBSERVATION, trace.trace_id, turn.turn_id, data)
        await self.ws_manager.send(event.to_dict(), client_id=trace.client_id)

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
                await self._emit_action_batch(trace, turn, step)
                trace.status = AgentStatus.WAITING
                trace.node = NodeType.HITL
            else:
                trace.node = NodeType.EXECUTE
                await self._emit_action_batch(trace, turn, step)
        self.checkpoint.save(trace)

    async def _action(self, trace: Trace, turn: Turn, step: Step):
        finish_action_list = [action for action in step.actions if action.type == ActionType.FINISH]
        if finish_action_list and ActionType.FINISH == finish_action_list[0].type:
            finish_action = finish_action_list[0]
            observation = Observation(observation_id=get_sonyflake("observation_"),
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
                observation = await self._execute_tool(trace, turn, step, action)
                step.observations.append(observation)

            trace.node = NodeType.OBSERVE

        self.checkpoint.save(trace)

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
        self.checkpoint.save(trace)

    async def _observe(self, trace: Trace, turn: Turn, step: Step):

        # 如果当前Step还存在没有Done的Action，继续执行，可能是因为WaitConfirm
        actions = [action for action in step.actions if action.status != ActionStatus.DONE]
        if actions:
            trace.node = NodeType.THINK
        else:
            step.status = StepStatus.DONE
            step.finished_at = datetime.utcnow()
            trace.node = NodeType.THINK
            await self._emit_observation_batch(trace, turn, step)
            trace.current_step_id = None
        self.checkpoint.save(trace)

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
        self.checkpoint.save(trace)


        answer_content = step.actions[-1].message
        tao_observation_content = step.observations[-1].content if step.observations else None
        final_content = answer_content if answer_content else tao_observation_content
        event = Event(EventType.FINAL,
                      trace.trace_id,
                      turn.turn_id,
                      {"content": final_content})
        await self.ws_manager.send(event.to_dict(), client_id=trace.client_id)
        tool_state = {
            tc.get("function", {}).get("name")
            for step in (turn.steps or [])
            for tc in (step.tool_calls or [])
            if tc.get("function", {}).get("name")
        }
        step_ids = [getattr(s, "step_id", None) for s in (turn.steps or []) if getattr(s, "step_id", None)]
        await self.cache_manager.finalize_turn_to_result_card(trace_id=trace.trace_id,
                                                              turn_id=turn.turn_id,
                                                              user_input=turn.user_input,
                                                              final_answer=final_content,
                                                              tool_state=sorted(tool_state),
                                                              step_ids=step_ids)


    async def _execute_tool(self, trace: Trace, turn: Turn, step: Step, action: Action):
        tool_name = action.tool_name
        args = action.args
        screenshot_tool = None
        screenshot_path = None
        observation_id = get_sonyflake("observation_")
        if action.confirm_status == "denied":
            # 用户拒绝执行
            action.status = ActionStatus.DONE
            denied_payload = self.tool_result_externalizer.build_error(
                tool_name=tool_name or "unknown_tool",
                error_text="The user refuses to perform the current tool call",
            )
            return Observation(
                observation_id=observation_id,
                action_id=action.action_id,
                type=ObservationType.HITL_DENIED,
                ok=True,
                content=json.dumps(denied_payload, ensure_ascii=False),
            )
        tool = self.tool_registry.get(tool_name)
        if not tool:
            action.status = ActionStatus.FAILED
            not_found_payload = self.tool_result_externalizer.build_error(
                tool_name=tool_name or "unknown_tool",
                error_text=f"Tool '{tool_name}' not found",
            )
            return Observation(
                observation_id=observation_id,
                action_id=action.action_id,
                type=ObservationType.TOOL_ERROR,
                ok=False,
                content=json.dumps(not_found_payload, ensure_ascii=False),
            )
        if tool.name in DEFAULT_SCREENSHOT_FLAG:
            screenshot_tool = self.tool_registry.get("chrome-devtools_take_screenshot")
            screenshot_path = f"./.screenshots/{trace.trace_id}_screenshot.png"
        try:
            execute_result = await tool.execute(**(args or {}))
            if tool_name == "chrome-devtools_wait_for" and execute_result == "wait_for response":
                # wait_for作为响应屏障，拿到结果后直接snapshot，并返回链接，由LLM按需检索
                snapshot_tool = self.tool_registry.get("chrome-devtools_take_snapshot")
                execute_result = await snapshot_tool.execute()
            action.status = ActionStatus.DONE
            if screenshot_tool:
                # 手动截图会存在一个问题，页面还未完全响应，就截图了，导致前端的浏览器视图里的图片没有完全响应。
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

            observation_payload = self.tool_result_externalizer.externalize(
                tool_name=tool_name or "unknown_tool",
                raw_result=execute_result,
                trace_id=trace.trace_id,
                turn_id=turn.turn_id,
                step_id=step.step_id,
                tool_call_id=action.action_id,
            )
            return Observation(
                observation_id=observation_id,
                action_id=action.action_id,
                type=ObservationType.TOOL_RESULT,
                ok=True,
                content=json.dumps(observation_payload, ensure_ascii=False),
            )
        except Exception as e:
            action.status = ActionStatus.FAILED
            error_payload = self.tool_result_externalizer.build_error(
                tool_name=tool_name or "unknown_tool",
                error_text=f"Tool '{tool_name}' executed error: {str(e)}",
            )
            return Observation(
                observation_id=observation_id,
                action_id=action.action_id,
                type=ObservationType.TOOL_ERROR,
                ok=False,
                content=json.dumps(error_payload, ensure_ascii=False),
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
        self.checkpoint.save(trace)

    def _parse_confirmation(self, input_text: str) -> bool:
        normalized = (input_text or "").strip().lower()
        return normalized in {"yes", "y", "confirm", "ok", "true", "1"}

