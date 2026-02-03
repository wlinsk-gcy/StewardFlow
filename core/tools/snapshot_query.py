import json
import os
import re
import hashlib
from typing import List, Optional, Tuple, Dict, Any
from .tool import Tool

# 你的历史文件
DEFAULT_LATEST_PATH = "data/snapshot_latest.txt"

# wait_for / take_snapshot 都会有这一段 marker
SNAPSHOT_MARKER = "## Latest page snapshot"

# a11y 行：缩进 + uid=... role "name" ...
A11Y_LINE_RE = re.compile(r'^(\s*)uid=([^\s]+)\s+([^\s]+)(?:\s+"([^"]*)")?(.*)$')


def _sha1_12(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8", errors="ignore")).hexdigest()[:12]


def _read_latest_lines(path: str) -> List[str]:
    if not os.path.exists(path):
        raise FileNotFoundError(f"snapshot latest file not found: {path}")
    with open(path, "r", encoding="utf-8") as f:
        return f.read().splitlines()

def _split_snapshot(lines: List[str]) -> Tuple[List[str], List[str], int]:
    """
    将文件按 marker 拆分：
    - header_lines: marker 之前 + marker 行
    - snapshot_lines: marker 之后（真正的 a11y 树）
    """
    marker_index = -1
    for i, ln in enumerate(lines):
        if ln.strip() == SNAPSHOT_MARKER:
            marker_index = i
            break

    if marker_index == -1:
        # 没找到 marker：认为整个文件都可搜索，header 为空
        return [], lines, -1

    header_lines = lines[:marker_index + 1]
    snapshot_lines = lines[marker_index + 1:]
    return header_lines, snapshot_lines, marker_index

def _extract_found_line(header_lines: List[str]) -> str:
    """
    wait_for 特有：Element with text "xxx" found.
    take_snapshot 一般没有。没找到就返回空串。
    """
    for ln in header_lines:
        if ln.startswith("Element with text "):
            return ln
    return ""


def _parse_a11y_line(line: str) -> Optional[Tuple[int, str, str, str]]:
    """
    返回 (indent, uid, role, name)
    """
    m = A11Y_LINE_RE.match(line)
    if not m:
        return None
    indent = len(m.group(1) or "")
    uid = (m.group(2) or "").strip()
    role = (m.group(3) or "").strip()
    name = (m.group(4) or "").strip() if m.group(4) else ""
    return indent, uid, role, name


def _extract_ancestor_chain(lines: List[str], start_index: int, max_ancestors: int = 12) -> List[str]:
    """
    从 start_index 往上找缩进逐步变小的节点，当作祖先链。
    """
    chain: List[str] = []
    cur = _parse_a11y_line(lines[start_index])
    if not cur:
        return chain

    want_indent = cur[0]
    for i in range(start_index - 1, -1, -1):
        p = _parse_a11y_line(lines[i])
        if not p:
            continue
        p_indent = p[0]
        if p_indent < want_indent:
            chain.append(lines[i])
            want_indent = p_indent
            if len(chain) >= max_ancestors:
                break
            if want_indent == 0:
                break

    chain.reverse()
    return chain


def _query_by_uid(
        lines: List[str],
        uid: str,
        max_lines: int = 200,
        include_ancestors: bool = True,
) -> Dict[str, Any]:
    hit_idx = None
    hit_indent = None

    for i, ln in enumerate(lines):
        p = _parse_a11y_line(ln)
        if p and p[1] == uid:
            hit_idx = i
            hit_indent = p[0]
            break

    if hit_idx is None:
        return {"found": False, "reason": f"uid not found: {uid}", "text": ""}

    out: List[str] = []

    if include_ancestors:
        ancestors = _extract_ancestor_chain(lines, hit_idx)
        if ancestors:
            out.extend(ancestors)
            out.append("...(ancestors above)...")

    out.append(lines[hit_idx])

    # 子树：后续缩进 > hit_indent 的连续行
    for j in range(hit_idx + 1, len(lines)):
        p = _parse_a11y_line(lines[j])
        if not p:
            # 非 a11y 行：少量保留，避免丢标题等
            if len(out) < max_lines:
                out.append(lines[j])
            continue

        indent = p[0]
        if indent <= (hit_indent or 0):
            break

        out.append(lines[j])
        if len(out) >= max_lines:
            break

    truncated = len(out) >= max_lines
    return {
        "found": True,
        "mode": "uid_subtree",
        "uid": uid,
        "truncated": truncated,
        "returned_lines": len(out),
        "text": "\n".join(out[:max_lines]),
    }

def _query_by_keyword(
        lines: List[str],
        keyword: str,
        max_lines: int = 200,
        context_lines: int = 8,
        case_insensitive: bool = True,
) -> Dict[str, Any]:
    if not keyword:
        return {"found": False, "reason": "empty keyword", "text": ""}

    key = keyword.lower() if case_insensitive else keyword
    hits: List[int] = []

    for i, ln in enumerate(lines):
        hay = ln.lower() if case_insensitive else ln
        if key in hay:
            hits.append(i)

    if not hits:
        return {
            "found": False,
            "mode": "keyword_grep",
            "keyword": keyword,
            "matched": 0,
            "text": "",
        }

    # 命中行 -> 区间
    intervals: List[Tuple[int, int]] = []
    for i in hits:
        a = max(0, i - context_lines)
        b = min(len(lines) - 1, i + context_lines)
        intervals.append((a, b))

    # 合并重叠区间
    intervals.sort()
    merged: List[Tuple[int, int]] = []
    cur_a, cur_b = intervals[0]
    for a, b in intervals[1:]:
        if a <= cur_b + 1:
            cur_b = max(cur_b, b)
        else:
            merged.append((cur_a, cur_b))
            cur_a, cur_b = a, b
    merged.append((cur_a, cur_b))

    out: List[str] = []
    matched = 0

    for a, b in merged:
        out.append(f"...(match window {a}-{b})...")
        for i in range(a, b + 1):
            ln = lines[i]
            hay = ln.lower() if case_insensitive else ln
            if key in hay:
                matched += 1
                out.append(f">>> {ln}")  # 标记命中
            else:
                out.append(ln)

            if len(out) >= max_lines:
                return {
                    "found": True,
                    "mode": "keyword_grep",
                    "keyword": keyword,
                    "matched": matched,
                    "truncated": True,
                    "returned_lines": max_lines,
                    "text": "\n".join(out[:max_lines]),
                }

    return {
        "found": True,
        "mode": "keyword_grep",
        "keyword": keyword,
        "matched": matched,
        "truncated": False,
        "returned_lines": len(out),
        "text": "\n".join(out),
    }


def snapshot_query_latest(
        *,
        latest_path: str = DEFAULT_LATEST_PATH,
        uid: Optional[str] = None,
        keyword: Optional[str] = None,
        max_lines: int = 200,
        context_lines: int = 8,
        include_ancestors: bool = True,
        search_scope: str = "snapshot",  # "snapshot" | "all"
) -> Dict[str, Any]:
    """
    通用查询器：
    - latest_path 可以是 snapshot_latest.txt 或 wait_for_log_latest.txt
    - 默认 search_scope="snapshot"：只查 marker 后的 snapshot 区域（强烈推荐）
    """
    if not uid and not keyword:
        raise ValueError("either uid or keyword must be provided")

    lines = _read_latest_lines(latest_path)
    header_lines, snapshot_lines, marker_index = _split_snapshot(lines)

    if search_scope == "all":
        target_lines = lines
    else:
        target_lines = snapshot_lines

    # 建议只对 snapshot_lines 算 hash：header 波动不影响 snapshot_id
    raw_text = "\n".join(snapshot_lines if snapshot_lines else lines)
    snapshot_id = _sha1_12(raw_text)

    if uid:
        result = _query_by_uid(target_lines, uid, max_lines=max_lines, include_ancestors=include_ancestors)
    else:
        result = _query_by_keyword(target_lines, keyword or "", max_lines=max_lines, context_lines=context_lines)

    return {
        "type": "snapshot_query_result",
        "latest_path": latest_path,
        "snapshot_id": snapshot_id,
        "meta": {
            "marker_index": marker_index,
            "header_lines": len(header_lines),
            "snapshot_lines": len(snapshot_lines),
            "found_line": _extract_found_line(header_lines),
            "search_scope": search_scope,
        },
        "query": {
            "uid": uid,
            "keyword": keyword,
            "max_lines": max_lines,
            "context_lines": context_lines,
            "include_ancestors": include_ancestors,
            "search_scope": search_scope,
        },
        "result": result,
    }


class SnapshotQueryTool(Tool):
    def __init__(self):
        super().__init__()
        self.name = "snapshot_query"
        self.description = (
            "Query latest a11y snapshot file by uid subtree or keyword grep. "
            "Supports both take_snapshot (snapshot_latest.txt) and wait_for logs (wait_for_log_latest.txt) "
            "via latest_path. Default searches only within '## Latest page snapshot' section."
        )

    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "latest_path": {
                            "type": "string",
                            "default": DEFAULT_LATEST_PATH,
                            "description": "Path to latest snapshot file (e.g., data/snapshot_latest.txt or data/wait_for_log_latest.txt).",
                        },
                        "uid": {"type": "string", "description": "Find subtree by uid (preferred)."},
                        "keyword": {"type": "string", "description": "Search by keyword (grep-like)."},
                        "max_lines": {"type": "integer", "default": 200, "minimum": 20, "maximum": 2000},
                        "context_lines": {"type": "integer", "default": 8, "minimum": 0, "maximum": 50},
                        "include_ancestors": {"type": "boolean", "default": True},
                        "search_scope": {
                            "type": "string",
                            "enum": ["snapshot", "all"],
                            "default": "snapshot",
                            "description": "Search within snapshot only (after marker) or the full file.",
                        },
                    },
                    "required": [],
                    "additionalProperties": False,
                },
                "strict": True,
            },
        }

    async def execute(self, **kwargs) -> str:
        payload = snapshot_query_latest(
            latest_path=kwargs.get("latest_path", DEFAULT_LATEST_PATH),
            uid=kwargs.get("uid"),
            keyword=kwargs.get("keyword"),
            max_lines=int(kwargs.get("max_lines", 60)),
            context_lines=int(kwargs.get("context_lines", 2)),
            include_ancestors=bool(kwargs.get("include_ancestors", True)),
            search_scope=kwargs.get("search_scope", "snapshot"),
        )
        return json.dumps(payload, ensure_ascii=False)
