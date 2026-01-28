import json
import os
import unittest
from datetime import datetime

from core.context_manager import ContextManager, ContextManagerConfig


class DummyClient:
    def __init__(self, content: str = "[summary]\ntask_summary: dummy\n"):
        self._content = content
        self.chat = type("chat", (), {})()

        def _create(**kwargs):
            class DummyResp:
                class DummyChoice:
                    class DummyMsg:
                        content = self._content
                    message = DummyMsg()
                choices = [DummyChoice()]
            return DummyResp()

        self.chat.completions = type("completions", (), {"create": staticmethod(_create)})()


def make_traj(tool_name: str, content: str, args: dict | None = None) -> dict:
    return {
        "turn_id": "t1",
        "thought": {"content": "", "turn_id": "t1"},
        "action": {"type": "tool", "tool_name": tool_name, "args": args or {}},
        "observation": {"role": "tool", "content": content},
        "timestamp": "2026-01-27T00:00:00",
    }


class TestContextManager(unittest.TestCase):
    def setUp(self):
        self.dump_root = os.path.join(os.getcwd(), "data", "test_context_manager")
        os.makedirs(self.dump_root, exist_ok=True)

    def tearDown(self):
        try:
            if os.path.exists(self.dump_root):
                for name in os.listdir(self.dump_root):
                    try:
                        os.remove(os.path.join(self.dump_root, name))
                    except OSError:
                        pass
                os.rmdir(self.dump_root)
        except OSError:
            pass

    def test_estimate_tokens_minimum(self):
        cm = ContextManager(ContextManagerConfig(dry_run=True), DummyClient(), "dummy")
        self.assertEqual(cm._estimate_tokens([]), 1)

    def test_build_messages_tool_prefix(self):
        cm = ContextManager(ContextManagerConfig(dry_run=True), DummyClient(), "dummy")
        traj = [make_traj("bash", "ok", {"command": "echo ok"})]
        messages = cm._build_messages("task", "sys", traj, include_thought=False)
        user_msgs = [m["content"] for m in messages if m["role"] == "user"]
        self.assertTrue(any("工具执行结果：" in m for m in user_msgs))

    def test_compact_bash_content(self):
        cfg = ContextManagerConfig(bash_max_key_lines=2, dry_run=True)
        cm = ContextManager(cfg, DummyClient(), "dummy")
        payload = json.dumps({"output": "ok\nfail one\nfail two", "error": ""}, ensure_ascii=False)
        compacted = json.loads(cm._compact_bash_content(payload, {"args": {"command": "run"}}))
        self.assertEqual(compacted["type"], "bash_compact")
        self.assertEqual(compacted["command"], "run")
        self.assertLessEqual(len(compacted["key_output"]), 2)

    def test_compact_web_search_content(self):
        cfg = ContextManagerConfig(web_search_top_n=2, dry_run=True)
        cm = ContextManager(cfg, DummyClient(), "dummy")
        content = json.dumps(
            [
                {"title": "a", "link": "u1", "snippet": "s1"},
                {"title": "b", "link": "u2", "snippet": "s2"},
                {"title": "c", "link": "u3", "snippet": "s3"},
            ]
        )
        compacted = json.loads(cm._compact_web_search_content(content))
        self.assertEqual(compacted["type"], "web_search_compact")
        self.assertEqual(len(compacted["top_results"]), 2)

    def test_compact_chrome_devtools_content(self):
        cm = ContextManager(ContextManagerConfig(dry_run=True), DummyClient(), "dummy")
        compacted = json.loads(
            cm._compact_chrome_devtools_content("raw", {"tool_name": "chrome-devtools.foo", "args": {"url": "x"}})
        )
        self.assertEqual(compacted["type"], "chrome_devtools_compact")
        self.assertEqual(compacted["args"], {"url": "x"})

    def test_compact_ls_glob_content(self):
        cfg = ContextManagerConfig(ls_glob_max_items=1, dry_run=True)
        cm = ContextManager(cfg, DummyClient(), "dummy")
        content = json.dumps(
            [
                {"path": "a", "type": "file", "size": 1},
                {"path": "b", "type": "file", "size": 2},
            ]
        )
        compacted = json.loads(cm._compact_ls_glob_content(content, "ls"))
        self.assertEqual(compacted["type"], "ls_compact")
        self.assertEqual(compacted["total_items"], 2)
        self.assertEqual(len(compacted["items"]), 1)

    def test_compact_grep_content(self):
        cfg = ContextManagerConfig(grep_max_matches=1, dry_run=True)
        cm = ContextManager(cfg, DummyClient(), "dummy")
        content = json.dumps(
            [
                {"path": "a", "line": 1, "text": "hit"},
                {"path": "b", "line": 2, "text": "hit2"},
            ]
        )
        compacted = json.loads(cm._compact_grep_content(content))
        self.assertEqual(compacted["type"], "grep_compact")
        self.assertEqual(compacted["total_matches"], 2)
        self.assertEqual(len(compacted["matches"]), 1)

    def test_compact_read_content_truncates(self):
        cfg = ContextManagerConfig(read_max_chars=5, read_tail_chars=3, dry_run=True)
        cm = ContextManager(cfg, DummyClient(), "dummy")
        content = json.dumps(
            {
                "path": "p",
                "mode": "head",
                "content": "abcdefg",
                "truncated": False,
            }
        )
        compacted = json.loads(cm._compact_read_content(content))
        self.assertEqual(compacted["type"], "read_compact")
        self.assertTrue(compacted["truncated"])
        self.assertIn("...<tail>...", compacted["excerpt"])

    def test_compact_trajectory_respects_ratios(self):
        cfg = ContextManagerConfig(keep_recent_ratio=0.3, compaction_ratio=0.7, dry_run=True)
        cm = ContextManager(cfg, DummyClient(), "dummy")
        traj = [
            make_traj("bash", json.dumps({"output": "x" * 10, "error": ""}), {"command": "x"})
            for _ in range(10)
        ]
        compacted = cm._compact_trajectory(traj)
        compacted_count = sum(1 for t in compacted[:7] if "bash_compact" in t["observation"]["content"])
        self.assertEqual(compacted_count, 7)

    def test_apply_summary_block_keeps_recent(self):
        cfg = ContextManagerConfig(keep_recent_ratio=0.3, summarization_ratio=0.7, dry_run=True)
        cm = ContextManager(cfg, DummyClient(), "dummy")
        traj = [make_traj("bash", "ok", {"command": "x"}) for _ in range(10)]
        summarized = cm._apply_summary_block(traj, "[summary]\n...")
        self.assertEqual(len(summarized), 4)
        self.assertEqual(summarized[0]["action"]["tool_name"], "context_summary")

    def test_dump_full_history_format(self):
        cfg = ContextManagerConfig(dump_dir=self.dump_root, dry_run=True)
        cm = ContextManager(cfg, DummyClient(), "dummy")
        traj = [make_traj("bash", "ok", {"command": "x"})]
        path = cm._dump_full_history("agent1", traj)
        self.assertTrue(os.path.exists(path))
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
        self.assertIn("=== CONTEXT DUMP START ===", text)
        self.assertIn("=== CONTEXT DUMP END ===", text)

    def test_summarize_history_dry_run(self):
        cm = ContextManager(ContextManagerConfig(dry_run=True), DummyClient(), "dummy")
        summary = cm._summarize_history([make_traj("bash", "ok", {"command": "x"})])
        self.assertIn("[summary]", summary)

    def test_summarize_history_uses_client(self):
        client = DummyClient("[summary]\ntask_summary: real\n")
        cm = ContextManager(ContextManagerConfig(dry_run=False), client, "dummy")
        summary = cm._summarize_history([make_traj("bash", "ok", {"command": "x"})])
        self.assertIn("task_summary: real", summary)

    def test_find_dump_path_in_trajectory(self):
        cm = ContextManager(ContextManagerConfig(dry_run=True), DummyClient(), "dummy")
        summary_block = "[summary]\n...\n\n[context_dump]\npath: data/context_dump_x.txt\nhint: use grep"
        traj = [
            {
                "turn_id": "summary",
                "thought": {"content": "", "turn_id": "summary"},
                "action": {"type": "tool", "tool_name": "context_summary", "args": {}},
                "observation": {"role": "user", "content": summary_block},
                "timestamp": datetime.utcnow().isoformat(),
            }
        ]
        path = cm._find_dump_path_in_trajectory(traj)
        self.assertEqual(path, "data/context_dump_x.txt")

    def test_backfill_appended(self):
        dump_path = os.path.join(self.dump_root, "context_dump_test.txt")
        with open(dump_path, "w", encoding="utf-8") as f:
            f.write("line1\nerror: bad\nline3\n")

        cfg = ContextManagerConfig(dry_run=True)
        cm = ContextManager(cfg, DummyClient(), "dummy")
        traj = [make_traj("bash", "ok", {"command": "x"})]
        messages, audit = cm.build_messages(
            {
                "task": "do",
                "tao_trajectory": traj,
                "context_dump_path": dump_path,
                "backfill_patterns": ["error"],
            },
            "sys",
        )
        backfill_msgs = [m for m in messages if m["role"] == "user" and "[context_backfill]" in m["content"]]
        self.assertEqual(len(backfill_msgs), 1)

    def test_auto_backfill_triggered(self):
        dump_path = os.path.join(self.dump_root, "context_dump_auto.txt")
        with open(dump_path, "w", encoding="utf-8") as f:
            f.write("line1\nerror: bad\nline3\n")

        cfg = ContextManagerConfig(dry_run=True, auto_backfill_enabled=True)
        cm = ContextManager(cfg, DummyClient(), "dummy")
        traj = [make_traj("bash", "ok", {"command": "x"})]
        messages, audit = cm.build_messages(
            {
                "task": "请查看之前报错并找出原因",
                "tao_trajectory": traj,
                "context_dump_path": dump_path,
            },
            "sys",
        )
        backfill_msgs = [m for m in messages if m["role"] == "user" and "[context_backfill]" in m["content"]]
        self.assertEqual(len(backfill_msgs), 1)

    def test_auto_backfill_not_triggered(self):
        dump_path = os.path.join(self.dump_root, "context_dump_auto_none.txt")
        with open(dump_path, "w", encoding="utf-8") as f:
            f.write("line1\nerror: bad\nline3\n")

        cfg = ContextManagerConfig(dry_run=True, auto_backfill_enabled=True)
        cm = ContextManager(cfg, DummyClient(), "dummy")
        traj = [make_traj("bash", "ok", {"command": "x"})]
        messages, audit = cm.build_messages(
            {
                "task": "继续执行即可",
                "tao_trajectory": traj,
                "context_dump_path": dump_path,
            },
            "sys",
        )
        backfill_msgs = [m for m in messages if m["role"] == "user" and "[context_backfill]" in m["content"]]
        self.assertEqual(len(backfill_msgs), 0)

    def test_compaction_triggered(self):
        bash_payload = json.dumps({"output": "x" * 5000, "error": ""}, ensure_ascii=False)
        traj = [
            make_traj("bash", bash_payload, {"command": "echo test"}),
            make_traj("bash", "ok", {"command": "echo ok"}),
        ]

        probe_cfg = ContextManagerConfig(pre_rot_threshold_tokens=10**9, dry_run=True)
        probe = ContextManager(probe_cfg, DummyClient(), "dummy-model")
        raw_messages = probe._build_messages("do", "sys", traj, include_thought=False)
        raw_est = probe._estimate_tokens(raw_messages)
        compacted_traj = probe._compact_trajectory(traj)
        compacted_messages = probe._build_messages("do", "sys", compacted_traj, include_thought=False)
        compacted_est = probe._estimate_tokens(compacted_messages)
        self.assertGreater(raw_est, compacted_est)

        threshold = (raw_est + compacted_est) // 2
        cfg = ContextManagerConfig(pre_rot_threshold_tokens=threshold, dry_run=True)
        cm = ContextManager(cfg, DummyClient(), "dummy-model")

        messages, audit = cm.build_messages({"task": "do", "tao_trajectory": traj}, "sys")
        self.assertEqual(audit["stage"], "compaction")
        joined = "\n".join(m["content"] for m in messages if m["role"] == "user")
        self.assertIn("bash_compact", joined)

    def test_summarization_triggered_and_dumped(self):
        dump_dir = os.path.join(self.dump_root, "summarize")
        cfg = ContextManagerConfig(pre_rot_threshold_tokens=80, dump_dir=dump_dir, dry_run=True)
        cm = ContextManager(cfg, DummyClient(), "dummy-model")

        big = "y" * 2000
        traj = [make_traj("unknown_tool", big) for _ in range(4)]

        messages, audit = cm.build_messages({"task": "do", "tao_trajectory": traj, "agent_id": "agent1"}, "sys")
        self.assertEqual(audit["stage"], "summarization")
        self.assertTrue(audit["dump_path"])
        self.assertTrue(os.path.exists(audit["dump_path"]))

        joined = "\n".join(m["content"] for m in messages if m["role"] == "user")
        self.assertIn("[summary]", joined)


if __name__ == "__main__":
    unittest.main()

