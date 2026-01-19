import re
import json
import logging
logger = logging.getLogger(__name__)

THINK_PATTERN = re.compile(r"<think>(.*?)</think>", re.DOTALL)
# 有JSON标识
JSON_FENCE_PATTERN = re.compile(
    r"```(?:json)?\s*(\{[\s\S]*?\})\s*```",
    re.DOTALL
)
# 裸JSON
JSON_BARE_PATTERN = re.compile(
    r"^(\{[\s\S]*\})",
    re.DOTALL
)
# 第三种情况：
# 'Thought: 用户已提供要查询的城市是"北京"。现在我可以调用天气查询工具来获取北京的天气信息。Action: {"tool_name": "get_weather", "type": "tool", "args": {"city": "北京"}}'
THOUGHT_PATTERN = re.compile(r"Thought\s*:\s*(.*?)(?:\n\n|$)", re.S)

def extract_json(text: str) -> dict:
    text = text.strip()
    match = JSON_FENCE_PATTERN.search(text)
    if match:
        candidate = match.group(1).strip()
        try:
            return json.loads(candidate)
        except json.JSONDecodeError as e:
            logger.error(f"JSON parse failed: {e}")
            logger.error(f"Raw json string: {candidate}")

    match = JSON_BARE_PATTERN.search(text)
    if match:
        candidate = match.group(1).strip()
        try:
            return json.loads(candidate)
        except json.JSONDecodeError as e:
            logger.error(f"JSON parse failed: {e}")
            logger.error(f"Raw json string: {candidate}")

    # 第三种情况：
    thought_match = THOUGHT_PATTERN.search(text)
    if thought_match:
        thought_text = thought_match.group(1).strip()
        action_dict = extract_json_by_brace_matching(text)
        return {
            "thought": thought_text,
            "action": action_dict
        }

    raise ValueError("No valid JSON object found in LLM output")


def extract_json_by_brace_matching(text: str) -> dict:
    """括号平衡算法兜底"""
    stack = []
    start = None

    for i, ch in enumerate(text):
        if ch == '{':
            if not stack:
                start = i
            stack.append(ch)
        elif ch == '}':
            if stack:
                stack.pop()
                if not stack and start is not None:
                    candidate = text[start:i + 1]
                    try:
                        return json.loads(candidate)
                    except json.JSONDecodeError:
                        start = None

    raise ValueError("No valid JSON object found in LLM output")

class ThinkStreamExtractor:
    def __init__(self):
        self.buffer = ""
        self.in_think = False

    def feed(self, chunk: str):
        """
        返回：
          normal_parts: list[str]  # 可对外输出
          think_parts: list[str]   # 只用于日志
        """
        self.buffer += chunk
        normal_parts = []
        think_parts = []
        while self.buffer:
            if not self.in_think:
                idx = self.buffer.find("<think>")
                if idx == -1:
                    normal_parts.append(self.buffer)
                    self.buffer = ""
                else:
                    if idx > 0:
                        normal_parts.append(self.buffer[:idx])
                    self.buffer = self.buffer[idx + len("<think>"):]
                    self.in_think = True
            else:
                idx = self.buffer.find("</think>")
                if idx == -1:
                    think_parts.append(self.buffer)
                    self.buffer = ""
                else:
                    think_parts.append(self.buffer[:idx])
                    self.buffer = self.buffer[idx + len("</think>"):]
                    self.in_think = False
        return normal_parts, think_parts



