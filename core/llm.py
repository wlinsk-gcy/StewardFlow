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

    async def generate(self, context: Dict[str, Any]) -> tuple[str, str, list, dict]:
        step = cast(Step,context.get("step")) # current_step
        is_thinking = context.get("is_thinking", True)
        # TODO 针对 429 Error Code 做重试
        response = await self.async_client.chat.completions.create(
            model=self.model,
            messages=context.get("messages"),
            temperature=0.2,
            top_p=0.9,
            tools=self.tool_registry.get_all_schemas(),
            extra_body={"enable_thinking": is_thinking},
            parallel_tool_calls=True,
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

        cached_tokens = 0
        if response.usage.prompt_tokens_details and response.usage.prompt_tokens_details.cached_tokens:
            cached_tokens = int(response.usage.prompt_tokens_details.cached_tokens)
        logger.info(
            "llm_usage finish_reason=%s actions=%s prompt=%s completion=%s total=%s cache=%s",
            finish_reason,
            len(actions),
            response.usage.prompt_tokens,
            response.usage.completion_tokens,
            response.usage.total_tokens,
            cached_tokens,
        )
        token_info = {
            "cache_tokens": cached_tokens,
            "prompt_tokens": response.usage.prompt_tokens,
            "completion_tokens": response.usage.completion_tokens,
            "total_tokens": response.usage.total_tokens
        }
        return finish_reason, reasoning, actions, token_info

