from __future__ import annotations

import glob as globlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from .fs_tools import DEFAULT_MAX_ITEMS
from .path_sandbox import (
    assert_path_in_allowed_roots,
    resolve_allowed_path,
    tool_result_root,
    workspace_root,
)
from .tool import Tool


UID_RE = re.compile(r"\buid=([A-Za-z0-9_:-]+)\b")


def _safe_int(value: Any, default: int, min_value: int = 0) -> int:
    try:
        parsed = int(value)
        if parsed < min_value:
            return default
        return parsed
    except Exception:
        return default


def _build_error(error: str) -> str:
    return json.dumps({"ok": False, "error": error}, ensure_ascii=False)


def _to_rel_display(path: Path) -> str:
    root = workspace_root()
    try:
        return path.resolve().relative_to(root).as_posix()
    except Exception:
        return path.resolve().as_posix()


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
        target = resolve_allowed_path(raw, field_name="path")
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
    root = workspace_root()
    if not pattern:
        return []
    p = Path(pattern)
    if p.is_absolute() or ".." in p.parts:
        raise PermissionError("glob_must_be_relative_and_without_parent_segments")
    raw_matches = globlib.glob(pattern, root_dir=str(root), recursive=True)
    files: List[Path] = []
    for m in raw_matches:
        candidate = resolve_allowed_path(m, field_name="glob_match")
        if candidate.is_file():
            files.append(candidate)
    return files


def _find_rg_binary() -> Optional[str]:
    env_path = os.getenv("TOOL_RESULT_RG_PATH")
    if env_path and Path(env_path).exists():
        return env_path
    resolved = shutil.which("rg")
    return resolved


def _search_with_rg(
    *,
    files: List[Path],
    normalized_queries: List[str],
    is_regex: bool,
    case_sensitive: bool,
) -> List[Tuple[Path, int, str]]:
    rg_path = _find_rg_binary()
    if not rg_path:
        raise RuntimeError("rg_not_found")

    cmd = [rg_path, "--json", "--line-number", "--color", "never"]
    if not case_sensitive:
        cmd.append("-i")
    if not is_regex:
        cmd.append("-F")
    for q in normalized_queries:
        cmd.extend(["-e", q])
    cmd.append("--")
    cmd.extend(str(f) for f in files)

    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    if proc.returncode not in (0, 1):
        stderr = (proc.stderr or "").strip()
        raise RuntimeError(f"rg_failed: {stderr}" if stderr else "rg_failed")

    hits: List[Tuple[Path, int, str]] = []
    for line in proc.stdout.splitlines():
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if obj.get("type") != "match":
            continue
        data = obj.get("data") or {}
        line_no = int(data.get("line_number") or 0)
        if line_no <= 0:
            continue
        path_text = ((data.get("path") or {}).get("text") or "").strip()
        if not path_text:
            continue
        line_text = ((data.get("lines") or {}).get("text") or "").rstrip("\n")
        hits.append((Path(path_text).resolve(), line_no, line_text))
    return hits


def _search_with_python(
    *,
    files: List[Path],
    normalized_queries: List[str],
    is_regex: bool,
    case_sensitive: bool,
) -> List[Tuple[Path, int, str]]:
    flags = 0 if case_sensitive else re.IGNORECASE
    patterns: List[Tuple[str, Optional[re.Pattern[str]]]] = []
    if is_regex:
        for q in normalized_queries:
            patterns.append((q, re.compile(q, flags)))
    else:
        for q in normalized_queries:
            patterns.append((q if case_sensitive else q.lower(), None))

    hits: List[Tuple[Path, int, str]] = []
    for file_path in files:
        try:
            with file_path.open("r", encoding="utf-8", errors="replace") as f:
                for idx, line in enumerate(f, start=1):
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
                    if matched:
                        hits.append((file_path.resolve(), idx, line.rstrip("\n")))
        except OSError:
            continue
    return hits


class TextSearchTool(Tool):
    def __init__(self):
        super().__init__()
        self.name = "text_search"
        self.description = "Search plain text in workspace files (ripgrep preferred, sandboxed paths only)."

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
            skipped_out_of_roots = 0
            allowed_roots = (workspace_root(), tool_result_root())
            for fp in files:
                try:
                    resolved_fp = fp.resolve()
                    assert_path_in_allowed_roots(resolved_fp, allowed_roots)
                except Exception:
                    skipped_out_of_roots += 1
                    continue

                key = str(resolved_fp)
                if key in seen_files:
                    continue
                seen_files.add(key)
                unique_files.append(resolved_fp)

            if not unique_files:
                return json.dumps(
                    {
                        "ok": True,
                        "matches": [],
                        "truncated": False,
                        "summary": {
                            "queries": normalized_queries,
                            "searched_files": 0,
                            "returned_matches": 0,
                            "total_matches": 0,
                            "engine": "none",
                            "skipped_out_of_roots": skipped_out_of_roots,
                        },
                    },
                    ensure_ascii=False,
                )

            engine = "rg"
            try:
                raw_hits = _search_with_rg(
                    files=unique_files,
                    normalized_queries=normalized_queries,
                    is_regex=bool(is_regex),
                    case_sensitive=bool(case_sensitive),
                )
            except Exception:
                engine = "python"
                raw_hits = _search_with_python(
                    files=unique_files,
                    normalized_queries=normalized_queries,
                    is_regex=bool(is_regex),
                    case_sensitive=bool(case_sensitive),
                )

            # Deduplicate by (path, line)
            deduped_hits: List[Tuple[Path, int, str]] = []
            seen_hit_keys: Set[Tuple[str, int]] = set()
            for fp, line_no, line_text in raw_hits:
                key = (str(fp), line_no)
                if key in seen_hit_keys:
                    continue
                seen_hit_keys.add(key)
                deduped_hits.append((fp, line_no, line_text))

            file_cache: Dict[str, List[str]] = {}
            all_matches: List[Dict[str, Any]] = []

            for file_path, line_no, line_text in deduped_hits:
                path_key = str(file_path)
                if path_key not in file_cache:
                    try:
                        with file_path.open("r", encoding="utf-8", errors="replace") as f:
                            file_cache[path_key] = f.readlines()
                    except OSError:
                        file_cache[path_key] = []
                lines = file_cache[path_key]
                total = len(lines)
                start_idx = max(1, line_no - ctx)
                end_idx = min(total, line_no + ctx)
                snippet = "".join(lines[start_idx - 1: end_idx]).rstrip("\n")

                uid_match = UID_RE.search(line_text)
                hit = {
                    "path": _to_rel_display(file_path),
                    "line": line_no,
                    "text": snippet,
                }
                if uid_match:
                    hit["uid"] = uid_match.group(1)
                all_matches.append(hit)

            payload = {
                "ok": True,
                "matches": all_matches[:limit],
                "truncated": len(all_matches) > limit,
                "summary": {
                    "queries": normalized_queries,
                    "searched_files": len(unique_files),
                    "returned_matches": min(len(all_matches), limit),
                    "total_matches": len(all_matches),
                    "engine": engine,
                    "skipped_out_of_roots": skipped_out_of_roots,
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
                        "path": {"type": "string", "description": "Single relative file/directory path."},
                        "paths": {"type": "array", "items": {"type": "string"}, "description": "Multiple relative paths."},
                        "glob": {"type": "string", "description": "Relative glob pattern to collect files first."},
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
