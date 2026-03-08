from __future__ import annotations

import abc
import hashlib
import json
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Union

logger = logging.getLogger(__name__)
#TODO: debug-only full context snapshot for local observability, remove after context manager refactor.
DEBUG_FULL_CONTEXT_PATH = Path("data") / "llm_context_full_debug.json"


def _stable_json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _sha1(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


@dataclass
class TokenEstimatorConfig:
    calibration_ema: float = 0.15
    calibration_min: float = 0.6
    calibration_max: float = 2.5
    chars_per_token_text: int = 4
    chars_per_token_struct: int = 3
    ratio_min: float = 0.5
    ratio_max: float = 2.0


class TokenEstimator:
    def __init__(self, config: Optional[TokenEstimatorConfig] = None) -> None:
        self.config = config or TokenEstimatorConfig()
        self._multiplier: float = 1.0

    @property
    def multiplier(self) -> float:
        return self._multiplier

    def set_multiplier(self, m: float) -> None:
        self._multiplier = float(_clamp(m, self.config.calibration_min, self.config.calibration_max))

    def estimate_message_tokens_raw(self, msg: Dict[str, Any]) -> int:
        text_chars = 0
        struct_chars = 0
        text_chars += len(str(msg.get("role", "")))
        text_chars += len(str(msg.get("content", "")))
        if msg.get("tool_call_id"):
            text_chars += len(str(msg.get("tool_call_id")))
        if msg.get("tool_calls") is not None:
            struct_chars += len(_stable_json(msg.get("tool_calls")))
        t_text = text_chars // self.config.chars_per_token_text
        t_struct = struct_chars // self.config.chars_per_token_struct
        return max(1, int(t_text + t_struct))

    def estimate_struct_tokens_raw(self, obj: Any) -> int:
        if obj is None:
            return 0
        return max(1, len(_stable_json(obj)) // self.config.chars_per_token_struct)

    def update_calibration_from_ratio(self, ratio: float) -> None:
        ratio = _clamp(ratio, self.config.ratio_min, self.config.ratio_max)
        alpha = self.config.calibration_ema
        new_mult = (1 - alpha) * self._multiplier + alpha * ratio
        self._multiplier = float(_clamp(new_mult, self.config.calibration_min, self.config.calibration_max))


@dataclass
class RuntimeContext:
    trace_id: str
    calibration_multiplier: float = 1.0
    messages: List[Dict[str, Any]] = field(default_factory=list)
    msg_tokens_raw: List[int] = field(default_factory=list)
    msg_tokens_raw_sum: int = 0
    tool_schema_key: Optional[str] = None
    tool_schema_tokens_raw: int = 0
    last_applied_step_id: Optional[str] = None
    last_build_turns: int = 0
    last_build_steps: int = 0
    updated_at: float = field(default_factory=lambda: time.time())

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(data: Dict[str, Any]) -> "RuntimeContext":
        return RuntimeContext(
            trace_id=str(data.get("trace_id", "")),
            calibration_multiplier=float(data.get("calibration_multiplier", 1.0)),
            messages=list(data.get("messages", []) or []),
            msg_tokens_raw=[int(x) for x in (data.get("msg_tokens_raw", []) or [])],
            msg_tokens_raw_sum=int(data.get("msg_tokens_raw_sum", 0)),
            tool_schema_key=data.get("tool_schema_key"),
            tool_schema_tokens_raw=int(data.get("tool_schema_tokens_raw", 0)),
            last_applied_step_id=data.get("last_applied_step_id"),
            last_build_turns=int(data.get("last_build_turns", 0)),
            last_build_steps=int(data.get("last_build_steps", 0)),
            updated_at=float(data.get("updated_at", time.time())),
        )


@dataclass
class CacheManagerConfig:
    pass


CtxType = Union[RuntimeContext, Dict[str, Any]]


class ContextOverflowUnresolved(Exception):
    def __init__(self, user_message: str, *, metrics: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(user_message)
        self.user_message = str(user_message)
        self.metrics = metrics or {}


class CacheManager(abc.ABC):
    def __init__(
        self,
        model: str,
        api_key: str,
        base_url: str,
        build_system_prompt_fn: Callable[[], str],
        config: Optional[CacheManagerConfig] = None,
        estimator: Optional[TokenEstimator] = None,
    ) -> None:
        del api_key, base_url
        self.model = model
        self.config = config or CacheManagerConfig()
        self.estimator = estimator or TokenEstimator()
        self._build_system_prompt_fn = build_system_prompt_fn

    @abc.abstractmethod
    async def _load_ctx(self, trace_id: str) -> Optional[CtxType]:
        ...

    @abc.abstractmethod
    async def _save_ctx(self, ctx: RuntimeContext) -> None:
        ...

    @abc.abstractmethod
    async def _delete_ctx(self, trace_id: str) -> None:
        ...

    async def build_messages(
        self,
        trace: Any,
        tool_schemas: Optional[List[Dict[str, Any]]] = None,
        toolset_version: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        trace_id = getattr(trace, "trace_id")
        ctx = await self._get_or_create_ctx(trace_id)
        self._rebuild_messages_from_trace(ctx, trace)
        self._ensure_schema_tokens_cached(ctx, tool_schemas=tool_schemas, toolset_version=toolset_version)
        ctx.updated_at = time.time()
        self._write_full_context_debug_file(ctx)
        await self._save_ctx(ctx)
        return ctx.messages

    def _write_full_context_debug_file(self, ctx: RuntimeContext) -> None:
        #TODO: debug-only full context snapshot for local observability, remove after context manager refactor.
        try:
            target = DEBUG_FULL_CONTEXT_PATH.resolve()
            target.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "generated_at": time.time(),
                "trace_id": ctx.trace_id,
                "context": ctx.to_dict(),
            }
            tmp = target.with_suffix(target.suffix + ".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(target)
        except Exception as exc:
            logger.warning("Failed to write debug context snapshot: %s", exc)

    async def clear(self, trace_id: str) -> None:
        await self._delete_ctx(trace_id)

    async def get_context(self, trace_id: str) -> Optional[RuntimeContext]:
        loaded = await self._load_ctx(trace_id)
        if loaded is None:
            return None
        return self._coerce_ctx(loaded)

    async def context_report(self, trace_id: str) -> Optional[Dict[str, Any]]:
        loaded = await self._load_ctx(trace_id)
        if loaded is None:
            return None
        ctx = self._coerce_ctx(loaded)
        estimated_prompt_tokens = self._estimate_prompt_tokens_from_ctx(ctx)
        estimated_prompt_tokens_raw = self._estimate_prompt_tokens_raw_from_ctx(ctx)
        tail_tool_highlight: Dict[str, Any] | None = None
        for msg in reversed(ctx.messages):
            if msg.get("role") != "tool":
                continue
            tail_tool_highlight = self._extract_tool_result_highlights(str(msg.get("content", "")))
            if tail_tool_highlight:
                break
        report: Dict[str, Any] = {
            "trace_id": ctx.trace_id,
            "updated_at": ctx.updated_at,
            "calibration_multiplier": ctx.calibration_multiplier,
            "estimated_prompt_tokens": estimated_prompt_tokens,
            "estimated_prompt_tokens_raw": estimated_prompt_tokens_raw,
            "buckets": {
                "messages_raw_tokens": ctx.msg_tokens_raw_sum,
                "tool_schema_raw_tokens": ctx.tool_schema_tokens_raw,
            },
            "counts": {
                "messages": len(ctx.messages),
                "turns": int(ctx.last_build_turns),
                "steps": int(ctx.last_build_steps),
            },
            "last_applied_step_id": ctx.last_applied_step_id,
        }
        if tail_tool_highlight:
            report["tail_tool_highlight"] = tail_tool_highlight
        return report

    async def update_calibration(
        self,
        trace_id: str,
        actual_prompt_tokens: int,
        tool_schemas: Optional[List[Dict[str, Any]]] = None,
        toolset_version: Optional[str] = None,
    ) -> None:
        if not actual_prompt_tokens:
            return
        loaded = await self._load_ctx(trace_id)
        if not loaded:
            return
        ctx = self._coerce_ctx(loaded)
        self._ensure_schema_tokens_cached(ctx, tool_schemas=tool_schemas, toolset_version=toolset_version)
        estimated_raw = self._estimate_prompt_tokens_raw_from_ctx(ctx)
        ratio = actual_prompt_tokens / max(1, estimated_raw)
        self.estimator.set_multiplier(ctx.calibration_multiplier)
        self.estimator.update_calibration_from_ratio(ratio)
        ctx.calibration_multiplier = self.estimator.multiplier
        ctx.updated_at = time.time()
        await self._save_ctx(ctx)

    async def finalize_turn_to_result_card(
        self,
        trace_id: str,
        *,
        turn_id: str,
        user_input: Optional[str],
        final_answer: Optional[str],
        tool_state: Optional[List[str]] = None,
        step_ids: Optional[List[str]] = None,
    ) -> None:
        # Intentionally no-op: keep context as-is (no replacement/compression).
        del trace_id, turn_id, user_input, final_answer, tool_state, step_ids
        return

    async def _get_or_create_ctx(self, trace_id: str) -> RuntimeContext:
        sys_prompt = self._build_system_prompt_fn()
        loaded = await self._load_ctx(trace_id)
        if loaded:
            ctx = self._coerce_ctx(loaded)
            self.estimator.set_multiplier(ctx.calibration_multiplier)
            return ctx
        ctx = self._new_ctx(trace_id, sys_prompt)
        ctx.calibration_multiplier = self.estimator.multiplier
        return ctx

    def _new_ctx(self, trace_id: str, sys_prompt: str) -> RuntimeContext:
        ctx = RuntimeContext(trace_id=trace_id, calibration_multiplier=self.estimator.multiplier)
        sys_msg = {"role": "system", "content": sys_prompt}
        tok = self.estimator.estimate_message_tokens_raw(sys_msg)
        ctx.messages = [sys_msg]
        ctx.msg_tokens_raw = [tok]
        ctx.msg_tokens_raw_sum = tok
        return ctx

    def _coerce_ctx(self, loaded: CtxType) -> RuntimeContext:
        if isinstance(loaded, RuntimeContext):
            if loaded.msg_tokens_raw_sum <= 0 and loaded.msg_tokens_raw:
                loaded.msg_tokens_raw_sum = int(sum(loaded.msg_tokens_raw))
            return loaded
        if isinstance(loaded, dict):
            ctx = RuntimeContext.from_dict(loaded)
            if ctx.msg_tokens_raw_sum <= 0 and ctx.msg_tokens_raw:
                ctx.msg_tokens_raw_sum = int(sum(ctx.msg_tokens_raw))
            return ctx
        raise TypeError(f"Unsupported ctx type: {type(loaded)}")

    def _reset_runtime_view(self, ctx: RuntimeContext) -> None:
        system_prompt = self._build_system_prompt_fn()
        sys_msg = {"role": "system", "content": system_prompt}
        sys_tok = self.estimator.estimate_message_tokens_raw(sys_msg)
        ctx.messages = [sys_msg]
        ctx.msg_tokens_raw = [sys_tok]
        ctx.msg_tokens_raw_sum = int(sys_tok)
        ctx.last_applied_step_id = None
        ctx.last_build_turns = 0
        ctx.last_build_steps = 0

    def _turns_for_context_view(self, trace: Any) -> List[Any]:
        return list(getattr(trace, "turns", []) or [])

    def _rebuild_messages_from_trace(self, ctx: RuntimeContext, trace: Any) -> None:
        self._reset_runtime_view(ctx)
        turns = self._turns_for_context_view(trace)
        ctx.last_build_turns = len(turns)
        applied_steps = 0
        seen_turn_ids: set[str] = set()
        seen_step_ids: set[str] = set()
        for turn in turns:
            turn_id = getattr(turn, "turn_id", None) or f"turn_missing_{len(seen_turn_ids)}"
            if turn_id not in seen_turn_ids:
                user_input = getattr(turn, "user_input", None)
                if user_input is not None:
                    self._append_message(ctx, {"role": "user", "content": str(user_input)})
                seen_turn_ids.add(turn_id)
            for step in getattr(turn, "steps", []) or []:
                step_id = getattr(step, "step_id", None) or f"step_missing_{len(seen_step_ids)}"
                if step_id in seen_step_ids:
                    continue
                seen_step_ids.add(step_id)
                if self._append_step_if_new_or_empty(ctx, step, step_id=step_id):
                    applied_steps += 1
                    ctx.last_applied_step_id = step_id
        ctx.last_build_steps = applied_steps

    def _estimate_prompt_tokens_raw_from_ctx(self, ctx: RuntimeContext) -> int:
        return int(ctx.msg_tokens_raw_sum + ctx.tool_schema_tokens_raw)

    def _estimate_prompt_tokens_from_ctx(self, ctx: RuntimeContext) -> int:
        self.estimator.set_multiplier(ctx.calibration_multiplier)
        raw = self._estimate_prompt_tokens_raw_from_ctx(ctx)
        return max(1, int(raw * self.estimator.multiplier))

    def _schema_key(self, schema: Any, version: Optional[str], prefix: str) -> Optional[str]:
        if schema is None:
            return None
        if version:
            return f"{prefix}:v:{version}"
        return f"{prefix}:h:{_sha1(_stable_json(schema))}"

    def _ensure_schema_tokens_cached(
        self,
        ctx: RuntimeContext,
        tool_schemas: Optional[List[Dict[str, Any]]],
        toolset_version: Optional[str],
    ) -> None:
        tkey = self._schema_key(tool_schemas, toolset_version, "toolset")
        if tkey != ctx.tool_schema_key:
            ctx.tool_schema_key = tkey
            ctx.tool_schema_tokens_raw = self.estimator.estimate_struct_tokens_raw(tool_schemas)

    def _append_message(self, ctx: RuntimeContext, msg: Dict[str, Any]) -> None:
        t = self.estimator.estimate_message_tokens_raw(msg)
        ctx.messages.append(msg)
        ctx.msg_tokens_raw.append(t)
        ctx.msg_tokens_raw_sum += t

    def _append_step_if_new_or_empty(self, ctx: RuntimeContext, step: Any, *, step_id: str) -> bool:
        start = len(ctx.messages)
        tool_calls = getattr(step, "tool_calls", None) or []
        observations = getattr(step, "observations", None) or []
        actions = getattr(step, "actions", None) or []

        obs_map: Dict[str, Any] = {}
        for observation in observations:
            action_id = getattr(observation, "action_id", None)
            if action_id:
                obs_map[action_id] = observation

        if tool_calls:
            self._append_message(ctx, {"role": "assistant", "tool_calls": tool_calls})
            for call in tool_calls:
                call_id = (call or {}).get("id")
                obs = obs_map.get(call_id)
                if obs is None:
                    raise RuntimeError(f"Missing observation for tool call id={call_id} in step={step_id}")
                content = getattr(obs, "content", "")
                self._append_message(ctx, {"role": "tool", "tool_call_id": call_id, "content": self._to_str(content)})
        else:
            for action in actions:
                full_ref = getattr(action, "full_ref", None)
                if full_ref:
                    self._append_message(ctx, {"role": "assistant", "content": self._to_str(full_ref)})
                req = getattr(action, "request_input", None)
                if req:
                    self._append_message(ctx, {"role": "user", "content": self._to_str(req)})

        end = len(ctx.messages)
        if end == start:
            return False
        return True

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

    def _extract_tool_result_highlights(self, content: str) -> Optional[Dict[str, Any]]:
        text = str(content or "").strip()
        if not text:
            return None
        try:
            obj = json.loads(text)
        except Exception:
            return {"tool_type": "tool_text", "preview": text[:200]}
        if not isinstance(obj, dict):
            return {"tool_type": "tool_json", "preview": text[:200]}
        out: Dict[str, Any] = {"tool_type": str(obj.get("type") or "") or "tool_json"}
        if obj.get("summary"):
            out["summary"] = str(obj.get("summary"))
        facts = obj.get("facts")
        if isinstance(facts, dict):
            if "exit_code" in facts:
                out["exit_code"] = facts.get("exit_code")
            if "timed_out" in facts:
                out["timed_out"] = facts.get("timed_out")
            if "engine_used" in facts:
                out["engine_used"] = facts.get("engine_used")
            if "fallback_from" in facts:
                out["fallback_from"] = facts.get("fallback_from")
        return out


class InMemoryCacheManager(CacheManager):
    def __init__(
        self,
        model: str,
        api_key: str,
        base_url: str,
        build_system_prompt_fn: Callable[[], str],
        config: Optional[CacheManagerConfig] = None,
        estimator: Optional[TokenEstimator] = None,
    ) -> None:
        super().__init__(
            model=model,
            api_key=api_key,
            base_url=base_url,
            build_system_prompt_fn=build_system_prompt_fn,
            config=config,
            estimator=estimator,
        )
        self._store: Dict[str, RuntimeContext] = {}

    async def _load_ctx(self, trace_id: str) -> Optional[CtxType]:
        return self._store.get(trace_id)

    async def _save_ctx(self, ctx: RuntimeContext) -> None:
        self._store[ctx.trace_id] = ctx

    async def _delete_ctx(self, trace_id: str) -> None:
        self._store.pop(trace_id, None)
