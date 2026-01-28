from __future__ import annotations

import json
from pathlib import Path
from typing import List

from .tool import Tool
from .bash import Instance


class ReadTool(Tool):
    def __init__(self):
        super().__init__()
        self.name = "read"
        self.description = "Read the head or tail of a text file with limits."

    async def execute(
        self,
        path: str,
        mode: str = "head",
        max_lines: int = 200,
        max_chars: int = 8000,
    ) -> str:
        if not path:
            return "[]"

        p = Path(path)
        if not Instance.contains_path(str(p)):
            return "[]"

        if max_lines <= 0:
            max_lines = 200
        if max_chars <= 0:
            max_chars = 8000

        try:
            lines: List[str] = []
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                if mode == "tail":
                    # Read all lines but cap memory by slicing
                    all_lines = f.readlines()
                    lines = all_lines[-max_lines:]
                else:
                    for _ in range(max_lines):
                        line = f.readline()
                        if not line:
                            break
                        lines.append(line)
        except OSError:
            return "[]"

        content = "".join(lines)
        truncated = False
        if len(content) > max_chars:
            content = content[:max_chars]
            truncated = True

        payload = {
            "path": str(p),
            "mode": mode,
            "max_lines": max_lines,
            "max_chars": max_chars,
            "truncated": truncated,
            "content": content,
        }
        return json.dumps(payload, ensure_ascii=False)

    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "File path to read.",
                        },
                        "mode": {
                            "type": "string",
                            "description": "Read mode: 'head' or 'tail'.",
                            "enum": ["head", "tail"],
                            "default": "head",
                        },
                        "max_lines": {
                            "type": "integer",
                            "description": "Maximum number of lines to read.",
                            "default": 200,
                        },
                        "max_chars": {
                            "type": "integer",
                            "description": "Maximum number of characters to return.",
                            "default": 8000,
                        },
                    },
                    "required": ["path"],
                    "additionalProperties": False,
                },
                "strict": True,
            },
        }
