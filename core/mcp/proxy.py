import logging
import json
from core.tools.tool import Tool
from mcp.types import TextContent

logger = logging.getLogger(__name__)

class MCPToolProxy(Tool):
    def __init__(self, fq_name: str, description: str, input_schema: dict, call_fn):
        super().__init__()
        self.name = fq_name  # e.g. "chrome-devtools.click"
        self.description = description
        self._input_schema = input_schema
        self._call_fn = call_fn  # async (args) -> result

    def schema(self) -> dict:
        return {
                "type": "function",
                "function": {
                    "name": self.name,
                    "description": self.description,
                    "parameters": self._input_schema
                }
            }

    async def execute(self, **kwargs) -> str:
        # 你也可以在这里做 Pydantic 校验
        res = await self._call_fn(kwargs)
        content = res.content
        if content and isinstance(content[0], TextContent):
            logger.info(f"MCP Proxy Tool Result: {content}")
            return content[0].text
        # MCP 返回结构你可以统一成你系统的格式
        # 常见：{"content":[{"type":"text","text":"..."}, ...]}
        # return {"ok": True, "raw": res}
        return json.dumps(res)