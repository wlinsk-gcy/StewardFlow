# StewardFlow: ReAct & HITL Agent

![StewardFlow Banner](public/banner.png)

 中文 | [English](README_en.md)

StewardFlow 是一个基于 FastAPI 的 ReAct + HITL（人机协作）智能体系统，提供可视化前端工作台、可追踪的执行日志，以及可扩展的工具与 MCP 服务接入。它适合快速构建“可控、可追溯、可复现”的智能助手。

## Demo

### 1. 打开小红书，搜索千问大模型的主页，并总结

<video src="./public/demo1.mp4"></video>

### 2. 查看当前目录有哪些文件？

<video src="./public/demo2.mp4"></video>

### 3. 使用bash工具执行dir命令，查看当前目录有什么东西

<video src="./public/demo3.mp4"></video>

## 功能概览
- ReAct + HITL 任务编排：支持需要用户确认或补充输入的步骤
- 工具系统：内置 `bash`、`ls`、`grep`、`glob`、`read`、`snapshot_query` 等
- Web Search 与截图回传：前端可显示浏览器截图与检索结果
- WebSocket 实时推送：展示 Thought/Action/Observation/Final 等执行日志
- 前后端分离：FastAPI 后端 + Vite/React 前端工作台

## 目录结构（关键）
- `main.py`：后端入口
- `config.yaml.example`：后端配置示例
- `mcp_config.json.example`：MCP 服务配置示例
- `ui/`：前端项目（Vite + React）
- `public/banner.png`：README 顶部横幅

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
- `snapshot_path`：截图/快照存储目录（默认 `data`）
- `llm.model` / `llm.api_key` / `llm.base_url`：LLM 提供商配置

## API 入口
- `POST /agent/run`：启动或继续一次任务
- `GET /agent/health`：Agent 子系统健康检查
- `GET /health`：服务健康检查

