from __future__ import annotations

import re
import logging
import abc
import json
import time
import hashlib
from dataclasses import dataclass, field, asdict
from pydantic import BaseModel, Field
from typing import Any, Dict, List, Optional, Tuple, Callable, Union
from openai import OpenAI
from openai.types.shared_params.response_format_json_schema import ResponseFormatJSONSchema, JSONSchema
from core.context_event_audit import append_context_event

logger = logging.getLogger(__name__)


class LlmSummary(BaseModel):
    """
    你可以按需扩字段，比如：
    - key_facts: list[str]
    - decisions: list[str]
    - open_questions: list[str]
    - tool_usage: list[dict]
    但注意：字段越多，summary 越长。先小而稳。
    """
    summary: str = Field(..., description="Compressed summary of the head context, concise but complete.")
    key_points: List[str] = Field(default_factory=list, description="Optional bullet highlights.")

summary_llm_schema = ResponseFormatJSONSchema(
    type="json_schema",
    json_schema=JSONSchema(
        name="llm_summary",
        description="""
Compressed summary object for head-context compaction.

Keep it small and stable:
- summary is required and should be concise but complete.
- key_points is optional bullet highlights.
""",
        strict=True,
        schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["summary"],
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "Compressed summary of the head context, concise but complete.",
                    "minLength": 1
                },
                "key_points": {
                    "type": "array",
                    "description": "Optional bullet highlights.",
                    "items": {
                        "type": "string",
                        "minLength": 1
                    },
                    "default": []
                }
            }
        }
    )
)

def _extract_first_balanced_json_object(text: str) -> Optional[str]:
    """
    从 text 中抽取第一个“完整配对”的 JSON 对象：{ ... }。
    关键：忽略字符串中的花括号，并处理转义字符。
    """
    start = text.find("{")
    if start < 0:
        return None

    in_str = False
    escape = False
    depth = 0

    for i in range(start, len(text)):
        ch = text[i]

        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        else:
            if ch == '"':
                in_str = True
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return text[start:i+1].strip()

    return None

JSON_CODEBLOCK_RE = re.compile(
    r"```(?:json)?\s*([\s\S]*?)\s*```",
    re.IGNORECASE
)

def extract_json(s: str) -> str:
    s = (s or "").strip()
    # markdown code block
    m = JSON_CODEBLOCK_RE.search(s)
    if m:
        return m.group(1).strip()
    else:
        res =  _extract_first_balanced_json_object(s)
        return res if res else s


def _stable_json(obj: Any) -> str:
    # stable hashing across runs
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

    # Buckets (cheap improvement over "chars/4" only)
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
        """
        Rough per-message token estimate.
        - content/role/tool_call_id treated as text
        - tool_calls treated as structured json
        """
        text_chars = 0
        struct_chars = 0

        text_chars += len(str(msg.get("role", "")))
        text_chars += len(str(msg.get("content", "")))
        if msg.get("tool_call_id"):
            text_chars += len(str(msg["tool_call_id"]))

        if msg.get("tool_calls"):
            struct_chars += len(_stable_json(msg["tool_calls"]))

        # chars -> tokens
        t_text = text_chars // self.config.chars_per_token_text
        t_struct = struct_chars // self.config.chars_per_token_struct
        return max(1, int(t_text + t_struct))

    def estimate_struct_tokens_raw(self, obj: Any) -> int:
        """
        Rough token estimate for structured JSON blobs like tool_schemas/response_schema.
        """
        s = _stable_json(obj)
        return max(1, len(s) // self.config.chars_per_token_struct)

    def update_calibration_from_ratio(self, ratio: float) -> None:
        ratio = _clamp(ratio, self.config.ratio_min, self.config.ratio_max)
        alpha = self.config.calibration_ema
        new_mult = (1 - alpha) * self._multiplier + alpha * ratio
        self._multiplier = float(_clamp(new_mult, self.config.calibration_min, self.config.calibration_max))


@dataclass
class RuntimeContext:
    trace_id: str
    system_prompt_hash: str

    # persisted calibration
    calibration_multiplier: float = 1.0

    # messages cache
    messages: List[Dict[str, Any]] = field(default_factory=list)
    msg_tokens_raw: List[int] = field(default_factory=list)
    msg_tokens_raw_sum: int = 0  # 增量维护 messages raw token 总和

    # schema caches (version or hash key)
    tool_schema_key: Optional[str] = None
    tool_schema_tokens_raw: int = 0

    response_schema_key: Optional[str] = None
    response_schema_tokens_raw: int = 0

    # step bookkeeping (incremental build)
    step_order: List[str] = field(default_factory=list)
    step_span_map: Dict[str, Tuple[int, int]] = field(default_factory=dict)
    step_tokens_raw: Dict[str, int] = field(default_factory=dict)
    last_applied_step_id: Optional[str] = None

    # turn bookkeeping
    seen_turn_ids: List[str] = field(default_factory=list)
    # step-level dedupe (must survive result-card replacement)
    seen_step_ids: List[str] = field(default_factory=list)

    # summarization audit
    summary_versions: List[Dict[str, Any]] = field(default_factory=list)
    # pruning cooldown marker
    last_prune_turn_index: int = 0

    updated_at: float = field(default_factory=lambda: time.time())

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["step_span_map"] = {k: [v[0], v[1]] for k, v in self.step_span_map.items()}
        return d

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "RuntimeContext":
        return RuntimeContext(
            trace_id=d["trace_id"],
            system_prompt_hash=d["system_prompt_hash"],
            calibration_multiplier=float(d.get("calibration_multiplier", 1.0)),
            messages=d.get("messages", []),
            msg_tokens_raw=d.get("msg_tokens_raw", []),
            msg_tokens_raw_sum=int(d.get("msg_tokens_raw_sum", 0)),
            tool_schema_key=d.get("tool_schema_key"),
            tool_schema_tokens_raw=int(d.get("tool_schema_tokens_raw", 0)),
            response_schema_key=d.get("response_schema_key"),
            response_schema_tokens_raw=int(d.get("response_schema_tokens_raw", 0)),
            step_order=d.get("step_order", []),
            step_span_map={k: (v[0], v[1]) for k, v in (d.get("step_span_map") or {}).items()},
            step_tokens_raw=d.get("step_tokens_raw", {}),
            last_applied_step_id=d.get("last_applied_step_id"),
            # migration-safe
            seen_step_ids=d.get("seen_step_ids", []),
            seen_turn_ids=d.get("seen_turn_ids", []),
            summary_versions=d.get("summary_versions", []),
            last_prune_turn_index=int(d.get("last_prune_turn_index", 0)),
            updated_at=float(d.get("updated_at", time.time())),
        )


@dataclass
class CacheManagerConfig:
    # trigger compaction when estimated prompt tokens >= threshold_tokens
    threshold_tokens: int = 20_000
    # keep latest tail ratio raw (e.g. 30%)
    keep_tail_ratio: float = 0.30
    # try to compact until estimated tokens <= target_after_tokens
    target_after_tokens: int = 17_000

    # summary message placement
    summary_role: str = "system"  # or "assistant"

    # summary limits
    max_user_goal_chars: int = 300
    max_tool_args_chars: int = 300
    max_tool_result_chars: int = 2000
    # For snapshot_query_result extraction
    max_top_hits_lines: int = 12

    # safety loop to avoid infinite compaction
    max_compaction_rounds: int = 6

    max_summary_tokens: int = 2000

    # context defense pipeline (v2)
    history_limit_turns: int = 10
    history_limit_trigger_ratio: float = 0.70
    pruning_soft_trigger_ratio: float = 0.72
    pruning_hard_trigger_ratio: float = 0.86
    compaction_trigger_ratio: float = 0.92
    compaction_target_ratio: float = 0.75
    pruning_cooldown_turns: int = 2
    prune_protect_recent_tool_results: int = 3

    # soft/hard pruning details
    soft_trim_min_chars: int = 4000
    soft_trim_head_chars: int = 1500
    soft_trim_tail_chars: int = 1500
    hard_clear_min_total_chars: int = 50_000

    # Turn Result Card behavior
    # 约定 Result Card 作为普通 content 插入 messages，靠前缀识别并在压缩时保留
    result_card_prefix: str = "TURN_RESULT_CARD_JSON:"
    max_result_card_chars: int = 4000
    max_turn_cards: int = 50


CtxType = Union[RuntimeContext, Dict[str, Any]]


class CacheManager(abc.ABC):
    """
    Abstract CacheManager contract + shared algorithm implementation.
    Subclasses only implement storage hooks.
    """

    def __init__(
            self,
            model: str, api_key: str, base_url: str,
            build_system_prompt_fn: Callable[[], str],
            config: Optional[CacheManagerConfig] = None,
            estimator: Optional[TokenEstimator] = None,
    ) -> None:
        self.config = config or CacheManagerConfig()
        self.estimator = estimator or TokenEstimator()
        self._build_system_prompt_fn = build_system_prompt_fn
        self.model = model
        self.client = OpenAI(
            base_url=base_url,
            api_key=api_key
        )

    # ---------- storage hooks ----------
    @abc.abstractmethod
    async def _load_ctx(self, trace_id: str) -> Optional[CtxType]:
        ...

    @abc.abstractmethod
    async def _save_ctx(self, ctx: RuntimeContext) -> None:
        ...

    @abc.abstractmethod
    async def _delete_ctx(self, trace_id: str) -> None:
        ...

    # ---------- public api ----------
    async def build_messages(
            self,
            trace: Any,
            tool_schemas: Optional[List[Dict[str, Any]]] = None,
            response_schema: Optional[Dict[str, Any]] = None,
            toolset_version: Optional[str] = None,
            response_schema_version: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Rebuilds model context view from trace and applies deterministic context defenses:
        history_limit -> pruning -> compaction.

        toolset_version/response_schema_version:
        - If provided, used as cache key (fastest).
        - If not provided, will fallback to hashing stable-json of the schema (still cheap).
        """

        trace_id = getattr(trace, "trace_id")
        ctx = await self._get_or_create_ctx(trace_id)

        # Rebuild runtime view from trace each round to keep pipeline deterministic.
        self._rebuild_messages_from_trace(ctx, trace)

        # cache schema token estimates (cheap; only on change)
        self._ensure_schema_tokens_cached(
            ctx,
            tool_schemas=tool_schemas,
            response_schema=response_schema,
            toolset_version=toolset_version,
            response_schema_version=response_schema_version,
        )

        await self._apply_context_defense(ctx, trace=trace)

        ctx.updated_at = time.time()
        await self._save_ctx(ctx)
        return ctx.messages

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

        report = {
            "trace_id": ctx.trace_id,
            "updated_at": ctx.updated_at,
            "calibration_multiplier": ctx.calibration_multiplier,
            "estimated_prompt_tokens": estimated_prompt_tokens,
            "estimated_prompt_tokens_raw": estimated_prompt_tokens_raw,
            "buckets": {
                "messages_raw_tokens": ctx.msg_tokens_raw_sum,
                "tool_schema_raw_tokens": ctx.tool_schema_tokens_raw,
                "response_schema_raw_tokens": ctx.response_schema_tokens_raw,
            },
            "counts": {
                "messages": len(ctx.messages),
                "steps": len(ctx.step_order),
                "seen_turns": len(ctx.seen_turn_ids),
                "seen_steps": len(ctx.seen_step_ids),
                "summary_versions": len(ctx.summary_versions),
            },
            "last_applied_step_id": ctx.last_applied_step_id,
            "step_order_tail": ctx.step_order[-10:],
            "summary_versions_tail": ctx.summary_versions[-5:],
        }
        if tail_tool_highlight:
            report["tail_tool_highlight"] = tail_tool_highlight
        return report

    async def update_calibration(
            self,
            trace_id: str,
            actual_prompt_tokens: int,
            tool_schemas: Optional[List[Dict[str, Any]]] = None,
            response_schema: Optional[Dict[str, Any]] = None,
            toolset_version: Optional[str] = None,
            response_schema_version: Optional[str] = None,
    ) -> None:
        """
        O(1) calibration update using ctx cached raw token sums + cached schema raw tokens.
        Multiplier is persisted into ctx.calibration_multiplier.

        Note: must be called AFTER build_messages() of that request to ensure ctx has latest messages.
        """
        if not actual_prompt_tokens:
            return

        loaded = await self._load_ctx(trace_id)
        if not loaded:
            return

        ctx = self._coerce_ctx(loaded)

        # ensure schema caches aligned with current request
        self._ensure_schema_tokens_cached(
            ctx,
            tool_schemas=tool_schemas,
            response_schema=response_schema,
            toolset_version=toolset_version,
            response_schema_version=response_schema_version,
        )

        estimated_raw = self._estimate_prompt_tokens_raw_from_ctx(ctx)
        ratio = actual_prompt_tokens / max(1, estimated_raw)

        self.estimator.set_multiplier(ctx.calibration_multiplier)
        self.estimator.update_calibration_from_ratio(ratio)

        ctx.calibration_multiplier = self.estimator.multiplier
        ctx.updated_at = time.time()
        await self._save_ctx(ctx)

    # ---------- ctx init/reset ----------
    async def _get_or_create_ctx(self, trace_id: str) -> RuntimeContext:
        sys_prompt = self._build_system_prompt_fn()
        sys_hash = _sha1(sys_prompt)

        loaded = await self._load_ctx(trace_id)
        if loaded:
            ctx = self._coerce_ctx(loaded)
            # apply ctx multiplier for consistent estimation in this build
            self.estimator.set_multiplier(ctx.calibration_multiplier)
            # If system prompt changed, reset runtime view
            if ctx.system_prompt_hash != sys_hash:
                # reset messages cache, keep multiplier
                m = ctx.calibration_multiplier
                ctx = self._new_ctx(trace_id, sys_prompt, sys_hash)
                ctx.calibration_multiplier = m
            return ctx

        ctx = self._new_ctx(trace_id, sys_prompt, sys_hash)
        # new ctx uses current estimator multiplier (default 1.0)
        ctx.calibration_multiplier = self.estimator.multiplier
        return ctx

    def _new_ctx(self, trace_id: str, sys_prompt: str, sys_hash: str) -> RuntimeContext:
        ctx = RuntimeContext(trace_id=trace_id, system_prompt_hash=sys_hash,
                             calibration_multiplier=self.estimator.multiplier)
        sys_msg = {"role": "system", "content": sys_prompt}
        t = self.estimator.estimate_message_tokens_raw(sys_msg)
        ctx.messages = [sys_msg]
        ctx.msg_tokens_raw = [t]
        ctx.msg_tokens_raw_sum = t
        return ctx

    def _coerce_ctx(self, loaded: CtxType) -> RuntimeContext:
        if isinstance(loaded, RuntimeContext):
            # if sum missing, rebuild once (migration safety)
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
        ctx.step_order = []
        ctx.step_span_map = {}
        ctx.step_tokens_raw = {}
        ctx.last_applied_step_id = None
        ctx.seen_turn_ids = []
        ctx.seen_step_ids = []

    def _turns_for_context_view(self, trace: Any) -> List[Any]:
        turns = list(getattr(trace, "turns", []) or [])
        limit = max(1, int(self.config.history_limit_turns))
        if len(turns) <= limit:
            return turns
        return turns[-limit:]

    def _rebuild_messages_from_trace(self, ctx: RuntimeContext, trace: Any) -> None:
        self._reset_runtime_view(ctx)
        turns = self._turns_for_context_view(trace)
        for turn in turns:
            self._append_turn_user_input_if_needed(ctx, turn)
            for step in getattr(turn, "steps", []) or []:
                self._append_step_if_new_or_empty(ctx, step)

    def _context_metrics(self, ctx: RuntimeContext) -> Dict[str, Any]:
        est = self._estimate_prompt_tokens_from_ctx(ctx)
        threshold = max(1, int(self.config.threshold_tokens))
        return {
            "estimated_prompt_tokens": est,
            "threshold_tokens": threshold,
            "u": round(est / threshold, 4),
        }

    def _replace_message_content(self, ctx: RuntimeContext, idx: int, new_content: str) -> None:
        old_msg = ctx.messages[idx]
        new_msg = dict(old_msg)
        new_msg["content"] = new_content
        old_tok = int(ctx.msg_tokens_raw[idx])
        new_tok = int(self.estimator.estimate_message_tokens_raw(new_msg))
        ctx.messages[idx] = new_msg
        ctx.msg_tokens_raw[idx] = new_tok
        ctx.msg_tokens_raw_sum += (new_tok - old_tok)

    def _soft_trim_text(self, text: str) -> str:
        if len(text) <= max(0, self.config.soft_trim_head_chars + self.config.soft_trim_tail_chars):
            return text
        head = text[: self.config.soft_trim_head_chars]
        tail = text[-self.config.soft_trim_tail_chars:]
        omitted = max(0, len(text) - len(head) - len(tail))
        marker = f"\n[... OMITTED MIDDLE: {omitted} chars ...]\n"
        return head + marker + tail

    def _build_hard_placeholder_content(self, old_content: str) -> str:
        highlight = self._extract_tool_result_highlights(old_content) or {}
        summary = (
            str(highlight.get("summary") or "").strip()
            or str(highlight.get("preview") or "").strip()
            or "tool result replaced by placeholder"
        )
        if len(summary) > 240:
            summary = summary[:240] + "..."

        refs: Dict[str, Any] = {}
        if highlight.get("latest_path"):
            refs["latest_path"] = highlight.get("latest_path")
        artifact_paths = highlight.get("artifact_paths")
        if isinstance(artifact_paths, list) and artifact_paths:
            refs["artifact_paths"] = artifact_paths[:3]

        facts: Dict[str, Any] = {
            "tool_type": highlight.get("tool_type"),
            "exit_code": highlight.get("exit_code"),
            "timed_out": highlight.get("timed_out"),
            "engine_used": highlight.get("engine_used"),
            "fallback_from": highlight.get("fallback_from"),
            "trim_mode": "hard",
            "truncated": True,
            "raw_chars": len(old_content),
            "kept_head_chars": 0,
            "kept_tail_chars": 0,
        }
        placeholder = {
            "type": "tool_result_placeholder_v1",
            "summary": summary,
            "facts": {k: v for k, v in facts.items() if v is not None},
            "refs": refs,
        }
        return json.dumps(placeholder, ensure_ascii=False, separators=(",", ":"))

    def _prunable_tool_indexes(self, ctx: RuntimeContext) -> List[int]:
        tool_indexes = [i for i, m in enumerate(ctx.messages) if m.get("role") == "tool" and i > 0]
        if not tool_indexes:
            return []
        protect_n = max(0, int(self.config.prune_protect_recent_tool_results))
        protected = set(tool_indexes[-protect_n:]) if protect_n > 0 else set()
        return [idx for idx in tool_indexes if idx not in protected]

    def _apply_tool_result_pruning(self, ctx: RuntimeContext, *, trace: Any) -> None:
        threshold = max(1, int(self.config.threshold_tokens))
        soft_trigger = int(threshold * float(self.config.pruning_soft_trigger_ratio))
        hard_trigger = int(threshold * float(self.config.pruning_hard_trigger_ratio))
        stop_target = int(threshold * float(self.config.compaction_target_ratio))

        before = self._context_metrics(ctx)
        if before["estimated_prompt_tokens"] < soft_trigger:
            return

        turn_count = len(getattr(trace, "turns", []) or [])
        cooldown = max(0, int(self.config.pruning_cooldown_turns))
        if ctx.last_prune_turn_index > 0 and (turn_count - ctx.last_prune_turn_index) <= cooldown:
            return

        candidates = self._prunable_tool_indexes(ctx)
        if not candidates:
            return

        changed_soft = 0
        released_soft_chars = 0
        for idx in candidates:
            current = self._context_metrics(ctx)
            if current["estimated_prompt_tokens"] <= stop_target:
                break
            text = str(ctx.messages[idx].get("content", ""))
            if len(text) < int(self.config.soft_trim_min_chars):
                continue
            trimmed = self._soft_trim_text(text)
            if trimmed == text:
                continue
            self._replace_message_content(ctx, idx, trimmed)
            changed_soft += 1
            released_soft_chars += max(0, len(text) - len(trimmed))

        if changed_soft > 0:
            after_soft = self._context_metrics(ctx)
            append_context_event(
                trace_id=getattr(trace, "trace_id"),
                turn_id=getattr(trace, "current_turn_id", None),
                step_id=getattr(trace, "current_step_id", None),
                event_type="CONTEXT_PRUNE_SOFT",
                reason_code="soft_trigger",
                metrics_before=before,
                metrics_after=after_soft,
                changes={
                    "trimmed_items": changed_soft,
                    "released_chars": released_soft_chars,
                    "protected_recent_tool_results": int(self.config.prune_protect_recent_tool_results),
                },
            )
            ctx.last_prune_turn_index = turn_count

        mid = self._context_metrics(ctx)
        if mid["estimated_prompt_tokens"] < hard_trigger:
            return

        total_candidate_chars = 0
        for idx in candidates:
            total_candidate_chars += len(str(ctx.messages[idx].get("content", "")))
        if total_candidate_chars < int(self.config.hard_clear_min_total_chars):
            return

        changed_hard = 0
        released_hard_chars = 0
        for idx in candidates:
            current = self._context_metrics(ctx)
            if current["estimated_prompt_tokens"] <= stop_target:
                break
            old = str(ctx.messages[idx].get("content", ""))
            placeholder = self._build_hard_placeholder_content(old)
            if placeholder == old:
                continue
            self._replace_message_content(ctx, idx, placeholder)
            changed_hard += 1
            released_hard_chars += max(0, len(old) - len(placeholder))

        if changed_hard > 0:
            after_hard = self._context_metrics(ctx)
            append_context_event(
                trace_id=getattr(trace, "trace_id"),
                turn_id=getattr(trace, "current_turn_id", None),
                step_id=getattr(trace, "current_step_id", None),
                event_type="CONTEXT_PRUNE_HARD",
                reason_code="hard_trigger",
                metrics_before=mid,
                metrics_after=after_hard,
                changes={
                    "cleared_items": changed_hard,
                    "released_chars": released_hard_chars,
                },
            )
            ctx.last_prune_turn_index = turn_count

    async def _apply_context_defense(self, ctx: RuntimeContext, *, trace: Any) -> None:
        turns_total = len(getattr(trace, "turns", []) or [])
        turns_kept = len(self._turns_for_context_view(trace))
        if turns_total > turns_kept:
            append_context_event(
                trace_id=getattr(trace, "trace_id"),
                turn_id=getattr(trace, "current_turn_id", None),
                step_id=getattr(trace, "current_step_id", None),
                event_type="CONTEXT_HISTORY_LIMIT",
                reason_code="history_limit",
                metrics_before=None,
                metrics_after=self._context_metrics(ctx),
                changes={"dropped_turns": turns_total - turns_kept, "kept_turns": turns_kept},
            )

        self._apply_tool_result_pruning(ctx, trace=trace)

        before_compact = self._context_metrics(ctx)
        compaction_trigger = int(max(1, self.config.threshold_tokens) * float(self.config.compaction_trigger_ratio))
        if before_compact["estimated_prompt_tokens"] < compaction_trigger:
            return

        append_context_event(
            trace_id=getattr(trace, "trace_id"),
            turn_id=getattr(trace, "current_turn_id", None),
            step_id=getattr(trace, "current_step_id", None),
            event_type="CONTEXT_COMPACT_START",
            reason_code="compaction_trigger",
            metrics_before=before_compact,
            metrics_after=before_compact,
            changes=None,
        )
        try:
            await self._maybe_compact(ctx)
            after_compact = self._context_metrics(ctx)
            append_context_event(
                trace_id=getattr(trace, "trace_id"),
                turn_id=getattr(trace, "current_turn_id", None),
                step_id=getattr(trace, "current_step_id", None),
                event_type="CONTEXT_COMPACT_DONE",
                reason_code="compaction_done",
                metrics_before=before_compact,
                metrics_after=after_compact,
                changes=None,
            )
        except Exception as exc:
            append_context_event(
                trace_id=getattr(trace, "trace_id"),
                turn_id=getattr(trace, "current_turn_id", None),
                step_id=getattr(trace, "current_step_id", None),
                event_type="CONTEXT_COMPACT_FAILED",
                reason_code=type(exc).__name__,
                metrics_before=before_compact,
                metrics_after=self._context_metrics(ctx),
                changes={"error": str(exc)},
            )

    # ---------- O(1) estimation ----------
    def _estimate_prompt_tokens_raw_from_ctx(self, ctx: RuntimeContext) -> int:
        return int(ctx.msg_tokens_raw_sum + ctx.tool_schema_tokens_raw + ctx.response_schema_tokens_raw)

    def _estimate_prompt_tokens_from_ctx(self, ctx: RuntimeContext) -> int:
        self.estimator.set_multiplier(ctx.calibration_multiplier)
        raw = self._estimate_prompt_tokens_raw_from_ctx(ctx)
        return max(1, int(raw * self.estimator.multiplier))

    # ---------- schema token caches ----------
    def _schema_key(
            self,
            schema: Any,
            version: Optional[str],
            prefix: str,
    ) -> Optional[str]:
        if schema is None:
            return None
        if version:
            return f"{prefix}:v:{version}"
        # fallback to hash
        return f"{prefix}:h:{_sha1(_stable_json(schema))}"

    def _ensure_schema_tokens_cached(
            self,
            ctx: RuntimeContext,
            tool_schemas: Optional[List[Dict[str, Any]]],
            response_schema: Optional[Dict[str, Any]],
            toolset_version: Optional[str],
            response_schema_version: Optional[str],
    ) -> None:
        # tool schemas
        tkey = self._schema_key(tool_schemas, toolset_version, "toolset")
        if tkey != ctx.tool_schema_key:
            ctx.tool_schema_key = tkey
            ctx.tool_schema_tokens_raw = self.estimator.estimate_struct_tokens_raw(
                tool_schemas) if tool_schemas else 0

        # response schema
        rkey = self._schema_key(response_schema, response_schema_version, "resp")
        if rkey != ctx.response_schema_key:
            ctx.response_schema_key = rkey
            ctx.response_schema_tokens_raw = self.estimator.estimate_struct_tokens_raw(
                response_schema) if response_schema else 0

    # ---------- append logic (incremental) ----------
    def _append_message(self, ctx: RuntimeContext, msg: Dict[str, Any]) -> None:
        t = self.estimator.estimate_message_tokens_raw(msg)
        ctx.messages.append(msg)
        ctx.msg_tokens_raw.append(t)
        ctx.msg_tokens_raw_sum += t

    # ---------- append logic ----------
    def _append_turn_user_input_if_needed(self, ctx: RuntimeContext, turn: Any) -> None:
        """
        Ensures each Turn.user_input is appended once as a user message.
        We treat turn_id as idempotency key. We do NOT store turn spans; it's enough to avoid duplicates.
        """
        # We only append user_input when we see the first step of that turn,
        # OR if the turn has no steps yet (still want the user query in context).
        # Here we append whenever we encounter the turn, but with dedupe token "turn:<turn_id>".
        turn_id = getattr(turn, "turn_id", None) or f"turn_missing_{len(ctx.seen_turn_ids)}"
        if turn_id in ctx.seen_turn_ids:
            return

        user_input = getattr(turn, "user_input", None)
        if user_input is None:
            return

        self._append_message(ctx, {"role": "user", "content": str(user_input)})
        ctx.seen_turn_ids.append(turn_id)

    def _append_step_if_new_or_empty(self, ctx: RuntimeContext, step: Any) -> None:
        """
        Append a step into messages (for unfinished/current turns).
        We still track step spans so tail compaction works if threshold is crossed mid-turn.
        """
        step_id = getattr(step, "step_id", None) or f"step_missing_{len(ctx.step_order)}"
        if step_id in ctx.seen_step_ids:
            ctx.last_applied_step_id = step_id
            return

        start = len(ctx.messages)

        tool_calls = getattr(step, "tool_calls", None) or []
        observations = getattr(step, "observations", None) or []
        actions = getattr(step, "actions", None) or []

        # Map observation by action_id
        obs_map: Dict[str, Any] = {}
        for o in observations:
            aid = getattr(o, "action_id", None)
            if aid:
                obs_map[aid] = o

        if tool_calls:
            self._append_message(ctx, {"role": "assistant", "tool_calls": tool_calls})
            for call in tool_calls:
                call_id = (call or {}).get("id")
                obs = obs_map.get(call_id)
                if obs is None:
                    raise RuntimeError(f"Missing observation for tool call id={call_id} in step={step_id}")
                content = getattr(obs, "content", "")
                self._append_message(
                    ctx,
                    {"role": "tool", "tool_call_id": call_id, "content": self._to_str(content)},
                )
        else:
            for a in actions:
                full_ref = getattr(a, "full_ref", None)
                if full_ref:
                    self._append_message(ctx, {"role": "assistant", "content": self._to_str(full_ref)})

                req = getattr(a, "request_input", None)
                if req:
                    self._append_message(ctx, {"role": "user", "content": self._to_str(req)})

        end = len(ctx.messages)
        if end == start:
            # step 目前对 messages 没有贡献，不要记录为已应用
            return

        ctx.seen_step_ids.append(step_id)

        ctx.step_order.append(step_id)
        ctx.step_span_map[step_id] = (start, end)
        ctx.step_tokens_raw[step_id] = int(sum(ctx.msg_tokens_raw[start:end]))
        ctx.last_applied_step_id = step_id

    @staticmethod
    def _to_str(x: Any) -> str:
        if x is None:
            return ""
        if isinstance(x, str):
            return x
        try:
            return json.dumps(x, ensure_ascii=False)
        except Exception:
            return str(x)

    # ---------- compaction ----------
    async def _maybe_compact(self, ctx: RuntimeContext) -> None:
        est = self._estimate_prompt_tokens_from_ctx(ctx)
        if est < self.config.threshold_tokens:
            return

        logger.info("has occurred fast compaction")
        did = await self._compact_keep_tail(ctx)
        if not did:
            return
        est = self._estimate_prompt_tokens_from_ctx(ctx)
        logger.info(f"current context tokens: {est} after compaction")

        if est > self.config.target_after_tokens:
            logger.info("has occurred summarization by LLM")
            await self._summary_keep_tail(ctx)
            est = self._estimate_prompt_tokens_from_ctx(ctx)
            logger.info(f"current context tokens: {est} after summarization")


    def _find_tail_start_step_id(self, ctx: RuntimeContext) -> Optional[str]:
        if not ctx.step_order:
            return None
        total = sum(ctx.step_tokens_raw.get(sid, 0) for sid in ctx.step_order)
        if total <= 0:
            return ctx.step_order[0]

        target_tail = total * float(self.config.keep_tail_ratio)
        acc = 0
        for sid in reversed(ctx.step_order):
            acc += ctx.step_tokens_raw.get(sid, 0)
            if acc >= target_tail:
                return sid
        return ctx.step_order[0]

    async def _compact_keep_tail(self, ctx: RuntimeContext) -> bool:
        """
        Deterministic local summary compaction:
        - Keep system message
        - Replace older messages with one summary message
        - Keep tail (latest keep_tail_ratio by step token sum)
        """
        tail_start_sid = self._find_tail_start_step_id(ctx)
        if not tail_start_sid:
            return False

        cut_idx = ctx.step_span_map[tail_start_sid][0]
        # messages[0] is system; we also may have many turn user messages before steps.
        # We allow compressing everything after system, before cut_idx.
        if cut_idx <= 1:
            return False

        head_msgs = ctx.messages[1:cut_idx]
        tail_msgs = ctx.messages[cut_idx:]
        tail_tokens = ctx.msg_tokens_raw[cut_idx:]

        summary_obj = self._build_local_summary(ctx, head_msgs, tail_start_sid)
        summary_msg = {"role": self.config.summary_role,
                       "content": "CONTEXT_SUMMARY_JSON:\n" + _stable_json(summary_obj)}
        summary_tok = self.estimator.estimate_message_tokens_raw(summary_msg)

        # rebuild caches
        sys_msg = ctx.messages[0]
        sys_tok = ctx.msg_tokens_raw[0]
        # Replace
        ctx.messages = [sys_msg, summary_msg] + tail_msgs
        ctx.msg_tokens_raw = [sys_tok, summary_tok] + tail_tokens

        # rebuild sum in O(len(tail)) (we already have tail_tokens list)
        ctx.msg_tokens_raw_sum = int(sys_tok + summary_tok + sum(tail_tokens))

        ctx.summary_versions.append(
            {"ts": time.time(), "kept_tail_start_step": tail_start_sid, "cut_idx": cut_idx,
             "summary_tokens_raw": summary_tok}
        )
        # Rebuild step maps for remaining steps (tail only)
        self._rebuild_step_maps_after_compaction(ctx, tail_start_sid, old_cut_idx=cut_idx)
        return True

    async def _summary_keep_tail(self, ctx: "RuntimeContext") -> bool:
        tail_start_sid = self._find_tail_start_step_id(ctx)
        if not tail_start_sid:
            return False

        cut_idx = ctx.step_span_map[tail_start_sid][0]
        if cut_idx <= 1:
            return False

        sys_msg = ctx.messages[0]
        sys_tok = ctx.msg_tokens_raw[0]

        head_msgs = ctx.messages[1:cut_idx]
        tail_msgs = ctx.messages[cut_idx:]
        tail_tokens = ctx.msg_tokens_raw[cut_idx:]

        summarizer_sys = {
            "role": "system",
            "content": (
                "You are a context compressor for an LLM agent.\n"
                "Summarize ONLY the provided HEAD messages into a compact, loss-minimizing memory.\n"
                "Do NOT mention the TAIL.\n"
                "Preserve: user constraints/goals, decisions, plans, unresolved questions, important entities,\n"
                "tool usage (tool name + purpose + key args + key results/errors).\n"
                "Remove: greetings, repetition, long logs, raw DOM/snapshots, boilerplate.\n"
                "Output must match the JSON schema.\n"
                "Keep it as short as possible while remaining useful.\n"
            ),
        }
        user_msg = {"role": "user", "content": "Extract summaries for:\n" + json.dumps(head_msgs,ensure_ascii=False)}
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[summarizer_sys, user_msg],
            response_format=summary_llm_schema,
            extra_body={"enable_thinking": False},
            max_tokens=self.config.max_summary_tokens
        )
        if response is None:
            raise Exception("OpenAI response is empty.")
        content = response.choices[0].message.content
        try:
            parsed = extract_json(content)
        except Exception as e:
            logger.error(f"parse llm raw content error: {e}, content: {content}")
            raise e


        summary_obj = {
            "type": "llm_summary_v1",
            "ts": time.time(),
            "tail_start_sid": tail_start_sid,
            "cut_idx": cut_idx,
            "summary": parsed
        }
        summary_msg = {
            "role": self.config.summary_role,
            "content": "CONTEXT_SUMMARY_JSON:\n" + _stable_json(summary_obj),
        }
        summary_tok = self.estimator.estimate_message_tokens_raw(summary_msg)

        # 写回 ctx：这里仍然保留原 sys_msg
        ctx.messages = [sys_msg, summary_msg] + tail_msgs
        ctx.msg_tokens_raw = [sys_tok, summary_tok] + tail_tokens
        ctx.msg_tokens_raw_sum = int(sys_tok + summary_tok + sum(tail_tokens))

        ctx.summary_versions.append({
            "ts": time.time(),
            "kept_tail_start_step": tail_start_sid,
            "cut_idx": cut_idx,
            "summary_tokens_raw": summary_tok,
            "mode": "llm_summary_keep_tail",
        })

        self._rebuild_step_maps_after_compaction(ctx, tail_start_sid, old_cut_idx=cut_idx)
        return True




    def _rebuild_step_maps_after_compaction(self, ctx: RuntimeContext, tail_start_sid: str, old_cut_idx: int) -> None:
        """
        After compaction:
            new_messages = [system, summary] + old_messages[old_cut_idx:]

        Need to keep only steps from tail_start_sid onward and remap indices.
        """
        if tail_start_sid not in ctx.step_order:
            # fallback: wipe step tracking
            ctx.step_order = []
            ctx.step_span_map = {}
            ctx.step_tokens_raw = {}
            return

        start_pos = ctx.step_order.index(tail_start_sid)
        remaining = ctx.step_order[start_pos:]

        # remap function
        # new_idx = (old_idx - old_cut_idx) + 2   (2 = system + summary)
        def remap(old_i: int) -> int:
            # after compaction: [system, summary, ...tail...]
            return (old_i - old_cut_idx) + 2  # [system, summary]

        new_span: Dict[str, Tuple[int, int]] = {}
        new_tokens: Dict[str, int] = {}
        for sid in remaining:
            old_s, old_e = ctx.step_span_map[sid]
            ns, ne = remap(old_s), remap(old_e)
            new_span[sid] = (ns, ne)
            new_tokens[sid] = sum(ctx.msg_tokens_raw[ns:ne])

        ctx.step_order = remaining
        ctx.step_span_map = new_span
        ctx.step_tokens_raw = new_tokens
        # Also reset turn dedupe set, because old user messages may have been summarized away.
        # We WANT future build_messages to NOT re-append past turn user_input again.
        # So keep the dedupe set intact. But the head user messages are gone—still OK.
        # No action needed because dedupe lives in ctx._turn_seen.

    # ---------- Result Card extraction ----------
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
        loaded = await self._load_ctx(trace_id)
        if not loaded:
            return
        ctx = self._coerce_ctx(loaded)
        # 最后一步finish时，结果并没有append到窗口里，所以在这里做一次补充
        if step_ids:
            for sid in step_ids:
                if sid and sid not in ctx.seen_step_ids:
                    ctx.seen_step_ids.append(sid)

        # 1) 找到该 turn 对应的“消息区间”
        # 最小方案：通过 step_span_map 找到该 turn 的最后一个 step span，
        # 再向前回溯找到 turn 的 user_input message（content == user_input 且 role=user 的最后一次出现）
        # 注意：这是启发式；更稳的做法是给 message 写 turn_id 元信息，但那就不是最小补丁了。
        end_idx = None
        if ctx.step_order:
            # 假设 turn 完成时，它的 step 都已经 append 进来了，最后一个 step 就是当前 turn 的尾部
            last_sid = ctx.step_order[-1]
            end_idx = ctx.step_span_map[last_sid][1]

        if end_idx is None:
            return

        # 2) 找起点：从尾部往前找最近的 user_input（role=user 且 content==user_input）
        start_idx = None
        for i in range(end_idx - 1, 0, -1):
            m = ctx.messages[i]
            if m.get("role") == "user" and str(m.get("content", "")) == str(user_input or ""):
                start_idx = i
                break

        if start_idx is None:
            # 找不到就退化：仅把最后一个 step 的 span 替换成卡片（保守）
            last_sid = ctx.step_order[-1]
            start_idx = ctx.step_span_map[last_sid][0]

        # 3) 构造卡片消息
        card_msg = self._build_turn_result_card_message(
            turn_id=turn_id,
            user_input=user_input,
            final_answer=final_answer,
            tool_state=tool_state,
            role="system",
        )
        card_tok = self.estimator.estimate_message_tokens_raw(card_msg)

        # 4) 替换 messages[start_idx:end_idx] -> [card_msg]
        old_tokens = sum(ctx.msg_tokens_raw[start_idx:end_idx])
        ctx.messages = ctx.messages[:start_idx] + [card_msg] + ctx.messages[end_idx:]
        ctx.msg_tokens_raw = ctx.msg_tokens_raw[:start_idx] + [card_tok] + ctx.msg_tokens_raw[end_idx:]
        ctx.msg_tokens_raw_sum = int(ctx.msg_tokens_raw_sum - old_tokens + card_tok)

        # 5) step bookkeeping：最小方案是清掉（因为我们破坏了 step span 的连续性）
        # 这样后续 build_messages 不会再依赖旧 span（会继续 append 新 step）
        ctx.step_order = []
        ctx.step_span_map = {}
        ctx.step_tokens_raw = {}
        ctx.last_applied_step_id = None

        # turn 去重保持不变（避免未来重放 user_input）
        ctx.updated_at = time.time()
        await self._save_ctx(ctx)

    def _build_turn_result_card_message(
            self,
            *,
            turn_id: str,
            user_input: Optional[str],
            final_answer: Optional[str],
            tool_state: Optional[List[str]] = None,
            extra: Optional[Dict[str, Any]] = None,
            role: str = "assistant",
    ) -> Dict[str, Any]:
        """
        生成一条“Result Card”消息：
        - 用固定前缀标识，确保压缩时能被 _try_extract_result_card() 捕获并保留
        - payload 是 JSON（卡片结构你可扩展）
        """
        card = {
            "type": "turn_result_card",
            "turn_id": turn_id,
            "user_input": (user_input or "")[:600],  # 适当截断，避免卡片膨胀
            "final_answer": (final_answer or "")[:2000],  # 适当截断
            "tool_state": tool_state or [],
            "extra": extra or {},
            "ts": time.time(),
        }
        payload = _stable_json(card)
        if len(payload) > self.config.max_result_card_chars:
            payload = payload[:self.config.max_result_card_chars] + "…"

        return {"role": role, "content": f"{self.config.result_card_prefix}\n{payload}"}

    def _try_extract_result_card(self, content: str) -> Optional[Dict[str, Any]]:
        """
        Recognize and preserve a Turn Result Card embedded in normal message content.

        Expected formats:
            "TURN_RESULT_CARD_JSON:\n{...json...}"
            "TURN_RESULT_CARD_JSON:{...json...}"  (also tolerated)

        Returns a compact structure safe for summary insertion.
        """
        if not content:
            return None

        prefix = self.config.result_card_prefix
        s = content.lstrip()
        if not s.startswith(prefix):
            return None

        payload = s[len(prefix):].lstrip()
        if payload.startswith("\n"):
            payload = payload.lstrip("\n")

        # Hard cap for safety (avoid blowing summary)
        if len(payload) > self.config.max_result_card_chars:
            payload = payload[: self.config.max_result_card_chars] + "…"

        # Try parse JSON, fallback to preview
        try:
            obj = json.loads(payload)
            return {"type": "turn_result_card", "card": obj}
        except Exception:
            return {"type": "turn_result_card", "preview": payload}

    # ---------- deterministic local summary ----------
    def _build_local_summary(self, ctx: RuntimeContext, head_msgs: List[Dict[str, Any]], kept_tail_start_step: str) -> \
            Dict[str, Any]:
        """
        Deterministic, local extraction-based summary.
        Keeps: user goals, tool calls (name + args short), tool results highlights (snapshot query top_hits trimmed),
        and turn result cards
        """
        user_goals: List[str] = []
        progress: List[Dict[str, Any]] = []
        key_facts: List[str] = []
        tool_state: Dict[str, Any] = {}

        # keep turn result cards even if they fall into head and get compacted
        turn_cards: List[Dict[str, Any]] = []
        seen_card_hash: set[str] = set()

        last_snapshot_id = None
        last_snapshot_path = None
        last_page_url = None

        def add_fact(s: str) -> None:
            if s and s not in key_facts:
                key_facts.append(s)

        for m in head_msgs:
            role = m.get("role")
            content = str(m.get("content", "") or "")

            # 优先识别 Result Card
            if role == "system":
                card = self._try_extract_result_card(content)
                # TODO 还有一种情况是LLM提取摘要的前缀也需要处理
                if card:
                    # de-dup
                    h = _sha1(_stable_json(card))
                    if h not in seen_card_hash:
                        seen_card_hash.add(h)
                        turn_cards.append(card)
                    # Result Card 不再继续走其他分支（避免重复进 user_goals/progress）
                    continue

            if role == "user":
                c = str(m.get("content", "")).strip()
                if c:
                    user_goals.append(c[: self.config.max_user_goal_chars])  # 截断用户请求描述


            elif role == "assistant" and m.get("tool_calls"):
                for tc in m["tool_calls"]:
                    fn = (tc or {}).get("function") or {}
                    name = fn.get("name")
                    args = fn.get("arguments", "")
                    if isinstance(args, str) and len(args) > self.config.max_tool_args_chars:
                        args = args[: self.config.max_tool_args_chars] + "…"  # 截断tool_call的参数
                    progress.append({"type": "tool_call", "tool": name, "args": args})


            elif role == "tool":
                extracted = self._extract_tool_result_highlights(str(m.get("content", "")))
                if extracted:
                    progress.append({"type": "tool_result", **extracted})
                    if extracted.get("snapshot_id"):
                        last_snapshot_id = extracted["snapshot_id"]
                    if extracted.get("latest_path"):
                        last_snapshot_path = extracted["latest_path"]
                    if extracted.get("page_url"):
                        last_page_url = extracted["page_url"]

        if last_page_url:
            tool_state["last_page_url"] = last_page_url
        if last_snapshot_path:
            tool_state["last_snapshot_path"] = last_snapshot_path
            add_fact(f"snapshot_path={last_snapshot_path}")
        if last_snapshot_id:
            tool_state["last_snapshot_id"] = last_snapshot_id
            add_fact(f"snapshot_id={last_snapshot_id}")

        user_goals = user_goals[-10:]  # 汇总用户的主要需求，截断只取最新的十个需求
        progress = progress[:160]  # 汇总工具执行结果，取前160项结果
        key_facts = key_facts[:40]  # 汇总外部文件路径，文件id等信息，取前40条关键信息，
        turn_cards = turn_cards[-self.config.max_turn_cards:]  # 截断结果卡片的信息

        return {
            "type": "compressed_history",
            "trace_id": ctx.trace_id,
            "kept_tail_start_step": kept_tail_start_step,
            "user_goals": user_goals,
            "progress": progress,
            "turn_cards": turn_cards,
            "key_facts": key_facts,
            "tool_state": tool_state,
            "calibration_multiplier": ctx.calibration_multiplier,
        }

    def _extract_tool_result_highlights(self, content: str) -> Optional[Dict[str, Any]]:
        """
        Parse common tool result JSON and extract compact highlights.
        - snapshot_ref: keep path
        - snapshot_query_result: keep snapshot_id/latest_path/meta + trimmed top_hits
        - else: keep preview truncated
        """
        if not content:
            return None
        s = content.strip()

        # Attempt JSON parse if it's a JSON object
        if s.startswith("{") and s.endswith("}"):
            try:
                obj = json.loads(s)
            except Exception:
                obj = None

            if isinstance(obj, dict):
                if (
                    obj.get("type") == "observation_card_v1"
                    or {"outcome", "summary", "facts", "refs"}.issubset(obj.keys())
                ):
                    out: Dict[str, Any] = {
                        "tool_type": "observation_card_v1",
                        "outcome": obj.get("outcome"),
                        "summary": obj.get("summary"),
                    }
                    if obj.get("tool_name"):
                        out["tool_name"] = obj.get("tool_name")
                    facts = obj.get("facts") if isinstance(obj.get("facts"), dict) else {}
                    if "exit_code" in facts:
                        out["exit_code"] = facts.get("exit_code")
                    if "timed_out" in facts:
                        out["timed_out"] = bool(facts.get("timed_out"))
                    if "engine_used" in facts:
                        out["engine_used"] = facts.get("engine_used")
                    if "fallback_from" in facts:
                        out["fallback_from"] = facts.get("fallback_from")

                    refs = obj.get("refs") if isinstance(obj.get("refs"), dict) else {}
                    artifacts = refs.get("artifacts") if isinstance(refs.get("artifacts"), list) else []
                    paths: List[str] = []
                    for artifact in artifacts:
                        if not isinstance(artifact, dict):
                            continue
                        path = artifact.get("path")
                        if isinstance(path, str) and path.strip():
                            paths.append(path.strip())
                    if paths:
                        out["artifact_paths"] = paths[:3]
                        out["latest_path"] = paths[-1]
                    return out

                if {"ok", "data", "artifacts", "error"}.issubset(obj.keys()):
                    out: Dict[str, Any] = {
                        "tool_type": "tool_envelope",
                        "ok": bool(obj.get("ok")),
                    }

                    data = obj.get("data")
                    if isinstance(data, dict):
                        if "exit_code" in data:
                            out["exit_code"] = data.get("exit_code")
                        if "engine_used" in data:
                            out["engine_used"] = data.get("engine_used")
                        if "fallback_from" in data:
                            out["fallback_from"] = data.get("fallback_from")
                        if data.get("snapshot_id"):
                            out["snapshot_id"] = data.get("snapshot_id")
                        if data.get("latest_path"):
                            out["latest_path"] = data.get("latest_path")

                    if obj.get("error") is not None:
                        err_obj = obj.get("error")
                        if isinstance(err_obj, dict):
                            err = json.dumps(err_obj, ensure_ascii=False)
                        else:
                            err = str(err_obj)
                        if len(err) > self.config.max_tool_result_chars:
                            err = err[: self.config.max_tool_result_chars] + "…"
                        out["error"] = err

                    artifacts = obj.get("artifacts") or []
                    if isinstance(artifacts, list):
                        paths: List[str] = []
                        for artifact in artifacts:
                            if not isinstance(artifact, dict):
                                continue
                            path = artifact.get("path")
                            if isinstance(path, str) and path.strip():
                                paths.append(path.strip())
                        if paths:
                            out["artifact_paths"] = paths[:3]
                            if "latest_path" not in out:
                                out["latest_path"] = paths[-1]
                    return out

                t = obj.get("type")
                if t == "snapshot_ref":
                    return {"tool_type": "snapshot_ref", "latest_path": obj.get("path")}

                if t == "snapshot_query_result":
                    out: Dict[str, Any] = {"tool_type": "snapshot_query_result"}
                    if obj.get("snapshot_id"):
                        out["snapshot_id"] = obj["snapshot_id"]
                    if obj.get("latest_path"):
                        out["latest_path"] = obj["latest_path"]

                    meta = obj.get("meta") or {}
                    if isinstance(meta, dict):
                        out["meta"] = {k: meta.get(k) for k in ("snapshot_lines", "search_scope", "marker_index") if
                                       k in meta}

                    hits: List[str] = []
                    result = obj.get("result") or {}
                    if isinstance(result, dict):
                        items = result.get("items") or []
                        if isinstance(items, list):
                            for it in items:
                                r = (it or {}).get("result") or {}
                                th = r.get("top_hits") or []
                                if isinstance(th, list):
                                    for line in th:
                                        if isinstance(line, str) and line.strip():
                                            hits.append(line.strip())
                                            if len(hits) >= self.config.max_top_hits_lines:  # 截断
                                                break
                                if len(hits) >= self.config.max_top_hits_lines:  # 截断
                                    break
                    if hits:
                        out["top_hits"] = hits
                    return out
                # Generic json response
                preview = s
                if len(preview) > self.config.max_tool_result_chars:
                    preview = preview[: self.config.max_tool_result_chars] + "…"
                return {"tool_type": t or "tool_json", "preview": preview}
        # Non-JSON tool output: keep trimmed preview
        preview2 = s
        if len(preview2) > self.config.max_tool_result_chars:  # 截断工具执行结果
            preview2 = preview2[: self.config.max_tool_result_chars] + "…"
        return {"tool_type": "tool_text", "preview": preview2}


class InMemoryCacheManager(CacheManager):
    def __init__(
            self,
            model: str, api_key: str, base_url: str,
            build_system_prompt_fn: Callable[[], str],
            config: Optional[CacheManagerConfig] = None,
            estimator: Optional[TokenEstimator] = None,
    ) -> None:
        super().__init__(model=model, api_key=api_key, base_url=base_url,build_system_prompt_fn=build_system_prompt_fn, config=config, estimator=estimator)
        self._store: Dict[str, RuntimeContext] = {}

    async def _load_ctx(self, trace_id: str) -> Optional[CtxType]:
        return self._store.get(trace_id)

    async def _save_ctx(self, ctx: RuntimeContext) -> None:
        self._store[ctx.trace_id] = ctx

    async def _delete_ctx(self, trace_id: str) -> None:
        self._store.pop(trace_id, None)

# class RedisCacheManager(CacheManager):
#     def __init__(self, build_system_prompt_fn, redis_client, key_prefix="ctx:", **kwargs):
#         super().__init__(build_system_prompt_fn, **kwargs)
#         self.redis = redis_client
#         self.key_prefix = key_prefix
#
#     def _key(self, trace_id: str) -> str:
#         return f"{self.key_prefix}{trace_id}"
#
#     async def _load_ctx(self, trace_id: str) -> Optional[RuntimeContext]:
#         raw = await self.redis.get(self._key(trace_id))
#         if not raw:
#             return None
#         d = json.loads(raw)
#         return RuntimeContext.from_dict(d)
#
#     async def _save_ctx(self, ctx: RuntimeContext) -> None:
#         await self.redis.set(self._key(ctx.trace_id), json.dumps(ctx.to_dict(), ensure_ascii=False))
#
#     async def _delete_ctx(self, trace_id: str) -> None:
#         await self.redis.delete(self._key(trace_id))
