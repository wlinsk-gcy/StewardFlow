# StewardFlow: A Visual ReAct Agent for Browser and Tool Execution

![StewardFlow Banner](public/banner-option-ops.svg)

[中文](README.md) | English

StewardFlow is an engineering-first agent workspace built with `FastAPI` + `React`. It combines `ReAct` reasoning, tool
execution, browser automation, `HITL` recovery, execution trace visualization, and a Docker sandbox runtime in one
surface. The goal is not just to chat, but to execute in a real environment with visibility, interruption, and recovery.

## Demo Case

### 1. Browser automation

```text
Open Xiaohongshu. Summary The top 10 blog posts with the most likes and AI topics published in the last week. And count the titles of these ten articles, the content of the articles, whether they are pictures or videos, and the topics they carry.
```

### 2. Tools are called in parallel

```text
Please do a parallel inspection of the current work area. The following tasks are independent of each other, please try to call the tool in parallel, and do not process them one by one: 
1. Count all '.py' files and total lines of code 
2. Count all '.md' files and total lines of code 
3. Search all 'TODO' and 'FIXME' 
4. Find the 10 largest files by volume 
5. Check for 'README.md', 'requirements.txt', 'package.json' to exist
```

### 3. Code writing

```text
Create a minimum workable Node.js static page project in your current environment, write the project file and start the local service via npm, then use the browser tool to open the localhost page and verify that the page content interacts with the button correctly. I'll watch the final page effect directly through VNC, so don't just generate code, you have to actually start and open the page.
```

## What StewardFlow Gives You

StewardFlow is built for the part that many agent demos avoid: once an agent enters the execution environment, you still
need to see what it is doing, stop it safely, and resume the run without corrupting future context.

Key capabilities:

- visual execution trace for `thought`, `action`, `observation`, `final`, and token usage
- browser execution panel through noVNC plus sandbox browser tools
- `HITL` pause-and-resume flow for confirmation, login, captcha, OAuth, and access denial
- stop and context-boundary control so incomplete work does not leak into future LLM context
- a single sandbox-backed runtime with both `AgentWorkbench` and `SandboxConsole`

## Architecture Overview

![StewardFlow Architecture](public/stewardflow-architecture.png)

Key modules:

- `main.py`: wires `ToolRegistry`, `TaskService`, `SandboxManager`, `MCPClient`, `ConnectionManager`, and FastAPI
  lifecycle
- `TaskService`: trace initialization, queueing, active-task tracking, per-trace serialization, and resume flows
- `TaskExecutor`: advances the state machine and emits `thought/action/observation/final/end` over WebSocket
- `SandboxToolRuntime`: forwards tool calls into the sandbox-internal HTTP API
- `SandboxManager`: creates, deletes, health-checks, and tails Docker sandboxes
- `AgentWorkbench`: chat, execution trace, browser view, HITL, and stop interaction
- `SandboxConsole`: sandbox health inspection and logs

## State Machine

![StewardFlow State Machine](public/stewardflow-state-machine.png)

## Quick Start in 5 Minutes

### Prerequisites

You need:

- Docker Engine
- a Python virtual environment workflow
- Node.js and npm
- an OpenAI-compatible LLM API key

### 1. Build the sandbox image

```bash
cd sandbox
docker build --progress=plain -t gui-sandbox:dev -f Dockerfile .
```

### 2. Configure the backend

Copy the config file:

```bash
cp config.yaml.example config.yaml
# PowerShell
Copy-Item config.yaml.example config.yaml
```

At minimum, confirm these fields:

- `llm.api_key`
- `llm.model`
- `llm.base_url`
- `sandbox.image`
- `sandbox.public_host`
- `sandbox.healthcheck_host`
- `sandbox.docker_base_url`
- `sandbox.start_url`

Field notes:

- `sandbox.image`: default sandbox image, typically `gui-sandbox:dev`
- `sandbox.public_host`: host used by the frontend to reach noVNC and sandbox APIs
- `sandbox.healthcheck_host`: address used by the backend for sandbox `/health`, usually the same as `public_host`
- `sandbox.docker_base_url`: Docker Engine address, for example `tcp://127.0.0.1:2375`
- `sandbox.start_url`: initial browser page, currently best set to `chrome://new-tab-page/`

### 3. Start the backend

```bash
python -m venv .venv
# Windows PowerShell
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python main.py
```

The backend listens on `8000` by default. On startup it automatically creates one sandbox and binds the active agent
runtime to it.

### 4. Start the frontend

```bash
cd ui
npm install
npm run dev
```

Default frontend URL: `http://localhost:5173`

### 5. Open the workspace

- the frontend calls the backend at `http://localhost:8000`
- WebSocket URL: `ws://localhost:8000/ws/{client_id}`
- the browser panel uses the sandbox noVNC endpoint
- inspect mapped ports with `GET /sandboxes?include_exited=false`

## Runtime Model

### ReAct loop

The main execution flow is:

`THINK -> DECIDE -> EXECUTE -> OBSERVE -> GUARD -> END`

Where:

- `THINK`: the LLM proposes the next step
- `DECIDE`: the runtime decides between answer, tool call, HITL, or stop path
- `EXECUTE`: tools or browser actions run
- `OBSERVE`: results are collected and fed back
- `GUARD`: blocking signals, confirmations, interruption, and context boundaries are enforced

### Stop / CANCELLED semantics

- idle: the primary button shows `Send`
- request sent but `trace_id` not returned yet: loading
- active run with `trace_id`: `Stop`
- trace in `WAITING/HITL`: `Stop` is hidden and HITL takes over

Backend behavior:

- `POST /agent/stop` resolves the active worker task for the target trace and cancels it
- the executor catches `asyncio.CancelledError` and records the trace as `CANCELLED`
- cancelled traces can later start a new turn on the same trace

### HITL and context boundaries

The runtime is intentionally strict about what is allowed to enter future context.

Rules:

- plain-text answers enter future messages only after completion
- tool steps enter future messages only when `assistant.tool_calls` and all tool results are structurally complete
- `REQUEST_INPUT` continuation is injected only after the user actually completes it with `done`
- interrupted partial steps are excluded from future LLM context

## Built-in Tools and Sandbox Runtime

### File and command tools

- `bash`
- `glob`
- `read`
- `grep`
- `edit`
- `write`

### Browser tools

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

### Tool contract

- every tool returns `output` with optional `metadata`
- oversized output is referenced through `metadata.truncated=true` and `metadata.outputPath`
- browser tools attach current tab summaries
- blocking pages may attach `metadata.hitlBarrier`

### Sandbox model

- one sandbox is auto-created on backend startup
- the current agent runtime stays bound to that startup sandbox
- `New Session` resets browser state but does not destroy the sandbox
- `SandboxConsole` uses `/sandboxes/health` and `/sandboxes/{sandbox_id}/logs` for diagnostics

## API Summary

### Agent API

- `POST /agent/run`: create a new trace, resume `WAITING/HITL`, or start a new turn on an existing terminal trace
- `POST /agent/stop`: stop the active run for a trace
- `GET /agent/health`: agent service health
- `GET /agent/registry-summary`: current tool registry summary

### Sandbox API

- `GET /sandboxes`: list sandboxes
- `POST /sandboxes`: create a sandbox
- `GET /sandboxes/{sandbox_id}`: inspect one sandbox
- `POST /sandboxes/{sandbox_id}/start`: start a sandbox
- `POST /sandboxes/{sandbox_id}/stop`: stop a sandbox
- `DELETE /sandboxes/{sandbox_id}`: delete a sandbox
- `GET /sandboxes/health?sandbox_id=<optional>`: health-check a specific sandbox or the currently running one
- `GET /sandboxes/{sandbox_id}/logs`: read sandbox logs
- `POST /sandboxes/browser/reset`: reset browser tabs in the currently running sandbox

## Configuration Notes

### `config.yaml`

- `app.port`: backend listening port
- `log.level`: log level
- `llm.model` / `llm.api_key` / `llm.base_url`: OpenAI-compatible LLM settings
- `sandbox.image`: default sandbox image
- `sandbox.public_host`: host used by the frontend for noVNC
- `sandbox.healthcheck_host`: host used by the backend for sandbox `/health`
- `sandbox.docker_base_url`: Docker Engine address
- `sandbox.start_url`: initial Chromium page
- `sandbox.display_width` / `sandbox.display_height`: virtual desktop resolution

### `mcp_config.json.example`

- the repository already includes MCP client and connector implementations
- both `stdio` and `http` MCP integration are supported

## Known Limitations

- checkpoints, message caches, and WebSocket connection state are still process-memory only; in-flight traces are not
  recoverable after restart
- the LLM provider path is now async chat completion; stop usually reaches an in-flight LLM await faster, but actual
  cancellation latency still depends on the OpenAI SDK, underlying HTTP behavior, and upstream compatibility
- some tool awaits may not be truly cancellable; a trace can move to `CANCELLED` before the underlying I/O fully exits
- the agent runtime still binds only to the sandbox auto-created on startup; manually created sandboxes do not
  automatically become the active execution target
- `CheckpointStore` and `InMemoryCacheManager` are still memory-backed only


## Special Thanks

- `docker-baseimage-gui` for the base image:
  https://github.com/jlesage/docker-baseimage-gui
- `opencode` for tool-runtime and interaction design references:
  https://github.com/anomalyco/opencode
