# StewardFlow: ReAct 与 HITL Agent

![StewardFlow Banner](public/banner.png)

中文 | [English](README_en.md)

StewardFlow 是一个基于 `FastAPI` + `React` 的可视化 `ReAct Agent`。它支持接入 `MCP Server`，内置 `Docker sandbox`，并提供可追踪的执行日志、浏览器 noVNC 视图、HITL 恢复机制，以及面向 Trace 的内存 checkpoint 与上下文重建能力。

## 最新更新

- 新增前端运行中断能力：输入区主按钮在运行期间会先显示 loading，拿到 `trace_id` 后切换为 `Stop`，用户可中断当前运行。
- 新增 `POST /agent/stop`，通过取消后端活跃任务停止当前 Trace，不需要关闭整个后端服务。
- 新增 `CANCELLED` 终态；被中断的 Trace 后续可以继续在同一 Trace 上开启新 turn。
- 上下文重建改为只序列化已提交历史：未完成的 tool transcript 不再回灌给模型，避免生成非法的 `assistant.tool_calls` / `tool` 对。
- HITL synthetic continuation 只会在 `REQUEST_INPUT` 真正以 `done` 完成后注入，浏览器刷新或用户中断时不会误污染后续上下文。

## Demo

本示例使用的 LLM 为 `qwen3.5-plus`。

> 如果你没有 qwen 的 API Key，可通过以下方式体验：
>
> 1. 到 `https://www.modelscope.cn/` 获取免费 API Key，每天可免费调用 20 次
> 2. 到 `https://bailian.console.aliyun.com/` 申请免费 API Key，新用户可获得 `qwen3.5-plus` 的免费体验额度

### 1. 浏览器自动化
```text
打开小红书。
总结发布时间为最近一周，点赞量前十的且含有 #AI 话题的博文。
并统计出这十篇文章的标题，文章内容，是图文还是视频，以及携带的话题。
```


### 2. 工具并行调用

```text
请对当前工作区做一次并行巡检。以下任务彼此独立，请尽量并行调用工具完成，不要串行逐个处理：

1. 统计所有 `.py` 文件数量和总代码行数
2. 统计所有 `.md` 文件数量和总代码行数
3. 搜索所有 `TODO` 和 `FIXME`
4. 查找体积最大的 10 个文件
5. 检查 `README.md`、`requirements.txt`、`package.json` 是否存在
```

### 3. 代码编写

```text
请在当前环境中创建一个最小可运行的 Node.js 静态页面项目，写入项目文件并通过 npm 启动本地服务，然后使用浏览器工具打开 localhost 页面并验证页面内容与按钮交互是否正常。我会通过 VNC 直接观看最终页面效果，所以不要只生成代码，必须真正启动并打开页面。
```

## 功能概览

- ReAct 执行循环：`THINK -> DECIDE -> EXECUTE -> OBSERVE -> GUARD -> END`
- 两类 HITL 等待点：
  - 高风险 `bash` 命令确认
  - 浏览器阻塞信号（登录 / 验证码 / OAuth 授权 / 拒绝访问）
- 实时 WebSocket 事件：`thought` / `action` / `observation` / `final` / `hitl_confirm` / `hitl_request` / `token_info` / `error` / `end`
- 前端双工作区：
  - `AgentWorkbench`：聊天、运行状态、执行轨迹、浏览器视图
  - `SandboxConsole`：sandbox 健康检查与日志查看
- 自动 sandbox 生命周期：后端启动时自动创建 1 个 sandbox，关闭时自动删除
- 调度保护：`TaskService` 内置全局并发上限 `4`、排队上限 `128`、排队超时 `15s`
- 中断保护：
  - 活跃运行通过任务取消停止
  - 已进入 `WAITING/HITL` 的 trace 不显示 stop
  - 中断中的未完成 step 不会进入未来的 LLM context

## 代码架构

![stewardflow-state-machine](public/stewardflow-architecture.png)

### 架构说明

- `main.py` 负责组装 `ToolRegistry`、`TaskService`、`SandboxManager`、`MCPClient`、`ConnectionManager` 和 FastAPI 生命周期。
- `TaskService` 负责 Trace 初始化、队列调度、活跃任务注册、按 Trace 串行执行，以及 `WAITING/HITL` / 新 turn 的恢复。
- `TaskExecutor` 负责推进状态机，处理 `CancelledError`，并把 `thought/action/observation/final/end` 等事件写入 WebSocket。
- `CheckpointStore` 与 `InMemoryCacheManager` 当前均为内存实现，不做磁盘持久化。
- `CacheManager` 只重建已提交上下文：
  - 普通回答 step 只有在最终完成后才进入 messages
  - tool step 只有 `assistant.tool_calls` 与全部 tool 结果完整配对后才进入 messages
  - HITL synthetic continuation 只有在 `REQUEST_INPUT(done)` 后才注入
- `SandboxToolRuntime` 把工具调用转发到 sandbox 内部 HTTP API；浏览器工具会在 `metadata` 中附带标签页信息，必要时附带 `metadata.hitlBarrier`
- `SandboxManager` 负责 Docker 容器创建、删除、健康检查与日志读取；当前 Agent 运行时只绑定启动时自动创建的 sandbox

## 状态机流转

![stewardflow-state-machine](public/stewardflow-state-machine.png)

## 快速启动

### 1. 构建 sandbox 镜像

```bash
cd sandbox
docker build -t gui-sandbox:dev .
```

### 2. 配置后端

复制配置文件：

```bash
cp config.yaml.example config.yaml
# PowerShell
Copy-Item config.yaml.example config.yaml
```

至少确认以下字段：

- `llm.api_key`
- `llm.model`
- `llm.base_url`
- `sandbox.image`
- `sandbox.public_host`
- `sandbox.healthcheck_host`
- `sandbox.docker_base_url`
- `sandbox.start_url`

说明：

- `sandbox.public_host`：StewardFlow 请求 sandbox API 与 noVNC 时使用的节点 IP
- `sandbox.healthcheck_host`：后端探测 sandbox `/health` 时使用的地址，通常可与 `public_host` 一致
- `sandbox.docker_base_url`：后端可访问的 Docker Engine，例如 `tcp://192.168.130.147:2375`

### 3. 启动后端

```bash
python -m venv .venv
# Windows PowerShell
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python main.py
```

默认端口为 `8000`。后端启动后会自动创建一个 sandbox，并把 Agent 工具运行时绑定到该 sandbox。

### 4. 启动前端

```bash
cd ui
npm install
npm run dev
```

默认前端地址：`http://localhost:5173`

### 5. 访问前端

- 前端默认通过 `http://localhost:8000` 调用后端
- WebSocket 地址为 `ws://localhost:8000/ws/{client_id}`
- 浏览器视图使用 sandbox 的 noVNC 地址；可通过 `GET /sandboxes?include_exited=false` 查看当前映射端口

## 运行与中断语义

### 运行按钮

- 空闲：显示 `Send`
- 刚发起首轮运行但 `trace_id` 尚未返回：显示 loading
- 活跃运行且已拿到 `trace_id`：显示 `Stop`
- 进入 `WAITING/HITL`：不显示 `Stop`，回到原有 HITL 交互

### Stop 的后端语义

- `POST /agent/stop` 会定位 `trace_id` 对应的活跃 worker task 并触发取消
- executor 会单独处理 `asyncio.CancelledError`，把 Trace 记录为 `CANCELLED`
- 停止不会自动把当前未完成 step 持久化为可回灌上下文

### 上下文边界规则

- 纯文本回答：只有最终完成的回答才进入后续 messages
- tool step：只有完整的 `assistant.tool_calls` 与全部 tool 结果齐全后才进入 messages
- HITL continuation：只有在 `REQUEST_INPUT` 的 `request_input == "done"` 且状态完成后才注入

## 内置工具清单

### 文件与命令工具

- `bash`
- `glob`
- `read`
- `grep`
- `edit`
- `write`

### 浏览器工具

- `browser_navigate_page`
- `browser_take_snapshot`
- `browser_click`
- `browser_fill`
- `browser_wait_for`
- `browser_take_screenshot`
- `browser_press_key`
- `browser_handle_dialog`
- `browser_hover`
- `browser_upload_file`
- `browser_select_page`

### 当前工具契约

- 所有工具都会返回 `output` 与可选 `metadata`
- 输出过大时统一通过 `metadata.truncated=true` 与 `metadata.outputPath` 指向完整结果
- 浏览器工具会补充当前标签页摘要；若检测到登录、验证码、OAuth 授权等阻塞页面，还会附带 `metadata.hitlBarrier`

## API 概览

### Agent API

- `POST /agent/run`
  - 无 `trace_id`：创建新 Trace 并排队执行
  - `trace_id` 对应 `WAITING + HITL`：恢复当前 turn 的一次 HITL 输入
  - `trace_id` 对应 `DONE` / `FAILED` / `CANCELLED` + `END`：在同一 Trace 上开启新 turn
- `POST /agent/stop`
  - trace 有活跃任务：返回 accepted，并取消当前运行
  - trace 已经 `WAITING/HITL` 或已终态：返回 no-op
- `GET /agent/health`
- `GET /agent/registry-summary`

### Sandbox API

- `GET /sandboxes`
- `POST /sandboxes`
- `GET /sandboxes/{sandbox_id}`
- `POST /sandboxes/{sandbox_id}/start`
- `POST /sandboxes/{sandbox_id}/stop`
- `DELETE /sandboxes/{sandbox_id}`
- `GET /sandboxes/{sandbox_id}/health`
- `GET /sandboxes/{sandbox_id}/logs`

## 配置说明

### `config.yaml`

- `app.port`：后端监听端口
- `log.level`：日志级别
- `llm.model` / `llm.api_key` / `llm.base_url`：OpenAI-compatible LLM 配置
- `sandbox.image`：默认 sandbox 镜像
- `sandbox.public_host`：前端访问 noVNC 使用的宿主机地址
- `sandbox.healthcheck_host`：后端探测 sandbox `/health` 使用的地址
- `sandbox.docker_base_url`：Docker Engine 地址
- `sandbox.start_url`：Chromium 首次打开地址
- `sandbox.display_width` / `sandbox.display_height`：虚拟桌面分辨率

### `mcp_config.json.example`

- 仓库内包含 MCP 客户端与连接器实现
- 支持通过 `stdio` / `http` 两种方式接入 MCP Server

## 已知限制

- 当前 checkpoint、消息缓存、WebSocket 连接状态均为进程内存态；服务重启不会恢复运行中的 Trace
- LLM provider 已切到异步 chat completion 路径；`stop` 通常可在等待中的 LLM 请求上更快生效，但实际取消延迟仍取决于 OpenAI SDK、底层 HTTP 连接以及上游兼容服务的取消行为
- 部分工具若内部 I/O 不可取消，Trace 会先进入 `CANCELLED`，但底层 await 的实际终止速度仍取决于工具实现
- 当前 Agent 运行时只绑定启动时自动创建的 sandbox；手动新建的 sandbox 不会自动切换为 Agent 的活跃执行目标

## 特别鸣谢

- `docker-baseimage-gui` 提供基础镜像：
  https://github.com/jlesage/docker-baseimage-gui
- `opencode` 提供工具运行时与交互设计参考：
  https://github.com/anomalyco/opencode
