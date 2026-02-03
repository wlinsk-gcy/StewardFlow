import sys
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

# def build_system_prompt_v2():
#     return"""
# # Role
# You are an Agent Core Model, not a chatbot.
#
# # Objective
# Drive a deterministic agent state machine. For each turn, output the next operations as an actions list.
#
# # Context
# You will receive a chronological execution trace as messages (task, prior actions, tool observations, and human input/confirmation). Treat it as ground truth. Do not invent tool results.
#
# # Output Rules (Strict)
# - Output ONLY a single JSON object with exactly one top-level key: actions. No other keys. No extra text.
# - actions MUST be a non-empty array of action objects.
# - Allowed action types are ONLY: tool, finish, request_input, request_confirm.
# - You MAY output multiple tool actions in one response.
# - You MUST NOT output more than one finish. If finish appears, it MUST be the last action in the array.
#
# # Field Constraints (Must Follow)
# - If action type is tool: you MUST include tool_name and args, and they MUST NOT be empty. Do not include prompt or answer.
# - If action type is finish: output ONLY answer. Do not include tool_name, args, or prompt.
# - If action type is request_input or request_confirm: output ONLY prompt. Do not include tool_name, args, or answer.
#
# # When to use request_input vs request_confirm
# - Use request_input when you need the user to provide information/materials (e.g., username/password, verification code, files, missing parameters).
# - Use request_confirm when the user must perform a manual external step and then click Confirm to continue (e.g., scanning a QR code to log in, approving a login prompt on a phone, completing a captcha manually).
#     - Example: If a page requires QR login, ask the user to scan and then confirm.
#
# # Behavior
# - Decide only the next operations.
# - Keep prompts concise and actionable.
# - Prefer tools for retrieving external information instead of guessing.
# """
def build_system_prompt_v2():
    platform = sys.platform
    if platform.startswith("win"):
        os_name = "Windows"
        cmd_rule = "- You MUST use PowerShell commands for any command-line operations.\n- Do NOT assume Linux/Bash or WSL is available."
    elif platform == "darwin":
        os_name = "macOS"
        cmd_rule = "- Use standard POSIX shell commands (bash/zsh).\n- Do NOT use Windows PowerShell-specific syntax."
    else:
        os_name = "Linux"
        cmd_rule = "- Use standard POSIX shell commands (bash/sh).\n- Do NOT use Windows PowerShell-specific syntax."

    return f"""
# Role
You are an StewardFlow Agent, not a chatbot.

# Objective
Drive a deterministic agent state machine.

# Environment (Important)
- Current OS: {os_name}.
{cmd_rule}

# Tooling (Important)
- You have access to tools via tool calling (tool_calls).
- If you need external information or to perform operations, you MUST use tool_calls.
- Do NOT describe tool execution plans in the content JSON.
- Never fabricate tool results or page content.

# Snapshot / DOM Handling (Critical)
- Never paste or request the full DOM tree / full a11y snapshot into the LLM context.
- When browser tools produce a file path/reference (e.g., snapshot_latest.txt), you MUST query it using snapshot_query (preferred) or a bounded read/grep tool. Do NOT assume/invent content.
- Prefer snapshot_query with search_scope="snapshot" (default). Use search_scope="all" only if the marker section is missing or you explicitly need header/debug lines.
- To locate elements:
  1) First call snapshot_query with keyword to get a small, bounded excerpt (or candidates if available).
  2) Then call snapshot_query with uid to fetch the subtree (include_ancestors=true) for precise actions.
  3) Only then perform click/input actions using the uid or derived selector.
- Always keep excerpts bounded (max_lines) and avoid returning large raw logs.

# Output (Strict)
- Output ONLY a single JSON object (a dict) with exactly two top-level keys: "type" and "message".
- Do NOT output any other keys or any extra text.

# Allowed Types
- "type" MUST be one of: "finish", "tool", "request_input", "request_confirm".

# Meaning of Types
- request_input: Ask the user to provide missing information/materials (e.g., username/password, verification code, files, missing parameters).
- request_confirm: Ask the user to complete a manual external step and then click Confirm to continue
  (e.g., scan QR code to login, approve login on phone, complete captcha manually).
- finish: Provide the final answer/outcome/conclusion when the task is complete.
- tool: Just output "__tool_calls__"

# Additional Constraints
- Keep "message" concise and actionable.
- When using tools, prefer deterministic, minimal steps.
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


# llm_response_schema_v2 = ResponseFormatJSONSchema(
#     type="json_schema",
#     json_schema=JSONSchema(
#         name="agent",
#         description="""
# Structured output schema for deterministic Agent execution engine.
#
# The model must output only an actions array. Each action is one of:
# tool | finish | request_input | request_confirm.
# """,
#         strict=True,
#         schema={
#             "type": "object",
#             "additionalProperties": False,
#             "required": ["actions"],
#             "properties": {
#                 "actions": {
#                     "type": "array",
#                     "minItems": 1,
#                     "items": {
#                         "anyOf": [
#                             # tool: must have tool_name + args, nothing else
#                             {
#                                 "type": "object",
#                                 "additionalProperties": False,
#                                 "required": ["type", "tool_name", "args"],
#                                 "properties": {
#                                     "type": {
#                                         "type": "string",
#                                         "enum": ["tool"],
#                                     },
#                                     "tool_name": {
#                                         "type": "string",
#                                         "minLength": 1
#                                     },
#                                     "args": {
#                                         "type": "object",
#                                         "minProperties": 1
#                                     }
#                                 }
#                             },
#                             # finish: only answer
#                             {
#                                 "type": "object",
#                                 "additionalProperties": False,
#                                 "required": ["type", "answer"],
#                                 "properties": {
#                                     "type": {
#                                         "type": "string",
#                                         "enum": ["finish"],
#                                     },
#                                     "answer": {
#                                         "type": "string",
#                                         "minLength": 1
#                                     }
#                                 }
#                             },
#                             # request_input: only prompt
#                             {
#                                 "type": "object",
#                                 "additionalProperties": False,
#                                 "required": ["type", "prompt"],
#                                 "properties": {
#                                     "type": {
#                                         "type": "string",
#                                         "enum": ["request_input"],
#                                     },
#                                     "prompt": {
#                                         "type": "string",
#                                         "minLength": 1
#                                     }
#                                 }
#                             },
#                             # request_confirm: only prompt
#                             {
#                                 "type": "object",
#                                 "additionalProperties": False,
#                                 "required": ["type", "prompt"],
#                                 "properties": {
#                                     "type": {
#                                         "type": "string",
#                                         "enum": ["request_confirm"],
#                                     },
#                                     "prompt": {
#                                         "type": "string",
#                                         "minLength": 1
#                                     }
#                                 }
#                             }
#                         ]
#                     },
#
#                     # at most one finish action (0 or 1)
#                     "contains": {
#                         "type": "object",
#                         "required": ["type"],
#                         "properties": {
#                             "type": {"const": "finish"}
#                         }
#                     },
#                     "minContains": 0,
#                     "maxContains": 1
#                 }
#             }
#         }
#     )
# )
# llm_response_schema_v2 = ResponseFormatJSONSchema(
#     type="json_schema",
#     json_schema=JSONSchema(
#         name="agent_control",
#         description="""
# Control-only output for the deterministic agent engine.
#
# Tools are expressed ONLY via tool_calls.
# This JSON is ONLY for control signals: finish / request_input / request_confirm.
# """,
#         strict=True,
#         schema={
#             "type": "object",
#             "additionalProperties": False,
#             "required": ["actions"],
#             "properties": {
#                 "actions": {
#                     "type": "array",
#                     # allow empty when tool_calls are present
#                     "minItems": 0,
#                     # usually only need one control signal per turn
#                     "maxItems": 1,
#                     "items": {
#                         "anyOf": [
#                             # finish -> only answer
#                             {
#                                 "type": "object",
#                                 "additionalProperties": False,
#                                 "required": ["type", "answer"],
#                                 "properties": {
#                                     "type": {"type": "string", "enum": ["finish"]},
#                                     "answer": {"type": "string", "minLength": 1},
#                                 },
#                             },
#                             # request_input -> only prompt
#                             {
#                                 "type": "object",
#                                 "additionalProperties": False,
#                                 "required": ["type", "prompt"],
#                                 "properties": {
#                                     "type": {"type": "string", "enum": ["request_input"]},
#                                     "prompt": {"type": "string", "minLength": 1},
#                                 },
#                             },
#                             # request_confirm -> only prompt
#                             {
#                                 "type": "object",
#                                 "additionalProperties": False,
#                                 "required": ["type", "prompt"],
#                                 "properties": {
#                                     "type": {"type": "string", "enum": ["request_confirm"]},
#                                     "prompt": {"type": "string", "minLength": 1},
#                                 },
#                             },
#                         ]
#                     },
#                 }
#             },
#         },
#     ),
# )
llm_response_schema_v2 = ResponseFormatJSONSchema(
    type="json_schema",
    json_schema=JSONSchema(
        name="agent",
        description="""
Control-only output for the deterministic agent engine.

Tools are expressed ONLY via tool_calls.
The assistant content must be a single dict with keys: type, message.
""",
        strict=True,
        schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["type", "message"],
            "properties": {
                "type": {
                    "type": "string",
                    "enum": ["finish", "tool", "request_input", "request_confirm"]
                },
                "message": {
                    "type": "string",
                    "minLength": 1
                }
            }
        }
    )
)


