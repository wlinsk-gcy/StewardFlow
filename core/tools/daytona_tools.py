from __future__ import annotations

import asyncio
from typing import Any, Mapping

from core.daytona.context import get_current_trace_id
from core.daytona.manager import DaytonaSandboxManager, get_daytona_manager

from .tool import Tool, ToolRegistry


class DaytonaTool(Tool):
    def __init__(self, manager: DaytonaSandboxManager):
        super().__init__()
        self.manager = manager

    @staticmethod
    def _trace_id() -> str:
        return get_current_trace_id()


class DaytonaFsListTool(DaytonaTool):
    def __init__(self, manager: DaytonaSandboxManager):
        super().__init__(manager)
        self.name = "daytona_fs_list"
        self.description = "List files and folders in a Daytona sandbox path."

    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Target path in sandbox, e.g. '.' or '/workspace'."}
                    },
                    "required": ["path"],
                    "additionalProperties": False,
                },
                "strict": True,
            },
        }

    async def execute(self, **kwargs) -> dict[str, Any]:
        trace_id = self._trace_id()
        path = kwargs["path"]
        return await asyncio.to_thread(self.manager.fs_list, trace_id, path)


class DaytonaFsReadTool(DaytonaTool):
    def __init__(self, manager: DaytonaSandboxManager):
        super().__init__(manager)
        self.name = "daytona_fs_read"
        self.description = "Read a file from Daytona sandbox."

    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string", "description": "Absolute or relative file path in sandbox."}},
                    "required": ["path"],
                    "additionalProperties": False,
                },
                "strict": True,
            },
        }

    async def execute(self, **kwargs) -> dict[str, Any]:
        trace_id = self._trace_id()
        path = kwargs["path"]
        return await asyncio.to_thread(self.manager.fs_read, trace_id, path)


class DaytonaFsWriteTool(DaytonaTool):
    def __init__(self, manager: DaytonaSandboxManager):
        super().__init__(manager)
        self.name = "daytona_fs_write"
        self.description = "Write file content into Daytona sandbox."

    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "File path in sandbox."},
                        "content": {"type": "string", "description": "UTF-8 text content to write."},
                    },
                    "required": ["path", "content"],
                    "additionalProperties": False,
                },
                "strict": True,
            },
        }

    async def execute(self, **kwargs) -> dict[str, Any]:
        trace_id = self._trace_id()
        return await asyncio.to_thread(
            self.manager.fs_write,
            trace_id,
            kwargs["path"],
            kwargs["content"],
        )


class DaytonaFsStatTool(DaytonaTool):
    def __init__(self, manager: DaytonaSandboxManager):
        super().__init__(manager)
        self.name = "daytona_fs_stat"
        self.description = "Get file metadata in Daytona sandbox."

    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string", "description": "Path in sandbox."}},
                    "required": ["path"],
                    "additionalProperties": False,
                },
                "strict": True,
            },
        }

    async def execute(self, **kwargs) -> dict[str, Any]:
        trace_id = self._trace_id()
        return await asyncio.to_thread(self.manager.fs_stat, trace_id, kwargs["path"])


class DaytonaGitCloneTool(DaytonaTool):
    def __init__(self, manager: DaytonaSandboxManager):
        super().__init__(manager)
        self.name = "daytona_git_clone"
        self.description = "Clone a Git repository in Daytona sandbox."

    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "description": "Git repository URL."},
                        "path": {"type": "string", "description": "Target folder path in sandbox."},
                    },
                    "required": ["url", "path"],
                    "additionalProperties": False,
                },
                "strict": True,
            },
        }

    async def execute(self, **kwargs) -> dict[str, Any]:
        trace_id = self._trace_id()
        return await asyncio.to_thread(
            self.manager.git_clone,
            trace_id,
            kwargs["url"],
            kwargs["path"],
        )


class DaytonaGitStatusTool(DaytonaTool):
    def __init__(self, manager: DaytonaSandboxManager):
        super().__init__(manager)
        self.name = "daytona_git_status"
        self.description = "Get git status for a sandbox repository path."

    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string", "description": "Repository path in sandbox."}},
                    "required": ["path"],
                    "additionalProperties": False,
                },
                "strict": True,
            },
        }

    async def execute(self, **kwargs) -> dict[str, Any]:
        trace_id = self._trace_id()
        return await asyncio.to_thread(self.manager.git_status, trace_id, kwargs["path"])


class DaytonaGitAddTool(DaytonaTool):
    def __init__(self, manager: DaytonaSandboxManager):
        super().__init__(manager)
        self.name = "daytona_git_add"
        self.description = "Stage files in a Daytona sandbox git repository."

    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Repository path in sandbox."},
                        "files": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "File paths relative to repo root.",
                        },
                    },
                    "required": ["path", "files"],
                    "additionalProperties": False,
                },
                "strict": True,
            },
        }

    async def execute(self, **kwargs) -> dict[str, Any]:
        trace_id = self._trace_id()
        return await asyncio.to_thread(
            self.manager.git_add,
            trace_id,
            kwargs["path"],
            kwargs["files"],
        )


class DaytonaGitCommitTool(DaytonaTool):
    def __init__(self, manager: DaytonaSandboxManager):
        super().__init__(manager)
        self.name = "daytona_git_commit"
        self.description = "Create a commit in a Daytona sandbox git repository."

    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Repository path in sandbox."},
                        "message": {"type": "string", "description": "Commit message."},
                        "author": {"type": "string", "description": "Optional author name."},
                        "email": {"type": "string", "description": "Optional author email."},
                    },
                    "required": ["path", "message"],
                    "additionalProperties": False,
                },
                "strict": True,
            },
        }

    async def execute(self, **kwargs) -> dict[str, Any]:
        trace_id = self._trace_id()
        return await asyncio.to_thread(
            self.manager.git_commit,
            trace_id,
            kwargs["path"],
            kwargs["message"],
            kwargs.get("author"),
            kwargs.get("email"),
        )


class DaytonaGitPullTool(DaytonaTool):
    def __init__(self, manager: DaytonaSandboxManager):
        super().__init__(manager)
        self.name = "daytona_git_pull"
        self.description = "Pull latest changes in a Daytona sandbox git repository."

    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string", "description": "Repository path in sandbox."}},
                    "required": ["path"],
                    "additionalProperties": False,
                },
                "strict": True,
            },
        }

    async def execute(self, **kwargs) -> dict[str, Any]:
        trace_id = self._trace_id()
        return await asyncio.to_thread(self.manager.git_pull, trace_id, kwargs["path"])


class DaytonaGitPushTool(DaytonaTool):
    def __init__(self, manager: DaytonaSandboxManager):
        super().__init__(manager)
        self.name = "daytona_git_push"
        self.description = "Push commits from a Daytona sandbox git repository."

    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string", "description": "Repository path in sandbox."}},
                    "required": ["path"],
                    "additionalProperties": False,
                },
                "strict": True,
            },
        }

    async def execute(self, **kwargs) -> dict[str, Any]:
        trace_id = self._trace_id()
        return await asyncio.to_thread(self.manager.git_push, trace_id, kwargs["path"])


class DaytonaComputerStartTool(DaytonaTool):
    def __init__(self, manager: DaytonaSandboxManager):
        super().__init__(manager)
        self.name = "daytona_computer_start"
        self.description = "Start Daytona computer-use session and return VNC preview URL."

    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
                "strict": True,
            },
        }

    async def execute(self, **kwargs) -> dict[str, Any]:
        trace_id = self._trace_id()
        return await asyncio.to_thread(self.manager.computer_start, trace_id)


class DaytonaComputerStatusTool(DaytonaTool):
    def __init__(self, manager: DaytonaSandboxManager):
        super().__init__(manager)
        self.name = "daytona_computer_status"
        self.description = "Check Daytona computer-use status for current trace sandbox."

    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
                "strict": True,
            },
        }

    async def execute(self, **kwargs) -> dict[str, Any]:
        trace_id = self._trace_id()
        return await asyncio.to_thread(self.manager.computer_status, trace_id)


class DaytonaBrowserNavigateTool(DaytonaTool):
    def __init__(self, manager: DaytonaSandboxManager):
        super().__init__(manager)
        self.name = "daytona_browser_navigate"
        self.description = "Launch a browser in Daytona sandbox desktop and navigate to a URL."

    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "description": "Target URL to open in sandbox browser."},
                        "browser": {
                            "type": "string",
                            "description": "Optional browser executable hint, e.g. chromium, google-chrome, firefox.",
                        },
                    },
                    "required": ["url"],
                    "additionalProperties": False,
                },
                "strict": True,
            },
        }

    async def execute(self, **kwargs) -> dict[str, Any]:
        trace_id = self._trace_id()
        return await asyncio.to_thread(
            self.manager.browser_navigate,
            trace_id,
            kwargs["url"],
            kwargs.get("browser"),
        )


class DaytonaComputerStopTool(DaytonaTool):
    def __init__(self, manager: DaytonaSandboxManager):
        super().__init__(manager)
        self.name = "daytona_computer_stop"
        self.description = "Stop Daytona computer-use session for current trace sandbox."

    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
                "strict": True,
            },
        }

    async def execute(self, **kwargs) -> dict[str, Any]:
        trace_id = self._trace_id()
        return await asyncio.to_thread(self.manager.computer_stop, trace_id)


class DaytonaComputerMouseTool(DaytonaTool):
    def __init__(self, manager: DaytonaSandboxManager):
        super().__init__(manager)
        self.name = "daytona_computer_mouse"
        self.description = "Control mouse in Daytona computer-use session."

    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["move", "click", "drag", "scroll"],
                            "description": "Mouse action.",
                        },
                        "x": {"type": "integer"},
                        "y": {"type": "integer"},
                        "start_x": {"type": "integer"},
                        "start_y": {"type": "integer"},
                        "end_x": {"type": "integer"},
                        "end_y": {"type": "integer"},
                        "button": {"type": "string", "enum": ["left", "right", "middle"]},
                        "direction": {"type": "string", "enum": ["up", "down"]},
                        "amount": {"type": "integer"},
                        "double": {"type": "boolean"},
                    },
                    "required": ["action"],
                    "additionalProperties": False,
                },
                "strict": True,
            },
        }

    async def execute(self, **kwargs) -> dict[str, Any]:
        trace_id = self._trace_id()
        return await asyncio.to_thread(
            self.manager.computer_mouse,
            trace_id,
            action=kwargs["action"],
            x=kwargs.get("x"),
            y=kwargs.get("y"),
            start_x=kwargs.get("start_x"),
            start_y=kwargs.get("start_y"),
            end_x=kwargs.get("end_x"),
            end_y=kwargs.get("end_y"),
            button=kwargs.get("button"),
            direction=kwargs.get("direction"),
            amount=kwargs.get("amount"),
            double=kwargs.get("double", False),
        )


class DaytonaComputerKeyboardTool(DaytonaTool):
    def __init__(self, manager: DaytonaSandboxManager):
        super().__init__(manager)
        self.name = "daytona_computer_keyboard"
        self.description = "Control keyboard in Daytona computer-use session."

    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["type", "press", "hotkey"],
                            "description": "Keyboard action.",
                        },
                        "text": {"type": "string"},
                        "key": {"type": "string"},
                        "keys": {
                            "anyOf": [
                                {"type": "array", "items": {"type": "string"}},
                                {"type": "string"},
                            ]
                        },
                        "modifiers": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["action"],
                    "additionalProperties": False,
                },
                "strict": True,
            },
        }

    async def execute(self, **kwargs) -> dict[str, Any]:
        trace_id = self._trace_id()
        return await asyncio.to_thread(
            self.manager.computer_keyboard,
            trace_id,
            action=kwargs["action"],
            text=kwargs.get("text"),
            key=kwargs.get("key"),
            keys=kwargs.get("keys"),
            modifiers=kwargs.get("modifiers"),
        )


class DaytonaComputerScreenshotTool(DaytonaTool):
    def __init__(self, manager: DaytonaSandboxManager):
        super().__init__(manager)
        self.name = "daytona_computer_screenshot"
        self.description = "Take screenshot in Daytona computer-use session."

    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "mode": {
                            "type": "string",
                            "enum": ["full", "region", "compressed", "compressed_region"],
                            "description": "Screenshot mode.",
                        },
                        "x": {"type": "integer"},
                        "y": {"type": "integer"},
                        "width": {"type": "integer"},
                        "height": {"type": "integer"},
                        "quality": {"type": "integer"},
                        "image_format": {"type": "string", "enum": ["jpeg", "png", "webp"]},
                    },
                    "required": [],
                    "additionalProperties": False,
                },
                "strict": True,
            },
        }

    async def execute(self, **kwargs) -> dict[str, Any]:
        trace_id = self._trace_id()
        return await asyncio.to_thread(
            self.manager.computer_screenshot,
            trace_id,
            mode=kwargs.get("mode", "full"),
            x=kwargs.get("x"),
            y=kwargs.get("y"),
            width=kwargs.get("width"),
            height=kwargs.get("height"),
            quality=kwargs.get("quality", 75),
            image_format=kwargs.get("image_format", "jpeg"),
        )


class DaytonaVncViewTool(DaytonaTool):
    def __init__(self, manager: DaytonaSandboxManager):
        super().__init__(manager)
        self.name = "daytona_vnc_view"
        self.description = "Get Daytona noVNC preview URL for current trace sandbox."

    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
                "strict": True,
            },
        }

    async def execute(self, **kwargs) -> dict[str, Any]:
        trace_id = self._trace_id()
        return await asyncio.to_thread(self.manager.get_vnc_view, trace_id)


def register_daytona_tools(registry: ToolRegistry, config: Mapping[str, Any] | None = None) -> None:
    manager = get_daytona_manager(config or {})
    tools: list[Tool] = [
        DaytonaFsListTool(manager),
        DaytonaFsReadTool(manager),
        DaytonaFsWriteTool(manager),
        DaytonaFsStatTool(manager),
        DaytonaGitCloneTool(manager),
        DaytonaGitStatusTool(manager),
        DaytonaGitAddTool(manager),
        DaytonaGitCommitTool(manager),
        DaytonaGitPullTool(manager),
        DaytonaGitPushTool(manager),
        DaytonaComputerStartTool(manager),
        DaytonaComputerStatusTool(manager),
        DaytonaBrowserNavigateTool(manager),
        DaytonaComputerStopTool(manager),
        DaytonaComputerMouseTool(manager),
        DaytonaComputerKeyboardTool(manager),
        DaytonaComputerScreenshotTool(manager),
        DaytonaVncViewTool(manager),
    ]
    for tool in tools:
        registry.register(tool)
