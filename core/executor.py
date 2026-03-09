"""
ReAct execution engine.
Implements the Thought -> Action -> Observation loop.
"""
import asyncio
import json
import logging
from datetime import datetime
from typing import Any, Optional

from .protocol import (
    AgentStatus, NodeType,
    ActionType, Event, EventType,
    Trace, Turn, Step, ActionStatus, Action, Observation, ObservationType, StepStatus, TurnStatus, HitlTicket
)
from .llm import Provider
from .tools.tool import ToolRegistry
from .storage.checkpoint import CheckpointStore
from utils.id_util import get_sonyflake
from ws.connection_manager import ConnectionManager
from core.cache_manager import CacheManager

logger = logging.getLogger(__name__)

CONFIRM_TRUE_SET = {
    "yes", "y", "confirm", "ok", "true", "1",
    "sure", "approve", "approved", "continue", "go ahead",
    "是", "好的", "确认", "同意", "继续", "可以", "行", "好",
}
CONFIRM_FALSE_SET = {
    "no", "n", "deny", "denied", "reject", "false", "0", "cancel", "stop",
    "否", "不", "拒绝", "取消", "停止", "不用", "不要",
}
READ_ONLY_BASH_PREFIXES = (
    "git status",
    "git diff",
    "git log",
    "git show",
    "git branch",
    "git rev-parse",
    "git --version",
    "ls",
    "pwd",
    "whoami",
    "uname",
    "cat ",
    "head ",
    "tail ",
    "sed -n",
    "grep ",
    "rg ",
    "find ",
    "echo ",
    "printf ",
    "wc ",
    "stat ",
    "file ",
    "which ",
)
BASH_HIGH_RISK_MARKERS = (
    "rm ",
    "mv ",
    "cp ",
    "chmod ",
    "chown ",
    "mkdir ",
    "rmdir ",
    "touch ",
    "ln ",
    "tee ",
    "curl ",
    "wget ",
    "pip ",
    "npm ",
    "pnpm ",
    "yarn ",
    "apt ",
    "yum ",
    "dnf ",
    "apk ",
    "docker ",
    "kubectl ",
    "systemctl ",
    "service ",
    "shutdown",
    "reboot",
    "kill ",
    "pkill ",
    "dd ",
    "mkfs",
    "mount ",
    "umount ",
    "useradd ",
    "userdel ",
    "passwd ",
    "git push",
)
BASH_OPERATOR_MARKERS = (">", ">>", "<", "<<", "| tee", "$(", "`")


def _serialize_observation_content(payload: Any) -> str:
    if payload is None:
        return ""
    if isinstance(payload, bytes):
        return payload.decode("utf-8", errors="replace")
    if isinstance(payload, (dict, list)):
        return json.dumps(payload, ensure_ascii=False)
    return str(payload)






class TaskExecutor:
    stream: bool = False

    def __init__(self, checkpoint: CheckpointStore, provider: Provider, tool_registry: ToolRegistry,
                 ws_manager: ConnectionManager, cache_manager: CacheManager,
                 ):
        self.llm = provider
        self.tool_registry = tool_registry
        self.checkpoint = checkpoint
        self.ws_manager = ws_manager
        self.cache_manager = cache_manager
        self.schema_v2_enabled = True
        self.obs_card_v1_enabled = True
        self.toolset_schema_version = "tools_v2" if self.schema_v2_enabled else "tools_v1"

    @staticmethod
    def _try_parse_tool_payload(payload: Any) -> Any:
        if isinstance(payload, str):
            text = payload.strip()
            if text.startswith("{") and text.endswith("}"):
                try:
                    return json.loads(text)
                except Exception:
                    return payload
        return payload

    @staticmethod
    def _bash_requires_confirmation(command: str) -> bool:
        text = str(command or "").strip()
        if not text:
            return True
        lowered = text.lower()

        if any(marker in lowered for marker in BASH_OPERATOR_MARKERS):
            return True
        if any(marker in lowered for marker in BASH_HIGH_RISK_MARKERS):
            return True
        if any(token in lowered for token in ("&&", "||", ";")):
            return True

        for prefix in READ_ONLY_BASH_PREFIXES:
            if lowered.startswith(prefix):
                return False
        return True

    @staticmethod
    def _cancel_pending_actions(step: Step | None) -> None:
        if not step:
            return
        cancellable_statuses = {
            ActionStatus.PLANNED,
            ActionStatus.RUNNING,
            ActionStatus.WAITING_CONFIRM,
            ActionStatus.WAITING_INPUT,
        }
        for action in step.actions or []:
            if action.status in cancellable_statuses:
                action.status = ActionStatus.CANCELLED

    def _mark_trace_cancelled(self, trace: Trace, turn: Turn | None, step: Step | None) -> str:
        finished_at = datetime.utcnow()
        step_id = step.step_id if step else (trace.current_step_id or "-")

        trace.status = AgentStatus.CANCELLED
        trace.node = NodeType.END
        trace.finished_at = finished_at
        trace.error_message = None
        trace.pending_action_id = None
        trace.hitl_ticket = None
        trace.current_step_id = None
        trace.current_turn_id = None

        if turn and turn.status not in {TurnStatus.DONE, TurnStatus.FAILED, TurnStatus.CANCELLED}:
            turn.status = TurnStatus.CANCELLED
            turn.finished_at = finished_at
        if step and step.status not in {StepStatus.DONE, StepStatus.FAILED, StepStatus.CANCELLED}:
            step.status = StepStatus.CANCELLED
            step.finished_at = finished_at
            self._cancel_pending_actions(step)

        self.checkpoint.save(trace)
        return step_id


    async def run(self, trace: Trace):
        turn: Turn | None = None
        step: Step | None = None
        try:
            trace.started_at = datetime.utcnow()
            turn = [item for item in trace.turns if trace.current_turn_id == item.turn_id][0]
            if trace.current_step_id:
                step = [item for item in turn.steps if item.step_id == trace.current_step_id][0]
            while (
                turn.index < trace.max_turns
                and trace.status not in [
                    AgentStatus.DONE,
                    AgentStatus.FAILED,
                    AgentStatus.CANCELLED,
                    AgentStatus.WAITING,
                    AgentStatus.PAUSED,
                ]
            ):
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
                    case NodeType.OBSERVE:
                        await self._observe(trace, turn, step)
                    case NodeType.GUARD:
                        await self._guard(trace, turn, step)
                    case NodeType.END:
                        await self._end(trace, turn, step)
                    case _:
                        trace.status = AgentStatus.FAILED
                        trace.error_message = f"Unknown node: {trace.node}"
                        self.checkpoint.save(trace)
                        return
        except asyncio.CancelledError:
            step_id = self._mark_trace_cancelled(trace, turn, step)
            try:
                event = Event(EventType.END, trace.trace_id, step_id, {"content": "cancelled"})
                await self.ws_manager.send(event.to_dict(), client_id=trace.client_id)
            except Exception:
                logger.exception("Failed to push cancel event for trace=%s", trace.trace_id)
            logger.info("Executor run cancelled: trace=%s step=%s", trace.trace_id, step_id)
            return
        except Exception as exc:
            finished_at = datetime.utcnow()
            trace.status = AgentStatus.FAILED
            trace.node = NodeType.END
            trace.finished_at = finished_at
            trace.error_count = int(trace.error_count or 0) + 1
            trace.error_message = f"{type(exc).__name__}: {exc}"
            trace.pending_action_id = None
            trace.hitl_ticket = None

            if turn and turn.status != TurnStatus.DONE:
                turn.status = TurnStatus.FAILED
                turn.finished_at = finished_at
            if step and step.status != StepStatus.DONE:
                step.status = StepStatus.FAILED
                step.finished_at = finished_at
            self.checkpoint.save(trace)

            turn_id = turn.turn_id if turn else (trace.current_turn_id or "-")
            step_id = step.step_id if step else (trace.current_step_id or "-")


            error_message = f"执行中断：{type(exc).__name__}: {exc}"
            try:
                event = Event(EventType.ERROR, trace.trace_id, step_id, {"content": error_message})
                await self.ws_manager.send(event.to_dict(), client_id=trace.client_id)
            except Exception:
                logger.exception("Failed to push error event for trace=%s", trace.trace_id)
            try:
                event = Event(EventType.END, trace.trace_id, step_id, {"content": "failed"})
                await self.ws_manager.send(event.to_dict(), client_id=trace.client_id)
            except Exception:
                logger.exception("Failed to push end event for trace=%s", trace.trace_id)
            logger.exception("Executor run failed: trace=%s error=%s", trace.trace_id, exc)
            return

    async def _think(self, trace: Trace, turn: Turn) -> Step:
        step = Step(index=len(turn.steps) + 1)
        turn.steps.append(step)
        trace.current_step_id = step.step_id
        messages = await self.cache_manager.build_messages(trace)

        context = {
            "trace": trace,
            "step": step,
            "user_input": turn.user_input,
            "messages": messages,
        }
        finish_reason, reasoning, actions, token_info = self.llm.generate(context)

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
        # Non-stream mode: emit answer/end events directly.
        if finish_reason != "tool_calls":
            event = Event(EventType.ANSWER, trace.trace_id, step.step_id, {"content": actions[0].message})
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

    async def _enter_tool_confirm_wait(self, trace: Trace, turn: Turn, step: Step, action: Action) -> None:
        trace.pending_action_id = action.action_id
        prompt = f"Confirm to execute tool '{action.tool_name}' with args: {action.args}"
        description = action.args.get("description")
        if description:
            prompt = description
        trace.hitl_ticket = HitlTicket(
            kind="tool_confirm",
            status="open",
            turn_id=turn.turn_id,
            step_id=step.step_id,
            action_id=action.action_id,
            request_id=action.action_id,
            prompt=prompt,
        )
        action.status = ActionStatus.WAITING_CONFIRM
        step.status = StepStatus.WAITING_CONFIRM
        await self._request_confirm(
            trace.client_id,
            trace.trace_id,
            turn.turn_id,
            action.action_id,
            prompt=prompt,
            tool_name=action.tool_name,
            tool_args=action.args,
        )
        await self._emit_action_batch(trace, turn, step)
        trace.status = AgentStatus.WAITING
        trace.node = NodeType.HITL

    async def _enter_request_input_wait(self, trace: Trace, step: Step, action: Action) -> None:
        trace.pending_action_id = action.action_id
        trace.current_step_id = step.step_id
        trace.hitl_ticket = None
        trace.status = AgentStatus.WAITING
        trace.node = NodeType.HITL
        step.status = StepStatus.WAITING_INPUT
        action.status = ActionStatus.WAITING_INPUT
        event = Event(
            EventType.HITL_REQUEST,
            trace.trace_id,
            action.action_id,
            {"content": action.message},
        )
        await self.ws_manager.send(event.to_dict(), client_id=trace.client_id)

    async def _decide(self, trace: Trace, turn: Turn, step: Step):
        types = [action.type for action in step.actions]
        if ActionType.FINISH in types:
            action = step.actions[-1]
            action.status = ActionStatus.DONE
            trace.node = NodeType.GUARD
        else:
            # Code-side policy: keep confirmations for risky bash only.
            for action in step.actions or []:
                if action.type != ActionType.TOOL:
                    continue
                if not action.requires_confirm:
                    continue
                if (action.tool_name or "").strip().lower() != "bash":
                    continue
                command = str((action.args or {}).get("command") or "")
                if not self._bash_requires_confirmation(command): # 针对bash做过滤，避免每一行都need confirm
                    action.requires_confirm = False
                    action.confirm_status = None

            # Tool execution path: confirm one action at a time if needed.
            action_list = [action for action in step.actions if
                           action.status == ActionStatus.PLANNED and action.requires_confirm]
            if action_list:
                action = action_list[0]  # Handle one confirmation at a time.
                await self._enter_tool_confirm_wait(trace, turn, step, action)
            else:
                trace.hitl_ticket = None
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
            trace.node = NodeType.END  # End workflow.
        else:
            for action in step.actions:
                # Execute only planned non-confirm actions in current step.
                if action.requires_confirm:
                    continue
                if action.status != ActionStatus.PLANNED:
                    continue
                action.status = ActionStatus.RUNNING
                observation = await self._execute_tool(trace, turn, step, action)
                step.observations.append(observation)

            trace.node = NodeType.OBSERVE

        self.checkpoint.save(trace)

    def _detect_hitl_barrier(self, step: Step) -> dict[str, Any] | None:
        if not step or not step.observations:
            return None
        for observation in reversed(step.observations):
            metadata = observation.metadata if isinstance(observation.metadata, dict) else {}
            barrier = metadata.get("hitlBarrier")
            if isinstance(barrier, dict) and barrier.get("required") is True:
                return barrier
        return None

    async def _guard(self, trace: Trace, turn: Turn, step: Step):
        if step is None:
            trace.node = NodeType.THINK
            self.checkpoint.save(trace)
            return

        barrier_signal = self._detect_hitl_barrier(step)
        if barrier_signal:
            action = Action(
                action_id=get_sonyflake("action_"),
                type=ActionType.REQUEST_INPUT,
                message=(
                    "检测到页面可能需要人工操作（登录/验证码/授权）。"
                    "请先在浏览器完成操作，然后输入 done，流程将继续执行。"
                ),
                status=ActionStatus.PLANNED,
            )
            step.actions.append(action)
            await self._enter_request_input_wait(trace, step, action)
            self.checkpoint.save(trace)
            return

        step.status = StepStatus.DONE
        step.finished_at = datetime.utcnow()
        trace.current_step_id = None
        has_finish = any(action.type == ActionType.FINISH for action in (step.actions or []))
        if has_finish:
            trace.node = NodeType.END
        else:
            trace.node = NodeType.THINK
        self.checkpoint.save(trace)

    async def _observe(self, trace: Trace, turn: Turn, step: Step):

        # Continue loop if there are pending actions in current step.
        pending_statuses = {
            ActionStatus.PLANNED,
            ActionStatus.RUNNING,
            ActionStatus.WAITING_CONFIRM,
            ActionStatus.WAITING_INPUT,
        }
        actions = [action for action in step.actions if action.status in pending_statuses]
        if actions:
            trace.node = NodeType.THINK
        else:
            await self._emit_observation_batch(trace, turn, step)
            trace.node = NodeType.GUARD
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
        trace.hitl_ticket = None
        self.checkpoint.save(trace)


        answer_content = step.actions[-1].message
        tao_observation_content = step.observations[-1].content if step.observations else None
        final_content = answer_content if answer_content else tao_observation_content
        event = Event(EventType.FINAL,
                      trace.trace_id,
                      turn.turn_id,
                      {"content": final_content})
        await self.ws_manager.send(event.to_dict(), client_id=trace.client_id)


    async def _execute_tool(self, trace: Trace, turn: Turn, step: Step, action: Action):
        tool_name = action.tool_name
        args = action.args
        observation_id = get_sonyflake("observation_")
        if action.confirm_status == "denied":
            action.status = ActionStatus.DONE
            return Observation(
                observation_id=observation_id,
                action_id=action.action_id,
                type=ObservationType.HITL_DENIED,
                ok=True,
                content="The user refuses to perform the current tool call",
            )
        tool = self.tool_registry.get(tool_name)
        if not tool:
            action.status = ActionStatus.FAILED
            return Observation(
                observation_id=observation_id,
                action_id=action.action_id,
                type=ObservationType.TOOL_ERROR,
                ok=False,
                content=f"Tool '{tool_name}' not found"
            )
        try:
            execute_result = await tool.execute(**(args or {}))
            parsed_payload = self._try_parse_tool_payload(execute_result)
            action.status = ActionStatus.DONE
            return Observation(
                observation_id=observation_id,
                action_id=action.action_id,
                type=ObservationType.TOOL_RESULT,
                ok=True,
                content=parsed_payload.get("output"),
                metadata=parsed_payload.get("metadata")
            )
        except asyncio.CancelledError:
            action.status = ActionStatus.CANCELLED
            raise
        except Exception as e:
            action.status = ActionStatus.FAILED
            return Observation(
                observation_id=observation_id,
                action_id=action.action_id,
                type=ObservationType.TOOL_ERROR,
                ok=False,
                content=f"Tool '{tool_name}' executed error: {str(e)}"
            )


    async def _request_confirm(self, client_id: str, trace_id: str, turn_id: str, pending_action_id:str, prompt: str,
                               tool_name: Optional[str] = None,
                               tool_args: Optional[dict] = None):
        prompt_text = prompt or "Please confirm the action."
        event = Event(
            EventType.HITL_CONFIRM,
            trace_id,
            pending_action_id or turn_id,
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
        ticket = trace.hitl_ticket if isinstance(trace.hitl_ticket, HitlTicket) else None
        pending_action_id = (
            ticket.action_id
            if ticket and ticket.status == "open" and ticket.action_id
            else trace.pending_action_id
        )
        action = [action for action in step.actions if action.action_id == pending_action_id][0]
        if action.type == ActionType.REQUEST_INPUT:
            # Come from _guard node. Current state: AgentStatus.WAITING + NodeType.HITL.
            action.request_input = request_input
            action.status = ActionStatus.DONE

            # Start next step.
            trace.status = AgentStatus.RUNNING
            trace.node = NodeType.THINK

            step.status = StepStatus.DONE
            step.finished_at = datetime.utcnow()

            trace.pending_action_id = None
            trace.current_step_id = None
            trace.hitl_ticket = None

        elif action.type == ActionType.TOOL:
            # Come from _decide node. Current state: AgentStatus.WAITING + NodeType.HITL.
            accepted = self._parse_confirmation(request_input)
            action.confirm_status = "approved" if accepted else "denied"
            action.requires_confirm = False
            action.status = ActionStatus.PLANNED
            if ticket and ticket.status == "open":
                ticket.status = "resolved"
                ticket.resolved_at = datetime.utcnow()
                ticket.decision = action.confirm_status
                trace.hitl_ticket = ticket
            trace.pending_action_id = None


            trace.status = AgentStatus.RUNNING
            trace.node = NodeType.EXECUTE

            step.status = StepStatus.RUNNING
        else:
            raise NotImplementedError
        self.checkpoint.save(trace)

    def _parse_confirmation(self, input_text: str) -> bool:
        normalized = (input_text or "").strip().lower()
        if not normalized:
            return False
        if normalized in CONFIRM_TRUE_SET:
            return True
        if normalized in CONFIRM_FALSE_SET:
            return False
        # Prefix fallback keeps behavior predictable for short free-form replies.
        if normalized.startswith(("yes", "ok", "confirm", "是", "好", "同意", "继续")):
            return True
        if normalized.startswith(("no", "deny", "reject", "否", "不", "拒绝", "取消", "停止")):
            return False
        return False
