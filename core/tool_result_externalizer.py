from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .tool_result_store import ToolResultStore


@dataclass
class ToolResultExternalizerConfig:
    inline_limit: int = 500
    preview_limit: int = 500
    root_dir: str = "data/tool_results"
    always_externalize_tools: set[str] = field(
        default_factory=lambda: {
            "chrome-devtools_take_snapshot",
            "chrome-devtools_take_screenshot",
        }
    )

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "ToolResultExternalizerConfig":
        if not raw:
            return cls()
        default_always = cls().always_externalize_tools
        always = raw.get("always_externalize_tools")
        if always is None:
            always_set = set(default_always)
        else:
            always_set = {str(item) for item in always if isinstance(item, str)}
        return cls(
            inline_limit=max(1, int(raw.get("inline_limit", 500))),
            preview_limit=max(1, int(raw.get("preview_limit", 500))),
            root_dir=str(raw.get("root_dir", "data/tool_results")),
            always_externalize_tools=always_set,
        )


class ToolResultExternalizerMiddleware:
    def __init__(self, config: ToolResultExternalizerConfig):
        self.config = config
        self.store = ToolResultStore(root_dir=config.root_dir)

    def _summary(self, *, tool_name: str, kind: str, chars: int, bytes_size: int) -> str:
        if kind == "ref":
            return (
                f"Tool '{tool_name}' result externalized to ref "
                f"({chars} chars, {bytes_size} bytes)."
            )
        return f"Tool '{tool_name}' returned inline result ({chars} chars, {bytes_size} bytes)."

    def externalize(
        self,
        *,
        tool_name: str,
        raw_result: Any,
        trace_id: str,
        turn_id: str,
        step_id: str,
        tool_call_id: str,
    ) -> dict[str, Any]:
        normalized = self.store.normalize(raw_result)
        force_ref = tool_name in self.config.always_externalize_tools or normalized.is_binary
        use_ref = force_ref or normalized.chars > self.config.inline_limit

        preview, preview_truncated = self.store.preview(normalized.text, self.config.preview_limit)
        stats = {
            "bytes": normalized.bytes_size,
            "lines": normalized.lines,
            "chars": normalized.chars,
            "truncated": bool(preview_truncated),
        }

        if use_ref:
            ref = self.store.persist(
                trace_id=trace_id,
                turn_id=turn_id,
                step_id=step_id,
                tool_call_id=tool_call_id,
                normalized=normalized,
            )
            return {
                "kind": "ref",
                "tool_name": tool_name,
                "summary": self._summary(
                    tool_name=tool_name,
                    kind="ref",
                    chars=normalized.chars,
                    bytes_size=normalized.bytes_size,
                ),
                "preview": preview,
                "stats": stats,
                "ref": ref.to_dict(),
            }

        return {
            "kind": "inline",
            "tool_name": tool_name,
            "summary": self._summary(
                tool_name=tool_name,
                kind="inline",
                chars=normalized.chars,
                bytes_size=normalized.bytes_size,
            ),
            "preview": preview,
            "content": normalized.text,
            "stats": stats,
        }

    def build_error(self, *, tool_name: str, error_text: str) -> dict[str, Any]:
        preview, preview_truncated = self.store.preview(error_text or "", self.config.preview_limit)
        return {
            "kind": "inline",
            "tool_name": tool_name,
            "summary": f"Tool '{tool_name}' execution failed.",
            "preview": preview,
            "content": error_text or "",
            "stats": {
                "bytes": len((error_text or "").encode("utf-8")),
                "lines": 0 if not error_text else (error_text.count("\n") + 1),
                "chars": len(error_text or ""),
                "truncated": bool(preview_truncated),
            },
        }
