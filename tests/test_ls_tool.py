import json
import os
import unittest
from core.tools.ls import LsTool


class TestLsTool(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tool = LsTool()
        self.tmp_dir = os.path.join(os.getcwd(), "data", "test_ls")
        os.makedirs(self.tmp_dir, exist_ok=True)
        self.file_path = os.path.join(self.tmp_dir, "a.txt")
        self.sub_dir = os.path.join(self.tmp_dir, "sub")
        os.makedirs(self.sub_dir, exist_ok=True)
        with open(self.file_path, "w", encoding="utf-8") as f:
            f.write("hello")

    def tearDown(self):
        try:
            if os.path.exists(self.file_path):
                os.remove(self.file_path)
            if os.path.exists(self.sub_dir):
                os.rmdir(self.sub_dir)
            if os.path.exists(self.tmp_dir):
                os.rmdir(self.tmp_dir)
        except OSError:
            pass

    async def test_ls_non_recursive(self):
        res = await self.tool.execute(path=self.tmp_dir, recursive=False)
        data = json.loads(res)
        paths = [item["path"] for item in data]
        self.assertIn(self.file_path, paths)
        self.assertIn(self.sub_dir, paths)

    async def test_ls_recursive(self):
        nested_file = os.path.join(self.sub_dir, "b.txt")
        with open(nested_file, "w", encoding="utf-8") as f:
            f.write("x")
        res = await self.tool.execute(path=self.tmp_dir, recursive=True)
        data = json.loads(res)
        paths = [item["path"] for item in data]
        self.assertIn(nested_file, paths)
        os.remove(nested_file)

    async def test_ls_filters(self):
        res = await self.tool.execute(path=self.tmp_dir, recursive=False, include_dirs=False)
        data = json.loads(res)
        types = {item["type"] for item in data}
        self.assertEqual(types, {"file"})


if __name__ == "__main__":
    unittest.main()
