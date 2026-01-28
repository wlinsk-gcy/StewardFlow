from abc import abstractmethod
from typing import Optional,Dict
from pathlib import Path

class Instance:
    # Change this to your sandbox dir
    directory: str = str(Path.cwd())

    @staticmethod
    def contains_path(p: str) -> bool:
        """
        Whether path is inside sandbox root.
        Replace with your real policy.
        """
        root = Path(Instance.directory).resolve()
        try:
            return root in Path(p).resolve().parents or Path(p).resolve() == root
        except Exception:
            return False

class Tool:
    """
    doc: https://platform.openai.com/docs/guides/function-calling
    """
    def __init__(self):
        self.type = "function" # This should always be function
        self.name = "undefined"
        self.description = "undefined"
        self.requires_confirmation = False

    @abstractmethod
    def schema(self) -> dict:
        """
        doc: https://platform.openai.com/docs/guides/function-calling#defining-functions
        :return: json schema
        """
        raise NotImplementedError

    @abstractmethod
    async def execute(self, **kwargs) -> str:
        raise NotImplementedError


class ToolRegistry:
    def __init__(self):
        self.tools: Dict[str, Tool] = {}

    def register(self, tool: Tool):
        # TODO schema 校验
        self.tools[tool.name] = tool

    def get(self, name: str) -> Optional[Tool]:
        return self.tools.get(name)

    def list_tools(self) -> Dict[str, Tool]:
        return self.tools.copy()

    def get_tool_name(self) -> list[str]:
        return list(self.tools.keys())

    def get_all_schemas(self) -> list[Dict]:
        return [tool.schema() for tool in self.tools.values()]
