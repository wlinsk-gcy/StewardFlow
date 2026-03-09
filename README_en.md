# StewardFlow: ReAct & HITL Agent

![StewardFlow Banner](public/banner.png)

[中文](README.md) | English

StewardFlow is a visualized `ReAct Agent` built with `FastAPI` + `React`.

It supports integrating `MCP Servers` through both `stdio` and `http`.

It comes with a built-in `Docker sandbox`, and all built-in tools run inside the sandbox.

It provides a traceable execution event stream, a browser noVNC view, a HITL recovery mechanism, as well as trace-oriented in-memory checkpoints and context reconstruction capabilities.

## Demo

The LLM used in these demos is: `qwen3.5-plus`

> If you do not have a qwen API key, you can try it through the following ways:
>
> 1. Go to `https://www.modelscope.cn/` to get a free API key, which allows 20 free calls per day
> 2. Go to `https://bailian.console.aliyun.com/` to apply for a free API key. New users can get free trial credits for `qwen3.5-plus`

### 1. Open Xiaohongshu, search for the Qwen LLM homepage, and summarize it

You can watch `public/demo1.mp4`

### 2. Check what files are in the current directory

You can watch `public/demo2.mp4`

### 3. Use the `bash` tool to execute commands inside the Docker sandbox

You can watch `public/demo3.mp4`

## Feature Overview

- ReAct execution loop: `THINK -> DECIDE -> EXECUTE -> OBSERVE -> GUARD -> END`
- Two types of HITL waiting points:
  - High-risk `bash` command confirmation
  - Browser page blocking signals (login / verification code / OAuth authorization / access denied)
- Real-time WebSocket events: `thought/action/observation/final/hitl_confirm/hitl_request/token_info/error/end`
- Automatic sandbox lifecycle: the backend automatically creates one sandbox on startup and removes it on shutdown
- Dual frontend workspaces: `AgentWorkbench` for chat and execution traces, `SandboxConsole` for health checks and log viewing
- Scheduling safeguards: `TaskService` includes a global concurrency limit of `4`, a queue limit of `128`, and a queue wait timeout of `15s`

## Code Architecture

![stewardflow-state-machine](public/stewardflow-architecture.png)

### Architecture Notes

- `main.py` is responsible for wiring together `ToolRegistry`, `TaskService`, `SandboxManager`, `MCPClient`, `ConnectionManager`, and the FastAPI lifecycle.
- `TaskService` handles trace initialization, queue scheduling, per-trace serial locking, and the resumption of tasks in the WAITING state.
- `TaskExecutor` advances the state machine and writes events such as `thought/action/observation/final` to WebSocket.
- `CheckpointStore` and `InMemoryCacheManager` are currently in-memory only and do not persist data to disk.
- `SandboxToolRuntime` forwards tool calls to the internal HTTP API inside the sandbox; browser tools include tab information in `metadata` and may also attach `metadata.hitlBarrier`.
- `SandboxManager` is responsible for creating, deleting, health-checking, and reading logs from Docker containers, but the current Agent runtime only binds to the sandbox that is automatically created at startup.

## State Machine Flow

![stewardflow-state-machine](public/stewardflow-state-machine.png)

## Quick Start

### 1. Build the sandbox image

```bash
cd sandbox
docker build -t gui-sandbox:dev .
```

### 2. Configure the backend

Copy the config file:

```bash
cp config.yaml.example config.yaml
# PowerShell
Copy-Item config.yaml.example config.yaml
```

At minimum, confirm the following fields:

- `llm.api_key`
- `llm.model`
- `llm.base_url`
- `sandbox.image`
- `sandbox.public_host`
- `sandbox.healthcheck_host`
- `sandbox.docker_base_url`
- `sandbox.start_url`

`sandbox.public_host` is the node IP address that StewardFlow uses to request the sandbox API.

`sandbox.healthcheck_host` can stay the same as `public_host`.

`sandbox.docker_base_url` points to the Docker Engine accessible by the backend, for example: `tcp://192.168.130.147:2375`

### 3. Start the backend

```bash
python -m venv .venv
# Windows PowerShell
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python main.py
```

The default port is `8000`. After the backend starts, it will automatically create one sandbox and bind the Agent tool runtime to that sandbox.

### 4. Start the frontend

```bash
cd ui
npm install
npm run dev
```

Default frontend URL: `http://localhost:5173`

### 5. Open the frontend

- By default, the frontend calls the backend through `http://localhost:8000`
- The WebSocket URL is `ws://localhost:8000/ws/{client_id}`
- The browser view uses the sandbox's noVNC address; you can check the current mapped ports through `GET /sandboxes?include_exited=false`

## Sandbox and Port Notes

- When a sandbox is automatically created, `5800/5900/8899` are mapped to random host ports.
- If your environment has strict firewall policies, it is recommended to manually create a fixed-port instance through `POST /sandboxes`, or pre-open a usable port range. This is because the current system assigns ports randomly when automatically creating sandboxes.
- `SandboxConsole` currently only provides health checks and log viewing, and does not provide an interactive shell.

## Built-in Tool List

### File and Command Tools

- `bash`
- `glob`
- `read`
- `grep`
- `edit`
- `write`

### Browser Tools

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

### Current Tool Contract

- All tools return `output` and optional `metadata`
- When the output is too large, the full result is referenced through `metadata.truncated=true` and `metadata.outputPath`
- Browser tools append the current tab summary; if blocking pages such as login, verification code, or OAuth authorization are detected, they will also attach `metadata.hitlBarrier`

## Configuration Notes

### `config.yaml`

- `app.port`: backend listening port
- `log.level`: log level
- `llm.model` / `llm.api_key` / `llm.base_url`: OpenAI-compatible LLM configuration
- `sandbox.image`: default sandbox image
- `sandbox.public_host`: host address used by the frontend to access noVNC
- `sandbox.healthcheck_host`: address used by the backend to probe sandbox `/health`
- `sandbox.docker_base_url`: Docker Engine address
- `sandbox.start_url`: initial URL opened by Chromium
- `sandbox.display_width` / `sandbox.display_height`: virtual desktop resolution

### `mcp_config.json.example`

- The current repository includes an MCP client and connector implementations
- Supports connecting to MCP Servers through both `stdio` and `http`

## API Overview

### Agent API

- `POST /agent/run`
  - Without `trace_id`: creates a new Trace and enqueues execution
  - When `trace_id` corresponds to `WAITING + HITL`: resumes one HITL input
  - When `trace_id` corresponds to `DONE/FAILED + END`: starts a new turn on the same Trace
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

## Known Limitations

- Checkpoints, message caches, and WebSocket connection states are currently stored only in process memory, so running Traces cannot be recovered after a service restart.
- The current Agent runtime only binds to the sandbox that is automatically created at startup; manually created sandboxes will not automatically become the Agent's active execution target.

## Special Thanks

- `docker-baseimage-gui` for providing the base image:
  https://github.com/jlesage/docker-baseimage-gui
- `opencode` for providing reference ideas for tool runtime and interaction design:
  https://github.com/anomalyco/opencode
