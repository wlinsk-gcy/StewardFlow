import logging
import json
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

    async def execute(self, **kwargs) -> str:
        # 你也可以在这里做 Pydantic 校验
        res = await self._call_fn(kwargs)
        content = res.content
        if content and isinstance(content[0], TextContent):
            text = content[0].text
            logger.info(f"'{self.name}' MCP Proxy Tool Result length: {len(text)}")
            if should_summarize_snapshot(self.name, text):
                try:
                    save_snapshot_raw(text)
                except Exception as e:
                    logger.warning(f"Failed to save snapshot logs: {e}")
                summary = build_snapshot_summary(text, max_tokens=1000, max_items=120)
                logger.info(f"MCP Proxy Tool summary Result length: {len(summary)}")
                return summary
            return text
        return json.dumps(res)
