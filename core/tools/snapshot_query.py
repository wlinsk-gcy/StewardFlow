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

def _find_uid_index(lines: List[str], uid: str) -> Optional[int]:
    for i, ln in enumerate(lines):
        p = _parse_a11y_line(ln)
        if p and p[1] == uid:
            return i
    return None

def _query_by_uid_at_index(
    lines: List[str],
    hit_idx: int,
    max_lines: int = 200,
    include_ancestors: bool = True,
) -> Dict[str, Any]:
    """
    在给定 hit_idx 的前提下做 uid_subtree（更稳：避免重复扫描；也便于做 segment 映射）
    注意：子树模式下不再保留非 a11y 行，避免把日志噪声带回来。
    """
    p0 = _parse_a11y_line(lines[hit_idx])
    if not p0:
        return {"found": False, "reason": "hit_idx is not an a11y line", "text": ""}

    hit_indent = p0[0]
    uid = p0[1]

    out: List[str] = []

    if include_ancestors:
        ancestors = _extract_ancestor_chain(lines, hit_idx)
        if ancestors:
            out.extend(ancestors)
            out.append("...(ancestors above)...")

    out.append(lines[hit_idx])

    # 子树：后续缩进 > hit_indent 的连续 a11y 行
    for j in range(hit_idx + 1, len(lines)):
        pj = _parse_a11y_line(lines[j])
        if not pj:
            # 非 a11y 行：直接跳过（不回填噪声）
            continue

        indent = pj[0]
        if indent <= hit_indent:
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
        "returned_lines": min(len(out), max_lines),
        "text": "\n".join(out[:max_lines]),
    }


def _query_by_uid(
    *,
    all_lines: List[str],
    header_lines: List[str],
    snapshot_lines: List[str],
    marker_index: int,
    uid: str,
    max_lines: int = 200,
    include_ancestors: bool = True,
) -> Dict[str, Any]:
    """
    uid 查询：永远先在全文件定位 uid（更稳），再决定在 snapshot 段或全文件上做 subtree。
    """
    hit_idx_all = _find_uid_index(all_lines, uid)
    if hit_idx_all is None:
        return {"found": False, "reason": f"uid not found: {uid}", "text": ""}

    # 如果存在 marker，且命中行在 marker 之后，则映射到 snapshot_lines
    if marker_index != -1 and hit_idx_all > marker_index:
        # snapshot_lines 从 marker_index+1 开始
        hit_idx_seg = hit_idx_all - (marker_index + 1)
        if 0 <= hit_idx_seg < len(snapshot_lines):
            return _query_by_uid_at_index(
                snapshot_lines,
                hit_idx_seg,
                max_lines=max_lines,
                include_ancestors=include_ancestors,
            )
        # 极端情况：映射失败则回退到全文件
        return _query_by_uid_at_index(
            all_lines,
            hit_idx_all,
            max_lines=max_lines,
            include_ancestors=include_ancestors,
        )

    # marker 不存在或命中在 header 段：对全文件做 subtree（一般不期望发生，但兜底）
    return _query_by_uid_at_index(
        all_lines,
        hit_idx_all,
        max_lines=max_lines,
        include_ancestors=include_ancestors,
    )

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
    hit_indices: List[int] = []

    for i, ln in enumerate(lines):
        hay = ln.lower() if case_insensitive else ln
        if key in hay:
            hit_indices.append(i)

    if not hit_indices:
        return {
            "found": False,
            "mode": "keyword_grep",
            "keyword": keyword,
            "matched": 0,
            "truncated": False,
            "returned_lines": 0,
            "text": "",
            "hit_indices": [],
        }

    # 命中行 -> 区间
    intervals: List[Tuple[int, int]] = []
    for i in hit_indices:
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
    truncated = False

    # 方便 compact：把命中索引也带回去（不影响旧逻辑：你可以忽略它）
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
                truncated = True
                break
        if truncated:
            break

    text_out = "\n".join(out[:max_lines])
    return {
        "found": True,
        "mode": "keyword_grep",
        "keyword": keyword,
        "matched": matched,
        "truncated": truncated,
        "returned_lines": min(len(out), max_lines),
        "text": text_out,
        "hit_indices": hit_indices,  # 供 compact 使用
    }

def _compact_top_hits(
    lines: List[str],
    hit_indices: List[int],
    *,
    keyword: str,
    neighbor_lines: int = 1,
    top_hits_limit: int = 8,
    case_insensitive: bool = True,
) -> List[str]:
    """
    compact 结果：每个 hit 保留命中行 ± neighbor_lines（默认 ±1）
    这样像“粉丝/14”这种相邻数值不会丢。
    """
    if not hit_indices:
        return []

    key = keyword.lower() if case_insensitive else keyword
    excerpts: List[str] = []

    # 仅取前 top_hits_limit 个命中点做 excerpt
    for idx in hit_indices[:top_hits_limit]:
        a = max(0, idx - neighbor_lines)
        b = min(len(lines) - 1, idx + neighbor_lines)
        chunk: List[str] = []
        for i in range(a, b + 1):
            ln = lines[i]
            hay = ln.lower() if case_insensitive else ln
            if i == idx or (key in hay and i == idx):
                chunk.append(f">>> {ln}")
            else:
                chunk.append(f"    {ln}")
        excerpts.append("\n".join(chunk))

    return excerpts


def snapshot_query_latest(
    *,
    latest_path: str = DEFAULT_LATEST_PATH,
    uid: Optional[str] = None,
    keyword: Optional[str] = None,
    max_lines: int = 200,
    context_lines: int = 8,
    include_ancestors: bool = True,
    search_scope: str = "snapshot",  # "snapshot" | "all"
    compact: bool = False,
    top_hits_limit: int = 8,
) -> Dict[str, Any]:
    """
    通用查询器：
    - latest_path 可以是 snapshot_latest.txt / wait_for_log_latest.txt
    - 默认 search_scope="snapshot"：只查 marker 后的 snapshot 区域（更稳、更省 token）
    - uid 查询：永远先全文件定位 uid，再映射到 snapshot 段做 subtree（避免找不到/错段）
    """
    if not uid and not keyword:
        raise ValueError("either uid or keyword must be provided")

    all_lines = _read_latest_lines(latest_path)
    header_lines, snapshot_lines, marker_index = _split_snapshot(all_lines)

    # keyword 查询的目标段
    if search_scope == "all":
        keyword_target_lines = all_lines
    else:
        # marker 不存在时 snapshot_lines==all_lines
        keyword_target_lines = snapshot_lines

    # 建议只对 snapshot_lines 算 hash：header 波动不影响 snapshot_id
    raw_text = "\n".join(snapshot_lines if snapshot_lines else all_lines)
    snapshot_id = _sha1_12(raw_text)

    if uid:
        result = _query_by_uid(
            all_lines=all_lines,
            header_lines=header_lines,
            snapshot_lines=snapshot_lines,
            marker_index=marker_index,
            uid=uid,
            max_lines=max_lines,
            include_ancestors=include_ancestors,
        )
    else:
        result = _query_by_keyword(
            keyword_target_lines,
            keyword or "",
            max_lines=max_lines,
            context_lines=context_lines,
            case_insensitive=True,
        )

    payload: Dict[str, Any] = {
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
            "compact": compact,
            "top_hits_limit": top_hits_limit,
        },
        "result": result,
    }

    # 裁剪 keyword_grep 的内容（compact）
    if compact and isinstance(result, dict) and result.get("mode") == "keyword_grep":
        hit_indices = result.get("hit_indices") or []
        top_hits = _compact_top_hits(
            keyword_target_lines,
            hit_indices,
            keyword=result.get("keyword") or (keyword or ""),
            neighbor_lines=1,          # 关键：带上相邻行，避免丢“粉丝/14”等
            top_hits_limit=top_hits_limit,
            case_insensitive=True,
        )

        payload["result"] = {
            "found": result.get("found"),
            "mode": "keyword_grep_compact",
            "keyword": result.get("keyword"),
            "matched": result.get("matched"),
            "truncated": result.get("truncated"),
            "returned_lines": result.get("returned_lines"),
            "top_hits": top_hits,
        }

    # 清理内部字段：避免把 hit_indices 暴露给 LLM（如果你想保留也行）
    if isinstance(payload.get("result"), dict) and payload["result"].get("mode") == "keyword_grep":
        payload["result"].pop("hit_indices", None)

    return payload


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
                        "max_lines": {"type": "integer", "default": 60, "minimum": 20, "maximum": 2000},
                        "context_lines": {"type": "integer", "default": 2, "minimum": 0, "maximum": 50},
                        "include_ancestors": {"type": "boolean", "default": True},
                        "search_scope": {
                            "type": "string",
                            "enum": ["snapshot", "all"],
                            "default": "snapshot",
                            "description": "Search within snapshot only (after marker) or the full file.",
                        },
                        "compact": {
                            "type": "boolean",
                            "default": False,
                            "description": "If true and mode=keyword_grep, return only compact top hit excerpts to reduce noise/tokens.",
                        },
                        "top_hits_limit": {
                            "type": "integer",
                            "default": 8,
                            "minimum": 1,
                            "maximum": 50,
                            "description": "Max number of hit excerpts to keep when compact=true.",
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
            compact=bool(kwargs.get("compact", False)),
            top_hits_limit=int(kwargs.get("top_hits_limit", 8)),
        )
        return json.dumps(payload, ensure_ascii=False)