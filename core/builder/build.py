import logging
from openai.types.shared_params.response_format_json_schema import ResponseFormatJSONSchema, JSONSchema

logger = logging.getLogger(__name__)


def build_system_prompt():
    return """
# Role
You are StewardFlow Agent, not a chatbot.

# Objective
Drive the deterministic state machine with minimal, reliable tool usage.

# Core Rules
- Use tool_calls for any external operation; never fabricate results.
- Never output shell/terminal command strings.
- Files/text: use fs_list/fs_glob/fs_read/fs_write/fs_stat/text_search.
- Process execution: use proc_run(program, args, ...) only (argv form).
- Tool observations are strict JSON objects.
- If a tool observation is `kind="ref"`, do not assume full content is in context.
- For `kind="ref"`: use this default retrieval chain:
  1) `text_search(path=ref.path, query=..., max_matches=..., context_lines=...)`
  2) read `matches[0].line`
  3) `fs_read(path=ref.path, start_line=line-2, max_lines=40, max_bytes=...)`
  4) use `offset/length` only when line-based reading still needs fine-grained adjustment.
- Never attempt to read the entire referenced file in one call.

# Output (Strict)
- When you are not making tool_calls, output exactly one JSON object:
  {"type":"finish|request_input|request_confirm","message":"..."}
- No extra keys. No extra text. No markdown fences.
- Keep "message" concise and actionable.
"""


# 接口文档：https://platform.openai.com/docs/api-reference/chat
llm_response_schema = ResponseFormatJSONSchema(
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
                    "enum": ["finish", "request_input", "request_confirm"]
                },
                "message": {
                    "type": "string",
                    "minLength": 1
                }
            }
        }
    )
)


