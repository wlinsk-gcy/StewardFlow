# StewardFlow 手工 E2E Cases（真实工具调用）

说明：
- 这些 case 用于前端输入框手工粘贴执行，要求真实触发 LLM `tool_calls`，不使用 mock。
- `proc_run` 需要确认时，请在 HITL 确认里输入 `yes`。
- 对 `proc_run` 文本生成，优先 `program="powershell"`（或 `powershell.exe`），避免 `python` 环境注入导致不稳定输出。
- 每个 case 建议单独新建一个 trace，便于按 `trace_id` grep 控制台日志。
- 判定通过以控制台 `event_log {...}` 为准。

---

## E2E-01: proc_run 小输出 -> inline

**目的**
- 覆盖 `proc_run` 小输出，验证 `externalize.kind=inline`。

**输入文本（直接粘贴）**
```text
你必须严格按步骤执行，不允许跳过工具调用：
1) 必须调用 proc_run，使用 powershell（若失败再用 powershell.exe）输出三行小文本：
   - E2E01_OK
   - alpha
   - beta
2) 不要调用其他工具。
3) 最终输出必须包含：externalize.kind、是否有 ref.path（没有就写 none）、stdout 的一句摘要。
```

**预期工具调用序列**
- `proc_run`

**预期关键日志字段**
- `event=llm_response` 且 `tool_calls` 包含 `proc_run`
- `event=tool_start` / `event=tool_end`，`tool_name=proc_run`，`ok=true`
- `event=externalize`，`tool_name=proc_run`，`kind=inline`

---

## E2E-02: proc_run 大输出 -> ref

**目的**
- 稳定触发大输出外部化，验证 `externalize.kind=ref`。

**输入文本（直接粘贴）**
```text
你必须严格执行：
1) 必须调用 proc_run，使用 powershell（若失败再用 powershell.exe）生成 20000 行输出，格式为 E2E02_LINE_<行号>。
2) 不允许写“我猜测内容”，必须基于工具结果。
3) 最终输出必须包含：externalize.kind 和 ref.path。
```

**预期工具调用序列**
- `proc_run`

**预期关键日志字段**
- `event=tool_end`，`tool_name=proc_run`，`ok=true`
- `event=externalize`，`tool_name=proc_run`，`kind=ref`，`ref_path` 非空，`force_ref=false`

---

## E2E-03: ref 读回链路（text_search + fs_read）

**目的**
- 覆盖 `kind=ref` 后的标准读回链路：`text_search -> fs_read(start_line)`。

**输入文本（直接粘贴）**
```text
必须按以下顺序调用工具，不允许省略：
1) 调用 proc_run 生成 3000 行文本（使用 powershell，失败再 powershell.exe），其中第 1777 行包含：
   E2E03_ANCHOR_LINE_1777 KEY=NEEDLE-9031
   其他行写成 E2E03_LINE_<行号>
2) 如果上一步 externalize.kind=ref，必须调用：
   - text_search(path=ref.path, query="NEEDLE-9031", max_matches=5, context_lines=0)
3) 读取 text_search 首个命中 line，必须再调用：
   - fs_read(path=ref.path, start_line=line-2, max_lines=8, max_bytes=2000)
4) 最终输出必须包含：externalize.kind、ref.path、text_search 命中 line、fs_read 片段摘要。
5) 禁止猜测，必须基于工具返回。
```

**预期工具调用序列**
- `proc_run`
- `text_search`（当 `kind=ref`）
- `fs_read`（`start_line` 模式）

**预期关键日志字段**
- `event=externalize`：`tool_name=proc_run`，`kind=ref`
- `event=text_search`：`searched_files>=1`，`returned_matches>=1`
- `event=fs_read`：`mode=line`，`start_line` 非空

---

## E2E-04: fs_read 大请求触发硬上限与 truncated

**目的**
- 覆盖 `fs_read` 硬上限截断，验证 `truncated=true` 以及后续 externalize。

**输入文本（直接粘贴）**
```text
严格按步骤执行：
1) 先调用 proc_run 创建文件 data/e2e/fsread_big.txt（使用 powershell，失败再 powershell.exe）：
   - 先创建目录 data/e2e
   - 写入一行超长文本（至少 30000 个字符）
2) 再调用 fs_read：
   - path="data/e2e/fsread_big.txt"
   - offset=0
   - length=8000
3) 最终输出必须包含：fs_read.mode、requested、returned、hard_limit、truncated、externalize.kind。
```

**预期工具调用序列**
- `proc_run`
- `fs_read`

**预期关键日志字段**
- `event=fs_read`：`mode=offset`，`requested=8000`，`truncated=true`，`hard_limit` 生效
- `event=externalize`：`tool_name=fs_read`，`kind=inline` 或 `kind=ref`（任一即可）

---

## E2E-05: 沙箱拒绝（绝对路径 + .. 路径）

**目的**
- 覆盖 `sandbox_reject` 的 `abs` 与 `dotdot` 两类拒绝。

**输入文本（直接粘贴）**
```text
必须调用工具验证沙箱，不允许口头判断：
1) 调用 fs_read(path="C:/Windows/System32/drivers/etc/hosts", offset=0, length=80)
2) 即使上一步失败，也必须继续调用：
   fs_read(path="../README.md", offset=0, length=80)
3) 最终输出必须列出两个调用的错误结果，并标注哪个是绝对路径、哪个是 .. 路径。
```

**预期工具调用序列**
- `fs_read`（绝对路径）
- `fs_read`（`..` 路径）

**预期关键日志字段**
- `event=sandbox_reject`：`reason=abs`
- `event=sandbox_reject`：`reason=dotdot`
- 两条日志都包含 `path` 与 `allowed_roots`

---

## E2E-06: text_search 常规优先 rg

**目的**
- 覆盖 `text_search` 常规路径（优先 `rg`）；若环境无 `rg` 则验证 fallback 日志。

**输入文本（直接粘贴）**
```text
必须按步骤调用：
1) 先调用 proc_run 写入 data/e2e/rg_first.txt（使用 powershell，失败再 powershell.exe）：
   - 共 120 行
   - 第 42 行是 E2E06_TARGET_KEYWORD row=42
   - 其他行为 E2E06_LINE_<行号>
2) 调用 text_search(path="data/e2e/rg_first.txt", query="E2E06_TARGET_KEYWORD", max_matches=5, context_lines=0)
3) 再调用 fs_read(path="data/e2e/rg_first.txt", start_line=40, max_lines=6, max_bytes=800)
4) 最终输出必须包含：text_search.engine、命中 line、fs_read 摘要。
```

**预期工具调用序列**
- `proc_run`
- `text_search`
- `fs_read`

**预期关键日志字段**
- `event=text_search`：
  - 常见：`engine=rg`
  - 若无 `rg`：`engine=python` 且 `fallback_reason` 非空
- `event=fs_read`：`mode=line`

---

## E2E-07: 强制 python fallback（rg 不支持 lookbehind）

**目的**
- 无需新增工具参数，手工稳定触发 `text_search` 从 `rg` 回退到 `python`。

**输入文本（直接粘贴）**
```text
严格执行，不要省略工具：
1) 调用 proc_run 创建 data/e2e/fallback_regex.txt（使用 powershell，失败再 powershell.exe），内容至少两行：
   TOKEN_ABC
   TOKEN_DEF
2) 调用 text_search，参数必须是：
   - path="data/e2e/fallback_regex.txt"
   - query="(?<=TOKEN_)ABC"
   - is_regex=true
   - max_matches=5
3) 再调用 fs_read(path="data/e2e/fallback_regex.txt", start_line=1, max_lines=3, max_bytes=500)
4) 最终输出必须包含：text_search.engine、fallback 说明、命中 line、fs_read 摘要。
```

**预期工具调用序列**
- `proc_run`
- `text_search`（regex lookbehind）
- `fs_read`

**预期关键日志字段**
- `event=text_search`：`engine=python`，`fallback_reason` 包含 `rg` 失败信息

---

## E2E-08: 外部化目录增长（多个 ref 不覆盖）

**目的**
- 在同一 trace 内产生多个 ref，验证 `ref_path` 不同（不覆盖）。

**输入文本（直接粘贴）**
```text
你必须分两次调用 proc_run（不能合并成一次）：
1) 第一次 proc_run：输出 12000 行，格式 E2E08_A_<行号>
2) 第二次 proc_run：输出 12000 行，格式 E2E08_B_<行号>
3) 对每次调用都检查 externalize 结果。
4) 最终输出必须包含两个 ref.path，并明确写出它们是否不同。
```

**预期工具调用序列**
- `proc_run`（第 1 次）
- `proc_run`（第 2 次）

**预期关键日志字段**
- 至少两条 `event=externalize` 且 `kind=ref`
- 两条日志的 `ref_path` 不相同
