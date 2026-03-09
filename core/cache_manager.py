from __future__ import annotations

import abc
import json
import logging
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

from core.protocol import ActionType

logger = logging.getLogger(__name__)

HITL_CONTINUATION_MESSAGE = "人工操作已完成，旧快照已过期，请先重新检查当前页面状态。"


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

        current_turn_id = getattr(trace, "current_turn_id", None)

        for turn in getattr(trace, "turns", []):
            user_input = getattr(turn, "user_input", None)
            turn_id = getattr(turn, "turn_id", None)
            is_latest_turn = turn_id == current_turn_id

            turn_step_messages: List[Dict[str, Any]] = []
            has_valid_step = False

            for step in getattr(turn, "steps", []):
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
                        content = getattr(obs, "content", "")
                        step_messages.append(
                            {"role": "tool", "tool_call_id": call_id, "content": self._to_str(content)})

                    # 针对HITL request_input做上下文注入 -- 只有tool call时才会出发request_input
                    request_input_actions = [a for a in actions if a.type == ActionType.REQUEST_INPUT]
                    if request_input_actions:
                        req = getattr(request_input_actions[0], "request_input", None)
                        if req == "done":
                            step_messages.append(
                                {"role": "user", "content": "人工操作已完成，旧快照已过期，请先重新检查当前页面状态"})
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

            if has_valid_step or is_latest_turn:
                messages.append({"role": "user", "content": str(user_input)})
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
