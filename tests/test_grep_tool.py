import json
import os
import unittest
from core.tools.grep import GrepTool


class TestGrepTool(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tool = GrepTool()
        self.tmp_dir = os.path.join(os.getcwd(), "data", "test_grep")
        os.makedirs(self.tmp_dir, exist_ok=True)
        self.file_path = os.path.join(self.tmp_dir, "sample.txt")
        with open(self.file_path, "w", encoding="utf-8") as f:
            f.write("hello world\n")
            f.write("foo bar\n")
            f.write("HELLO AGAIN\n")

    def tearDown(self):
        try:
            if os.path.exists(self.file_path):
                os.remove(self.file_path)
            if os.path.exists(self.tmp_dir):
                os.rmdir(self.tmp_dir)
        except OSError:
            pass

    async def test_grep_case_insensitive(self):
        res = await self.tool.execute(pattern="hello", path=self.tmp_dir, recursive=True)
        data = json.loads(res)
        self.assertEqual(len(data), 2)
        self.assertTrue(any("hello world" in m["text"].lower() for m in data))
        self.assertTrue(any("hello again" in m["text"].lower() for m in data))

    async def test_grep_case_sensitive(self):
        res = await self.tool.execute(pattern="HELLO", path=self.tmp_dir, recursive=True, case_sensitive=True)
        data = json.loads(res)
        self.assertEqual(len(data), 1)
        self.assertIn("HELLO AGAIN", data[0]["text"])

    async def test_grep_max_results(self):
        res = await self.tool.execute(pattern="o", path=self.tmp_dir, recursive=True, max_results=1)
        data = json.loads(res)
        self.assertEqual(len(data), 1)


if __name__ == "__main__":
    unittest.main()
