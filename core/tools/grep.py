from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import List, Optional

from .tool import Tool
from .bash import Instance


class GrepTool(Tool):
    def __init__(self):
        super().__init__()
        self.name = "grep"
        self.description = (
            "Search text in files under a path. Supports recursive search, "
            "case sensitivity, and max results limiting."
        )

    async def execute(
        self,
        pattern: str,
        path: str = ".",
        recursive: bool = True,
        case_sensitive: bool = False,
        max_results: int = 50,
        max_file_size_kb: int = 2048,
    ) -> str:
        if not pattern:
            return "[]"

        root = Path(path)
        if not Instance.contains_path(str(root)):
            return "[]"

        flags = 0 if case_sensitive else re.IGNORECASE
        try:
            regex = re.compile(pattern, flags)
        except re.error:
            return "[]"

        matches: List[dict] = []

        def search_file(file_path: Path) -> None:
            nonlocal matches
            if max_results > 0 and len(matches) >= max_results:
                return
            try:
                size_kb = file_path.stat().st_size // 1024
                if size_kb > max_file_size_kb:
                    return
                with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                    for idx, line in enumerate(f, start=1):
                        if max_results > 0 and len(matches) >= max_results:
                            return
                        if regex.search(line):
                            matches.append(
                                {
                                    "path": str(file_path),
                                    "line": idx,
                                    "text": line.rstrip(),
                                }
                            )
            except (OSError, UnicodeDecodeError):
                return

        if root.is_file():
            search_file(root)
        else:
            if recursive:
                for dirpath, _, filenames in os.walk(root):
                    for name in filenames:
                        search_file(Path(dirpath) / name)
            else:
                try:
                    for name in os.listdir(root):
                        p = root / name
                        if p.is_file():
                            search_file(p)
                except OSError:
                    pass

        return json.dumps(matches, ensure_ascii=False)

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
                            "description": "Regex pattern to search for.",
                        },
                        "path": {
                            "type": "string",
                            "description": "File or directory path to search.",
                        },
                        "recursive": {
                            "type": "boolean",
                            "description": "Whether to search recursively.",
                            "default": True,
                        },
                        "case_sensitive": {
                            "type": "boolean",
                            "description": "Whether the search is case sensitive.",
                            "default": False,
                        },
                        "max_results": {
                            "type": "integer",
                            "description": "Maximum number of matching lines to return.",
                            "default": 50,
                        },
                        "max_file_size_kb": {
                            "type": "integer",
                            "description": "Skip files larger than this size in KB.",
                            "default": 2048,
                        },
                    },
                    "required": ["pattern"],
                    "additionalProperties": False,
                },
                "strict": True,
            },
        }
