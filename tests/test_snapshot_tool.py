import json
import os
import re
import unittest

from core.tools.snapshot_query import SnapshotQueryTool


UID_ROLE_RE = re.compile(r'uid=([^\s]+)\s+([^\s]+)')          # uid=1_1 banner
UID_BANNER_RE = re.compile(r'uid=([^\s]+)\s+banner')         # uid=... banner
UID_TEXTBOX_RE = re.compile(r'uid=([^\s]+)\s+textbox')       # uid=... textbox


class TestSnapshotQueryTool(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tool = SnapshotQueryTool()
        self.paths = {
            "snapshot_latest.txt": os.path.join("data", "snapshot_latest.txt"),
            "wait_for_log_latest.txt": os.path.join("data", "wait_for_log_latest.txt"),
        }

    def _skip_if_missing(self, path: str):
        if not os.path.exists(path):
            raise unittest.SkipTest(f"missing file: {path}")

    async def _grep(self, latest_path: str, keyword: str, max_lines: int = 300, context_lines: int = 6):
        out = await self.tool.execute(
            **{
                "latest_path": latest_path,
                "keyword": keyword,
                "max_lines": max_lines,
                "context_lines": context_lines,
                "search_scope": "snapshot",
            }
        )
        return json.loads(out)

    def _extract_first_uid_by_regex(self, text: str, rx: re.Pattern) -> str:
        m = rx.search(text)
        if not m:
            return ""
        return m.group(1)

    async def _discover_banner_uid(self, latest_path: str) -> str:
        """
        通过 keyword=banner 找到第一个 banner 的 uid
        """
        payload = await self._grep(latest_path, keyword="banner", max_lines=400, context_lines=2)
        result = payload["result"]
        if not result.get("found"):
            return ""
        return self._extract_first_uid_by_regex(result["text"], UID_BANNER_RE)

    async def _discover_textbox_uid(self, latest_path: str) -> str:
        """
        通过 keyword=搜索小红书 找到 textbox 的 uid（更稳定）
        """
        payload = await self._grep(latest_path, keyword="搜索小红书", max_lines=400, context_lines=4)
        result = payload["result"]
        if not result.get("found"):
            return ""
        # 尽量取 textbox 那行
        uid = self._extract_first_uid_by_regex(result["text"], UID_TEXTBOX_RE)
        if uid:
            return uid
        # 兜底：随便取一个 uid（极少发生）
        m = UID_ROLE_RE.search(result["text"])
        return m.group(1) if m else ""

    async def test_uid_subtree_query_banner_both_files(self):
        """
        对两个文件都测试 banner 的 uid_subtree：
        - 必须 found=True
        - 必须包含自身 banner 行
        - 通常应包含搜索框/创作中心等 banner 内元素（不强行写死 uid）
        - 不应包含 banner 的同级节点（典型是 link "发现"）
        """
        for name, path in self.paths.items():
            with self.subTest(file=name):
                self._skip_if_missing(path)

                banner_uid = await self._discover_banner_uid(path)
                self.assertTrue(banner_uid, f"cannot discover banner uid from {name}")

                out = await self.tool.execute(
                    **{
                        "latest_path": path,
                        "uid": banner_uid,
                        "max_lines": 250,
                        "include_ancestors": False,
                        "search_scope": "snapshot",
                    }
                )
                payload = json.loads(out)
                result = payload["result"]

                self.assertTrue(result["found"], f"uid_subtree not found for banner uid={banner_uid} in {name}")
                self.assertEqual(result["mode"], "uid_subtree")

                text = result["text"]
                self.assertIn(f"uid={banner_uid} banner", text)

                # banner 子树里通常会有这些关键元素（任意命中一个即可，避免页面微调导致测试脆弱）
                self.assertTrue(
                    ('textbox "搜索小红书"' in text) or ('button "创作中心"' in text) or ('button "业务合作"' in text),
                    f"banner subtree seems incomplete in {name}",
                )

                # banner 的同级一般包含 “发现”，不应被包含进子树
                self.assertNotIn('link "发现"', text)

    async def test_uid_subtree_with_ancestors_both_files(self):
        """
        include_ancestors=True 时：
        - 应包含 RootWebArea 祖先链（不写死 uid）
        - 应包含目标 textbox 行（不写死 uid）
        """
        for name, path in self.paths.items():
            with self.subTest(file=name):
                self._skip_if_missing(path)

                textbox_uid = await self._discover_textbox_uid(path)
                self.assertTrue(textbox_uid, f"cannot discover textbox uid from {name}")

                out = await self.tool.execute(
                    **{
                        "latest_path": path,
                        "uid": textbox_uid,
                        "max_lines": 220,
                        "include_ancestors": True,
                        "search_scope": "snapshot",
                    }
                )
                payload = json.loads(out)
                result = payload["result"]

                self.assertTrue(result["found"])
                self.assertEqual(result["mode"], "uid_subtree")

                text = result["text"]
                self.assertIn("RootWebArea", text)
                self.assertIn(f"uid={textbox_uid} textbox", text)

    async def test_keyword_query_both_files(self):
        """
        keyword_grep：
        - found=True
        - mode=keyword_grep
        - matched>=1
        - text 含 >>> 标记
        """
        for name, path in self.paths.items():
            with self.subTest(file=name):
                self._skip_if_missing(path)

                out = await self.tool.execute(
                    **{
                        "latest_path": path,
                        "keyword": "搜索小红书",
                        "max_lines": 200,
                        "context_lines": 6,
                        "search_scope": "snapshot",
                    }
                )
                payload = json.loads(out)
                result = payload["result"]

                self.assertTrue(result["found"])
                self.assertEqual(result["mode"], "keyword_grep")
                self.assertGreaterEqual(result["matched"], 1)

                text = result["text"]
                self.assertIn(">>>", text)
                self.assertIn('textbox "搜索小红书"', text)

    async def test_uid_not_found_both_files(self):
        for name, path in self.paths.items():
            with self.subTest(file=name):
                self._skip_if_missing(path)

                out = await self.tool.execute(
                    **{
                        "latest_path": path,
                        "uid": "no_such_uid",
                        "max_lines": 80,
                        "search_scope": "snapshot",
                    }
                )
                payload = json.loads(out)
                result = payload["result"]
                self.assertFalse(result["found"])
                self.assertIn("uid not found", result["reason"])

    async def test_keyword_truncation_both_files(self):
        """
        max_lines 很小时，keyword 查询应 truncated=True
        """
        for name, path in self.paths.items():
            with self.subTest(file=name):
                self._skip_if_missing(path)

                out = await self.tool.execute(
                    **{
                        "latest_path": path,
                        "keyword": "沪ICP备",
                        "max_lines": 12,
                        "context_lines": 6,
                        "search_scope": "snapshot",
                    }
                )
                payload = json.loads(out)
                result = payload["result"]
                self.assertTrue(result["found"])
                self.assertTrue(result.get("truncated", False))
                self.assertLessEqual(result["returned_lines"], 12)

    async def test_meta_found_line_wait_for(self):
        """
        额外验证 meta：
        - wait_for_log_latest.txt 应该能提取到 found_line（通常是 Element with text ... found.）
        - snapshot_latest.txt 的 found_line 通常为空（允许为空）
        """
        snap_path = self.paths["snapshot_latest.txt"]
        wait_path = self.paths["wait_for_log_latest.txt"]

        self._skip_if_missing(snap_path)
        self._skip_if_missing(wait_path)

        snap_payload = await self._grep(snap_path, keyword="搜索小红书", max_lines=80, context_lines=2)
        self.assertIn("meta", snap_payload)
        self.assertIn("found_line", snap_payload["meta"])  # 允许为空

        wait_payload = await self._grep(wait_path, keyword="搜索小红书", max_lines=80, context_lines=2)
        self.assertIn("meta", wait_payload)
        self.assertIn("found_line", wait_payload["meta"])
        self.assertTrue(
            wait_payload["meta"]["found_line"] == "" or wait_payload["meta"]["found_line"].startswith("Element with text "),
            f"Unexpected found_line: {wait_payload['meta']['found_line']}",
        )


if __name__ == "__main__":
    unittest.main()
