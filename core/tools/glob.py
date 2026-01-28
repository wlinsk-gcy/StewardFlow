from __future__ import annotations

import json
import os
import glob as globlib
from pathlib import Path
from typing import List

from .tool import Tool
from .bash import Instance


class GlobTool(Tool):
    def __init__(self):
        super().__init__()
        self.name = "glob"
        self.description = "List files matching a glob pattern under a base path."

    async def execute(
        self,
        pattern: str,
        base_path: str = ".",
        recursive: bool = True,
        max_items: int = 200,
    ) -> str:
        if not pattern:
            return "[]"

        base = Path(base_path)
        if not Instance.contains_path(str(base)):
            return "[]"

        search_pattern = str(base / pattern)
        matches = globlib.glob(search_pattern, recursive=recursive)

        results: List[dict] = []
        for m in matches:
            if max_items > 0 and len(results) >= max_items:
                break
            p = Path(m)
            if not Instance.contains_path(str(p)):
                continue
            try:
                stat = p.stat()
            except OSError:
                continue
            results.append(
                {
                    "path": str(p),
                    "type": "dir" if p.is_dir() else "file",
                    "size": stat.st_size,
                }
            )

        return json.dumps(results, ensure_ascii=False)

    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "pattern": {
                            "type": "string",
                            "description": "Glob pattern, e.g. '*.txt' or '**/*.log'.",
                        },
                        "base_path": {
                            "type": "string",
                            "description": "Base directory for the glob search.",
                        },
                        "recursive": {
                            "type": "boolean",
                            "description": "Whether to allow '**' recursive patterns.",
                            "default": True,
                        },
                        "max_items": {
                            "type": "integer",
                            "description": "Maximum number of items to return.",
                            "default": 200,
                        },
                    },
                    "required": ["pattern"],
                    "additionalProperties": False,
                },
                "strict": True,
            },
        }
