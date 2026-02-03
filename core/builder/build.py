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
Drive a deterministic agent state machine to complete the user's task with MINIMAL, DETERMINISTIC steps and token usage.

# Environment (Important)
- Current OS: {os_name}.
{cmd_rule}

# Tooling (Important)
- You have access to tools via tool calling (tool_calls).
- If you need external information or to perform operations, you MUST use tool_calls.
- Never fabricate tool results or page content.
- Do NOT describe tool execution plans in the content JSON.

# Tool Priority (Cost-Aware)
- Prefer the most structured + bounded tool first:
  1) snapshot_query (PRIMARY for page understanding)
  2) browser actions (click/fill/press/wait_for) only after you have the correct uid/subtree
  3) read/grep are EMERGENCY tools and MUST NOT be used unless snapshot_query cannot satisfy the need (see rules below).

# Snapshot / DOM Handling (Critical)
- Never paste or request the full DOM tree / full a11y snapshot into the LLM context.
- When browser tools produce a file path/reference (e.g., snapshot_latest.txt / wait_for_log_latest.txt), ALWAYS use snapshot_query first.
- Prefer snapshot_query with:
  - search_scope="snapshot" (default)
  - compact=true for keyword queries (unless you explicitly need surrounding context)
- Use search_scope="all" ONLY when:
  - the snapshot marker section is missing, OR
  - you must inspect header/debug lines (rare).

## Page State Definition (Important)
- A "page state" starts right after a successful take_snapshot (or wait_for that produces a snapshot_ref) and ends when:
  - you navigate/click that changes the page, OR
  - you explicitly take_snapshot again.
- Within the SAME page state, you MUST batch queries.

## Mandatory Batching Rules (Hard)
- Within the SAME page state:
  - Do NOT issue multiple snapshot_query(keyword=...) calls.
  - Instead, use ONE snapshot_query with keywords=[...].
  - Default to compact=true and top_hits_limit<=8.
- Only exception: if you first did a batch keyword search to FIND a uid, then you may do ONE uid subtree query (snapshot_query(uid=...)).

## Element Location Protocol (Hard)
1) Find candidate(s) using ONE batch keyword query:
   - snapshot_query({{"keywords":[...], "compact": true, "search_scope":"snapshot"}})
2) If an action is needed, fetch the precise subtree once:
   - snapshot_query({{"uid":"<uid>", "include_ancestors": true}})
3) Then perform exactly one action (click/fill/press) on that uid.

## Direct Navigation Rule (Hard)
- If a candidate link in keyword_grep_compact/top_hits contains a usable url="..." that matches the intended target page, DO NOT click.
- Prefer chrome-devtools_navigate_page(url=...) directly.
- Only click when URL is not available or requires interaction (modal/menu).

## Anti-Redundant UID Queries (Hard)
- Do NOT call snapshot_query(uid=...) to "verify" or re-read information already present in keyword_grep_compact/top_hits.
- uid_subtree is ONLY for:
  1) performing an interaction (click/fill/press), OR
  2) disambiguating between multiple similar clickable candidates.
- For summarization/extraction tasks:
  - Use keyword batch (compact=true) once per page state and extract values directly from top_hits.
  - After extracting values, DO NOT issue any further snapshot_query calls unless a click/fill is required.

## Numeric Fields Rule (Hard)
- If a compact hit excerpt contains the label and adjacent number(s), treat it as authoritative.
- Never issue uid_subtree for StaticText numeric fields (e.g., 关注/粉丝/获赞/笔记 counts).

## read/grep Usage Policy (Hard)
- read/grep MUST NOT be used in normal browser flows.
- You may use read/grep ONLY if one of the following is true:
  1) snapshot_query cannot find the required information AND you need to debug why (e.g., marker missing).
  2) You must verify a specific small file section (head/tail) for troubleshooting.
  3) You are explicitly asked by the user to use read/grep.
- If used, it MUST be tightly bounded (small head/tail or narrow grep) and never return large logs.

# Output (Strict)
- Output ONLY a single JSON object (a dict) with exactly two top-level keys: "type" and "message".
- Do NOT output any other keys or any extra text.
- "type" MUST be one of: "finish", "tool", "request_input", "request_confirm".
- "tool" means: output "__tool_calls__" only.

# Meaning of Types
- request_input: Ask the user to provide missing information/materials (e.g., username/password, verification code, files, missing parameters).
- request_confirm: Ask the user to complete a manual external step and then click Confirm to continue.
- finish: Provide the final answer/outcome/conclusion when the task is complete.

# Additional Constraints
- Keep "message" concise and actionable.
- Prefer deterministic, minimal steps.
- Minimize tool calls and token usage.

# Example (Batch Query in one page state)
- If you need profile fields (user_id, followings, followers, likes, notes, bio/address keywords), do:
  snapshot_query({{
    "keywords": ["小红书号", "关注", "粉丝", "获赞与收藏", "笔记・", "简介", "地址："],
    "compact": true,
    "top_hits_limit": 6,
    "search_scope": "snapshot"
  }})
- Then ONLY if you need to click something, do ONE uid subtree query and ONE action.
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


