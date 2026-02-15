import json
import shutil
import unittest
from pathlib import Path
from unittest.mock import patch

from core.tools.text_search import TextSearchTool


class TestTextSearchTool(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.base_dir = Path("data/test_text_search")
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.snapshot_file = self.base_dir / "snapshot.txt"
        self.snapshot_file.write_text(
            'uid=5_1 StaticText "home"\n'
            'uid=5_2 button "login"\n'
            'uid=5_3 button "register"\n'
            'uid=5_4 StaticText "other content"\n',
            encoding="utf-8",
        )

    def tearDown(self):
        if self.base_dir.exists():
            shutil.rmtree(self.base_dir, ignore_errors=True)

    async def test_batch_queries_on_snapshot(self):
        tool = TextSearchTool()
        res = await tool.execute(
            path="data/test_text_search/snapshot.txt",
            queries=["home", "login", "register"],
            is_regex=False,
            context_lines=0,
            max_matches=20,
        )
        data = json.loads(res)
        self.assertTrue(data["ok"])
        self.assertFalse(data["truncated"])
        self.assertGreaterEqual(len(data["matches"]), 3)
        self.assertTrue(any(m.get("line") == 1 for m in data["matches"]))
        self.assertTrue(any(m.get("line") == 2 for m in data["matches"]))
        self.assertTrue(any(m.get("line") == 3 for m in data["matches"]))
        self.assertTrue(any(m.get("uid") == "5_2" for m in data["matches"]))
        self.assertIn(data["summary"]["engine"], {"rg", "python"})

    async def test_reject_absolute_path(self):
        tool = TextSearchTool()
        abs_path = str(self.snapshot_file.resolve())
        res = await tool.execute(path=abs_path, query="home")
        data = json.loads(res)
        self.assertFalse(data["ok"])

    async def test_directory_scan_skips_out_of_roots_after_resolve(self):
        tool = TextSearchTool()
        scan_dir = self.base_dir / "scan"
        scan_dir.mkdir(parents=True, exist_ok=True)
        inside_file = scan_dir / "inside.txt"
        outside_like_file = scan_dir / "outside_like.txt"
        inside_file.write_text("needle-inside\n", encoding="utf-8")
        outside_like_file.write_text("needle-outside\n", encoding="utf-8")

        blocked_resolved = outside_like_file.resolve()
        rg_seen = {}
        py_seen = {}

        def fake_assert(path, roots):
            del roots
            if path == blocked_resolved:
                raise PermissionError("path_outside_allowed_roots")
            return None

        def fake_search_with_rg(*, files, normalized_queries, is_regex, case_sensitive):
            del normalized_queries, is_regex, case_sensitive
            rg_seen["files"] = [str(f.resolve()) for f in files]
            self.assertNotIn(str(blocked_resolved), rg_seen["files"])
            raise RuntimeError("force_python_fallback")

        def fake_search_with_python(*, files, normalized_queries, is_regex, case_sensitive):
            del normalized_queries, is_regex, case_sensitive
            py_seen["files"] = [str(f.resolve()) for f in files]
            self.assertNotIn(str(blocked_resolved), py_seen["files"])
            return []

        with patch("core.tools.text_search.assert_path_in_allowed_roots", side_effect=fake_assert), \
             patch("core.tools.text_search._search_with_rg", side_effect=fake_search_with_rg), \
             patch("core.tools.text_search._search_with_python", side_effect=fake_search_with_python):
            res = await tool.execute(path="data/test_text_search/scan", query="needle", recursive=True)

        data = json.loads(res)
        self.assertTrue(data["ok"])
        self.assertEqual(data["summary"]["engine"], "python")
        self.assertEqual(data["summary"]["skipped_out_of_roots"], 1)
        self.assertEqual(data["summary"]["searched_files"], 1)
        self.assertEqual(len(rg_seen["files"]), 1)
        self.assertEqual(len(py_seen["files"]), 1)


if __name__ == "__main__":
    unittest.main()
