from __future__ import annotations

import json
import os
import re
import glob as globlib
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from .fs_tools import DEFAULT_MAX_ITEMS, _build_error, _resolve_workspace_path, _to_rel_display, _write_artifact
from .tool import Instance, Tool


UID_RE = re.compile(r"\buid=([A-Za-z0-9_:-]+)\b")


def _safe_int(value: Any, default: int, min_value: int = 0) -> int:
    try:
        parsed = int(value)
        if parsed < min_value:
            return default
        return parsed
    except Exception:
        return default


def _normalize_queries(query: Optional[str], queries: Optional[List[Any]]) -> List[str]:
    out: List[str] = []
    if query and isinstance(query, str):
        q = query.strip()
        if q:
            out.append(q)
    for item in queries or []:
        if not isinstance(item, str):
            continue
        q = item.strip()
        if q:
            out.append(q)
    # Keep stable order while deduplicating.
    seen: Set[str] = set()
    normalized: List[str] = []
    for q in out:
        if q in seen:
            continue
        seen.add(q)
        normalized.append(q)
    return normalized


def _iter_candidate_files(path: Optional[str], paths: Optional[List[str]], recursive: bool) -> List[Path]:
    candidates: List[Path] = []
    provided = []
    if path:
        provided.append(path)
    provided.extend(paths or [])
    if not provided:
        provided = ["."]

    for raw in provided:
        target = _resolve_workspace_path(raw)
        if target.is_file():
            candidates.append(target)
            continue
        if target.is_dir():
            if recursive:
                for dirpath, _, filenames in os.walk(target):
                    base = Path(dirpath)
                    for name in filenames:
                        candidates.append(base / name)
            else:
                try:
                    for child in target.iterdir():
                        if child.is_file():
                            candidates.append(child)
                except OSError:
                    continue
    return candidates


def _iter_glob_files(pattern: str) -> List[Path]:
    root = Path(Instance.directory).resolve()
    if not pattern:
        return []
    raw_matches = globlib.glob(pattern, root_dir=str(root), recursive=True)
    files: List[Path] = []
    for m in raw_matches:
        p = (root / m).resolve()
        if not Instance.contains_path(str(p)):
            continue
        if p.is_file():
            files.append(p)
    return files


class TextSearchTool(Tool):
    def __init__(self):
        super().__init__()
        self.name = "text_search"
        self.description = "Search plain text in workspace files using semantic, OS-agnostic parameters."

    async def execute(
        self,
        path: Optional[str] = None,
        paths: Optional[List[str]] = None,
        glob: Optional[str] = None,
        query: Optional[str] = None,
        queries: Optional[List[str]] = None,
        is_regex: bool = False,
        context_lines: int = 0,
        max_matches: int = DEFAULT_MAX_ITEMS,
        recursive: bool = True,
        case_sensitive: bool = False,
        **kwargs,
    ) -> str:
        del kwargs
        try:
            normalized_queries = _normalize_queries(query=query, queries=queries)
            if not normalized_queries:
                return _build_error("query_or_queries_required")

            limit = _safe_int(max_matches, DEFAULT_MAX_ITEMS, min_value=1)
            ctx = _safe_int(context_lines, 0, min_value=0)

            files: List[Path] = []
            if glob:
                files.extend(_iter_glob_files(glob))
            files.extend(_iter_candidate_files(path=path, paths=paths, recursive=bool(recursive)))

            unique_files: List[Path] = []
            seen_files: Set[str] = set()
            for fp in files:
                key = str(fp)
                if key in seen_files:
                    continue
                seen_files.add(key)
                unique_files.append(fp)

            flags = 0 if case_sensitive else re.IGNORECASE
            patterns: List[Tuple[str, Optional[re.Pattern[str]]]] = []
            if is_regex:
                for q in normalized_queries:
                    patterns.append((q, re.compile(q, flags)))
            else:
                for q in normalized_queries:
                    patterns.append((q if case_sensitive else q.lower(), None))

            all_matches: List[Dict[str, Any]] = []

            for file_path in unique_files:
                try:
                    with file_path.open("r", encoding="utf-8", errors="replace") as f:
                        lines = f.readlines()
                except OSError:
                    continue

                total = len(lines)
                for idx, line in enumerate(lines, start=1):
                    line_for_match = line if case_sensitive else line.lower()
                    matched = False
                    for pattern_text, compiled in patterns:
                        if compiled is not None:
                            if compiled.search(line):
                                matched = True
                                break
                        else:
                            if pattern_text in line_for_match:
                                matched = True
                                break
                    if not matched:
                        continue

                    start_idx = max(1, idx - ctx)
                    end_idx = min(total, idx + ctx)
                    snippet = "".join(lines[start_idx - 1 : end_idx]).rstrip("\n")
                    uid_match = UID_RE.search(line)

                    hit = {
                        "path": _to_rel_display(file_path),
                        "line": idx,
                        "text": snippet,
                    }
                    if uid_match:
                        hit["uid"] = uid_match.group(1)
                    all_matches.append(hit)

            truncated = len(all_matches) > limit
            preview_matches = all_matches[:limit]

            artifact_path = None
            if truncated:
                artifact_path = _write_artifact(
                    "text_search",
                    {
                        "queries": normalized_queries,
                        "path": path,
                        "paths": paths,
                        "glob": glob,
                        "matches": all_matches,
                    },
                )

            payload = {
                "ok": True,
                "matches": preview_matches,
                "truncated": truncated,
                "artifact_path": artifact_path,
                "summary": {
                    "queries": normalized_queries,
                    "searched_files": len(unique_files),
                    "returned_matches": len(preview_matches),
                    "total_matches": len(all_matches),
                },
            }
            return json.dumps(payload, ensure_ascii=False)
        except re.error as exc:
            return _build_error(f"invalid_regex: {str(exc)}")
        except Exception as exc:
            return _build_error(str(exc))

    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Single file/directory path."},
                        "paths": {"type": "array", "items": {"type": "string"}, "description": "Multiple paths."},
                        "glob": {"type": "string", "description": "Glob pattern to collect files first."},
                        "query": {"type": "string", "description": "Single query."},
                        "queries": {"type": "array", "items": {"type": "string"}, "description": "Batch queries."},
                        "is_regex": {"type": "boolean", "default": False},
                        "context_lines": {"type": "integer", "default": 0, "minimum": 0, "maximum": 20},
                        "max_matches": {"type": "integer", "default": DEFAULT_MAX_ITEMS, "minimum": 1, "maximum": 5000},
                        "recursive": {"type": "boolean", "default": True},
                        "case_sensitive": {"type": "boolean", "default": False},
                    },
                    "additionalProperties": False,
                },
                "strict": True,
            },
        }
