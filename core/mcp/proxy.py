import logging
import json
from typing import cast
from core.tools.tool import Tool
from mcp.types import TextContent
from utils.snapshot_util import should_summarize_snapshot, save_snapshot_raw, build_snapshot_summary

logger = logging.getLogger(__name__)

class MCPToolProxy(Tool):
    def __init__(self, fq_name: str, description: str, input_schema: dict, call_fn):
        super().__init__()
        self.name = fq_name  # e.g. "chrome-devtools_click"
        self.description = description
        self._input_schema = input_schema
        self._call_fn = call_fn  # async (args) -> result

    def schema(self) -> dict:
        return {
                "type": "function",
                "function": {
                    "name": self.name,
                    "description": self.description,
                    "parameters": self._input_schema,
                    "strict": True,
                }
            }

    async def do_another(self, **kwargs) -> str:
        if self.name == "chrome-devtools_take_snapshot":
            try:
                return save_snapshot_raw(self.name, kwargs.get("text"))
            except Exception as e:
                logger.warning(f"{self.name}' MCP Proxy Tool Result Failed to save snapshot logs: {e}")
                raise e
        elif self.name == "chrome-devtools_wait_for":
            text = cast(str, kwargs.get("text"))
            logger.info(f"'{self.name}' MCP Proxy Tool Result length: {len(text)}")
            if text.startswith("# wait_for response"):
                return "wait_for response"
            else:
                return text
        else:
            return ""
        # if not should_summarize_snapshot(self.name):
        #     return ""
        # try:
        #     return save_snapshot_raw(self.name, kwargs.get("text"))
        # except Exception as e:
        #     logger.warning(f"{self.name}' MCP Proxy Tool Result Failed to save snapshot logs: {e}")
        #     raise e


    async def execute(self, **kwargs) -> str:
        # 你也可以在这里做 Pydantic 校验
        res = await self._call_fn(kwargs)
        content = res.content
        if content and isinstance(content[0], TextContent):
            text = content[0].text
            do_another_result = await self.do_another(text=text)
            return do_another_result if do_another_result else text
        return json.dumps(res)
