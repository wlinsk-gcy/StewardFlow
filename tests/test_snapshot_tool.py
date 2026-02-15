import json
import shutil
import unittest
from pathlib import Path

from core.tool_result_externalizer import ToolResultExternalizerConfig, ToolResultExternalizerMiddleware
from core.tools.fs_tools import FsReadTool
from core.tools.text_search import TextSearchTool


class TestSnapshotRefFlow(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.externalizer = ToolResultExternalizerMiddleware(
            ToolResultExternalizerConfig(
                inline_limit=80,
                preview_limit=80,
                root_dir="data/tool_results",
                always_externalize_tools={"chrome-devtools_take_snapshot"},
            )
        )

    def tearDown(self):
        test_trace_dir = Path("data/tool_results/test_trace")
        if test_trace_dir.exists():
            shutil.rmtree(test_trace_dir, ignore_errors=True)

    async def test_ref_then_text_search_then_fs_read(self):
        large_snapshot = "\n".join(
            [f'uid={i}_1 StaticText "row {i}"' for i in range(1, 120)]
            + ['uid=999_1 button "TargetButton"']
        )

        observation = self.externalizer.externalize(
            tool_name="chrome-devtools_take_snapshot",
            raw_result=large_snapshot,
            trace_id="test_trace",
            turn_id="turn_1",
            step_id="step_1",
            tool_call_id="call_1",
        )

        self.assertEqual(observation["kind"], "ref")
        ref_path = observation["ref"]["path"]
        self.assertTrue(Path(ref_path).exists())

        text_search = TextSearchTool()
        search_res = await text_search.execute(path=ref_path, query="TargetButton", max_matches=5)
        search_data = json.loads(search_res)
        self.assertTrue(search_data["ok"])
        self.assertGreaterEqual(len(search_data["matches"]), 1)

        full_text = Path(ref_path).read_text(encoding="utf-8")
        offset = full_text.index("TargetButton")

        fs_read = FsReadTool()
        read_res = await fs_read.execute(path=ref_path, offset=offset - 20, length=80)
        read_data = json.loads(read_res)
        self.assertTrue(read_data["ok"])
        self.assertIn("TargetButton", read_data["text"])


class TestToolResultExternalizer(unittest.TestCase):
    def setUp(self):
        self.externalizer = ToolResultExternalizerMiddleware(
            ToolResultExternalizerConfig(
                inline_limit=20,
                preview_limit=20,
                root_dir="data/tool_results/test_ext",
            )
        )

    def tearDown(self):
        test_ext_dir = Path("data/tool_results/test_ext")
        if test_ext_dir.exists():
            shutil.rmtree(test_ext_dir, ignore_errors=True)

    def test_inline_branch(self):
        obs = self.externalizer.externalize(
            tool_name="fs_stat",
            raw_result='{"ok":true}',
            trace_id="trace_a",
            turn_id="turn_a",
            step_id="step_a",
            tool_call_id="call_a",
        )
        self.assertEqual(obs["kind"], "inline")
        self.assertIn("content", obs)
        self.assertNotIn("ref", obs)

    def test_ref_branch_and_unique_paths(self):
        obs1 = self.externalizer.externalize(
            tool_name="text_search",
            raw_result="x" * 200,
            trace_id="trace_a",
            turn_id="turn_a",
            step_id="step_a",
            tool_call_id="call_same",
        )
        obs2 = self.externalizer.externalize(
            tool_name="text_search",
            raw_result="x" * 200,
            trace_id="trace_a",
            turn_id="turn_a",
            step_id="step_a",
            tool_call_id="call_same",
        )
        self.assertEqual(obs1["kind"], "ref")
        self.assertEqual(obs2["kind"], "ref")
        self.assertTrue(Path(obs1["ref"]["path"]).exists())
        self.assertTrue(Path(obs2["ref"]["path"]).exists())
        self.assertNotEqual(obs1["ref"]["path"], obs2["ref"]["path"])


class TestSandboxGuards(unittest.IsolatedAsyncioTestCase):
    async def test_fs_read_rejects_absolute_and_parent_paths(self):
        tool = FsReadTool()
        abs_path = str((Path.cwd() / "README.md").resolve())
        abs_res = await tool.execute(path=abs_path, offset=0, length=20)
        abs_data = json.loads(abs_res)
        self.assertFalse(abs_data["ok"])

        parent_res = await tool.execute(path="../README.md", offset=0, length=20)
        parent_data = json.loads(parent_res)
        self.assertFalse(parent_data["ok"])

    async def test_text_search_rejects_absolute_paths(self):
        tool = TextSearchTool()
        abs_path = str((Path.cwd() / "README.md").resolve())
        res = await tool.execute(path=abs_path, query="StewardFlow")
        data = json.loads(res)
        self.assertFalse(data["ok"])


if __name__ == "__main__":
    unittest.main()
