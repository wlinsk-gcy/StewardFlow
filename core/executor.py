"""
ReAct execution engine.
Implements the Thought -> Action -> Observation loop.
"""
import json
import logging
import time
from datetime import datetime
from typing import Any, Optional

from .protocol import (
    AgentStatus, NodeType,
    ActionType, Event, EventType,
    Trace, Turn, Step, ActionStatus, Action, Observation, ObservationType, StepStatus, TurnStatus
)
from .llm import Provider
from .trace_event_logger import bind_event_context, emit_trace_event
from .tool_raw_store import ToolRawStore
from .tools.tool import ToolRegistry
from .storage.checkpoint import CheckpointStore
from utils.id_util import get_sonyflake
from ws.connection_manager import ConnectionManager
from core.cache_manager import CacheManager

logger = logging.getLogger(__name__)

HITL_BARRIER_KEYWORDS = (
    "log in",
    "login",
    "sign in",
    "signin",
    "verify",
    "verification",
    "captcha",
    "2fa",
    "two-factor",
    "authenticate",
    "authorization required",
    "access denied",
    "unauthorized",
    "please authorize",
    "please login",
    "扫码",
    "登录",
    "验证码",
    "授权",
    "验证",
)
HITL_BARRIER_NEGATIVE_KEYWORDS = (
    "login successful",
    "logged in",
    "验证通过",
    "授权成功",
)
CLI_TOOL_NAMES = {"bash", "glob", "read", "search", "grep", "rg"}


def _extract_tool_args_keys(raw_args: Any) -> list[str]:
    if isinstance(raw_args, dict):
        return sorted(str(k) for k in raw_args.keys())
    if isinstance(raw_args, str):
        try:
            parsed = json.loads(raw_args)
        except Exception:
            return []
        if isinstance(parsed, dict):
            return sorted(str(k) for k in parsed.keys())
    return []


def _summarize_tool_calls(tool_calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    summarized: list[dict[str, Any]] = []
    for tc in tool_calls or []:
        function = tc.get("function") if isinstance(tc, dict) else {}
        function = function if isinstance(function, dict) else {}
        summarized.append(
            {
                "tool_call_id": tc.get("id") if isinstance(tc, dict) else None,
                "name": function.get("name"),
                "args_keys": _extract_tool_args_keys(function.get("arguments")),
            }
        )
    return summarized


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
        self.tool_raw_store = ToolRawStore()

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
    def _is_cli_envelope(tool_name: str, payload: Any) -> bool:
        if str(tool_name or "").strip().lower() not in CLI_TOOL_NAMES:
            return False
        if not isinstance(payload, dict):
            return False
        if not {"ok", "data", "artifacts", "error"}.issubset(payload.keys()):
            return False
        data = payload.get("data")
        if not isinstance(data, dict):
            return False
        return ("exit_code" in data) or ("timed_out" in data)

    @staticmethod
    def _build_cli_summary(tool_name: str, payload: dict[str, Any]) -> str:
        ok = bool(payload.get("ok"))
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        exit_code = data.get("exit_code")
        timed_out = bool(data.get("timed_out"))
        engine_used = data.get("engine_used")
        fallback_from = data.get("fallback_from")
        if ok:
            if engine_used:
                msg = f"{tool_name} completed, engine={engine_used}, exit_code={exit_code}."
                if fallback_from:
                    msg = f"{tool_name} completed, engine={engine_used}, fallback_from={fallback_from}, exit_code={exit_code}."
                return msg
            return f"{tool_name} completed, exit_code={exit_code}."

        err = payload.get("error") if isinstance(payload.get("error"), dict) else {}
        err_type = err.get("type") if isinstance(err, dict) else None
        if timed_out:
            return f"{tool_name} failed, timed_out=true, exit_code={exit_code}."
        if err_type:
            return f"{tool_name} failed, type={err_type}, exit_code={exit_code}."
        return f"{tool_name} failed, exit_code={exit_code}."

    def _build_cli_observation_card(
        self,
        *,
        tool_name: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        artifacts = payload.get("artifacts") if isinstance(payload.get("artifacts"), list) else []

        refs: list[dict[str, Any]] = []
        preview_chars = 0
        truncated = False
        for artifact in artifacts:
            if not isinstance(artifact, dict):
                continue
            ref = {
                "name": str(artifact.get("name", "")),
                "kind": str(artifact.get("kind", "text")),
            }
            path = artifact.get("path")
            if isinstance(path, str) and path.strip():
                ref["path"] = path.strip()
            by = artifact.get("by")
            if isinstance(by, str) and by.strip():
                ref["by"] = by.strip()
            if artifact.get("truncated") is True:
                ref["truncated"] = True
                truncated = True
            preview_chars += len(str(artifact.get("preview", "")))
            refs.append(ref)

        facts: dict[str, Any] = {
            "ok": bool(payload.get("ok")),
            "timed_out": bool(data.get("timed_out")),
            "exit_code": data.get("exit_code"),
            "truncated": truncated,
            "trim_mode": "none",
            "raw_chars": preview_chars,
            "kept_head_chars": preview_chars,
            "kept_tail_chars": 0,
        }
        if "engine_used" in data:
            facts["engine_used"] = data.get("engine_used")
        if "fallback_from" in data:
            facts["fallback_from"] = data.get("fallback_from")

        return {
            "type": "observation_card_v1",
            "tool_name": tool_name,
            "outcome": "success" if bool(payload.get("ok")) else "error",
            "summary": self._build_cli_summary(tool_name, payload),
            "facts": facts,
            "refs": {"artifacts": refs},
        }

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
                case NodeType.GUARD:
                    await self._guard(trace, turn, step)
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
        schemas = self.tool_registry.get_all_schemas()
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
        emit_trace_event(
            logger,
            event="llm_response",
            trace_id=trace.trace_id,
            turn_id=turn.turn_id,
            step_id=step.step_id,
            action_count=len(actions or []),
            tool_calls=_summarize_tool_calls(step.tool_calls or []),
        )
        action = actions[0]
        # Non-stream mode: emit answer/end events directly.
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
            trace.node = NodeType.GUARD
        elif ActionType.REQUEST_INPUT in types or ActionType.REQUEST_CONFIRM in types:
            # Treat model HITL output as candidate only; GUARD decides final transition.
            trace.node = NodeType.GUARD
        else:
            # Tool execution path: confirm one action at a time if needed.
            action_list = [action for action in step.actions if
                           action.status == ActionStatus.PLANNED and action.requires_confirm]
            if action_list:
                action = action_list[0]  # Handle one confirmation at a time.
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

    def _detect_hitl_barrier(self, step: Step) -> str | None:
        if not step or not step.observations:
            return None
        for observation in reversed(step.observations):
            content = _serialize_observation_content(observation.content).lower()
            if not content:
                continue
            if any(noise in content for noise in HITL_BARRIER_NEGATIVE_KEYWORDS):
                continue
            for keyword in HITL_BARRIER_KEYWORDS:
                if keyword in content:
                    return keyword
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
                    "检测到页面可能需要人工介入（登录/验证码/授权）。"
                    "请完成页面操作后回复 done 继续。"
                ),
                status=ActionStatus.PLANNED,
            )
            step.actions.append(action)
            trace.pending_action_id = action.action_id
            trace.node = NodeType.HITL
            emit_trace_event(
                logger,
                event="hitl_guard_trigger",
                trace_id=trace.trace_id,
                turn_id=turn.turn_id,
                step_id=step.step_id,
                reason_code="barrier_detected",
                barrier_signal=barrier_signal,
            )
            self.checkpoint.save(trace)
            return

        # If model requested HITL explicitly, GUARD still owns the final transition.
        model_hitl_action = next(
            (
                action for action in reversed(step.actions)
                if action.type in {ActionType.REQUEST_INPUT, ActionType.REQUEST_CONFIRM}
                and action.status in {ActionStatus.PLANNED, ActionStatus.WAITING_INPUT, ActionStatus.WAITING_CONFIRM}
            ),
            None,
        )
        if model_hitl_action is not None:
            trace.pending_action_id = model_hitl_action.action_id
            trace.node = NodeType.HITL
            emit_trace_event(
                logger,
                event="hitl_guard_trigger",
                trace_id=trace.trace_id,
                turn_id=turn.turn_id,
                step_id=step.step_id,
                reason_code="model_hitl_candidate",
            )
            self.checkpoint.save(trace)
            return

        has_finish = any(action.type == ActionType.FINISH for action in (step.actions or []))
        if has_finish:
            trace.node = NodeType.END
        else:
            trace.node = NodeType.THINK
        self.checkpoint.save(trace)

    async def _hitl(self, trace: Trace, turn: Turn, step: Step):
        trace.status = AgentStatus.WAITING

        action = None
        if trace.pending_action_id:
            action = next((a for a in step.actions if a.action_id == trace.pending_action_id), None)
        if action is None and step.actions:
            action = step.actions[-1]
        if action is None:
            raise Exception("Hitl action not found")

        trace.pending_action_id = action.action_id

        if action.type == ActionType.REQUEST_CONFIRM:
            # For LLM-issued request_confirm action.
            step.status = StepStatus.WAITING_CONFIRM
            action.status = ActionStatus.WAITING_CONFIRM
            await self._request_confirm(trace.client_id, trace.trace_id, turn.turn_id, action.action_id, action.message, action.tool_name, action.args)
        elif action.type == ActionType.REQUEST_INPUT:
            step.status = StepStatus.WAITING_INPUT
            action.status = ActionStatus.WAITING_INPUT
            # if not stream
            event = Event(
                EventType.HITL_REQUEST,
                trace.trace_id,
                action.action_id,
                {"content": action.message},
            )
            await self.ws_manager.send(event.to_dict(), client_id=trace.client_id)
        else:
            # error
            raise Exception(f"Hitl Unknown action: {action.type}")
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
            step.status = StepStatus.DONE
            step.finished_at = datetime.utcnow()
            trace.node = NodeType.GUARD
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
        trace_id = trace.trace_id
        turn_id = turn.turn_id
        step_id = step.step_id
        tool_call_id = action.action_id
        observation_id = get_sonyflake("observation_")
        if action.confirm_status == "denied":
            # User denied this tool action.
            action.status = ActionStatus.DONE
            denied_payload = {
                "ok": False,
                "tool_name": tool_name or "unknown_tool",
                "error": "The user refuses to perform the current tool call",
            }
            return Observation(
                observation_id=observation_id,
                action_id=action.action_id,
                type=ObservationType.HITL_DENIED,
                ok=True,
                content=_serialize_observation_content(denied_payload),
            )
        tool = self.tool_registry.get(tool_name)
        if not tool:
            action.status = ActionStatus.FAILED
            not_found_payload = {
                "ok": False,
                "tool_name": tool_name or "unknown_tool",
                "error": f"Tool '{tool_name}' not found",
            }
            return Observation(
                observation_id=observation_id,
                action_id=action.action_id,
                type=ObservationType.TOOL_ERROR,
                ok=False,
                content=_serialize_observation_content(not_found_payload),
            )
        started_at = time.perf_counter()
        tool_ok = False
        emit_trace_event(
            logger,
            event="tool_start",
            trace_id=trace_id,
            turn_id=turn_id,
            step_id=step_id,
            tool_call_id=tool_call_id,
            tool_name=tool_name or "unknown_tool",
            elapsed_ms=0,
            ok=None,
        )
        try:
            with bind_event_context(
                trace_id=trace_id,
                turn_id=turn_id,
                step_id=step_id,
                tool_call_id=tool_call_id,
                tool_name=tool_name or "unknown_tool",
            ):
                execute_result = await tool.execute(**(args or {}))
            action.status = ActionStatus.DONE
            with bind_event_context(
                trace_id=trace_id,
                turn_id=turn_id,
                step_id=step_id,
                tool_call_id=tool_call_id,
                tool_name=tool_name or "unknown_tool",
            ):
                observation_payload = execute_result
            observation_full_ref: dict[str, Any] | None = None
            parsed_payload = self._try_parse_tool_payload(execute_result)
            if self._is_cli_envelope(tool_name or "unknown_tool", parsed_payload):
                raw_path = self.tool_raw_store.write(
                    trace_id=trace_id,
                    turn_id=turn_id,
                    step_id=step_id,
                    action_id=tool_call_id,
                    tool_name=tool_name or "unknown_tool",
                    ok=bool(parsed_payload.get("ok")),
                    payload=parsed_payload,
                )
                observation_full_ref = {
                    "store": "tool_raw_file",
                    "path": raw_path,
                }
                observation_payload = self._build_cli_observation_card(
                    tool_name=tool_name or "unknown_tool",
                    payload=parsed_payload,
                )
            tool_ok = True
            return Observation(
                observation_id=observation_id,
                action_id=action.action_id,
                type=ObservationType.TOOL_RESULT,
                ok=True,
                content=_serialize_observation_content(observation_payload),
                full_ref=observation_full_ref,
            )
        except Exception as e:
            action.status = ActionStatus.FAILED
            tool_ok = False
            error_payload = {
                "ok": False,
                "tool_name": tool_name or "unknown_tool",
                "error": f"Tool '{tool_name}' executed error: {str(e)}",
            }
            return Observation(
                observation_id=observation_id,
                action_id=action.action_id,
                type=ObservationType.TOOL_ERROR,
                ok=False,
                content=_serialize_observation_content(error_payload),
            )
        finally:
            elapsed_ms = max(0, int((time.perf_counter() - started_at) * 1000))
            emit_trace_event(
                logger,
                event="tool_end",
                trace_id=trace_id,
                turn_id=turn_id,
                step_id=step_id,
                tool_call_id=tool_call_id,
                tool_name=tool_name or "unknown_tool",
                elapsed_ms=elapsed_ms,
                ok=bool(tool_ok),
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
        action = [action for action in step.actions if action.action_id == trace.pending_action_id][0]
        if action.type == ActionType.REQUEST_INPUT:
            # Current state: AgentStatus.WAITING + NodeType.HITL.
            action.request_input = request_input
            action.status = ActionStatus.DONE

            # Start next step.
            trace.status = AgentStatus.RUNNING
            trace.node = NodeType.THINK

            step.status = StepStatus.DONE
            step.finished_at = datetime.utcnow()

            trace.pending_action_id = None
            trace.current_step_id = None

        elif action.type == ActionType.REQUEST_CONFIRM:
            # Handle LLM confirmation response.
            accepted = self._parse_confirmation(request_input)
            action.request_input = "I've followed your instructions to complete the operation, please start the next step" if accepted else "I refuse to do the current action, skip it, and if you can't skip it, terminate the process"

            # Start next step.
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

