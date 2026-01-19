import unittest
from typing import List
from core.json_stream_filter import HybridJSONStreamFilter


class TestHybridJSONStreamFilter(unittest.TestCase):
    def setUp(self):
        self.parser = HybridJSONStreamFilter()

    def _collect_results(self, chunks: List[str]):
        """辅助函数：模拟流式输入并收集结果"""
        collected = {"thought": "", "prompt": "", "answer": ""}
        for chunk in chunks:
            events = self.parser.feed(chunk)
            for event_type, content in events:
                if event_type in collected:
                    collected[event_type] += content
        return collected

    def collect(self, chunks):
        """喂多个chunk，汇总为 {type: joined_text}，以及原始事件序列"""
        all_events = []
        for c in chunks:
            all_events.extend(self.parser.feed(c))
        joined = {}
        for t, ch in all_events:
            joined.setdefault(t, "")
            joined[t] += ch
        return joined, all_events

    def test_case_1_strict_json(self):
        """测试情况 2: 严格的 JSON 格式"""
        # 模拟 LLM 输出一个标准的 JSON
        json_text = '{"thought": "正在思考...", "action": {"type": "request_input", "prompt": "请输入姓名"}}'

        # 模拟一次性返回 (或大块返回)
        results = self._collect_results([json_text])

        self.assertEqual(results["thought"], "正在思考...")
        self.assertEqual(results["prompt"], "请输入姓名")
        # 验证非目标 Key (如 type, action) 没有被泄露进内容中
        print(f"\n[Strict JSON] Passed. Captured thought: {results['thought'][:10]}...")

    def test_case_2_markdown_json(self):
        """测试情况 1: Markdown 包裹的 JSON"""
        chunks = [
            "```json\n",
            '{\n  "thought": "Analysis complete.",\n',
            '  "action": {\n',
            '    "type": "finish",\n',
            '    "prompt": "Here is the result."\n',
            '  }\n',
            "}\n",
            "```"
        ]

        results = self._collect_results(chunks)

        # 你的过滤器逻辑在非 JSON 模式下会过滤掉 "`" 和 "json"
        # 且在遇到 "{" 后进入 JSON 模式
        self.assertEqual(results["thought"], "Analysis complete.")
        self.assertEqual(results["prompt"], "Here is the result.")
        print(f"\n[Markdown JSON] Passed. Captured prompt: {results['prompt']}")

    def test_case_3_implicit_thought_mixed(self):
        """测试情况 3: 缺少外层括号的 Thought + Action"""
        # 前半部分是纯文本，后半部分是 JSON 对象
        stream_input = [
            "Thought: 用户想查天气。",
            " 既然如此，",
            "我应该调用工具。\n",
            "Action: ",
            '{"tool_name": "weather", "type": "tool", "args": {}, "thought": "internal check"}'
        ]

        results = self._collect_results(stream_input)

        # 在遇到 '{' 之前，所有内容都应该被视为 thought
        # 注意：你的代码逻辑是遇到 '{' 才进入 JSON 模式。
        # Action: 后面的 '{' 会触发状态切换。
        # 只要 JSON 内部还有 "thought" 字段，它也会被追加到 thought 结果中。

        # 验证前导文本被捕获
        self.assertIn("用户想查天气", results["thought"])
        self.assertIn("Action: ", results["thought"])  # "Action: " 在 { 之前，所以也是 thought

        # 验证 JSON 内部的 thought 也被捕获 (这是符合预期的行为)
        self.assertIn("internal check", results["thought"])
        print(f"\n[Implicit Mixed] Passed. Total thought length: {len(results['thought'])}")

    def test_case_4_extreme_chunking(self):
        """测试极端流式碎片化：字符被切断"""
        # 模拟网络极差，Key 和 Value 被切断
        full_text = '{"thought": "broken stream", "action": {"prompt": "hello world"}}'

        # 将字符串切成每 2 个字符一个 chunk
        chunks = [full_text[i:i + 2] for i in range(0, len(full_text), 2)]

        results = self._collect_results(chunks)

        self.assertEqual(results["thought"], "broken stream")
        self.assertEqual(results["prompt"], "hello world")
        print(f"\n[Extreme Chunking] Passed. Chunks count: {len(chunks)}")

    def test_case_5_escaped_quotes(self):
        """测试 JSON 内部的转义引号"""
        # prompt 内容本身包含引号： He said "Hello"
        json_text = '{"action": {"prompt": "He said \\"Hello\\" to me."}}'

        results = self._collect_results([json_text])

        # 过滤器应该能识别转义，不提前结束字符串
        self.assertEqual(results["prompt"], 'He said \\"Hello\\" to me.')
        # 注意：你的逻辑是把 char 原样 append，所以反斜杠也会保留，这符合预期（交给前端处理显示）
        print(f"\n[Escaped Quotes] Passed. Result: {results['prompt']}")

    def test_case_6_ignored_keys(self):
        """测试忽略不相关的 Key"""
        # 测试 target_keys 之外的字段不会被输出
        json_text = '{"other_field": "Should ignore me", "thought": "Keep me"}'

        results = self._collect_results([json_text])

        self.assertEqual(results["thought"], "Keep me")
        self.assertNotIn("Should ignore", results["thought"])
        self.assertNotIn("Should ignore", results["prompt"])
        print(f"\n[Ignored Keys] Passed.")

    def test_case_7_finish_with_answer_streaming(self):
        """测试情况 4: 当 ActionType 为 finish 时，流式提取 answer 字段"""
        # 模拟 LLM 遵循新指令输出的 JSON，answer 字段包含总结性内容
        full_output = (
            '{\n'
            '  "thought": "任务已完成，正在生成总结...",\n'
            '  "action": {\n'
            '    "type": "finish",\n'
            '    "answer": "根据您的要求，我已经成功查询了北京的天气：晴，25度。任务全部完成。"\n'
            '  }\n'
            '}'
        )

        # 模拟非常细碎的流式输出（每 3 个字符一个 chunk），测试状态机鲁棒性
        chunks = [full_output[i:i + 3] for i in range(0, len(full_output), 3)]

        results = self._collect_results(chunks)

        # 验证 thought 是否正确
        self.assertEqual(results["thought"], "任务已完成，正在生成总结...")

        # 验证 answer 是否被正确完整提取
        self.assertEqual(results["answer"], "根据您的要求，我已经成功查询了北京的天气：晴，25度。任务全部完成。")
        # 验证 prompt 为空（因为 LLM 此时不应输出 prompt）
        self.assertEqual(results["prompt"], "")

        print(f"\n[Finish Answer] Passed. Answer length: {len(results['answer'])}")

    def test_case_8_mixed_mode_with_answer(self):
        """测试混合模式（Case 3）下包含 answer 的情况"""
        # 模拟一种极端情况：LLM 先自言自语，然后直接输出了带 answer 的 JSON
        chunks = [
            "分析完毕。直接输出结果：",  # Implicit thought
            '{"action": {"type": "finish", "answer": "Done!"}, "thought": "Final check."}'
        ]

        results = self._collect_results(chunks)

        # 验证 Implicit thought 和 JSON 里的 thought 都被捕获并拼接
        self.assertIn("分析完毕", results["thought"])
        self.assertIn("Final check", results["thought"])
        # 验证 answer
        self.assertEqual(results["answer"], "Done!")

        print(f"\n[Mixed Finish] Passed. Final Thought: {results['thought']}")

    def test_case_9_strict_json_extracts_thought_and_answer_only(self):
        chunks = [
            '{ "thought": "hi", "action": {"type":"finish","prompt": null, "answer":"hello"} }'
        ]
        joined, _ = self.collect(chunks)
        self.assertEqual(joined["thought"], "hi")
        self.assertNotIn("prompt", joined) # prompt 是 null，不应输出
        self.assertEqual(joined.get("answer"), "hello")

    def test_case_10_prompt_null_must_not_leak_next_key_as_value(self):
        """
        复现bug：prompt 为 null 之后，不应该把 'answer' 这几个字符当成 prompt 输出
        """
        chunks = [
            '{ "action": { "prompt": null, "answer": "OK" } }'
        ]
        joined, events = self.collect(chunks)

        # prompt 不应有输出
        assert "prompt" not in joined or joined["prompt"] == ""

        # answer 应该正确输出
        assert joined.get("answer") == "OK"

        # 更严格：事件流中不应出现 'a','n','s','w','e','r' 被当成 prompt
        leaked = "".join(ch for t, ch in events if t == "prompt")
        assert "answer" not in leaked

    def test_case_11_streaming_split_across_chunks_key_and_value(self):
        chunks = [
            '{ "thou',
            'ght": "用',
            '户打招呼", ',
            '"action": {"type":"finish","answer":"你好',
            '！有什么可以帮助你的吗？"} }'
        ]
        joined, _ = self.collect(chunks)
        assert joined.get("thought") == "用户打招呼"
        assert joined.get("answer") == "你好！有什么可以帮助你的吗？"

    def test_case_12_markdown_json_block_prefix_should_not_emit_thought_prefix_garbage(self):
        """
        Case1: ```json 前缀 + JSON
        非JSON模式下你会把前导文本当 implicit thought 输出，但你又写了过滤 ```json 的逻辑。
        这里验证不会把 ```json 里的字符输出成 thought（至少不会输出反引号等）。
        """
        chunks = [
            "```json\n",
            '{ "thought": "A", "action": {"answer":"B"} }\n',
            "```"
        ]
        joined, events = self.collect(chunks)
        assert joined.get("thought") == "A"
        assert joined.get("answer") == "B"

        # 不应把反引号输出为 thought
        thought_stream = "".join(ch for t, ch in events if t == "thought")
        assert "`" not in thought_stream

    def test_case_13_non_string_values_should_not_emit_anything(self):
        chunks = [
            '{ "thought": null, "prompt": true, "answer": 123, "x": {"answer":"NO"} }'
        ]
        joined, _ = self.collect(chunks)

        # 这些都是非字符串 value，不应输出
        assert "thought" not in joined
        assert "prompt" not in joined
        assert "answer" not in joined

    def test_case_14_implicit_thought_before_json_then_json_answer(self):
        """
        Case3: Thought: ... Action: ... 这类前导内容在进入 JSON 前会当 thought 输出。
        一旦进入 JSON，仍应能解析 answer。
        """
        chunks = [
            "Thought: 用户打招呼，需要回应。\nAction: ",
            '{ "action": {"answer": "你好"} }'
        ]
        joined, _ = self.collect(chunks)

        # 进入 JSON 前会吐一些 thought 字符，这里只断言包含关键句片段
        assert "用户打招呼" in (joined.get("thought") or "")
        assert joined.get("answer") == "你好"