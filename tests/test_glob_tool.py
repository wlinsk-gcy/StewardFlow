import json
import os
import unittest
from core.tools.glob import GlobTool


class TestGlobTool(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tool = GlobTool()
        self.tmp_dir = os.path.join(os.getcwd(), "data", "test_glob")
        self.sub_dir = os.path.join(self.tmp_dir, "sub")
        os.makedirs(self.sub_dir, exist_ok=True)
        self.file_a = os.path.join(self.tmp_dir, "a.txt")
        self.file_b = os.path.join(self.sub_dir, "b.log")
        with open(self.file_a, "w", encoding="utf-8") as f:
            f.write("a")
        with open(self.file_b, "w", encoding="utf-8") as f:
            f.write("b")

    def tearDown(self):
        try:
            if os.path.exists(self.file_a):
                os.remove(self.file_a)
            if os.path.exists(self.file_b):
                os.remove(self.file_b)
            if os.path.exists(self.sub_dir):
                os.rmdir(self.sub_dir)
            if os.path.exists(self.tmp_dir):
                os.rmdir(self.tmp_dir)
        except OSError:
            pass

    async def test_glob_non_recursive(self):
        res = await self.tool.execute(pattern="*.txt", base_path=self.tmp_dir, recursive=False)
        data = json.loads(res)
        paths = [item["path"] for item in data]
        self.assertIn(self.file_a, paths)
        self.assertNotIn(self.file_b, paths)

    async def test_glob_recursive(self):
        res = await self.tool.execute(pattern="**/*.log", base_path=self.tmp_dir, recursive=True)
        data = json.loads(res)
        paths = [item["path"] for item in data]
        self.assertIn(self.file_b, paths)


if __name__ == "__main__":
    unittest.main()