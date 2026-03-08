from __future__ import annotations

import abc
import json
import logging
from typing import Any, Callable, Dict, List, Optional

from core.protocol import ActionType

logger = logging.getLogger(__name__)


class CacheManager(abc.ABC):
    def __init__(
            self,
            build_system_prompt_fn: Callable[[], str],
    ) -> None:
        self._build_system_prompt_fn = build_system_prompt_fn

    async def build_messages_v2(self, trace: Any) -> List[Dict[str, Any]]:
        sys_prompt = self._build_system_prompt_fn()
        messages = [{"role": "system", "content": sys_prompt}]
        for turn in getattr(trace, "turns", []):
            user_input = getattr(turn, "user_input", None)
            if not user_input:
                raise RuntimeError(f"turn {turn} has no user_input")
            messages.append({"role": "user", "content": str(user_input)})
            for step in getattr(turn, "steps", []):
                step_id = getattr(step, "step_id", None)
                tool_calls = getattr(step, "tool_calls", None) or []
                observations = getattr(step, "observations", None) or []
                actions = getattr(step, "actions", None) or []
                obs_map: Dict[str, Any] = {}
                for o in observations:
                    aid = getattr(o, "action_id", None)
                    if aid:
                        obs_map[aid] = o

                if tool_calls:
                    messages.append({"role": "assistant", "tool_calls": tool_calls})
                    for call in tool_calls:
                        call_id = (call or {}).get("id")
                        obs = obs_map.get(call_id)
                        if obs is None:
                            raise RuntimeError(f"Missing observation for tool call id={call_id} in step={step_id}")
                        content = getattr(obs, "content", "")
                        messages.append({"role": "tool", "tool_call_id": call_id, "content": self._to_str(content)})
                    # 针对HITL request_input做上下文注入 -- 只有tool call时才会出发request_input
                    if actions:
                        request_input_actions = [a for a in actions if a.type == ActionType.REQUEST_INPUT]
                        if request_input_actions:
                            req = getattr(request_input_actions[0], "request_input", None)
                            if req == "done":
                                messages.append({"role": "user", "content": "人工操作已完成，旧快照已过期，请先重新检查当前页面状态"})
                else:
                    for a in actions:
                        full_ref = getattr(a, "full_ref", None)
                        if full_ref:
                            messages.append({"role": "assistant", "content": self._to_str(full_ref)})
                        # req = getattr(a, "request_input", None) # Hitl时伪造action，不进context，如果human reject，直接进行next turn即可。

        return messages

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
        super().__init__(
            build_system_prompt_fn=build_system_prompt_fn,
        )
        self._store: Dict[str, Any] = {}

    async def _load_ctx(self, trace_id: str) -> Optional[Dict[str, Any]]:
        return self._store.get(trace_id)

    async def _save_ctx(self, ctx: Any) -> None:
        self._store[ctx.trace_id] = ctx

    async def _delete_ctx(self, trace_id: str) -> None:
        self._store.pop(trace_id, None)
