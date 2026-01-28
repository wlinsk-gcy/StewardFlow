import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Tuple

logger = logging.getLogger(__name__)


@dataclass
class ContextManagerConfig:
    pre_rot_threshold_tokens: int = 12_000 # 上下文腐烂前阈值
    keep_recent_ratio: float = 0.30 # 保留率
    compaction_ratio: float = 0.70 # 压缩率
    summarization_ratio: float = 0.70 # 摘要率
    dump_dir: str = "data" # 外部记忆目录
    summary_max_tokens: int = 2048 # 摘要最大token数
    summary_temperature: float = 0.2 # 摘要温度
    web_search_top_n: int = 5
    bash_max_key_lines: int = 6
    ls_glob_max_items: int = 20
    grep_max_matches: int = 5
    read_max_chars: int = 800
    read_tail_chars: int = 200
    backfill_max_matches: int = 20 # 回填最大匹配数据量
    backfill_max_chars: int = 4000 # 回填最大字符数
    backfill_case_sensitive: bool = False
    auto_backfill_enabled: bool = True
    auto_backfill_max_patterns: int = 6
    auto_backfill_keywords: List[str] = None
    calibration_ema: float = 0.2 # 指数滑动平均：控制“新观测值（ratio）”对当前乘数的影响程度，越小越稳定，收敛更慢，越大越激进，越容易抖动
    calibration_min: float = 0.5
    calibration_max: float = 20.0
    dry_run: bool = False

    def __post_init__(self) -> None:
        if self.auto_backfill_keywords is None:
            self.auto_backfill_keywords = [
                "error",
                "exception",
                "traceback",
                "stack",
                "fail",
                "crash",
                "日志",
                "报错",
                "错误",
                "异常",
                "回溯",
                "堆栈",
                "路径",
                "文件",
                "file",
                "path",
                "log",
            ]


class ContextManager:
    def __init__(self, config: ContextManagerConfig, client, model: str):
        self.config = config
        self.client = client
        self.model = model
        self._calibration_multiplier = 1.0

    def build_messages(
        self,
        context: Dict[str, Any],
        system_prompt: str,
        tool_schemas: List[Dict[str, Any]] | None = None,
        response_schema: Dict[str, Any] | None = None,
    ) -> Tuple[List[Dict[str, str]], Dict[str, Any]]:
        task = context.get("task") or ""
        trajectory = context.get("tao_trajectory") or []

        messages = self._build_messages(task, system_prompt, trajectory, include_thought=False)
        token_est_raw = self._estimate_tokens_raw(messages, tool_schemas, response_schema)
        token_est = self._estimate_tokens(messages, tool_schemas, response_schema)

        audit = {
            "stage": "none",
            "token_estimate": token_est,
            "token_estimate_raw": token_est_raw,
            "pre_rot_threshold": self.config.pre_rot_threshold_tokens,
            "kept_recent_ratio": self.config.keep_recent_ratio,
            "dump_path": None,
            "calibration_multiplier": self._calibration_multiplier,
        }

        if token_est < self.config.pre_rot_threshold_tokens:
            self._append_backfill_if_needed(context, trajectory, messages, audit)
            return messages, audit

        # Phase 1: Compaction
        compacted_trajectory = self._compact_trajectory(trajectory)
        messages = self._build_messages(task, system_prompt, compacted_trajectory, include_thought=False)
        token_est_raw = self._estimate_tokens_raw(messages, tool_schemas, response_schema)
        token_est = self._estimate_tokens(messages, tool_schemas, response_schema)
        audit["stage"] = "compaction"
        audit["token_estimate"] = token_est
        audit["token_estimate_raw"] = token_est_raw

        if token_est < self.config.pre_rot_threshold_tokens:
            self._append_backfill_if_needed(context, compacted_trajectory, messages, audit)
            return messages, audit

        # Phase 2: Summarization (with dump)
        dump_path = self._dump_full_history(context.get("agent_id"), trajectory)
        summary_block = self._summarize_history(trajectory)
        if dump_path:
            summary_block = (
                summary_block
                + "\n\n[context_dump]\n"
                + f"path: {dump_path}\n"
                + "hint: use grep -n \"TURN\" or grep -n \"tool:<name>\""
            )
        summarized_trajectory = self._apply_summary_block(trajectory, summary_block)
        messages = self._build_messages(task, system_prompt, summarized_trajectory, include_thought=False)
        token_est_raw = self._estimate_tokens_raw(messages, tool_schemas, response_schema)
        token_est = self._estimate_tokens(messages, tool_schemas, response_schema)

        audit["stage"] = "summarization"
        audit["token_estimate"] = token_est
        audit["token_estimate_raw"] = token_est_raw
        audit["dump_path"] = dump_path

        self._append_backfill_if_needed(context, summarized_trajectory, messages, audit)
        return messages, audit

    def _build_messages(self, task: str, system_prompt: str, trajectory: List[Dict[str, Any]], include_thought: bool) -> List[Dict[str, str]]:
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": task},
        ]

        n = len(trajectory)
        if include_thought:
            thought_start = 0
        else:
            thought_start = max(0, n - max(1, int(n * self.config.keep_recent_ratio)))

        for idx, traj in enumerate(trajectory):
            if include_thought or idx >= thought_start:
                thought_content = (traj.get("thought") or {}).get("content", "")
            else:
                thought_content = ""
            action = traj.get("action") or {}
            assistant_payload = {
                "thought": thought_content,
                "action": action,
            }
            messages.append({"role": "assistant", "content": json.dumps(assistant_payload, ensure_ascii=False)})

            observation = traj.get("observation") or {}
            obs_role = observation.get("role") or "user"
            obs_content = observation.get("content") or ""
            if obs_role == "tool":
                obs_content = "工具执行结果：" + obs_content
                messages.append({"role": "user", "content": obs_content})
            else:
                messages.append({"role": "user", "content": obs_content})

        return messages

    def _estimate_tokens_raw(
        self,
        messages: List[Dict[str, str]],
        tool_schemas: List[Dict[str, Any]] | None = None,
        response_schema: Dict[str, Any] | None = None,
    ) -> int:
        # Rough estimate: 1 token ~ 4 chars
        total_chars = 0
        for msg in messages:
            total_chars += len(msg.get("role", ""))
            total_chars += len(msg.get("content", ""))
        if tool_schemas:
            total_chars += len(json.dumps(tool_schemas, ensure_ascii=False))
        if response_schema:
            total_chars += len(json.dumps(response_schema, ensure_ascii=False))
        return max(1, total_chars // 4)

    def _estimate_tokens(
        self,
        messages: List[Dict[str, str]],
        tool_schemas: List[Dict[str, Any]] | None = None,
        response_schema: Dict[str, Any] | None = None,
    ) -> int:
        raw = self._estimate_tokens_raw(messages, tool_schemas, response_schema)
        return max(1, int(raw * self._calibration_multiplier))

    def update_calibration(
        self,
        actual_prompt_tokens: int,
        messages: List[Dict[str, str]],
        tool_schemas: List[Dict[str, Any]] | None = None,
        response_schema: Dict[str, Any] | None = None,
    ) -> None:
        if not actual_prompt_tokens:
            return
        estimated = self._estimate_tokens_raw(messages, tool_schemas, response_schema)
        if estimated <= 0:
            return
        ratio = actual_prompt_tokens / estimated
        alpha = self.config.calibration_ema
        new_multiplier = (1 - alpha) * self._calibration_multiplier + alpha * ratio # EMA的标准更新公式：新值 = 旧值的惯性 + 新观测的权重， old_multiplier是过去积累的校准结果
        new_multiplier = max(self.config.calibration_min, min(self.config.calibration_max, new_multiplier)) # clamp，安全护栏避免multiplier严重偏移
        self._calibration_multiplier = new_multiplier

    def _compact_trajectory(self, trajectory: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        if not trajectory:
            return []
        n = len(trajectory)
        min_recent = max(1, int(n * self.config.keep_recent_ratio))
        compact_until = max(0, min(n, int(n * self.config.compaction_ratio)))
        compact_until = min(compact_until, n - min_recent)
        keep_recent = max(1, n - compact_until)

        compacted: List[Dict[str, Any]] = []
        for i, traj in enumerate(trajectory):
            if i < compact_until:
                compacted.append(self._compact_traj_item(traj))
            else:
                compacted.append(traj)
        return compacted

    def _compact_traj_item(self, traj: Dict[str, Any]) -> Dict[str, Any]:
        action = traj.get("action") or {}
        tool_name = action.get("tool_name") or ""
        observation = traj.get("observation") or {}
        content = observation.get("content") or ""

        compacted_obs = dict(observation)

        if tool_name == "bash":
            compacted_obs["content"] = self._compact_bash_content(content, action)
        elif tool_name == "web_search":
            compacted_obs["content"] = self._compact_web_search_content(content)
        elif tool_name.startswith("chrome-devtools"):
            compacted_obs["content"] = self._compact_chrome_devtools_content(content, action)
        elif tool_name in {"ls", "glob"}:
            compacted_obs["content"] = self._compact_ls_glob_content(content, tool_name)
        elif tool_name == "grep":
            compacted_obs["content"] = self._compact_grep_content(content)
        elif tool_name == "read":
            compacted_obs["content"] = self._compact_read_content(content)

        compacted = dict(traj)
        compacted["observation"] = compacted_obs
        return compacted

    def _compact_bash_content(self, content: str, action: Dict[str, Any]) -> str:
        """
        拼接normal output �?error output，再根据bash_max_key_lines截断，error_text限制最大长�?00
        :param content:
        :param action:
        :return:
        """
        command = (action.get("args") or {}).get("command") or ""
        try:
            data = json.loads(content)
        except Exception:
            data = None

        key_lines: List[str] = []
        error_text = ""
        output_text = ""
        if isinstance(data, dict):
            output_text = data.get("output") or ""
            error_text = data.get("error") or ""
        else:
            output_text = content

        # Extract key lines
        for line in (output_text.splitlines() + error_text.splitlines()):
            line = line.strip()
            if not line:
                continue
            if "error" in line.lower() or "fail" in line.lower() or "exception" in line.lower():
                key_lines.append(line)
            if len(key_lines) >= self.config.bash_max_key_lines:
                break

        payload = {
            "type": "bash_compact",
            "command": command,
            "key_output": key_lines,
            "error": error_text[:500] if error_text else "",
        }
        return json.dumps(payload, ensure_ascii=False)

    def _compact_web_search_content(self, content: str) -> str:
        """
        根据top_n截断
        :param content:
        :return:
        """
        items = []
        try:
            data = json.loads(content)
            if isinstance(data, list):
                for item in data[: self.config.web_search_top_n]:
                    title = item.get("title") if isinstance(item, dict) else None
                    url = item.get("link") if isinstance(item, dict) else None
                    snippet = item.get("snippet") if isinstance(item, dict) else None
                    items.append({"title": title, "url": url, "snippet": snippet})
        except Exception:
            pass
        payload = {
            "type": "web_search_compact",
            "top_results": items,
        }
        return json.dumps(payload, ensure_ascii=False)

    def _compact_chrome_devtools_content(self, content: str, action: Dict[str, Any]) -> str:
        """
        把chrome操作记录从上下文窗口卸载到外部，
        :param content:
        :param action:
        :return:
        """
        payload = {
            "type": "chrome_devtools_compact",
            "note": "Large browser output removed. See artifacts (screenshot/path) if available.",
            "tool": action.get("tool_name"),
            "args": action.get("args") or {},
        }
        return json.dumps(payload, ensure_ascii=False)

    def _compact_ls_glob_content(self, content: str, tool_name: str) -> str:
        """
        根据ls_glob_max_items配置对结果进行截断�?
        :param content:
        :param tool_name:
        :return:
        """
        items: List[dict] = []
        total = 0
        try:
            data = json.loads(content)
            if isinstance(data, list):
                total = len(data)
                for item in data[: self.config.ls_glob_max_items]:
                    if isinstance(item, dict):
                        items.append(
                            {
                                "path": item.get("path"),
                                "type": item.get("type"),
                                "size": item.get("size"),
                            }
                        )
        except Exception:
            pass
        payload = {
            "type": f"{tool_name}_compact",
            "total_items": total,
            "items": items,
        }
        return json.dumps(payload, ensure_ascii=False)

    def _compact_grep_content(self, content: str) -> str:
        """
        根据grep_max_matches对结果进行截�?
        :param content:
        :return:
        """
        matches: List[dict] = []
        total = 0
        try:
            data = json.loads(content)
            if isinstance(data, list):
                total = len(data)
                for item in data[: self.config.grep_max_matches]:
                    if isinstance(item, dict):
                        matches.append(
                            {
                                "path": item.get("path"),
                                "line": item.get("line"),
                                "text": item.get("text"),
                            }
                        )
        except Exception:
            pass
        payload = {
            "type": "grep_compact",
            "total_matches": total,
            "matches": matches,
        }
        return json.dumps(payload, ensure_ascii=False)

    def _compact_read_content(self, content: str) -> str:
        """
        根据read_max_chars对read content进行截断，用特殊token拼接
        :param content:
        :return:
        """
        payload = {
            "type": "read_compact",
            "path": None,
            "mode": None,
            "excerpt": "",
            "truncated": False,
        }
        try:
            data = json.loads(content)
            if isinstance(data, dict):
                payload["path"] = data.get("path")
                payload["mode"] = data.get("mode")
                text = data.get("content") or ""
                truncated = bool(data.get("truncated"))
                if len(text) > self.config.read_max_chars:
                    head = text[: self.config.read_max_chars]
                    tail = text[-self.config.read_tail_chars :] if self.config.read_tail_chars > 0 else ""
                    text = head + ("\n...<tail>...\n" + tail if tail else "")
                    truncated = True
                payload["excerpt"] = text
                payload["truncated"] = truncated
        except Exception:
            pass
        return json.dumps(payload, ensure_ascii=False)

    def _dump_full_history(self, agent_id: str | None, trajectory: List[Dict[str, Any]]) -> str:
        os.makedirs(self.config.dump_dir, exist_ok=True)
        ts = datetime.utcnow().isoformat().replace(":", "-")
        safe_agent = agent_id or "unknown"
        filename = f"context_dump_{safe_agent}_{ts}.txt"
        path = os.path.join(self.config.dump_dir, filename)

        lines: List[str] = []
        lines.append("=== CONTEXT DUMP START ===")
        lines.append(f"agent_id: {safe_agent}")
        lines.append(f"timestamp: {datetime.utcnow().isoformat()}")
        lines.append(f"turns: {len(trajectory)}")
        lines.append("")

        for traj in trajectory:
            turn_id = traj.get("turn_id") or (traj.get("thought") or {}).get("turn_id") or ""
            lines.append(f"--- TURN {turn_id} ---")
            lines.append(f"time: {(traj.get('timestamp') or '')}")
            lines.append("role: assistant")
            lines.append("action:")
            lines.append(json.dumps(traj.get("action") or {}, ensure_ascii=False))
            lines.append("")
            lines.append("role: user")
            lines.append("observation:")
            obs = traj.get("observation") or {}
            lines.append(obs.get("content") or "")
            lines.append("")

        lines.append("=== CONTEXT DUMP END ===")

        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))

        logger.info(f"[context_manager] dumped full history to {path}")
        return path

    def _summarize_history(self, trajectory: List[Dict[str, Any]]) -> str:
        if self.config.dry_run:
            return (
                "[summary]\n"
                "task_summary: dry-run summary\n"
                "facts:\n- dry-run\n"
                "decisions:\n- dry-run\n"
                "open_threads:\n- dry-run\n"
                "artifacts:\n- path: data/context_dump_dummy.txt\n  note: dry-run\n"
            )
        # Use full raw history (not compacted) for summary
        raw_text = self._trajectory_to_text(trajectory)
        prompt = (
            "Summarize the following agent execution history. "
            "Return a structured summary using this template:\n"
            "[summary]\n"
            "task_summary: ...\n"
            "facts:\n- ...\n"
            "decisions:\n- ...\n"
            "open_threads:\n- ...\n"
            "artifacts:\n- path: ...\n  note: ...\n\n"
            "History:\n" + raw_text
        )

        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": "You are a concise summarizer for agent execution logs."},
                {"role": "user", "content": prompt},
            ],
            temperature=self.config.summary_temperature,
            max_tokens=self.config.summary_max_tokens,
        )
        summary = response.choices[0].message.content or ""
        return summary.strip()

    def _trajectory_to_text(self, trajectory: List[Dict[str, Any]]) -> str:
        parts: List[str] = []
        for traj in trajectory:
            action = traj.get("action") or {}
            obs = traj.get("observation") or {}
            parts.append("[action]")
            parts.append(json.dumps(action, ensure_ascii=False))
            parts.append("[observation]")
            parts.append(obs.get("content") or "")
            parts.append("")
        return "\n".join(parts)

    def _apply_summary_block(self, trajectory: List[Dict[str, Any]], summary_block: str) -> List[Dict[str, Any]]:
        if not trajectory:
            return []
        n = len(trajectory)
        min_recent = max(1, int(n * self.config.keep_recent_ratio))
        summarize_until = max(0, min(n, int(n * self.config.summarization_ratio)))
        summarize_until = min(summarize_until, n - min_recent)
        split_idx = summarize_until

        summary_traj = {
            "turn_id": "summary",
            "thought": {"content": "", "turn_id": "summary"},
            "action": {"type": "tool", "tool_name": "context_summary", "args": {}},
            "observation": {"role": "user", "content": summary_block},
            "timestamp": datetime.utcnow().isoformat(),
        }

        return [summary_traj] + trajectory[split_idx:]

    def _append_backfill_if_needed(
        self,
        context: Dict[str, Any],
        trajectory: List[Dict[str, Any]],
        messages: List[Dict[str, str]],
        audit: Dict[str, Any],
    ) -> None:
        task = context.get("task") or ""
        patterns = context.get("backfill_patterns") or context.get("backfill_queries") or []
        if isinstance(patterns, str):
            patterns = [patterns]
        elif not isinstance(patterns, list):
            patterns = list(patterns)
        if not patterns and self.config.auto_backfill_enabled:
            patterns = self._auto_backfill_patterns(task)
        if not patterns:
            return

        dump_path = (
            context.get("context_dump_path")
            or audit.get("dump_path")
            or self._find_dump_path_in_trajectory(trajectory)
        )
        if not dump_path or not os.path.exists(dump_path):
            return

        matches = self._search_dump_for_patterns(
            dump_path,
            patterns,
            max_matches=self.config.backfill_max_matches,
            max_chars=self.config.backfill_max_chars,
            case_sensitive=self.config.backfill_case_sensitive,
            use_regex=bool(context.get("backfill_use_regex")),
        )
        if not matches:
            return

        backfill_block = self._format_backfill_block(dump_path, matches)
        if any(m.get("role") == "user" and m.get("content") == backfill_block for m in messages):
            return
        messages.append({"role": "user", "content": backfill_block})

    def _auto_backfill_patterns(self, task: str) -> List[str]:
        if not task:
            return []
        patterns: List[str] = []
        lowered = task.lower()
        synonyms = {
            "报错": ["error", "exception", "traceback"],
            "错误": ["error", "exception", "traceback"],
            "异常": ["exception", "traceback"],
            "日志": ["log"],
            "路径": ["path", "file"],
        }
        for kw in self.config.auto_backfill_keywords:
            if kw.lower() in lowered:
                patterns.append(kw)
                if kw in synonyms:
                    patterns.extend(synonyms[kw])

        quoted = re.findall(r"[\"'`]{1}([^\"'`]{2,80})[\"'`]{1}", task)
        for token in quoted:
            patterns.append(token.strip())

        for match in re.findall(r"[A-Za-z]:\\\\[^\\s\"']+", task):
            patterns.append(match.strip())
        for match in re.findall(r"/[^\\s\"']+", task):
            patterns.append(match.strip())

        deduped: List[str] = []
        seen = set()
        for p in patterns:
            p = p.strip()
            if not p or p in seen:
                continue
            seen.add(p)
            deduped.append(p)
            if len(deduped) >= self.config.auto_backfill_max_patterns:
                break
        return deduped

    def _find_dump_path_in_trajectory(self, trajectory: List[Dict[str, Any]]) -> str | None:
        for traj in trajectory:
            action = traj.get("action") or {}
            if action.get("tool_name") != "context_summary":
                continue
            obs = traj.get("observation") or {}
            content = obs.get("content") or ""
            path = self._extract_dump_path_from_summary(content)
            if path:
                return path
        return None

    def _extract_dump_path_from_summary(self, content: str) -> str | None:
        marker = "[context_dump]"
        if marker not in content:
            return None
        lines = content.splitlines()
        for idx, line in enumerate(lines):
            if line.strip() == marker and idx + 1 < len(lines):
                next_line = lines[idx + 1].strip()
                if next_line.startswith("path:"):
                    return next_line.replace("path:", "", 1).strip()
        return None

    def _search_dump_for_patterns(
        self,
        dump_path: str,
        patterns: List[str],
        max_matches: int,
        max_chars: int,
        case_sensitive: bool,
        use_regex: bool,
    ) -> List[dict]:
        try:
            with open(dump_path, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
        except OSError:
            return []

        results: List[dict] = []
        flags = 0 if case_sensitive else re.IGNORECASE
        compiled: List[re.Pattern] = []
        if use_regex:
            for p in patterns:
                try:
                    compiled.append(re.compile(p, flags))
                except re.error:
                    continue

        total_chars = 0
        for line_no, line in enumerate(lines, start=1):
            if max_matches > 0 and len(results) >= max_matches:
                break
            hit = False
            if use_regex and compiled:
                hit = any(r.search(line) for r in compiled)
            else:
                for p in patterns:
                    if case_sensitive:
                        if p in line:
                            hit = True
                            break
                    else:
                        if p.lower() in line.lower():
                            hit = True
                            break
            if hit:
                text = line.rstrip()
                total_chars += len(text)
                if max_chars > 0 and total_chars > max_chars:
                    break
                results.append({"line": line_no, "text": text})
        return results

    def _format_backfill_block(self, dump_path: str, matches: List[dict]) -> str:
        lines = [
            "[context_backfill]",
            f"dump_path: {dump_path}",
            "matches:",
        ]
        for m in matches:
            lines.append(f"- line: {m.get('line')}")
            lines.append(f"  text: {m.get('text')}")
        return "\n".join(lines)


