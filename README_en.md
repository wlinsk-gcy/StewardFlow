# StewardFlow: ReAct & HITL Agent

![StewardFlow Banner](public/banner.png)

StewardFlow is a FastAPI-based ReAct + HITL (Human-in-the-Loop) agent system. It provides a visual front-end workspace, traceable execution logs, and extensible integrations for tools and MCP services. It is well-suited for quickly building intelligent assistants that are **controllable, traceable, and reproducible**.

## Demo

The LLM used in this case is qwen3.5-plus

> If you don't have a qwen API key, you have two ways to experience the Agent project.
> 
> 1. Go to `https:www.modelscope.cn` to get a free API key, which supports 20 free model calls per day
> 2. Go to `https:bailian.console.aliyun.com` to apply for a free API Key, and new users can get a free trial credit of 1 million tokens for the qwen3.5-plus model.


### 1. Open Xiaohongshu, search for the homepage of the Qianwen model, and summarize

**You can watch the 'public/demo1.mp4' video**

### 2. 查看当前目录有哪些文件？

**You can watch the 'public/demo2.mp4' video**

### 3. Use the `exec` tool to run commands in docker-sandbox

**You can watch the 'public/demo3.mp4' video**

## Key Features
- **ReAct + HITL orchestration**: supports steps that require user confirmation or additional input
- **Tool system**: docker-sandbox tools only (`bash` / `glob` / `read` / `grep` / `rg` / `browser_*`)
- **Docker sandbox lifecycle**: auto-create on backend startup, auto-delete on backend shutdown
- **VNC browser view**: UI renders sandbox noVNC URL directly
- **Real-time WebSocket streaming**: shows execution logs such as Thought/Action/Observation/Final
- **Frontend-backend separation**: FastAPI backend + Vite/React frontend workspace

## Project Structure (Key Files)
- `main.py`: backend entry point
- `config.yaml.example`: backend configuration example
- `mcp_config.json.example`: MCP service configuration example
- `ui/`: frontend project (Vite + React)
- `public/banner.png`: banner image at the top of the README

## Quick Start

### 1. Configure the backend
```bash
cp config.yaml.example config.yaml
```

Edit config.yaml and fill in at least:
- llm.api_key
- llm.model
- llm.base_url

If you need MCP services (optional):
```
cp mcp_config.json.example mcp_config.json
```

### 2. Start the backend
```
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
python main.py
```
Default port: `8000` (can be changed via `app.port` in `config.yaml`)

### 3. Start the frontend
```
cd ui
npm install
npm run dev
```
Default URL: http://localhost:5173

### 4. Open the UI and start using it
- The frontend communicates with the backend via `http://localhost:8000`
- WebSocket connects to `ws://localhost:8000/ws/{client_id}` to receive real-time events

## Configuration
### `config.yaml`
- `app.port`: backend listening port
- `log.level`: log level (e.g., info)
- `sandbox.image`: sandbox image name (for example `gui-sandbox:dev`)
- `sandbox.public_host`: host/IP used by frontend noVNC URL
- `sandbox.healthcheck_host`: host/IP used by backend when calling sandbox API
- `sandbox.docker_base_url`: Docker Engine endpoint (for example `tcp://192.168.130.147:2375`)
- `sandbox.start_url`: initial URL opened by Chromium inside sandbox
- `sandbox.display_width` / `sandbox.display_height`: virtual desktop resolution
- `llm.model` / `llm.api_key` / `llm.base_url`: LLM provider settings

## Tool Result Contract
- Tool results are returned directly from sandbox API; local `tool_result` externalization is removed.
- `bash/glob/read/grep/rg` return `stdout` / `stderr`; read `preview` first.
- When output is truncated, sandbox API includes `path` (inside `/config/tool-artifacts/results`) for follow-up targeted queries via `bash` + `rg/head/tail/sed`.
- By default, non-truncated output does not include `path`; set `persist_output=true` to force artifact persistence.

## API Endpoints
- `POST /agent/run`: start or continue a task
- `GET /agent/health`: agent subsystem health check
- `GET /health`: service health check
