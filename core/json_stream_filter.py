class HybridJSONStreamFilter:

    def __init__(self):
        self.buffer = ""
        self.in_json = False
        self.in_string = False

        self.key_buffer = ""
        self.target_keys = ["thought", "prompt", "answer"]

        self.pending_key = None
        self.expect_value = False
        self.current_key = None

        self.obj_depth = 0
        self.in_action = False
        self.action_depth = None
        self.next_obj_is_action = False  # 看到 "action": 之后，等待其 value 为 { 时进入 action

        # --- Markdown fence 兼容 ---
        self.fence_buf = ""     # 用于检测 ```
        self.in_fence = False   # 是否在 ``` ... ``` 内

        self.implicit_thought_buffer = ""

    def reset_json_state(self):
        self.buffer = ""
        self.in_string = False
        self.key_buffer = ""
        self.pending_key = None
        self.expect_value = False
        self.current_key = None

        self.obj_depth = 0
        self.in_action = False
        self.action_depth = None
        self.next_obj_is_action = False

    def is_escaped(self, s):
        if not s:
            return False
        count = 0
        for c in reversed(s):
            if c == '\\':
                count += 1
            else:
                break
        return count % 2 == 1

    def _allow_capture_key(self, key: str) -> bool:
        # 方案B规则
        if key == "thought":
            return self.obj_depth == 1 and (not self.in_action)
        if key in ("prompt", "answer"):
            return bool(self.in_action)
        return False

    def _update_fence(self, ch: str) -> bool:
        """
        检测 ``` fence。
        返回 True 表示本字符参与 fence 检测并被消费（建议外部 continue），
        以避免把 fence 字符误当成 implicit thought 输出。
        """
        # 只在非 JSON 模式下处理 fence（JSON 内不该出现 fence，出现也不处理）
        self.fence_buf += ch
        if len(self.fence_buf) > 3:
            self.fence_buf = self.fence_buf[-3:]

        if self.fence_buf == "```":
            self.in_fence = not self.in_fence
            self.fence_buf = ""
            return True

        # 在 fence 内，所有字符都“被消费”（不输出 implicit thought）
        if self.in_fence:
            return True

        return False

    def feed(self, chunk: str) -> list[tuple[str, str]]:
        events = []

        for char in chunk:
            # --- 非 JSON 模式 ---
            if not self.in_json:
                # 先看是否进入 JSON（即使在 fence 内也允许进入）
                if char == '{':
                    self.in_json = True
                    self.reset_json_state()
                    self.obj_depth = 1  # root
                    continue

                # 处理 markdown fence：在 ``` ... ``` 中不输出 implicit thought
                if self._update_fence(char):
                    continue

                # 保留你原来的 “```json” 忽略逻辑（非 fence 场景也能工作）
                self.buffer += char
                if len(self.buffer) > 10:
                    self.buffer = self.buffer[-10:]

                if "`" in self.buffer:
                    continue
                if "json" in self.buffer.lower():
                    continue

                events.append(("thought", char))
                continue

            # --- JSON 模式 ---

            # 2.0 字符串外：维护对象层级与 action 上下文
            if not self.in_string:
                if char == '{':
                    self.obj_depth += 1
                    if self.next_obj_is_action:
                        self.in_action = True
                        self.action_depth = self.obj_depth
                        self.next_obj_is_action = False
                        self.expect_value = False
                        self.pending_key = None
                    continue

                if char == '}':
                    # 退出 action 对象
                    if self.in_action and self.action_depth == self.obj_depth:
                        self.in_action = False
                        self.action_depth = None

                    self.obj_depth -= 1

                    # root 结束：退出 JSON 模式（但如果后面还有 ```，会被 fence 逻辑吞掉）
                    if self.obj_depth <= 0:
                        self.in_json = False
                        self.reset_json_state()
                    continue

            # 2.1 字符串外：处理冒号与期待 value
            if not self.in_string:
                if char == ":" and self.pending_key:
                    self.expect_value = True
                    # 如果 key 是 action，期待它的 value 为 object
                    if self.pending_key == "action":
                        self.next_obj_is_action = True
                    continue

                # 冒号后第一个非空白字符：判断 value 类型
                if self.expect_value and not char.isspace():
                    if char == '"':
                        # value 是字符串：只有在允许捕获的上下文才开始捕获
                        if self.pending_key and self._allow_capture_key(self.pending_key):
                            self.current_key = self.pending_key
                        else:
                            self.current_key = None

                        self.pending_key = None
                        self.expect_value = False
                        # 后面会走 quote 切换 in_string
                    else:
                        # value 非字符串：清掉，防串台
                        self.pending_key = None
                        self.expect_value = False
                        self.current_key = None
                        # 注意：如果这里是 action 的 value 且为 '{'，已在上面的 '{' 分支处理
                        # 其它类型（null/true/false/数字/数组）全部忽略

            # 2.2 处理引号：切换字符串状态
            if char == '"' and not self.is_escaped(self.buffer):
                self.in_string = not self.in_string

                if not self.in_string:
                    # 字符串结束
                    if self.current_key in self.target_keys and self.current_key is not None:
                        # 结束的是 value
                        self.current_key = None
                    else:
                        # 结束的是 key
                        clean_key = self.key_buffer.strip()
                        self.key_buffer = ""

                        if clean_key == "action":
                            self.pending_key = "action"
                        elif clean_key in self.target_keys:
                            self.pending_key = clean_key
                        else:
                            self.pending_key = None

                        self.expect_value = False

                self.buffer = ""
                continue

            # 用于转义检测
            self.buffer += char

            # 2.3 字符串内部：输出 value 或累计 key
            if self.in_string:
                if self.current_key:
                    events.append((self.current_key, char))
                else:
                    self.key_buffer += char

        return events
