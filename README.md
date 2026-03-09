# StewardFlow: ReAct & HITL Agent

![StewardFlow Banner](public/banner.png)

中文 | [English](README_en.md)

StewardFlow 是一个基于 `FastAPI` + `React` 的可视化 `ReAct Agent`。

支持通过 `stdio` / `http` 两种方式接入 `MCP Servers`。

内置 `Docker-sandbox`，所有工具内置工具均在沙箱内运行。

提供可追踪的执行事件流、浏览器 noVNC 视图、HITL 恢复机制，以及面向 Trace 的内存态 checkpoint 与上下文重建能力。

## Demo

本案例使用的 LLM 为：`qwen3.5-plus`

> 如果你没有 qwen 的 API Key，可以通过以下方式体验：
>
> 1. 到 `https://www.modelscope.cn/` 获取免费 API Key，每天可免费调用 20 次
> 2. 到 `https://bailian.console.aliyun.com/` 申请免费 API Key，新用户可获得 `qwen3.5-plus` 的免费体验额度

### 1. 打开小红书，搜索千问大模型主页并总结

可观看 `public/demo1.mp4`

### 2. 查看当前目录有哪些文件

可观看 `public/demo2.mp4`

### 3. 使用 `bash` 工具在 docker sandbox 内执行命令

可观看 `public/demo3.mp4`


## 功能概览

- ReAct 执行循环：`THINK -> DECIDE -> EXECUTE -> OBSERVE -> GUARD -> END`
- HITL 两类等待点：
  - 高风险 `bash` 命令确认
  - 浏览器页面阻塞信号（登录 / 验证码 / OAuth 授权 / 拒绝访问）
- WebSocket 实时事件：`thought/action/observation/final/hitl_confirm/hitl_request/token_info/error/end`
- 自动 sandbox 生命周期：后端启动自动创建 1 个 sandbox，关闭时自动删除
- 前端双工作区：`AgentWorkbench` 用于聊天与执行轨迹，`SandboxConsole` 用于健康检查与日志查看
- 调度保护：`TaskService` 内置全局并发上限 `4`、排队上限 `128`、排队等待超时 `15s`

## 代码架构

![stewardflow-state-machine](public/stewardflow-architecture.png)

### 架构说明

- `main.py` 负责组装 `ToolRegistry`、`TaskService`、`SandboxManager`、`MCPClient`、`ConnectionManager` 与 FastAPI 生命周期。
- `TaskService` 负责 trace 初始化、排队调度、trace 级串行锁，以及 WAITING 状态任务的恢复。
- `TaskExecutor` 负责状态机推进，并把 `thought/action/observation/final` 等事件写入 WebSocket。
- `CheckpointStore` 与 `InMemoryCacheManager` 当前都仅存内存，不做磁盘持久化。
- `SandboxToolRuntime` 把工具调用转发到 sandbox 内部 HTTP API；浏览器工具会在 `metadata` 中附带页签信息，并可能附加 `metadata.hitlBarrier`。
- `SandboxManager` 负责 Docker 容器的创建、删除、健康检查和日志读取，但当前 Agent 运行时只会绑定启动时自动创建的那个 sandbox。

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

`sandbox.public_host` 指StewardFlow请求sandbox-api的节点ip地址

`sandbox.healthcheck_host` 同public_host保持一致即可

`sandbox.docker_base_url` 指向后端可访问的 Docker Engine，例如：`tcp://192.168.130.147:2375`

### 3. 启动后端

```bash
python -m venv .venv
# Windows PowerShell
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python main.py
```

默认端口是 `8000`。后端启动后会自动创建一个 sandbox，并把 Agent 工具运行时绑定到该 sandbox。

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

## Sandbox 与端口说明

- 当前自动创建 sandbox 时，`5800/5900/8899` 会映射到随机宿主机端口。
- 如果你的环境有严格防火墙策略，建议通过 `POST /sandboxes` 手动创建固定端口实例，或预先放通一段可用端口范围。因为当前系统自动创建沙箱时，端口号随机分配。
- `SandboxConsole` 当前只提供健康检查和日志查看，不提供交互式 shell。

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
- 当输出过大时，统一通过 `metadata.truncated=true` 与 `metadata.outputPath` 指向完整结果
- 浏览器工具会补充当前页签摘要；若检测到登录、验证码、OAuth 授权等阻塞页面，还会附带 `metadata.hitlBarrier`

## 配置说明

### `config.yaml`

- `app.port`：后端监听端口
- `log.level`：日志级别
- `llm.model` / `llm.api_key` / `llm.base_url`：OpenAI Compatible LLM 配置
- `sandbox.image`：默认 sandbox 镜像
- `sandbox.public_host`：前端访问 noVNC 使用的宿主机地址
- `sandbox.healthcheck_host`：后端探测 sandbox `/health` 使用的地址
- `sandbox.docker_base_url`：Docker Engine 地址
- `sandbox.start_url`：Chromium 首次打开地址
- `sandbox.display_width` / `sandbox.display_height`：虚拟桌面分辨率

### `mcp_config.json.example`

- 当前仓库包含 MCP 客户端与连接器实现
- 支持stdio / http两种方式接入MCP Servers

## API 概览

### Agent API

- `POST /agent/run`
  - 无 `trace_id`：创建新 Trace 并入队执行
  - `trace_id` 对应 `WAITING + HITL`：恢复一次 HITL 输入
  - `trace_id` 对应 `DONE/FAILED + END`：在同一 Trace 上开启新 turn
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

## 已知限制

- 当前 checkpoint、消息缓存、WebSocket 连接状态均为进程内存态，服务重启不会恢复运行中的 Trace。
- 当前 Agent 运行时只会绑定启动时自动创建的 sandbox；手动新建 sandbox 不会自动切换为 Agent 的活跃执行目标。

## 特别鸣谢

- 提供基础镜像的 `docker-baseimage-gui`：
  https://github.com/jlesage/docker-baseimage-gui
- 提供工具运行时与交互设计参考的 `opencode`：
  https://github.com/anomalyco/opencode
