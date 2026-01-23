import json
import logging
from typing import Dict, Any
from openai.types.shared_params.response_format_json_schema import ResponseFormatJSONSchema, JSONSchema

logger = logging.getLogger(__name__)


def build_system_prompt():
    return """
# Role
You are an Agent Core Model, not a chatbot.

# Responsibilities
Your sole responsibility is to drive a deterministic agent state machine.
In each turn, based on the current task and the provided context, you must produce
ONE and ONLY ONE Thought and ONE Action.

# Context Protocol (Very Important)
You will receive prior context as a sequence of messages.

The context may include:
- Natural language descriptions of the initial task or goal.
- JSON objects representing actions you previously decided.
- Natural language observations representing feedback from the external world
  (such as tool results or human input).

The context is a chronological execution trace.
You must treat it as ground truth.

# Important rules about context:
- Thoughts are NEVER included in the context.
- You must NOT infer or reconstruct past thoughts.
- You must NOT reason about how the context was generated.
- You only decide the NEXT action.

# Behavioral Constraints
1. Output EXACTLY ONE action per turn.
2. Do NOT plan multiple steps.
3. Do NOT explain your reasoning outside the "thought" field.
4. Do NOT output anything other than the required JSON.
5. If required information is missing, you MUST use "request_input" and put the question in the "prompt" field.
6. When the task is successfully completed or an ultimate conclusion is reached, you MUST use "finish" and put the final response in the "answer" field.

# Output Format (Strict)
- Always output a single JSON object with keys: "thought" and "action".
- "thought" MUST be a string (can be empty).
- "action" MUST be an object with key "type".
- Valid action types: "tool", "request_input", "finish".
- If type is "tool": include "tool_name" (string) and "args" (object). Set "prompt" and "answer" to null.
- If type is "request_input": include "prompt" (string). Set "tool_name", "args", "answer" to null.
- If type is "finish": include "answer" (string). Set "tool_name", "args", "prompt" to null.
- Never omit required keys. Use null explicitly when a field does not apply.

# Example
- {"thought":"...","action":{"type":"finish","tool_name": null,"args": null,"prompt":null,"answer":"..."}},
- {"thought":"...","action":{"type":"request_input","tool_name": null,"args": null,"prompt":"...","answer": null}},
- {"thought":"...","action":{"type":"tool","tool_name":"web_search","args":{"query":"..."},"prompt":null,"answer":null}},
- {"thought":"...","action":{"type":"tool","tool_name":"bash","args":{"command":"..."},"prompt":null,"answer":null}}
"""


def build_llm_messages(context: Dict[str, Any], system_prompt: str):
    tao_trajectory = context.get("tao_trajectory") or []

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": context.get("task")}
    ]

    # TODO 后续缩短tao历史记录，进行摘要生成
    # 将历史轨迹单独加入 messages
    for traj in tao_trajectory:

        messages.append({"role": "assistant", "content": json.dumps({"thought": traj["thought"]["content"],"action": traj["action"]}, ensure_ascii=False)})
        # 非常重要：Observation 一定要用 user role
        # 因为这是“环境反馈”，不是 Agent 自言自语。
        # messages.append({"role": traj["observation"]["role"], "content": traj["observation"]["content"]})
        # QWEN系列不支持role为tool的情况下，没有tool call id，可是模型没有返回tool call记录，所以只能用user
        if traj["observation"]["role"] == "tool":
            content = "工具执行结果：" + traj["observation"]["content"]
            messages.append({"role": "user", "content": content})
        else:
            messages.append({"role": "user", "content": traj["observation"]["content"]})

    logger.info(f"===messages: {messages}")
    return messages


# 接口文档：https://platform.openai.com/docs/api-reference/chat
llm_response_schema = ResponseFormatJSONSchema(
    json_schema=JSONSchema(
        name="agent",
        description="""
Structured output schema for a deterministic Agent execution engine.

Each response represents exactly one reasoning step (thought) and one
state transition (action). All action-specific constraints are defined
at the field level and must be strictly followed.
""",
        schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["thought", "action"],
            "properties": {
                "thought": {
                    "type": "string",
                    "description": "Internal reasoning, not shown to user",
                    "max_length": 512
                },
                "action": {
                    "anyOf": [
                        # tool
                        {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["type", "tool_name", "args", "prompt", "answer"],
                            "properties": {
                                "type": {
                                    "type": "string",
                                    "enum": ["tool"],
                                    "description": "Use to call a tool."
                                },
                                "tool_name": {
                                    "type": "string",
                                    "description": "Registered tool name."
                                },
                                "args": {
                                    "type": "object",
                                    "description": "Tool arguments."
                                },
                                "prompt": {
                                    "type": "null",
                                    "description": "Must be null for tool actions."
                                },
                                "answer": {
                                    "type": "null",
                                    "description": "Must be null for tool actions."
                                }
                            }
                        },
                        # request_input
                        {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["type", "tool_name", "args", "prompt", "answer"],
                            "properties": {
                                "type": {
                                    "type": "string",
                                    "enum": ["request_input"],
                                    "description": "Use when required information is missing."
                                },
                                "tool_name": {
                                    "type": "null",
                                    "description": "Must be null for request_input."
                                },
                                "args": {
                                    "type": "null",
                                    "description": "Must be null for request_input."
                                },
                                "prompt": {
                                    "type": "string",
                                    "description": "Question to ask the user."
                                },
                                "answer": {
                                    "type": "null",
                                    "description": "Must be null for request_input."
                                }
                            }
                        },
                        # finish
                        {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["type", "tool_name", "args", "prompt", "answer"],
                            "properties": {
                                "type": {
                                    "type": "string",
                                    "enum": ["finish"],
                                    "description": "Use when the task is complete."
                                },
                                "tool_name": {
                                    "type": "null",
                                    "description": "Must be null for finish."
                                },
                                "args": {
                                    "type": "null",
                                    "description": "Must be null for finish."
                                },
                                "prompt": {
                                    "type": "null",
                                    "description": "Must be null for finish."
                                },
                                "answer": {
                                    "type": "string",
                                    "description": "Final response to the user."
                                }
                            }
                        }
                    ]
                }
            }
        },
        strict=True
    ),
    type="json_schema"
)
