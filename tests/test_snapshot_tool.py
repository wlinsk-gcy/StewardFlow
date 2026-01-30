import json
import os
import unittest
from core.tools.snapshot_query import SnapshotQueryTool

class TestSnapshotQueryTool(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tool = SnapshotQueryTool()
        self.latest_path = os.path.join("data", "snapshot_latest.txt")

    async def test_uid_subtree_query_banner(self):
        """
        uid=5_1 banner 子树应包含：
        - 自身行：uid=5_1 banner
        - 子节点：5_2/5_3/5_4/5_5
        并且应在遇到同级 uid=5_6 link "发现" 之前停止（不应包含 5_6 行）
        """
        out = await self.tool.execute(
            **{
                "latest_path": self.latest_path,
                "uid": "5_1",
                "max_lines": 200,
                "include_ancestors": False,
            }
        )
        payload = json.loads(out)

        result = payload["result"]
        self.assertTrue(result["found"])
        self.assertEqual(result["mode"], "uid_subtree")

        text = result["text"]
        self.assertIn("uid=5_1 banner", text)
        self.assertIn('uid=5_2 link url="https://www.xiaohongshu.com/explore?channel_type=web_user_page"', text)
        self.assertIn('uid=5_3 textbox "搜索小红书"', text)
        self.assertIn('uid=5_4 button "创作中心"', text)
        self.assertIn('uid=5_5 button "业务合作"', text)

        # 子树范围应停止于同级节点 5_6 之前
        self.assertNotIn('uid=5_6 link "发现"', text)

    async def test_uid_subtree_with_ancestors(self):
        """
        include_ancestors=True 时，应包含 RootWebArea 等祖先链提示（至少包含 uid=5_0 RootWebArea 行）
        """
        out = await self.tool.execute(
            **{
                "latest_path": self.latest_path,
                "uid": "5_3",
                "max_lines": 120,
                "include_ancestors": True,
            }
        )
        payload = json.loads(out)
        result = payload["result"]
        self.assertTrue(result["found"])

        text = result["text"]
        self.assertIn('uid=5_0 RootWebArea "小胡19吖 - 小红书"', text)
        self.assertIn('uid=5_3 textbox "搜索小红书"', text)

    async def test_keyword_query(self):
        """
        keyword 模式应命中并用 >>> 标记命中行
        """
        out = await self.tool.execute(
            **{
                "latest_path": self.latest_path,
                "keyword": "搜索小红书",
                "max_lines": 120,
                "context_lines": 6,
            }
        )
        payload = json.loads(out)

        result = payload["result"]
        self.assertTrue(result["found"])
        self.assertEqual(result["mode"], "keyword_grep")
        self.assertGreaterEqual(result["matched"], 1)

        text = result["text"]
        self.assertIn(">>>", text)
        self.assertIn('uid=5_3 textbox "搜索小红书"', text)

    async def test_uid_not_found(self):
        out = await self.tool.execute(
            **{
                "latest_path": self.latest_path,
                "uid": "no_such_uid",
                "max_lines": 80,
            }
        )
        payload = json.loads(out)
        result = payload["result"]
        self.assertFalse(result["found"])
        self.assertIn("uid not found", result["reason"])

    async def test_keyword_truncation(self):
        """
        max_lines 很小时，keyword 查询应返回 truncated=True
        """
        out = await self.tool.execute(
            **{
                "latest_path": self.latest_path,
                "keyword": "沪ICP备",
                "max_lines": 12,
                "context_lines": 6,
            }
        )
        payload = json.loads(out)
        result = payload["result"]
        self.assertTrue(result["found"])
        self.assertTrue(result.get("truncated", False))
        self.assertLessEqual(result["returned_lines"], 12)