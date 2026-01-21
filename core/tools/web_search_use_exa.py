from __future__ import annotations

import json
import logging
import random
import re
from dataclasses import dataclass
from datetime import date
from enum import Enum
from typing import Any, Dict, List, Optional

import httpx

from .tool import Tool

logger = logging.getLogger(__name__)

API_CONFIG = {
    "BASE_URL": "https://mcp.exa.ai",
    "ENDPOINTS": {"SEARCH": "/mcp"},
    "DEFAULT_NUM_RESULTS": 8,
}
DEFAULT_TIMEOUT_SECONDS = 25.0


class LiveCrawl(str, Enum):
    fallback = "fallback"
    preferred = "preferred"


class SearchType(str, Enum):
    auto = "auto"
    fast = "fast"
    deep = "deep"


def load_description(txt_path: Optional[str]) -> str:
    """
    Loads description file and injects {{date}} -> YYYY-MM-DD.
    If file doesn't exist or not provided, returns a minimal fallback description.
    """
    if not txt_path:
        return f"Web search tool (Exa MCP). Today is {date.today().isoformat()}."
    try:
        with open(txt_path, "r", encoding="utf-8") as f:
            raw = f.read()
    except FileNotFoundError:
        raw = "Web search tool (Exa MCP). Today is {{date}}."
    return raw.replace("{{date}}", date.today().isoformat())


@dataclass(frozen=True)
class _SearchOptions:
    numResults: int
    livecrawl: str
    type: str
    contextMaxCharacters: Optional[int]


TITLE_RE = re.compile(r"^Title:\s*(.+)$", re.MULTILINE)
URL_RE = re.compile(r"^URL:\s*(\S+)\s*$", re.MULTILINE)
TEXT_RE = re.compile(r"^Text:\s*(.*)$", re.MULTILINE)


def _clean_snippet(text: str) -> str:
    """清洗噪声 & 生成短摘要"""
    max_len = 300
    # 常见噪声行
    noise_patterns = [
        r"^\[\]\s*$",  # 单独的 []
        r"^\s*Signing in\.\.\.\s*$",
        r"^\s*Log in.*$",
        r"^\s*Sign up.*$",
        r"^\s*SearchK\s*$",
        r"^\s*Skip to.*$",
        r"^\s*\[\s*.*?\s*\]\s*$",  # 纯 [xxx] 导航
        r"^!\[\]\s*$",  # Markdown 空图片
    ]
    lines = []
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        if any(re.match(p, s, re.IGNORECASE) for p in noise_patterns):
            continue
        lines.append(s)

    cleaned = " ".join(lines)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()

    # 截断（你可以按 UI 需要调整）
    return cleaned[:max_len] + ("..." if len(cleaned) > max_len else "")


def parse_search_raw_text(raw_text: str) -> List[Dict[str, str]]:
    """
    将 SearchApi 返回的 raw_text 解析成：
    [{"title":..., "snippet":..., "link":...}, ...]
    """
    # 以 Title: 作为结果块的起点
    starts = [m.start() for m in TITLE_RE.finditer(raw_text)]
    if not starts:
        return []

    # 把文本切成多个“结果块”
    blocks = []
    for i, st in enumerate(starts):
        ed = starts[i + 1] if i + 1 < len(starts) else len(raw_text)
        blocks.append(raw_text[st:ed].strip())

    results: List[Dict[str, str]] = []
    for b in blocks:
        title_m = TITLE_RE.search(b)
        url_m = URL_RE.search(b)

        # snippet = Text: 后的内容（可能跨多行，所以手动截取）
        snippet = ""
        text_m = TEXT_RE.search(b)
        if text_m:
            # Text: 这一行的剩余部分
            first_line = text_m.group(1).strip()
            # Text: 行之后的所有内容
            after = b[text_m.end():].strip()
            snippet = (first_line + "\n" + after).strip() if after else first_line

        title = title_m.group(1).strip() if title_m else ""
        link = url_m.group(1).strip() if url_m else ""
        snippet = _clean_snippet(snippet)

        # 过滤掉没 URL 的块（可选）
        if link:
            results.append({
                "title": title,
                "snippet": snippet,
                "link": link,
            })

    return results


class WebSearch(Tool):
    """
    Exa MCP-based web_search tool, aligned with existing Tool design:
    - Inherits Tool
    - execute() returns str (JSON string)
    - schema() returns OpenAI function calling JSON schema
    """

    def __init__(
            self,
            description_file: Optional[str] = "./web_search_use_exa.txt",
            mcp_tool_name: str = "web_search_exa",
            paywall_keywords: Optional[List[str]] = None,
            # 如果你想严格对齐 TS：设为 False，则 contextMaxCharacters=None 时不下发该字段
            send_default_context_max: bool = True,
            default_context_max: int = 10000,
    ):
        super().__init__()
        self.name = "web_search"
        self.description = load_description(description_file)
        self._mcp_tool_name = mcp_tool_name


        self._send_default_context_max = send_default_context_max
        self._default_context_max = default_context_max


    def _make_request(self, query: str, opts: _SearchOptions) -> Dict[str, Any]:
        rpc_id = random.randint(1, 1_000_000_000)

        args: Dict[str, Any] = {
            "query": query,
            "type": opts.type,
            "numResults": opts.numResults,
            "livecrawl": opts.livecrawl,
        }

        # TS 对齐：可选择不下发 contextMaxCharacters
        if opts.contextMaxCharacters is not None:
            args["contextMaxCharacters"] = opts.contextMaxCharacters
        elif self._send_default_context_max:
            args["contextMaxCharacters"] = self._default_context_max

        return {
            "jsonrpc": "2.0",
            "id": rpc_id,
            "method": "tools/call",
            "params": {"name": self._mcp_tool_name, "arguments": args},
        }

    # SSE parsing helpers
    @staticmethod
    def _extract_first_text_from_mcp_payload(data: Dict[str, Any]) -> Optional[str]:
        """
        Expected:
        { "jsonrpc":"2.0", "result": { "content": [ { "type":"text","text":"..." }, ... ] } }
        """
        if not isinstance(data, dict):
            return None
        if "error" in data:
            raise RuntimeError(f"MCP error: {data['error']}")
        result = data.get("result")
        if not isinstance(result, dict):
            return None
        content = result.get("content")
        if not isinstance(content, list) or not content:
            return None
        first = content[0]
        if not isinstance(first, dict):
            return None
        text = first.get("text")
        if isinstance(text, str) and text.strip():
            return text
        return None

    @staticmethod
    def _iter_sse_data_lines(resp: httpx.Response):
        """
        Yields payload strings for SSE lines like: 'data: {...}'
        """
        for line in resp.iter_lines():
            if not line:
                continue
            # httpx returns str lines
            if line.startswith("data:"):
                payload = line[len("data:"):].lstrip()
                if not payload:
                    continue
                if payload.strip() == "[DONE]":
                    break
                yield payload

    async def execute(self, query: str, **kwargs) -> str:
        q = query
        raw_type = kwargs.get("raw_type") or None
        raw_live = kwargs.get("livecrawl") or None
        raw_num = kwargs.get("numResults") or None
        raw_ctx = kwargs.get("contextMaxCharacters") or None
        if not q:
            return json.dumps({"error": "No search query provided."}, ensure_ascii=False)

        stype = raw_type if raw_type in {e.value for e in SearchType} else SearchType.auto.value
        live = raw_live if raw_live in {e.value for e in LiveCrawl} else LiveCrawl.fallback.value
        try:
            num = int(raw_num) if raw_num is not None else API_CONFIG["DEFAULT_NUM_RESULTS"]
        except Exception:
            num = API_CONFIG["DEFAULT_NUM_RESULTS"]
        if num <= 0:
            num = API_CONFIG["DEFAULT_NUM_RESULTS"]

        try:
            ctx = int(raw_ctx) if raw_ctx is not None else None
        except Exception:
            ctx = None

        opts = _SearchOptions(
            numResults=num,
            livecrawl=live,
            type=stype,
            contextMaxCharacters=ctx,
        )

        request_body = self._make_request(q, opts)

        url = f"{API_CONFIG['BASE_URL']}{API_CONFIG['ENDPOINTS']['SEARCH']}"

        headers = {
            "accept": "application/json, text/event-stream",
            "content-type": "application/json",
        }

        logger.info(f"[web_search/exa] Searching: {q}")

        try:
            with httpx.Client(timeout=httpx.Timeout(DEFAULT_TIMEOUT_SECONDS)) as client:
                with client.stream("POST", url, headers=headers, json=request_body) as resp:
                    if resp.status_code < 200 or resp.status_code >= 300:
                        err = resp.read()
                        raise RuntimeError(f"Search error ({resp.status_code}): {err.decode('utf-8', 'ignore')}")

                    # 1) SSE first
                    text: Optional[str] = None
                    for payload in self._iter_sse_data_lines(resp):
                        try:
                            data = json.loads(payload)
                        except json.JSONDecodeError:
                            continue
                        text = self._extract_first_text_from_mcp_payload(data)
                        if text:
                            break

                    # 2) Non-SSE JSON fallback
                    if not text:
                        remaining = resp.read()
                        if remaining:
                            try:
                                data = json.loads(remaining.decode("utf-8", "ignore"))
                                text = self._extract_first_text_from_mcp_payload(data)
                            except Exception:
                                text = None

            if not text:
                return json.dumps(
                    {"query": q, "text": "", "links": [], "note": "No search results found. Try a different query."},
                    ensure_ascii=False,
                )

            items = parse_search_raw_text(text)
            return json.dumps(items, ensure_ascii=False)

        except httpx.ReadTimeout:
            raise RuntimeError("Search request timed out")
        except Exception as e:
            logger.exception(f"[web_search/exa] Failed: {e}")
            raise RuntimeError(str(e)) from e

    def schema(self) -> dict:
        """
        Function-calling schema aligned with your existing Tool.schema style.
        """
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "The web search query.",
                        }
                    },
                    "required": ["query"],
                },
                "strict": True,
            },
        }
