from __future__ import annotations

import abc
import json
import logging
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

from core.context_compaction import (
    WHAT_DID_WE_DO_PROMPT,
    get_active_compaction,
    resolve_compaction_boundary,
    should_skip_turn_step,
)
from core.protocol import ActionType

logger = logging.getLogger(__name__)

# HITL_CONTINUATION_MESSAGE = "Manual intervention has been completed. The previous snapshot has expired, so please re-check the current page state first."
HITL_CONTINUATION_MESSAGE = "人工操作已完成，旧快照已过期，请先重新检查当前页面状态。"
CONTEXT_WINDOW_METADATA_KEY = "context_window"
CONTEXT_WINDOW_ESTIMATED_TOKENS_KEY = "estimated_tokens"
CONTEXT_WINDOW_COMPACTED_AT_KEY = "compacted_at"
PLACEHOLDER_TOOL_RESULT = "[Old tool result content cleared]"


def _enum_value(value: Any) -> Any:
    return value.value if isinstance(value, Enum) else value


class CacheManager(abc.ABC):
    def __init__(
            self,
            build_system_prompt_fn: Callable[[], str],
    ) -> None:
        self._build_system_prompt_fn = build_system_prompt_fn

    async def build_messages(self, trace: Any) -> List[Dict[str, Any]]:
        sys_prompt = self._build_system_prompt_fn()
        messages = [{"role": "system", "content": sys_prompt}]
        active_compaction = get_active_compaction(trace)
        boundary_turn_index: int | None = None
        boundary_step_index: int | None = None
        if active_compaction:
            boundary_turn_index, boundary_step_index = resolve_compaction_boundary(trace)
            if boundary_turn_index is None:
                active_compaction = None
            else:
                messages.extend(
                    [
                        {"role": "user", "content": WHAT_DID_WE_DO_PROMPT},
                        {"role": "assistant", "content": self._to_str(active_compaction.get("summary_text"))},
                        {"role": "user", "content": self._to_str(active_compaction.get("resume_prompt"))},
                    ]
                )

        current_turn_id = getattr(trace, "current_turn_id", None)

        for turn_index, turn in enumerate(getattr(trace, "turns", [])):
            user_input = getattr(turn, "user_input", None)
            turn_id = getattr(turn, "turn_id", None)
            is_latest_turn = turn_id == current_turn_id
            include_turn_user_message = True
            if active_compaction and boundary_turn_index is not None and turn_index < boundary_turn_index:
                continue
            if active_compaction and boundary_turn_index == turn_index:
                include_turn_user_message = False

            turn_step_messages: List[Dict[str, Any]] = []
            has_valid_step = False

            for step_index, step in enumerate(getattr(turn, "steps", [])):
                if active_compaction and should_skip_turn_step(
                    turn_index=turn_index,
                    step_index=step_index,
                    boundary_turn_index=boundary_turn_index,
                    boundary_step_index=boundary_step_index,
                ):
                    continue
                step_messages: List[Dict[str, Any]] = []
                tool_calls = getattr(step, "tool_calls", None) or []
                observations = getattr(step, "observations", None) or []
                actions = getattr(step, "actions", None) or []
                obs_map = self._build_observation_map(observations)

                # 当前 step 是 tool step
                if tool_calls:
                    call_ids = [c.get("id") for c in tool_calls if (c or {}).get("id")]
                    # 要求并发调用集全部完成，才保留整个 step
                    is_complete = (len(call_ids) > 0 and all(call_id in obs_map for call_id in call_ids))
                    if not is_complete:
                        continue

                    step_messages.append({"role": "assistant", "tool_calls": tool_calls})
                    for call in tool_calls:
                        call_id = (call or {}).get("id")
                        obs = obs_map.get(call_id)
                        if self._is_compacted_tool_result(obs):
                            tool_content = PLACEHOLDER_TOOL_RESULT
                        else:
                            content = getattr(obs, "content", "")
                            tool_content = self._to_str(content)
                        step_messages.append(
                            {"role": "tool", "tool_call_id": call_id, "content": tool_content})

                    # 针对HITL request_input做上下文注入 -- 只有tool call时才会出发request_input
                    request_input_actions = [a for a in actions if a.type == ActionType.REQUEST_INPUT]
                    if request_input_actions:
                        req = getattr(request_input_actions[0], "request_input", None)
                        if req == "done":
                            step_messages.append(
                                {"role": "user", "content": HITL_CONTINUATION_MESSAGE})
                # 普通 step：有内容就保留
                else:
                    for a in actions:
                        full_ref = getattr(a, "full_ref", None)
                        if full_ref:
                            step_messages.append({"role": "assistant", "content": self._to_str(full_ref)})
                # 有内容才保留这个 step
                if step_messages:
                    has_valid_step = True
                    turn_step_messages.extend(step_messages)

            if include_turn_user_message and (has_valid_step or is_latest_turn):
                messages.append({"role": "user", "content": self._to_str(user_input)})
            if has_valid_step:
                messages.extend(turn_step_messages)
        return messages

    def _build_observation_map(self, observations: List[Any]) -> Dict[str, Any]:
        obs_map: Dict[str, Any] = {}
        for observation in observations:
            action_id = getattr(observation, "action_id", None)
            if action_id:
                obs_map[action_id] = observation
        return obs_map

    @staticmethod
    def _get_context_window_metadata(observation: Any) -> Dict[str, Any]:
        metadata = getattr(observation, "metadata", None)
        if not isinstance(metadata, dict):
            return {}
        value = metadata.get(CONTEXT_WINDOW_METADATA_KEY)
        return value if isinstance(value, dict) else {}

    @classmethod
    def _is_compacted_tool_result(cls, observation: Any) -> bool:
        context_window = cls._get_context_window_metadata(observation)
        return bool(context_window.get(CONTEXT_WINDOW_COMPACTED_AT_KEY))

    @staticmethod
    def _to_str(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        try:
            return json.dumps(value, ensure_ascii=False)
        except Exception:
            return str(value)


class InMemoryCacheManager(CacheManager):
    def __init__(
            self,
            build_system_prompt_fn: Callable[[], str],
    ) -> None:
        super().__init__(build_system_prompt_fn=build_system_prompt_fn)
        self._store: Dict[str, Any] = {}

    async def _load_ctx(self, trace_id: str) -> Optional[Dict[str, Any]]:
        return self._store.get(trace_id)

    async def _save_ctx(self, ctx: Any) -> None:
        self._store[ctx.trace_id] = ctx

    async def _delete_ctx(self, trace_id: str) -> None:
        self._store.pop(trace_id, None)
