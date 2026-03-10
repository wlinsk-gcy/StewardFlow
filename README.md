# StewardFlow: 面向浏览器与工具执行的可视化 ReAct Agent

![StewardFlow Banner](public/banner-option-ops.svg)

中文 | [English](README_en.md)

StewardFlow 是一个基于 `FastAPI` + `React` 的工程化 Agent 工作台。它把 `ReAct` 推理、工具执行、浏览器自动化、`HITL`
恢复、执行轨迹可视化和 Docker sandbox 运行时收敛到同一个界面里，目标不是“只会聊天”，而是“能在真实环境里执行并可追踪地停下来”。

## Demo Case

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

## 为什么用 StewardFlow

StewardFlow 解决的是“Agent 真正进入执行环境后，如何保持可见、可中断、可恢复”的问题。

核心能力：

- 可视化执行链路：`thought`、`action`、`observation`、`final` 与 token 统计实时推送到前端
- 浏览器执行面板：通过 noVNC 直接观察浏览器状态，并支持 sandbox 内浏览器工具
- HITL 恢复机制：命令确认、登录、验证码、OAuth 授权、拒绝访问等阻塞场景可暂停并恢复
- 停止与上下文边界控制：支持中断活跃运行，并避免把未完成 step 污染到未来 LLM 上下文
- 单 sandbox 工程运行时：后端启动即自动拉起一个 sandbox，前端同时提供 `AgentWorkbench` 与 `SandboxConsole`

## 架构概览

![StewardFlow Architecture](public/stewardflow-architecture.png)

关键模块：

- `main.py`：组装 `ToolRegistry`、`TaskService`、`SandboxManager`、`MCPClient`、`ConnectionManager` 与 FastAPI 生命周期
- `TaskService`：负责 trace 初始化、排队调度、活跃任务管理、按 trace 串行执行以及恢复逻辑
- `TaskExecutor`：推进状态机并向 WebSocket 推送 `thought/action/observation/final/end`
- `SandboxToolRuntime`：把工具调用转发到 sandbox 内部 HTTP API
- `SandboxManager`：负责 Docker sandbox 的创建、删除、健康检查与日志读取
- `AgentWorkbench`：聊天、执行轨迹、浏览器视图、HITL 与中断交互
- `SandboxConsole`：查看 sandbox 健康状态与日志输出

## 状态机

![StewardFlow State Machine](public/stewardflow-state-machine.png)

## 5 分钟跑起来

### 依赖前提

你至少需要：

- Docker Engine
- Python 虚拟环境能力
- Node.js 与 npm
- 一个 OpenAI-compatible LLM API Key

### 1. 构建 sandbox 镜像

```bash
cd sandbox
docker build -t gui-sandbox:dev -f Dockerfile .
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

字段说明：

- `sandbox.image`：默认 sandbox 镜像，通常就是 `gui-sandbox:dev`
- `sandbox.public_host`：前端访问 noVNC / sandbox API 时使用的宿主机地址
- `sandbox.healthcheck_host`：后端探测 sandbox `/health` 使用的地址，通常可与 `public_host` 一致
- `sandbox.docker_base_url`：后端访问 Docker Engine 的地址，例如 `tcp://127.0.0.1:2375`
- `sandbox.start_url`：浏览器启动后的默认页面，当前默认建议 `chrome://new-tab-page/`

### 3. 启动后端

```bash
python -m venv .venv
# Windows PowerShell
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python main.py
```

默认监听端口为 `8000`。后端启动时会自动创建一个 sandbox，并把 Agent 工具运行时绑定到这个 sandbox。

### 4. 启动前端

```bash
cd ui
npm install
npm run dev
```

默认前端地址：`http://localhost:5173`

### 5. 打开工作台

- 前端默认通过 `http://localhost:8000` 调用后端
- WebSocket 地址：`ws://localhost:8000/ws/{client_id}`
- 浏览器视图使用 sandbox 的 noVNC 地址
- 可通过 `GET /sandboxes?include_exited=false` 查看当前 sandbox 映射端口

## 运行模型

### ReAct 主循环

StewardFlow 的主执行链路是：

`THINK -> DECIDE -> EXECUTE -> OBSERVE -> GUARD -> END`

其中：

- `THINK`：LLM 生成下一步决策
- `DECIDE`：确定是否调用工具、是否进入 HITL、是否直接回答
- `EXECUTE`：执行工具或浏览器动作
- `OBSERVE`：采集工具结果并回灌
- `GUARD`：检查阻塞、确认、中断与上下文边界

### Stop / CANCELLED 语义

- 前端空闲时主按钮显示 `Send`
- 首轮请求已发出但还没拿到 `trace_id` 时显示 loading
- 活跃运行且已拿到 `trace_id` 时主按钮切换为 `Stop`
- trace 进入 `WAITING/HITL` 后不再展示 `Stop`

后端行为：

- `POST /agent/stop` 会定位该 `trace_id` 对应的活跃任务并触发取消
- executor 会显式处理 `asyncio.CancelledError`，并把 trace 标记为 `CANCELLED`
- 被中断的 trace 后续仍可以在同一 trace 上开启新 turn

### HITL 与上下文边界

StewardFlow 当前重点约束的是“不要把未完成执行污染到未来上下文”。

规则如下：

- 普通文本回答：只有最终完成的回答才会进入后续 messages
- tool step：只有完整的 `assistant.tool_calls` 与全部 tool 结果配对完成后才会进入 messages
- `REQUEST_INPUT` continuation：只有在用户真正以 `done` 完成后才会注入
- 已被取消、尚未完成的 step 不会进入未来的 LLM 上下文


## 内置工具与运行时

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

### 工具契约

- 所有工具都返回 `output` 与可选 `metadata`
- 输出过大时通过 `metadata.truncated=true` 与 `metadata.outputPath` 指向完整结果
- 浏览器工具会附带当前标签页摘要
- 当检测到登录、验证码、OAuth 授权、拒绝访问等阻塞页面时，会附带 `metadata.hitlBarrier`

### Sandbox 模型

- 后端启动时自动创建 1 个 sandbox
- 当前 Agent 运行时默认只绑定这个启动时自动创建的 sandbox
- `New Session` 会重置浏览器状态，但不会销毁整个 sandbox
- `SandboxConsole` 使用统一的 `/sandboxes/health` 与 `/sandboxes/{sandbox_id}/logs` 接口做诊断

## API 概览

### Agent API

- `POST /agent/run`：创建新 trace、恢复 `WAITING/HITL`，或在同一 trace 上开启新 turn
- `POST /agent/stop`：停止当前活跃运行
- `GET /agent/health`：查看 Agent 服务健康状态
- `GET /agent/registry-summary`：查看当前工具注册摘要

### Sandbox API

- `GET /sandboxes`：列出 sandbox
- `POST /sandboxes`：创建 sandbox
- `GET /sandboxes/{sandbox_id}`：查看单个 sandbox
- `POST /sandboxes/{sandbox_id}/start`：启动 sandbox
- `POST /sandboxes/{sandbox_id}/stop`：停止 sandbox
- `DELETE /sandboxes/{sandbox_id}`：删除 sandbox
- `GET /sandboxes/health?sandbox_id=<optional>`：检查指定 sandbox 或当前运行中的 sandbox 健康状态
- `GET /sandboxes/{sandbox_id}/logs`：读取 sandbox 日志
- `POST /sandboxes/browser/reset`：重置当前运行中 sandbox 的浏览器标签页状态

## 配置说明

### `config.yaml`

- `app.port`：后端监听端口
- `log.level`：日志级别
- `llm.model` / `llm.api_key` / `llm.base_url`：OpenAI-compatible LLM 配置
- `sandbox.image`：默认 sandbox 镜像
- `sandbox.public_host`：前端访问 noVNC 使用的宿主机地址
- `sandbox.healthcheck_host`：后端探测 sandbox `/health` 使用的地址
- `sandbox.docker_base_url`：Docker Engine 地址
- `sandbox.start_url`：浏览器首次打开地址
- `sandbox.display_width` / `sandbox.display_height`：虚拟桌面分辨率

### `mcp_config.json.example`

- 仓库内已经包含 MCP 客户端与连接器实现
- 支持通过 `stdio` 与 `http` 两种方式接入 MCP Server

## 已知限制

- 当前 checkpoint、消息缓存、WebSocket 连接状态均为进程内存态；服务重启不会恢复运行中的 trace
- LLM provider 当前使用异步 chat completion 路径；`stop` 对等待中的 LLM 请求通常能更快生效，但实际取消延迟仍取决于 OpenAI
  SDK、底层 HTTP 连接与上游兼容服务实现
- 部分工具若内部 I/O 不可取消，trace 会先进入 `CANCELLED`，但底层 await 的终止速度仍取决于工具实现
- 当前 Agent 运行时只绑定启动时自动创建的 sandbox；手动新建的 sandbox 不会自动切换为 Agent 的活跃执行目标
- `CheckpointStore` 与 `InMemoryCacheManager` 目前仍是内存实现，不做磁盘持久化

## 开源协议

本仓库当前以 `MIT` 协议发布。详见 [LICENSE](LICENSE)。

## 特别鸣谢

- `docker-baseimage-gui` 提供基础镜像：
  https://github.com/jlesage/docker-baseimage-gui
- `opencode` 提供工具运行时与交互设计参考：
  https://github.com/anomalyco/opencode
