import re
import logging
import json
from typing import Dict, Any, List, Optional, cast
from openai import OpenAI,AsyncOpenAI
from .tools.tool import ToolRegistry

from .builder.build import build_system_prompt
from ws.connection_manager import ConnectionManager
from .protocol import Action,ActionType,Step
from utils.id_util import get_sonyflake

logger = logging.getLogger(__name__)

THINK_PATTERN = re.compile(r"<think>(.*?)</think>", re.DOTALL)
ACTION_TYPE_ALIASES = {
    "done": "finish",
    "final": "finish",
    "completed": "finish",
    "complete": "finish",
    "confirm": "request_confirm",
}


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


def _coerce_content_action(content: str) -> tuple[ActionType, str, str]:
    extracted = extract_json(content)
    try:
        parsed = json.loads(extracted)
    except Exception as e:
        logger.error(f"parse llm raw content error: {e}")
        # 兜底：让用户补充输入，避免抛 KeyError/ValueError 打断流程
        msg = extracted or content or ""
        raw_ref = json.dumps({"type": "request_input", "message": msg}, ensure_ascii=False)
        return ActionType.REQUEST_INPUT, msg, raw_ref

    if not isinstance(parsed, dict):
        # 非对象输出统一收敛为 finish，message 为文本化结果
        msg = parsed if isinstance(parsed, str) else json.dumps(parsed, ensure_ascii=False)
        raw_ref = json.dumps({"type": "finish", "message": msg}, ensure_ascii=False)
        return ActionType.FINISH, msg, raw_ref

    raw_ref = json.dumps(parsed, ensure_ascii=False)

    action_type_raw = parsed.get("type")
    normalized_type = None
    if isinstance(action_type_raw, str):
        candidate = action_type_raw.strip().lower()
        candidate = ACTION_TYPE_ALIASES.get(candidate, candidate)
        if candidate in {ActionType.FINISH.value, ActionType.REQUEST_INPUT.value, ActionType.REQUEST_CONFIRM.value}:
            normalized_type = candidate

    message_raw = parsed.get("message")
    if isinstance(message_raw, str):
        message = message_raw.strip()
    elif message_raw is None:
        message = ""
    else:
        message = json.dumps(message_raw, ensure_ascii=False)

    # 没有 type/message 时，默认视为任务结果并 finish
    if not normalized_type:
        normalized_type = ActionType.FINISH.value
    if not message:
        message = json.dumps(parsed, ensure_ascii=False)

    return ActionType(normalized_type), message, raw_ref



class Provider:
    tool_registry: ToolRegistry
    system_prompt: str
    client: OpenAI
    ws_manager: ConnectionManager
    model: str

    def __init__(self, model:str, api_key: str, base_url: str, tool_registry: ToolRegistry, ws_manager: ConnectionManager, context_config: dict | None = None):
        self.model = model # NVIDIA 免费 API 接口测试：QWEN 系列模型不支持 function call
        self.client = OpenAI(
            base_url=base_url,
            api_key=api_key
        )
        self.async_client = AsyncOpenAI(
            base_url=base_url,
            api_key=api_key
        )
        self.tool_registry = tool_registry
        self.system_prompt = build_system_prompt()
        self.ws_manager = ws_manager

    def generate(self, context: Dict[str, Any]) -> tuple[str, list, dict]:
        step = cast(Step,context.get("step")) # current_step
        is_thinking = context.get("is_thinking", False)
        # TODO 针对 429 Error Code 做重试
        response = self.client.chat.completions.create(
            model=self.model,
            messages=context.get("messages"),
            temperature=0.2,
            top_p=0.9,
            tools=self.tool_registry.get_all_schemas(excludes=["chrome-devtools_take_screenshot"]),
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
        if response.choices[0].finish_reason == "tool_calls":
            calls = response.choices[0].message.tool_calls
            step.tool_calls = normalize_tool_calls(response.choices[0].message.tool_calls)
            for call in calls:
                tool = self.tool_registry.get(call.function.name)
                action = Action(action_id=call.id,
                                type=ActionType.TOOL,
                                tool_name=call.function.name,
                                args=safe_parse_tool_args(call.function.arguments),
                                requires_confirm=tool.requires_confirmation,
                                confirm_status="pending" if tool.requires_confirmation else None)
                actions.append(action)
        else:
            logger.info(f"llm result: {content}")
            action_type, action_message, raw = _coerce_content_action(content)
            logger.info(f"llm output json: {raw}")
            actions.append(
                Action(
                    action_id=get_sonyflake("action_"),
                    type=action_type,
                    message=action_message,
                    full_ref=raw,
                )
            )

        if response.usage.prompt_tokens_details:
            logger.info(f"Cache Tokens: {response.usage.prompt_tokens_details.cached_tokens}")
        logger.info(
            f"Prompt Token: {response.usage.prompt_tokens}, "
            f"Completion Token: {response.usage.completion_tokens}, "
            f"Total Token: {response.usage.total_tokens}"
        )
        token_info = {
            "cache_tokens": response.usage.prompt_tokens_details.cached_tokens if (response.usage.prompt_tokens_details and response.usage.prompt_tokens_details.cached_tokens) else 0,
            "prompt_tokens": response.usage.prompt_tokens,
            "completion_tokens": response.usage.completion_tokens,
            "total_tokens": response.usage.total_tokens
        }
        return reasoning, actions, token_info

