import asyncio
import re
import logging
import json
from typing import Dict, Any, List, Optional, cast
from openai import AsyncOpenAI
from .tools.tool import ToolRegistry

from .builder.build import build_system_prompt
from ws.connection_manager import ConnectionManager
from .protocol import Action,ActionType,Step
from utils.id_util import get_sonyflake

logger = logging.getLogger(__name__)

THINK_PATTERN = re.compile(r"<think>(.*?)</think>", re.DOTALL)
LLM_LOG_PREVIEW_CHARS = 120
SUMMARY_MAX_RETRIES = 3
CONTEXT_OVERFLOW_MARKERS = (
    "context length",
    "maximum context length",
    "max context length",
    "context window",
    "prompt is too long",
    "input is too long",
    "too many tokens",
    "token limit",
    "requested tokens",
    "reduce the length",
)
NON_RETRYABLE_QUOTA_MARKERS = (
    "insufficient_quota",
    "quota",
    "usage limit",
    "billing",
    "credit",
)
NON_RETRYABLE_AUTH_MARKERS = (
    "invalid api key",
    "authentication",
    "unauthorized",
    "forbidden",
    "permission",
)
RETRYABLE_ERROR_MARKERS = (
    "connection reset",
    "connection aborted",
    "temporarily unavailable",
    "temporarily overloaded",
    "server overloaded",
    "upstream error",
    "timed out",
    "timeout",
    "try again",
    "rate limit",
)


def _parse_json_dict(s: str) -> Optional[dict]:
    try:
        obj = json.loads(s)
    except Exception:
        return None
    return obj if isinstance(obj, dict) else None


def _repair_json_structure(s: str) -> Optional[str]:
    text = (s or "").strip()
    if not text.startswith("{"):
        return None

    in_str = False
    escape = False
    stack: list[str] = []
    out: list[str] = []

    for ch in text:
        if in_str:
            out.append(ch)
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue

        if ch == '"':
            in_str = True
            out.append(ch)
            continue
        if ch in "{[":
            stack.append(ch)
            out.append(ch)
            continue
        if ch in "}]":
            expected = "{" if ch == "}" else "["
            # Recover cases like missing "]" before a trailing "}".
            while stack and stack[-1] != expected:
                missing_open = stack.pop()
                out.append("}" if missing_open == "{" else "]")
            if stack and stack[-1] == expected:
                stack.pop()
                out.append(ch)
            # Ignore unmatched closing bracket.
            continue
        out.append(ch)

    if in_str:
        return None
    while stack:
        missing_open = stack.pop()
        out.append("}" if missing_open == "{" else "]")
    repaired = "".join(out).strip()
    return repaired if repaired.startswith("{") else None

def normalize_tool_calls(tool_calls: Any) -> List[Dict[str, Any]]:
    if not tool_calls:
        return []
    out = []
    for tc in tool_calls:
        if hasattr(tc, "model_dump"):
            d = tc.model_dump()
        elif hasattr(tc, "dict"):
            d = tc.dict()
        elif isinstance(tc, dict):
            d = tc
        else:
            d = vars(tc)
        out.append(d)
    return out

def safe_parse_tool_args(arg_str: str) -> dict:
    s = (arg_str or "").strip()
    if not s:
        return {}

    direct = _parse_json_dict(s)
    if direct is not None:
        return direct

    balanced = _extract_first_balanced_json_object(s)
    if balanced:
        parsed = _parse_json_dict(balanced)
        if parsed is not None:
            return parsed

    repaired = _repair_json_structure(s)
    if repaired and repaired != s:
        parsed = _parse_json_dict(repaired)
        if parsed is not None:
            logger.warning("Recovered malformed tool arguments JSON by structural repair: %r", s[:200])
            return parsed

    logger.warning("Invalid tool arguments JSON: %r", s[:200])
    return {}

def _extract_first_balanced_json_object(text: str) -> Optional[str]:
    """
    从 text 中抽取第一个“完整配对”的 JSON 对象：{ ... }。
    关键点：忽略字符串中的花括号，并处理转义字符。
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

def _coerce_natural_finish(content: str, fallback: str = "") -> str:
    message = (content or "").strip()
    if not message:
        message = (fallback or "").strip()
    if not message:
        message = "No actionable result returned."
    # Keep context history in plain text to avoid re-conditioning the model into
    # JSON control-style replies on subsequent turns.
    return message


def _clip_for_log(text: str, *, limit: int = LLM_LOG_PREVIEW_CHARS) -> str:
    value = str(text or "").strip()
    if len(value) <= limit:
        return value
    return value[:limit] + "..."


def _extract_status_code(exc: Exception) -> int | None:
    value = getattr(exc, "status_code", None)
    if isinstance(value, int):
        return value
    response = getattr(exc, "response", None)
    if response is None:
        return None
    value = getattr(response, "status_code", None)
    return value if isinstance(value, int) else None


def _extract_error_code(exc: Exception) -> str | None:
    for source in (exc, getattr(exc, "body", None)):
        if isinstance(source, dict):
            error = source.get("error") if isinstance(source.get("error"), dict) else source
            code = error.get("code")
            if code:
                return str(code).strip().lower()
    code = getattr(exc, "code", None)
    if code:
        return str(code).strip().lower()
    return None


def _extract_error_text(exc: Exception) -> str:
    parts: list[str] = [str(exc)]
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        parts.append(json.dumps(body, ensure_ascii=False))
    response = getattr(exc, "response", None)
    data = getattr(response, "text", None)
    if isinstance(data, str) and data:
        parts.append(data)
    return " ".join(part for part in parts if part).strip().lower()


def _get_retry_after_seconds(exc: Exception) -> float | None:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if not headers:
        return None
    raw = None
    if isinstance(headers, dict):
        raw = headers.get("retry-after") or headers.get("Retry-After")
    else:
        raw = getattr(headers, "get", lambda *_: None)("retry-after") or getattr(headers, "get", lambda *_: None)("Retry-After")
    if raw is None:
        return None
    try:
        seconds = float(raw)
    except (TypeError, ValueError):
        return None
    return seconds if seconds >= 0 else None


def _compute_retry_delay(exc: Exception, attempt: int) -> float:
    retry_after = _get_retry_after_seconds(exc)
    if retry_after is not None:
        return retry_after
    return min(8.0, float(2 ** max(attempt - 1, 0)))


def is_context_overflow_error(exc: Exception) -> bool:
    code = _extract_error_code(exc)
    if code in {"context_length_exceeded", "context_length_error"}:
        return True
    status_code = _extract_status_code(exc)
    text = _extract_error_text(exc)
    if status_code in {400, 413, 422} and any(marker in text for marker in CONTEXT_OVERFLOW_MARKERS):
        return True
    return any(marker in text for marker in CONTEXT_OVERFLOW_MARKERS)


def is_retryable_provider_error(exc: Exception) -> bool:
    if is_context_overflow_error(exc):
        return False

    error_type = type(exc).__name__.lower()
    status_code = _extract_status_code(exc)
    code = _extract_error_code(exc)
    text = _extract_error_text(exc)

    if code and any(marker in code for marker in NON_RETRYABLE_QUOTA_MARKERS):
        return False
    if any(marker in text for marker in NON_RETRYABLE_QUOTA_MARKERS):
        return False
    if status_code in {401, 403}:
        return False
    if any(marker in text for marker in NON_RETRYABLE_AUTH_MARKERS):
        return False
    if status_code in {408, 409, 429}:
        return True
    if isinstance(status_code, int) and status_code >= 500:
        return True
    if error_type in {"apiconnectionerror", "apitimeouterror", "timeouterror", "connectionerror"}:
        return True
    return any(marker in text for marker in RETRYABLE_ERROR_MARKERS)


def _extract_token_info(response: Any) -> dict:
    usage = getattr(response, "usage", None)
    if usage is None:
        return {"cache_tokens": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    cached_tokens = 0
    details = getattr(usage, "prompt_tokens_details", None)
    if details and getattr(details, "cached_tokens", None):
        cached_tokens = int(details.cached_tokens)
    return {
        "cache_tokens": cached_tokens,
        "prompt_tokens": int(getattr(usage, "prompt_tokens", 0) or 0),
        "completion_tokens": int(getattr(usage, "completion_tokens", 0) or 0),
        "total_tokens": int(getattr(usage, "total_tokens", 0) or 0),
    }



class Provider:
    tool_registry: ToolRegistry
    system_prompt: str
    async_client: AsyncOpenAI
    ws_manager: ConnectionManager
    model: str

    def __init__(self, model:str, api_key: str, base_url: str, tool_registry: ToolRegistry, ws_manager: ConnectionManager, context_config: dict | None = None):
        self.model = model
        self.async_client = AsyncOpenAI(
            base_url=base_url,
            api_key=api_key
        )
        self.tool_registry = tool_registry
        self.system_prompt = build_system_prompt()
        self.ws_manager = ws_manager

    async def _create_chat_completion(
        self,
        *,
        messages: List[Dict[str, Any]],
        tools_enabled: bool,
        is_thinking: bool,
    ) -> Any:
        request: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.2,
            "top_p": 0.9,
            "extra_body": {"enable_thinking": is_thinking},
        }
        if tools_enabled:
            request["tools"] = self.tool_registry.get_all_schemas()
            request["parallel_tool_calls"] = True
        return await self.async_client.chat.completions.create(**request)

    @staticmethod
    def _extract_summary_text(response: Any) -> str:
        if response is None:
            raise Exception("OpenAI response is empty.")
        message = response.choices[0].message
        content = getattr(message, "content", None) or ""
        fallback_text = getattr(message, "refusal", "") or ""
        return _coerce_natural_finish(content, fallback=fallback_text)

    async def generate(self, context: Dict[str, Any]) -> tuple[str, str, list, dict]:
        step = cast(Step,context.get("step")) # current_step
        is_thinking = context.get("is_thinking", True)
        response = await self._create_chat_completion(
            messages=context.get("messages"),
            tools_enabled=True,
            is_thinking=is_thinking,
        )
        if response is None:
            raise Exception("OpenAI response is empty.")
        raw = response.choices[0].message.content
        content = raw or ""
        think_match = THINK_PATTERN.search(content)
        reasoning = (think_match.group(1).strip() if think_match else response.choices[0].message.reasoning_content) if is_thinking else ""
        content = THINK_PATTERN.sub("", content).strip()
        actions = []
        finish_reason = response.choices[0].finish_reason
        if finish_reason == "tool_calls":
            calls = response.choices[0].message.tool_calls
            step.tool_calls = normalize_tool_calls(response.choices[0].message.tool_calls)
            for call in calls:
                tool = self.tool_registry.get(call.function.name)
                action = Action(action_id=call.id,
                                type=ActionType.TOOL,
                                tool_name=call.function.name,
                                args=safe_parse_tool_args(call.function.arguments),
                                requires_confirm=bool(getattr(tool, "requires_confirmation", False)),
                                confirm_status="pending" if bool(getattr(tool, "requires_confirmation", False)) else None)
                actions.append(action)
        else:
            fallback_text = getattr(response.choices[0].message, "refusal", "") or ""
            action_message = _coerce_natural_finish(content, fallback=fallback_text)
            actions.append(
                Action(
                    action_id=get_sonyflake("action_"),
                    type=ActionType.FINISH,
                    message=action_message,
                    full_ref=action_message,
                )
            )
            logger.info(
                "llm_finish finish_reason=%s content_chars=%s preview=%r",
                finish_reason,
                len(action_message),
                _clip_for_log(action_message),
            )

        if not actions:
            action_message = _coerce_natural_finish(content)
            actions.append(
                Action(
                    action_id=get_sonyflake("action_"),
                    type=ActionType.FINISH,
                    message=action_message,
                    full_ref=action_message,
                )
            )
            logger.warning(
                "llm_no_actions_fallback finish_reason=%s content_chars=%s",
                finish_reason,
                len(action_message),
            )

        token_info = _extract_token_info(response)
        logger.info(
            "llm_usage finish_reason=%s actions=%s prompt=%s completion=%s total=%s cache=%s",
            finish_reason,
            len(actions),
            token_info["prompt_tokens"],
            token_info["completion_tokens"],
            token_info["total_tokens"],
            token_info["cache_tokens"],
        )
        return finish_reason, reasoning, actions, token_info

    async def generate_summary(
        self,
        *,
        messages: list[dict],
        system_prompt: str,
        max_retries: int = SUMMARY_MAX_RETRIES,
    ) -> tuple[str, dict]:
        request_messages: list[dict] = [{"role": "system", "content": system_prompt}, *(messages or [])]
        attempt = 0
        while True:
            attempt += 1
            try:
                response = await self._create_chat_completion(
                    messages=request_messages,
                    tools_enabled=False,
                    is_thinking=False,
                )
                summary_text = self._extract_summary_text(response)
                token_info = _extract_token_info(response)
                logger.info(
                    "llm_summary finish_reason=%s prompt=%s completion=%s total=%s cache=%s",
                    getattr(response.choices[0], "finish_reason", None),
                    token_info["prompt_tokens"],
                    token_info["completion_tokens"],
                    token_info["total_tokens"],
                    token_info["cache_tokens"],
                )
                return summary_text, token_info
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if is_context_overflow_error(exc):
                    raise
                if attempt > max_retries or not is_retryable_provider_error(exc):
                    raise
                delay = _compute_retry_delay(exc, attempt)
                logger.warning(
                    "llm_summary_retry attempt=%s max_retries=%s delay=%.2fs error=%s",
                    attempt,
                    max_retries,
                    delay,
                    exc,
                )
                await asyncio.sleep(delay)

