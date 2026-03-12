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
from .context_compaction import (
    BOUNDARY_BEFORE_FIRST_STEP,
    COMPACTION_SYSTEM_PROMPT,
    CONTINUE_PROMPT,
    build_summary_instruction_prompt,
    make_context_compaction,
    make_pending_compaction,
)
from .llm import Provider, is_context_overflow_error
from .model_limits import ModelLimitRegistry, ModelLimits
from .tools.tool import ToolRegistry
from .storage.checkpoint import CheckpointStore
from utils.id_util import get_sonyflake
from ws.connection_manager import ConnectionManager
from core.cache_manager import (
    CacheManager,
    CONTEXT_WINDOW_COMPACTED_AT_KEY,
    CONTEXT_WINDOW_ESTIMATED_TOKENS_KEY,
    CONTEXT_WINDOW_METADATA_KEY,
)

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
PRUNE_PROTECT_TOKENS = 40_000
PRUNE_MINIMUM_TOKENS = 20_000
PRUNE_SKIP_RECENT_TURNS = 2


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
                 model_limit_registry: ModelLimitRegistry,
                 ):
        self.llm = provider
        self.tool_registry = tool_registry
        self.checkpoint = checkpoint
        self.ws_manager = ws_manager
        self.cache_manager = cache_manager
        self.model_limit_registry = model_limit_registry
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

    @staticmethod
    def _estimate_text_tokens(text: str) -> int:
        if not text:
            return 0
        return round(len(text) / 4)

    @staticmethod
    def _normalize_metadata(metadata: Any) -> dict[str, Any]:
        return metadata if isinstance(metadata, dict) else {}

    @classmethod
    def _ensure_context_window_metadata(cls, metadata: Any) -> dict[str, Any]:
        normalized = cls._normalize_metadata(metadata)
        context_window = normalized.get(CONTEXT_WINDOW_METADATA_KEY)
        if not isinstance(context_window, dict):
            context_window = {}
            normalized[CONTEXT_WINDOW_METADATA_KEY] = context_window
        return context_window

    @staticmethod
    def _get_call_ids(step: Step | None) -> list[str]:
        if not step:
            return []
        return [call.get("id") for call in (step.tool_calls or []) if (call or {}).get("id")]

    @staticmethod
    def _build_step_observation_map(step: Step | None) -> dict[str, Observation]:
        obs_map: dict[str, Observation] = {}
        if not step:
            return obs_map
        for observation in step.observations or []:
            action_id = getattr(observation, "action_id", None)
            if action_id:
                obs_map[action_id] = observation
        return obs_map

    def _is_complete_tool_step(self, step: Step | None) -> bool:
        call_ids = self._get_call_ids(step)
        if not call_ids:
            return False
        obs_map = self._build_step_observation_map(step)
        return all(call_id in obs_map for call_id in call_ids)

    @classmethod
    def _is_compacted_observation(cls, observation: Observation | None) -> bool:
        if observation is None:
            return False
        metadata = cls._normalize_metadata(getattr(observation, "metadata", None))
        context_window = metadata.get(CONTEXT_WINDOW_METADATA_KEY)
        if not isinstance(context_window, dict):
            return False
        return bool(context_window.get(CONTEXT_WINDOW_COMPACTED_AT_KEY))

    def _get_estimated_tokens(self, observation: Observation) -> int:
        metadata = self._normalize_metadata(getattr(observation, "metadata", None))
        context_window = metadata.get(CONTEXT_WINDOW_METADATA_KEY)
        if isinstance(context_window, dict):
            estimate = context_window.get(CONTEXT_WINDOW_ESTIMATED_TOKENS_KEY)
            if isinstance(estimate, int) and estimate >= 0:
                return estimate

        serialized_output = _serialize_observation_content(getattr(observation, "content", ""))
        estimate = self._estimate_text_tokens(serialized_output)
        if estimate > 0:
            metadata = self._normalize_metadata(getattr(observation, "metadata", None))
            context_window = self._ensure_context_window_metadata(metadata)
            context_window[CONTEXT_WINDOW_ESTIMATED_TOKENS_KEY] = estimate
            observation.metadata = metadata
        return estimate

    def _mark_observation_compacted(self, observation: Observation, *, compacted_at: str) -> None:
        metadata = self._normalize_metadata(getattr(observation, "metadata", None))
        context_window = self._ensure_context_window_metadata(metadata)
        context_window[CONTEXT_WINDOW_COMPACTED_AT_KEY] = compacted_at
        observation.metadata = metadata

    @staticmethod
    def _merge_token_info(existing: dict[str, Any] | None, delta: dict[str, Any] | None) -> dict[str, Any]:
        base = existing if isinstance(existing, dict) else {}
        update = delta if isinstance(delta, dict) else {}
        return {
            "cache_tokens": int(base.get("cache_tokens", 0) or 0) + int(update.get("cache_tokens", 0) or 0),
            "prompt_tokens": int(base.get("prompt_tokens", 0) or 0) + int(update.get("prompt_tokens", 0) or 0),
            "completion_tokens": int(base.get("completion_tokens", 0) or 0)
            + int(update.get("completion_tokens", 0) or 0),
            "total_tokens": int(base.get("total_tokens", 0) or 0) + int(update.get("total_tokens", 0) or 0),
        }

    @staticmethod
    def _get_pending_compaction(trace: Trace) -> dict[str, Any] | None:
        value = getattr(trace, "pending_compaction", None)
        return value if isinstance(value, dict) and value else None

    def _set_pending_compaction(
        self,
        trace: Trace,
        *,
        overflow: bool,
        source: str,
        turn: Turn,
        step: Step,
    ) -> None:
        trace.pending_compaction = make_pending_compaction(
            overflow=overflow,
            source=source,
            turn_id=turn.turn_id,
            step_id=step.step_id,
        )

    @staticmethod
    def _clear_pending_compaction(trace: Trace) -> None:
        trace.pending_compaction = {}

    @staticmethod
    def _find_turn_and_step(
        trace: Trace,
        *,
        turn_id: str | None,
        step_id: str | None,
    ) -> tuple[Turn | None, Step | None]:
        if not turn_id:
            return None, None
        for turn in trace.turns or []:
            if turn.turn_id != turn_id:
                continue
            if not step_id:
                return turn, None
            for step in turn.steps or []:
                if step.step_id == step_id:
                    return turn, step
            return turn, None
        return None, None

    def _get_model_limits(self) -> ModelLimits | None:
        return self.model_limit_registry.get_limits(
            model=getattr(self.llm, "model", ""),
            base_url=getattr(self.llm, "base_url", None),
        )

    @staticmethod
    def _usage_token_count(token_info: dict[str, Any] | None) -> int | None:
        if not isinstance(token_info, dict):
            return None
        total = token_info.get("total_tokens")
        if isinstance(total, int) and total >= 0:
            return total
        prompt = int(token_info.get("prompt_tokens", 0) or 0)
        completion = int(token_info.get("completion_tokens", 0) or 0)
        cache = int(token_info.get("cache_tokens", 0) or 0)
        count = prompt + completion + cache
        return count if count > 0 else None

    @staticmethod
    def _soft_overflow_threshold(limits: ModelLimits | None) -> int | None:
        if not limits:
            return None
        if limits.input and limits.output:
            reserved_tokens = min(20_000, limits.output)
            threshold = limits.input - reserved_tokens
            return threshold if threshold > 0 else None
        if limits.context and limits.output:
            threshold = limits.context - limits.output
            return threshold if threshold > 0 else None
        return None

    def _should_schedule_soft_compaction(self, token_info: dict[str, Any] | None) -> bool:
        threshold = self._soft_overflow_threshold(self._get_model_limits())
        count = self._usage_token_count(token_info)
        if threshold is None or count is None:
            return False
        return count >= threshold

    async def _emit_trace_token_info(self, trace: Trace, *, step_id: str) -> None:
        event = Event(EventType.TOKEN_INFO, trace.trace_id, step_id, trace.token_info)
        await self.ws_manager.send(event.to_dict(), client_id=trace.client_id)

    async def _build_summary_history_messages(self, trace: Trace) -> list[dict[str, Any]]:
        messages = await self.cache_manager.build_messages(trace)
        if messages and messages[0].get("role") == "system":
            return list(messages[1:])
        return list(messages)

    async def _execute_summary_compaction(
        self,
        trace: Trace,
        *,
        boundary_turn: Turn,
        boundary_step: Step | None,
        mode: str,
        source: str,
        resume_prompt: str,
        event_step_id: str | None = None,
        boundary_step_id: str | None = None,
        history_messages: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        history = list(history_messages) if history_messages is not None else await self._build_summary_history_messages(trace)
        history.append({"role": "user", "content": build_summary_instruction_prompt()})
        summary_text, token_info = await self.llm.generate_summary(
            messages=history,
            system_prompt=COMPACTION_SYSTEM_PROMPT,
        )
        trace.token_info = self._merge_token_info(trace.token_info, token_info)
        emit_step_id = event_step_id or getattr(boundary_step, "step_id", None)
        if emit_step_id:
            await self._emit_trace_token_info(trace, step_id=emit_step_id)
        trace.context_compaction = make_context_compaction(
            summary_text=summary_text,
            boundary_turn_id=boundary_turn.turn_id,
            boundary_step_id=boundary_step_id if boundary_step_id is not None else getattr(boundary_step, "step_id", None),
            resume_prompt=resume_prompt,
            mode=mode,
            source=source,
            model=getattr(self.llm, "model", None),
        )
        return trace.context_compaction

    async def _maybe_run_pending_soft_compaction(self, trace: Trace) -> bool:
        pending = self._get_pending_compaction(trace)
        if not pending or pending.get("overflow"):
            return False

        pending_turn, pending_step = self._find_turn_and_step(
            trace,
            turn_id=pending.get("trigger_turn_id"),
            step_id=pending.get("trigger_step_id"),
        )
        if not pending_turn or not pending_step:
            logger.warning("soft_compaction_skip_missing_boundary trace=%s pending=%s", trace.trace_id, pending)
            self._clear_pending_compaction(trace)
            return False

        previous_compaction = getattr(trace, "context_compaction", None)
        try:
            await self._execute_summary_compaction(
                trace,
                boundary_turn=pending_turn,
                boundary_step=pending_step,
                mode="continue",
                source=str(pending.get("source") or "soft_overflow"),
                resume_prompt=CONTINUE_PROMPT,
            )
            logger.info(
                "soft_compaction_success trace=%s turn=%s step=%s",
                trace.trace_id,
                pending_turn.turn_id,
                pending_step.step_id,
            )
            return True
        except Exception as exc:
            trace.context_compaction = previous_compaction
            logger.warning("soft_compaction_failed trace=%s error=%s", trace.trace_id, exc)
            return False
        finally:
            self._clear_pending_compaction(trace)

    @staticmethod
    def _find_last_completed_step(turn: Turn, *, exclude_step_id: str | None = None) -> Step | None:
        steps = list(turn.steps or [])
        for step in reversed(steps):
            if exclude_step_id and step.step_id == exclude_step_id:
                continue
            if step.status == StepStatus.DONE:
                return step
        return None

    async def _recover_from_hard_overflow(self, trace: Trace, turn: Turn, step: Step) -> None:
        self._set_pending_compaction(
            trace,
            overflow=True,
            source="hard_overflow",
            turn=turn,
            step=step,
        )
        previous_compaction = getattr(trace, "context_compaction", None)
        previous_step = self._find_last_completed_step(turn, exclude_step_id=step.step_id)
        history_messages = await self._build_summary_history_messages(trace)
        mode = "continue"
        resume_prompt = CONTINUE_PROMPT
        boundary_step: Step | None = previous_step
        boundary_step_id: str | None = getattr(previous_step, "step_id", None)

        if previous_step is None:
            mode = "replay"
            resume_prompt = str(getattr(turn, "user_input", "") or "")
            boundary_step = None
            boundary_step_id = BOUNDARY_BEFORE_FIRST_STEP
            if history_messages and history_messages[-1].get("role") == "user":
                last_user = str(history_messages[-1].get("content", "") or "")
                if last_user == resume_prompt:
                    history_messages = history_messages[:-1]

        try:
            await self._execute_summary_compaction(
                trace,
                boundary_turn=turn,
                boundary_step=boundary_step,
                boundary_step_id=boundary_step_id,
                event_step_id=step.step_id,
                mode=mode,
                source="hard_overflow",
                resume_prompt=resume_prompt,
                history_messages=history_messages,
            )
            logger.info(
                "hard_compaction_success trace=%s turn=%s step=%s mode=%s",
                trace.trace_id,
                turn.turn_id,
                step.step_id,
                mode,
            )
        except Exception:
            trace.context_compaction = previous_compaction
            raise
        finally:
            self._clear_pending_compaction(trace)

    async def _generate_with_hard_overflow_recovery(
        self,
        trace: Trace,
        turn: Turn,
        step: Step,
        context: dict[str, Any],
    ) -> tuple[str, str, list, dict]:
        try:
            return await self.llm.generate(context)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not is_context_overflow_error(exc):
                raise
            logger.warning(
                "hard_overflow_detected trace=%s turn=%s step=%s error=%s",
                trace.trace_id,
                turn.turn_id,
                step.step_id,
                exc,
            )
            await self._recover_from_hard_overflow(trace, turn, step)
            context["messages"] = await self.cache_manager.build_messages(trace)
            try:
                return await self.llm.generate(context)
            except asyncio.CancelledError:
                raise
            except Exception as retry_exc:
                if is_context_overflow_error(retry_exc):
                    raise RuntimeError("Context overflow persisted after summary compaction") from retry_exc
                raise

    def _iter_prune_candidates(self, trace: Trace) -> list[Observation]:
        if len(trace.turns) <= PRUNE_SKIP_RECENT_TURNS:
            return []

        candidates: list[Observation] = []
        older_turns = trace.turns[:-PRUNE_SKIP_RECENT_TURNS]
        for turn in reversed(older_turns):
            for step in reversed(turn.steps or []):
                if not self._is_complete_tool_step(step):
                    continue
                obs_map = self._build_step_observation_map(step)
                for call_id in reversed(self._get_call_ids(step)):
                    observation = obs_map.get(call_id)
                    if observation is None:
                        continue
                    if observation.type != ObservationType.TOOL_RESULT:
                        continue
                    if self._is_compacted_observation(observation):
                        continue
                    candidates.append(observation)
        return candidates

    def _prune_old_tool_results(self, trace: Trace) -> int:
        protected_tokens = 0
        candidate_tokens = 0
        prune_targets: list[Observation] = []

        for observation in self._iter_prune_candidates(trace):
            estimate = self._get_estimated_tokens(observation)
            if estimate <= 0:
                continue
            if protected_tokens < PRUNE_PROTECT_TOKENS:
                protected_tokens += estimate
                continue
            prune_targets.append(observation)
            candidate_tokens += estimate

        if candidate_tokens <= PRUNE_MINIMUM_TOKENS:
            return 0

        compacted_at = datetime.utcnow().isoformat()
        for observation in prune_targets:
            self._mark_observation_compacted(observation, compacted_at=compacted_at)
        return len(prune_targets)

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
        await self._maybe_run_pending_soft_compaction(trace)
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
        finish_reason, reasoning, actions, token_info = await self._generate_with_hard_overflow_recovery(
            trace,
            turn,
            step,
            context,
        )

        trace.token_info = self._merge_token_info(trace.token_info, token_info)
        if self._should_schedule_soft_compaction(token_info):
            self._set_pending_compaction(
                trace,
                overflow=False,
                source="soft_overflow",
                turn=turn,
                step=step,
            )
        event = Event(EventType.THOUGHT, trace.trace_id, step.step_id, {"content": reasoning})
        await self.ws_manager.send(event.to_dict(), client_id=trace.client_id)
        await self._emit_trace_token_info(trace, step_id=step.step_id)
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
        event = Event(EventType.ACTION, trace.trace_id, step.step_id, data)
        await self.ws_manager.send(event.to_dict(), client_id=trace.client_id)

    async def _emit_observation_batch(self, trace: Trace, turn: Turn, step: Step) -> None:
        observations = [o.to_dict() for o in (step.observations or [])]
        if not observations:
            return
        data = dict(observations[-1])
        data["observations"] = observations
        data["count"] = len(observations)
        event = Event(EventType.OBSERVATION, trace.trace_id, step.step_id, data)
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
            if self._is_complete_tool_step(step):
                self._prune_old_tool_results(trace)
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
        compaction_ran = await self._maybe_run_pending_soft_compaction(trace)
        if self._is_complete_tool_step(step) and not compaction_ran:
            self._prune_old_tool_results(trace)
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
                      step.step_id,
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
            if isinstance(parsed_payload, dict):
                observation_content = parsed_payload.get("output")
                observation_metadata = self._normalize_metadata(parsed_payload.get("metadata"))
            else:
                observation_content = parsed_payload
                observation_metadata = {}

            serialized_output = _serialize_observation_content(observation_content)
            context_window = self._ensure_context_window_metadata(observation_metadata)
            context_window[CONTEXT_WINDOW_ESTIMATED_TOKENS_KEY] = self._estimate_text_tokens(serialized_output)
            action.status = ActionStatus.DONE
            return Observation(
                observation_id=observation_id,
                action_id=action.action_id,
                type=ObservationType.TOOL_RESULT,
                ok=True,
                content=observation_content,
                metadata=observation_metadata,
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
