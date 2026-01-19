import json
import logging
from typing import Dict, Any
from openai.types.shared_params.response_format_json_schema import ResponseFormatJSONSchema, JSONSchema

logger = logging.getLogger(__name__)


def build_system_prompt():
    return f"""
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
        5. If required information is missing, you MUST use "request_input" or "request_confirm" and put the question in the "prompt" field.
        6. When the task is successfully completed or an ultimate conclusion is reached, you MUST use "finish" and put the final response in the "answer" field.
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
        messages.append({"role": "assistant", "content": json.dumps(traj["action"], ensure_ascii=False)})
        # 非常重要：Observation 一定要用 user role
        # 因为这是“环境反馈”，不是 Agent 自言自语。
        messages.append({"role": traj["observation"]["role"], "content": traj["observation"]["content"]})

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
                                "type": {"type": "string", "enum": ["tool"]},
                                "tool_name": {"type": "string"},
                                "args": {"type": "object"},
                                "prompt": {"type": "null"},
                                "answer": {"type": "null"}
                            }
                        },
                        # request_input
                        {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["type", "tool_name", "args", "prompt", "answer"],
                            "properties": {
                                "type": {"type": "string", "enum": ["request_input"]},
                                "tool_name": {"type": "null"},
                                "args": {"type": "null"},
                                "prompt": {"type": "string"},
                                "answer": {"type": "null"}
                            }
                        },
                        # request_confirm
                        {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["type", "tool_name", "args", "prompt", "answer"],
                            "properties": {
                                "type": {"type": "string", "enum": ["request_confirm"]},
                                "tool_name": {"type": "null"},
                                "args": {"type": "null"},
                                "prompt": {"type": "string"},
                                "answer": {"type": "null"}
                            }
                        },
                        # finish
                        {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["type", "tool_name", "args", "prompt", "answer"],
                            "properties": {
                                "type": {"type": "string", "enum": ["finish"]},
                                "tool_name": {"type": "null"},
                                "args": {"type": "null"},
                                "prompt": {"type": "null"},
                                "answer": {"type": "string"}
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
