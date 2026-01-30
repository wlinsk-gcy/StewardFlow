import re
import json
import hashlib
import os
from datetime import datetime
from dataclasses import dataclass
from typing import List, Optional, Dict, Tuple


def should_summarize_snapshot(tool_name: Optional[str], text: str) -> bool:
    if tool_name and "snapshot" in tool_name.lower():
        return True
    return False
    # lower = text.lower()
    # return "take_snapshot response" in lower or "latest page snapshot" in lower or "rootwebarea" in lower


def save_snapshot_raw(text: str) -> None:
    latest_path, history_path = _get_snapshot_paths()
    _ensure_parent_dir(latest_path)
    with open(latest_path, "w", encoding="utf-8") as f:
        f.write(text)
    _ensure_parent_dir(history_path)
    with open(history_path, "a", encoding="utf-8") as f:
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        f.write(f"\n--- snapshot {stamp} ---\n")
        f.write(text)


def clear_snapshot_logs() -> None:
    latest_path, history_path = _get_snapshot_paths()
    _ensure_parent_dir(latest_path)
    with open(latest_path, "w", encoding="utf-8") as f:
        f.write("")
    _ensure_parent_dir(history_path)
    with open(history_path, "w", encoding="utf-8") as f:
        f.write("")


def _get_snapshot_paths() -> Tuple[str, str]:
    base = os.getenv("SNAPSHOT_PATH", "data").strip() or "data"
    latest_path = os.path.join(base, "snapshot_latest.txt")
    history_path = os.path.join(base, "snapshot_history.txt")
    return latest_path, history_path


def _ensure_parent_dir(path: str) -> None:
    parent = os.path.dirname(path)
    if parent and not os.path.exists(parent):
        os.makedirs(parent, exist_ok=True)


UID_LINE_RE = re.compile(r'^(\s*)uid=([^\s]+)\s+([A-Za-z]+)\s+"([^"]*)"(.*)$')
VALUE_RE = re.compile(r'\bvalue="([^"]*)"')
URL_RE = re.compile(r'\burl="([^"]*)"')
ROOT_RE = re.compile(r'RootWebArea "([^"]+)" url="([^"]+)"')

ROLE_ACTION = {"textbox", "button", "combobox", "checkbox", "radio", "tab", "option"}
ROLE_KEEP = ROLE_ACTION | {"link", "heading", "navigation"}

NOISE_KW = (
    "广告", "ICP备", "ICP证", "网信", "公安备", "Copyright", "All rights reserved",
    "OpenStreetMap", "HERE", "可信网站", "信用中国", "网站导航", "索引", "友情链接"
)
def rough_tokens(s: str) -> int:
    return int(len(s) / 3.2) + 1

def sha1_12(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8", errors="ignore")).hexdigest()[:12]

@dataclass
class Node:
    indent: int
    uid: str
    role: str
    name: str
    value: Optional[str]
    url: Optional[str]
    raw: str
    path: List[str]  # 最近 heading 路径

def parse_nodes(lines: List[str]) -> Tuple[Optional[str], Optional[str], List[Node]]:
    title = url = None
    nodes: List[Node] = []
    heading_stack: List[Tuple[int, str]] = []  # (indent, heading_text)

    for ln in lines:
        if title is None and "RootWebArea" in ln and 'url="' in ln:
            m = ROOT_RE.search(ln)
            if m:
                title, url = m.group(1), m.group(2)

        m = UID_LINE_RE.match(ln)
        if not m:
            continue

        indent = len(m.group(1))
        uid, role, name, rest = m.group(2), m.group(3), m.group(4).strip(), m.group(5)

        value = None
        vm = VALUE_RE.search(rest)
        if vm:
            value = vm.group(1).strip()

        u = None
        um = URL_RE.search(rest)
        if um:
            u = um.group(1).strip()

        # 维护 heading 路径（按缩进出栈）
        while heading_stack and heading_stack[-1][0] >= indent:
            heading_stack.pop()

        if role == "heading" and name and not is_noise(name):
            heading_stack.append((indent, name))

        path = [h for _, h in heading_stack[-3:]]  # 只取最近 3 个 heading 做上下文
        nodes.append(Node(indent, uid, role, name, value, u, ln.rstrip("\n"), path))

    return title, url, nodes

def is_noise(text: str) -> bool:
    if not text:
        return True
    if any(k in text for k in NOISE_KW):
        return True
    # 过滤大量 icon/特殊符号占位
    if len(text) <= 1 and not text.isalnum():
        return True
    return False

def score_node(n: Node) -> float:
    # 基础分：可行动元素优先
    base = 0.0
    if n.role in {"textbox", "combobox"}:
        base = 100.0
    elif n.role in {"button", "checkbox", "radio", "tab"}:
        base = 90.0
    elif n.role == "link":
        base = 70.0
    elif n.role == "heading":
        base = 30.0
    else:
        base = 10.0

    # 有 value / url 加分（更可用）
    if n.value:
        base += 10
    if n.url:
        base += 8

    # 文本过长降分（通常是长说明/页脚）
    if len(n.name) > 80:
        base -= 15

    # 噪音关键词强降分
    if is_noise(n.name):
        base -= 60

    return base

def infer_labels(nodes: List[Node]) -> Dict[str, str]:
    """
    给 textbox/combobox 推断 label：在同一“父级缩进块”里向上找最近的 StaticText/heading 作为 label。
    这里用 indent 邻近 + 同级/上级规则，比 last_label_age 稳很多。
    """
    label_map: Dict[str, str] = {}
    # 建一个索引，方便回看前序节点
    for i, n in enumerate(nodes):
        if n.role not in {"textbox", "combobox"}:
            continue

        # 向上回看最多 12 个节点，找最像 label 的
        best = None
        for j in range(i - 1, max(-1, i - 13), -1):
            p = nodes[j]
            if is_noise(p.name):
                continue
            # label 通常是 StaticText 或 heading，且缩进不应比输入框更深太多
            if p.role in {"StaticText", "heading"} and p.indent <= n.indent:
                # 更偏好“同级或父级紧邻”
                dist = i - j
                indent_penalty = abs(n.indent - p.indent) / 10.0
                score = (20 - dist) - indent_penalty
                if best is None or score > best[0]:
                    best = (score, p.name)
        if best:
            label_map[n.uid] = best[1]
    return label_map

def build_snapshot_summary(text: str, max_tokens: int = 6000, max_items: int = 180) -> str:
    lines = [ln for ln in text.splitlines() if ln.strip()]
    title, url, nodes = parse_nodes(lines)

    label_map = infer_labels(nodes)

    # 过滤 + 打分
    kept = [n for n in nodes if n.role in ROLE_KEEP and not (n.role != "heading" and is_noise(n.name))]
    kept.sort(key=score_node, reverse=True)

    # 预算裁剪：优先保留高分元素
    picked: List[Node] = []
    out_tokens = 0
    for n in kept:
        if len(picked) >= max_items:
            break
        item_repr = {
            "uid": n.uid,
            "role": n.role,
            "name": (n.name[:80] + "…") if len(n.name) > 80 else n.name,
            "value": n.value,
            "url": n.url,
            "path": n.path,
        }
        if n.uid in label_map:
            item_repr["label"] = label_map[n.uid]

        s = json.dumps(item_repr, ensure_ascii=False)
        t = rough_tokens(s)
        if out_tokens + t > max_tokens:
            break
        out_tokens += t
        picked.append(n)

    payload = {
        "type": "snapshot_summary",
        "snapshot_id": sha1_12(text),
        "page": {"title": title, "url": url},
        "counts": {
            "raw_nodes": len(nodes),
            "kept_nodes": len(picked),
            "estimated_tokens": out_tokens,
        },
        "elements": [
            {
                "uid": n.uid,
                "role": n.role,
                "name": (n.name[:80] + "…") if len(n.name) > 80 else n.name,
                "label": label_map.get(n.uid),
                "value": n.value,
                "url": n.url,
                "path": n.path,
            }
            for n in picked
        ],
    }
    return json.dumps(payload, ensure_ascii=False)