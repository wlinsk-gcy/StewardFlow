from __future__ import annotations

import json
import os
from pathlib import Path
from typing import List

from .tool import Tool
from .bash import Instance


class LsTool(Tool):
    def __init__(self):
        super().__init__()
        self.name = "ls"
        self.description = (
            "List files and directories in a path with optional recursion and limits."
        )

    async def execute(
        self,
        path: str = ".",
        recursive: bool = False,
        max_items: int = 200,
        include_dirs: bool = True,
        include_files: bool = True,
    ) -> str:
        root = Path(path)
        if not Instance.contains_path(str(root)):
            return "[]"

        results: List[dict] = []

        def add_item(p: Path) -> None:
            if max_items > 0 and len(results) >= max_items:
                return
            if p.is_dir() and not include_dirs:
                return
            if p.is_file() and not include_files:
                return
            try:
                stat = p.stat()
            except OSError:
                return
            results.append(
                {
                    "path": str(p),
                    "type": "dir" if p.is_dir() else "file",
                    "size": stat.st_size,
                }
            )

        if root.is_file():
            add_item(root)
        else:
            if recursive:
                for dirpath, dirnames, filenames in os.walk(root):
                    for name in dirnames:
                        add_item(Path(dirpath) / name)
                        if max_items > 0 and len(results) >= max_items:
                            break
                    for name in filenames:
                        add_item(Path(dirpath) / name)
                        if max_items > 0 and len(results) >= max_items:
                            break
                    if max_items > 0 and len(results) >= max_items:
                        break
            else:
                try:
                    for name in os.listdir(root):
                        add_item(root / name)
                        if max_items > 0 and len(results) >= max_items:
                            break
                except OSError:
                    pass

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
                        "path": {
                            "type": "string",
                            "description": "Directory or file path to list.",
                        },
                        "recursive": {
                            "type": "boolean",
                            "description": "Whether to list recursively.",
                            "default": False,
                        },
                        "max_items": {
                            "type": "integer",
                            "description": "Maximum number of items to return.",
                            "default": 200,
                        },
                        "include_dirs": {
                            "type": "boolean",
                            "description": "Include directories in results.",
                            "default": True,
                        },
                        "include_files": {
                            "type": "boolean",
                            "description": "Include files in results.",
                            "default": True,
                        },
                    },
                    "additionalProperties": False,
                },
                "strict": True,
            },
        }
