# StewardFlow: ReAct & HITL Agent

![StewardFlow Banner](public/banner.png)

 中文 | [English](README_en.md)

StewardFlow 是一个基于 FastAPI 的 ReAct + HITL（人机协作）智能体系统，提供可视化前端工作台、可追踪的执行日志，以及可扩展的工具与 MCP 服务接入。它适合快速构建“可控、可追溯、可复现”的智能助手。

## Demo

本案例使用的LLM为：qwen3.5-plus

> 如果你没有qwen的API Key，你有两种方式可以体验Agent项目。
> 
> 1. 到 `https://www.modelscope.cn/` 获取免费的API Key，支持每天免费20次模型调用
> 2. 到 `https://bailian.console.aliyun.com/` 申请免费API Key，新用户可以获取qwen3.5-plus模型100万tokens的免费体验额度。

### 1. 打开小红书，搜索千问大模型的主页，并总结

**可以观看`public/demo1.mp4`视频**

### 2. 查看当前目录有哪些文件？

**可以观看`public/demo2.mp4`视频**

### 3. 使用 `exec` 工具在 docker-sandbox 内执行命令

**可以观看`public/demo3.mp4`视频**

## 功能概览
- ReAct + HITL 任务编排：支持需要用户确认或补充输入的步骤
- 工具系统：内置 docker-sandbox 工具（`bash` / `glob` / `read` / `search` / 任务向 `browser_*`）
- Docker Sandbox：服务启动自动创建，服务关闭自动删除
- VNC 浏览视图：前端直接渲染沙箱 noVNC 地址
- WebSocket 实时推送：展示 Thought/Action/Observation/Final 等执行日志
- 前后端分离：FastAPI 后端 + Vite/React 前端工作台

## 目录结构（关键）
- `main.py`：后端入口
- `config.yaml.example`：后端配置示例
- `mcp_config.json.example`：MCP 服务配置示例
- `ui/`：前端项目（Vite + React）
- `public/banner.png`：README 顶部横幅

## Docker Sandbox 构建与联通性检查（必读）
默认你已安装 Docker。本项目的后端会通过 `sandbox.docker_base_url` 连接远端 Docker Engine，并在服务启动时自动创建沙箱容器。

### 1. 检查 Docker 是否开放 2375
在 Docker 所在机器执行：
```bash
ss -lntp | grep 2375
```

如果没有监听 `2375`，建议使用 `systemd override` 开启 TCP 监听（仅限可信内网，不要直接暴露公网）：
```bash
sudo mkdir -p /etc/systemd/system/docker.service.d
sudo tee /etc/systemd/system/docker.service.d/override.conf > /dev/null <<'EOF'
[Service]
ExecStart=
ExecStart=/usr/bin/dockerd -H fd:// -H tcp://0.0.0.0:2375 --containerd=/run/containerd/containerd.sock
EOF
```
然后重载并重启 Docker：
```bash
sudo systemctl daemon-reload
sudo systemctl restart docker
```

如需撤销该配置：
```bash
sudo rm -f /etc/systemd/system/docker.service.d/override.conf
sudo systemctl daemon-reload
sudo systemctl restart docker
```

从 StewardFlow 所在机器验证连通：
```bash
curl http://<docker-vm-ip>:2375/_ping
```
预期返回：`OK`

### 2. 检查防火墙策略（随机端口 vs 固定端口）
查看防火墙状态：
```bash
sudo ufw status
```

当前后端自动创建沙箱时，`noVNC/VNC/API` 端口默认使用随机宿主机端口。  
如果防火墙未关闭，不建议用随机端口（难以逐个放行）。建议改固定端口并显式放行。

固定端口示例（修改 `main.py` 里的 `_auto_create_sandbox()`）：
```python
novnc_port=5800,
vnc_port=5900,
api_port=8899,
```
然后放行这 3 个端口。

### 3. 构建 docker-sandbox 镜像
```bash
cd sandbox
docker build -t gui-sandbox:dev .
```

### 4. 配置 `config.yaml`
先复制配置：
```bash
cp config.yaml.example config.yaml
```

至少确认以下字段：
- `sandbox.image: gui-sandbox:dev`
- `sandbox.tool_profile: task`（默认，仅暴露 9 个任务向浏览器工具：`navigate_page/take_snapshot/wait_for/browser_click/fill/type_text/browser_press_key/upload_file/browser_tabs`；设为 `debug` 可额外暴露细粒度页签与调试工具）
- `sandbox.public_host: <docker-vm-ip>`（前端访问 noVNC 用）
- `sandbox.healthcheck_host: <docker-vm-ip>`（后端调用沙箱 API 用）
- `sandbox.docker_base_url: tcp://<docker-vm-ip>:2375`
- `sandbox.start_url` / `sandbox.display_width` / `sandbox.display_height`
- `llm.model` / `llm.api_key` / `llm.base_url`

### 5. 启动后端（自动拉起沙箱容器）
```bash
python main.py
```
后端启动时会自动创建沙箱容器；后端关闭时会自动删除沙箱容器。  
可通过 `GET /sandboxes?include_exited=false` 查看当前运行中的沙箱实例和映射端口。

## 快速启动

### 1. 配置后端
```
cp config.yaml.example config.yaml
```
编辑 `config.yaml`，至少填写：
- `llm.api_key`
- `llm.model`
- `llm.base_url`

如果你需要 MCP 服务（可选）：
```
cp mcp_config.json.example mcp_config.json
```

### 2. 启动后端
```
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
python main.py
```
默认端口：`8000`（可在 `config.yaml` 的 `app.port` 修改）

### 3. 启动前端
```
cd ui
npm install
npm run dev
```
默认地址：`http://localhost:5173`

### 4. 访问前端并开始使用
- 前端会通过 `http://localhost:8000` 与后端通信
- WebSocket 会连接 `ws://localhost:8000/ws/{client_id}` 以获取实时事件

## 配置说明
### `config.yaml`
- `app.port`：后端监听端口
- `log.level`：日志级别（如 `info`）
- `sandbox.image`：沙箱镜像名（例如 `gui-sandbox:dev`）
- `sandbox.tool_profile`：浏览器工具暴露配置。`task`（默认）仅暴露 9 个任务向浏览器工具；`debug` 额外暴露 `list_pages/new_page/select_page/close_page/browser_handle_dialog/browser_select_option/browser_take_screenshot/browser_close/browser_drag/browser_evaluate/browser_hover`
- `sandbox.public_host`：用于前端 noVNC 访问的宿主机地址
- `sandbox.healthcheck_host`：后端访问沙箱 API 的地址
- `sandbox.docker_base_url`：Docker Engine 地址（例如 `tcp://192.168.130.147:2375`）
- `sandbox.start_url`：容器启动后 Chromium 首次打开地址
- `sandbox.display_width` / `sandbox.display_height`：VNC 虚拟桌面分辨率
- `llm.model` / `llm.api_key` / `llm.base_url`：LLM 提供商配置

## 工具结果约定
- 工具结果由沙箱 API 直接返回，不再做本地 `tool_result` 外部化封装。
- 仅命令行工具（`bash/glob/read/search`）使用 envelope：`{"ok":bool,"data":...,"artifacts":[...],"error":...}`。
- 上述命令行工具的 `artifacts` 内包含 `stdout/stderr`（含 `preview`，必要时包含 `path`）。
- 当 `artifacts[].truncated=true` 时，可继续用 `bash` 在对应 `path` 上做精确查询（如 `sed/head/tail/search`）。
- 其他工具（尤其浏览器工具）保持原有返回格式；仅在内容过大时返回 `output.preview/path/truncated` 外部化结构。
- 未截断时默认不返回 `path`；如需强制保留文件，可传 `persist_output=true`。

## API 入口
- `POST /agent/run`：启动或继续一次任务
- `GET /agent/health`：Agent 子系统健康检查
- `GET /health`：服务健康检查


codex resume 019cbc29-c47f-7541-a0f1-1c4706ca1b77
做code review
做好工具执行结果schema
