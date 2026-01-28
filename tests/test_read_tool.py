import json
import os
import unittest
from core.tools.read import ReadTool


class TestReadTool(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tool = ReadTool()
        self.tmp_dir = os.path.join(os.getcwd(), "data", "test_read_head")
        os.makedirs(self.tmp_dir, exist_ok=True)
        self.file_path = os.path.join(self.tmp_dir, "sample.txt")
        with open(self.file_path, "w", encoding="utf-8") as f:
            for i in range(1, 11):
                f.write(f"line{i}\n")

    def tearDown(self):
        try:
            if os.path.exists(self.file_path):
                os.remove(self.file_path)
            if os.path.exists(self.tmp_dir):
                os.rmdir(self.tmp_dir)
        except OSError:
            pass

    async def test_read_head(self):
        res = await self.tool.execute(path=self.file_path, mode="head", max_lines=3)
        data = json.loads(res)
        self.assertIn("line1", data["content"])
        self.assertIn("line3", data["content"])
        self.assertNotIn("line4", data["content"])

    async def test_read_tail(self):
        res = await self.tool.execute(path=self.file_path, mode="tail", max_lines=2)
        data = json.loads(res)
        self.assertIn("line9", data["content"])
        self.assertIn("line10", data["content"])
        self.assertNotIn("line8", data["content"])

    async def test_read_truncate(self):
        res = await self.tool.execute(path=self.file_path, mode="head", max_lines=10, max_chars=10)
        data = json.loads(res)
        self.assertTrue(data["truncated"])
        self.assertLessEqual(len(data["content"]), 10)


if __name__ == "__main__":
    unittest.main()
