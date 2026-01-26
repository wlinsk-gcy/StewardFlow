import React, { useState, useRef, useEffect, useCallback } from "react";
import { v4 as uuidv4 } from "uuid";
import {
  Play,
  Terminal,
  Cpu,
  MessageSquare,
  Layers,
  Send,
  Loader2,
  Globe,
  User,
  Bot,
  Search,
  ExternalLink,
} from "lucide-react";
import { type AgentStep, type ChatMessage } from "../types";

import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

function normalizeText(s: string) {
  return s.replace(/\\n/g, "\n").replace(/\\t/g, "\t");
}

const MessageContent: React.FC<{ content: string }> = ({ content }) => {
  return (
    <div className="prose prose-sm prose-p:my-2 prose-li:my-1 prose-ul:my-2 prose-ol:my-2 prose-a:text-indigo-600 prose-a:no-underline hover:prose-a:underline max-w-none">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          a: (props) => <a {...props} target="_blank" rel="noreferrer" />,
        }}
      >
        {normalizeText(content || "")}
      </ReactMarkdown>
    </div>
  );
};

type PendingConfirm = {
  requestId: string;
  prompt: string;
  toolName?: string;
  args?: unknown;
  turnId?: string;
};

function getConfirmCommand(pending: PendingConfirm | null): string | null {
  if (!pending) return null;
  const args = pending.args as { command?: unknown } | undefined;
  if (pending.toolName === "bash" && typeof args?.command === "string") {
    return args.command;
  }
  if (typeof pending.args === "string") return pending.args;
  if (pending.args && typeof pending.args === "object") {
    try {
      return JSON.stringify(pending.args);
    } catch {
      return null;
    }
  }
  return null;
}

export const AgentWorkbench: React.FC = () => {
  const [goal, setGoal] = useState("");
  const [chatHistory, setChatHistory] = useState<ChatMessage[]>([]);
  const [isRunning, setIsRunning] = useState(false);
  const [steps, setSteps] = useState<AgentStep[]>([]);
  const [activeTab, setActiveTab] = useState<"runner" | "browser">("runner");
  const [currentUrl, setCurrentUrl] = useState("about:blank");
  const [currentScreenshot, setCurrentScreenshot] = useState<string | null>(
    null,
  );
  const [tokenInfo, setTokenInfo] = useState<{
    prompt_tokens: number;
    completion_tokens: number;
    total_tokens: number;
  } | null>(null);
  const [pendingConfirm, setPendingConfirm] = useState<PendingConfirm | null>(
    null,
  );

  // --- 新增：WebSocket 相关状态 ---
  const [clientId] = useState(() => uuidv4()); // 保持整个生命周期 clientId 唯一
  const [agentId, setAgentId] = useState<string | null>(null); // 新增：存储后端返回的 agent_id
  const socketRef = useRef<WebSocket | null>(null);
  const chatScrollRef = useRef<HTMLDivElement>(null);
  const traceScrollRef = useRef<HTMLDivElement>(null);

  // --- 新增：初始化 WebSocket ---
  const handleIncomingEvent = useCallback((event: any) => {
    const { event_type, data, timestamp, turn_id } = event;
    // console.log('incoming event', event);
    if (event_type === "screenshot") {
      console.log("[ws] screenshot event", data);
      if (data?.content) {
        setCurrentScreenshot(data.content);
        setActiveTab("browser");
      }
      return;
    }

    if (event_type === "token_info") {
      setTokenInfo({
        prompt_tokens: Number(data?.prompt_tokens ?? 0),
        completion_tokens: Number(data?.completion_tokens ?? 0),
        total_tokens: Number(data?.total_tokens ?? 0),
      });
      return;
    }

    // 1. 处理执行日志 (Execution Trace)
    // 日志通常不需要流式输出，直接追加到 steps 数组即可
    if (["thought", "action", "observation", "final"].includes(event_type)) {
      // console.log('receive execute trace', event);
      const MAX_LEN = 500;
      const rawContent = data.content ?? "";
      const content =
        rawContent.length > MAX_LEN ? rawContent.slice(0, MAX_LEN) + "..." : rawContent;
      const newStep: AgentStep = {
        type: event_type,
        content: content,
        tool: data.tool_name,
        toolInput: data.args,
        timestamp: timestamp,
      };
      setSteps((prev) => [...prev, newStep]);

      // 如果是 action 且涉及到浏览器（根据你的业务逻辑），可以切换 Tab
      if (event_type === "action" && data.tool_name?.includes("browser")) {
        // 这里可以根据实际 data 里的 args 解析出 URL
        // setCurrentUrl(...)
      }
    }

    // --- 2. 处理流式消息 (Answer & HITL Request) ---
    if (event_type === "answer" || event_type === "hitl_request") {
      // console.log('receive answer or hitl_request');
      setIsRunning((prev) => (prev ? false : prev));
      setChatHistory((prev) => {
        // 查找是否已经存在相同 turn_id 且角色为 assistant 的消息
        const existingMsgIndex = prev.findIndex(
          (m) => m.turnId === turn_id && m.role === "assistant",
        );
        if (existingMsgIndex !== -1) {
          // 如果消息已存在，在原有内容基础上追加 text
          const newHistory = [...prev];
          const targetMsg = newHistory[existingMsgIndex];
          newHistory[existingMsgIndex] = {
            ...targetMsg,
            content: targetMsg.content + (data.content || ""),
            // 如果是 hitl_request，确保标记状态
            isHitl: event_type.startsWith("hitl") ? true : targetMsg.isHitl,
            hitlType:
              event_type === "hitl_request" ? "request" : targetMsg.hitlType,
          };
          return newHistory;
        } else {
          // 如果是该 turn_id 的第一块数据，创建新消息
          return [
            ...prev,
            {
              id: uuidv4(),
              role: "assistant",
              content: data.content || "",
              timestamp: new Date(timestamp).getTime(),
              turnId: turn_id,
              isHitl: event_type.startsWith("hitl"),
              hitlType: event_type === "hitl_request" ? "request" : undefined,
            },
          ];
        }
      });
    }

    if (event_type === "hitl_confirm") {
      setIsRunning(false);
      const promptText = data?.prompt || "Please confirm the action.";
      setPendingConfirm({
        requestId: data?.request_id || uuidv4(),
        prompt: promptText,
        toolName: data?.tool_name,
        args: data?.args,
        turnId: turn_id,
      });
      setChatHistory((prev) => [
        ...prev,
        {
          id: uuidv4(),
          role: "assistant",
          content: promptText,
          timestamp: new Date(timestamp).getTime(),
          turnId: turn_id,
          isHitl: true,
          hitlType: "confirm",
        },
      ]);
    }

    // 2. 处理聊天窗口消息 (Chat Window)
    if (event_type === "error") {
      const newMessage: ChatMessage = {
        id: uuidv4(),
        role: "assistant",
        content: data.content,
        timestamp: new Date(timestamp).getTime(),
        // 如果是 hitl_confirm，可以在这里扩展 type 以后展示按钮
        isHitl: event_type.startsWith("hitl"),
      };
      setChatHistory((prev) => [...prev, newMessage]);
      setIsRunning(false);
    }

    // if (event_type === 'end' || event_type === 'error') {
    //     // console.log('stop running')
    //     setIsRunning(false);
    // }
  }, []);

  useEffect(() => {
    let closedByUser = false;
    let retryCount = 0;
    let retryTimer: number | undefined;

    const connect = () => {
      const socket = new WebSocket(`ws://localhost:8000/ws/${clientId}`);
      socketRef.current = socket;

      socket.onopen = () => {
        retryCount = 0;
        console.log("Connected to WS:", clientId);
      };

      socket.onmessage = (event) => {
        const serverEvent = JSON.parse(event.data);
        handleIncomingEvent(serverEvent);
      };

      socket.onerror = () => {
        // Error ????? close?????????????
        try {
          socket.close();
        } catch {
          // ignore
        }
      };

      socket.onclose = () => {
        console.log("WS Disconnected");
        if (closedByUser) return;
        const delay = Math.min(1000 * 2 ** retryCount, 10000);
        retryCount += 1;
        retryTimer = window.setTimeout(connect, delay);
        console.log(`[ws] reconnect in ${delay}ms`);
      };
    };

    connect();

    return () => {
      closedByUser = true;
      if (retryTimer) {
        clearTimeout(retryTimer);
      }
      if (socketRef.current && socketRef.current.readyState <= 1) {
        socketRef.current.close();
      }
    };
  }, [clientId, handleIncomingEvent]);

  useEffect(() => {
    if (chatScrollRef.current) {
      chatScrollRef.current.scrollTop = chatScrollRef.current.scrollHeight;
    }
  }, [chatHistory, isRunning]);

  useEffect(() => {
    if (traceScrollRef.current) {
      traceScrollRef.current.scrollTop = traceScrollRef.current.scrollHeight;
    }
  }, [steps]);

  // --- 新增：事件分流处理器 ---
  const handleConfirm = async (decision: "confirm" | "reject") => {
    if (!agentId || isRunning) return;

    const userMessage: ChatMessage = {
      id: uuidv4(),
      role: "user",
      content: decision === "confirm" ? "Confirm" : "Reject",
      timestamp: Date.now(),
    };

    setChatHistory((prev) => [...prev, userMessage]);
    setPendingConfirm(null);
    setIsRunning(true);

    try {
      const response = await fetch("http://localhost:8000/agent/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          client_id: clientId,
          task: decision,
          ...(agentId && { agent_id: agentId }),
        }),
      });

      if (!response.ok) throw new Error("Failed to submit confirmation");
    } catch (e) {
      console.error(e);
      setChatHistory((prev) => [
        ...prev,
        {
          id: uuidv4(),
          role: "assistant",
          content: "Failed to submit confirmation. Please try again.",
          timestamp: Date.now(),
        },
      ]);
      setIsRunning(false);
    }
  };

  const handleRun = async () => {
    if (!goal.trim() || isRunning || pendingConfirm) return;

    // 1. UI 反馈
    const userMessage: ChatMessage = {
      id: uuidv4(),
      role: "user",
      content: goal,
      timestamp: Date.now(),
    };

    setChatHistory((prev) => [...prev, userMessage]);
    setSteps([]); // 清空旧日志
    if (!agentId) setTokenInfo(null);
    setIsRunning(true);
    const currentGoal = goal;
    setGoal("");

    // 2. 调用后端 API 触发任务
    try {
      const response = await fetch("http://localhost:8000/agent/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          client_id: clientId,
          task: currentGoal,
          // --- 新增：如果已经有 agent_id，则携带它 ---
          ...(agentId && { agent_id: agentId }),
        }),
      });

      if (!response.ok) throw new Error("Failed to start agent");

      const data = await response.json();
      if (data.agent_id) {
        setAgentId(data.agent_id);
        console.log("Current Agent ID session:", data.agent_id);
      }
    } catch (e) {
      console.error(e);
      setChatHistory((prev) => [
        ...prev,
        {
          id: uuidv4(),
          role: "assistant",
          content: "连接服务器失败，请稍后再试。",
          timestamp: Date.now(),
        },
      ]);
      setIsRunning(false);
    }
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleRun();
    }
  };

  return (
    <div className="flex h-full flex-col overflow-hidden rounded-2xl border border-gray-200 bg-white shadow-xl">
      {/* Tab Header */}
      <div className="flex shrink-0 items-center justify-between border-b border-gray-100 bg-white px-6 py-4">
        <div className="flex items-center gap-3">
          <div className="rounded-xl bg-indigo-600 p-2.5 shadow-lg shadow-indigo-200">
            <Cpu className="h-5 w-5 text-white" />
          </div>
          <div>
            <h2 className="font-bold text-gray-900">Steward Flow</h2>
            <div className="mt-0.5 flex items-center gap-2">
              <span className="flex h-2 w-2 rounded-full bg-green-500"></span>
              <p className="text-[10px] font-bold tracking-widest text-gray-500 uppercase">
                System Online
              </p>
            </div>
          </div>
        </div>

        <div className="flex gap-1.5 rounded-xl bg-gray-100 p-1">
          <TabButton
            active={activeTab === "runner"}
            onClick={() => setActiveTab("runner")}
            icon={Terminal}
            label="Execution Trace"
          />
          <TabButton
            active={activeTab === "browser"}
            onClick={() => setActiveTab("browser")}
            icon={Globe}
            label="Browser View"
          />
        </div>
      </div>

      <div className="flex flex-1 divide-x divide-gray-100 overflow-hidden">
        {/* Left Column: Chat History & Input */}
        <div className="flex w-1/2 flex-col bg-white">
          {/* Chat Messages */}
          <div
            ref={chatScrollRef}
            className="flex-1 space-y-6 overflow-y-auto bg-gray-50/30 p-6"
          >
            {chatHistory.length === 0 && (
              <div className="flex h-full flex-col items-center justify-center px-10 text-center">
                <div className="mb-6 flex h-20 w-20 items-center justify-center rounded-3xl bg-indigo-50">
                  <Bot className="h-10 w-10 text-indigo-500 opacity-60" />
                </div>
                <h3 className="mb-2 text-xl font-bold text-gray-900">
                  你好，我是你的私人AI助理
                </h3>
                <p className="text-sm leading-relaxed text-gray-500">
                  让我浏览网页、进行复杂计算，或用ReAct框架解决逻辑谜题。
                </p>
              </div>
            )}
            {chatHistory.map((msg) => (
              <div
                key={msg.id}
                className={`flex ${msg.role === "user" ? "justify-end" : "justify-start"}`}
              >
                <div
                  className={`flex max-w-[85%] gap-3 ${msg.role === "user" ? "flex-row-reverse" : "flex-row"}`}
                >
                  <div
                    className={`flex h-8 w-8 shrink-0 items-center justify-center rounded-lg shadow-sm ${
                      msg.role === "user"
                        ? "bg-indigo-600 text-white"
                        : "border border-gray-200 bg-white text-indigo-600"
                    }`}
                  >
                    {msg.role === "user" ? (
                      <User className="h-4 w-4" />
                    ) : (
                      <Bot className="h-4 w-4" />
                    )}
                  </div>
                  <div
                    className={`rounded-2xl p-4 text-left text-sm leading-relaxed break-words shadow-sm ${
                      msg.role === "user"
                        ? "rounded-tr-none bg-indigo-600 text-white"
                        : "rounded-tl-none border border-gray-100 bg-white text-gray-800"
                    }`}
                  >
                    <MessageContent content={String(msg.content ?? "")} />
                    {msg.role === "assistant" &&
                      msg.hitlType === "confirm" &&
                      pendingConfirm &&
                      msg.turnId === pendingConfirm.turnId && (
                        <div className="mt-3 rounded-xl border border-indigo-100 bg-indigo-50/60 p-3 shadow-inner">
                          <div className="flex items-center gap-2 text-[10px] font-black tracking-widest text-indigo-600 uppercase">
                            <span className="h-1.5 w-1.5 rounded-full bg-indigo-500"></span>
                            Bash Command Confirmation
                          </div>
                          {getConfirmCommand(pendingConfirm) && (
                            <div className="mt-2 rounded-lg border border-slate-900 bg-slate-950 px-3 py-2 font-mono text-[11px] text-slate-100">
                              $ {getConfirmCommand(pendingConfirm)}
                            </div>
                          )}
                          <div className="mt-3 flex gap-2">
                            <button
                              onClick={() => handleConfirm("confirm")}
                              className="rounded-lg bg-emerald-600 px-3 py-2 text-[10px] font-bold tracking-widest text-white uppercase hover:bg-emerald-700"
                            >
                              Approve
                            </button>
                            <button
                              onClick={() => handleConfirm("reject")}
                              className="rounded-lg bg-rose-600 px-3 py-2 text-[10px] font-bold tracking-widest text-white uppercase hover:bg-rose-700"
                            >
                              Reject
                            </button>
                          </div>
                        </div>
                      )}
                  </div>
                </div>
              </div>
            ))}
            {isRunning && (
              <div className="animate-in fade-in slide-in-from-left-2 flex justify-start">
                <div className="flex gap-3">
                  <div className="flex h-8 w-8 items-center justify-center rounded-lg border border-gray-200 bg-white text-indigo-600">
                    <Bot className="h-4 w-4" />
                  </div>
                  <div className="flex items-center gap-2 rounded-2xl rounded-tl-none border border-gray-100 bg-white p-4 shadow-sm">
                    <div className="flex gap-1">
                      <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-indigo-300"></span>
                      <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-indigo-400 [animation-delay:0.2s]"></span>
                      <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-indigo-500 [animation-delay:0.4s]"></span>
                    </div>
                    <span className="ml-2 text-xs font-bold tracking-widest text-gray-400 uppercase">
                      Agent is thinking...
                    </span>
                  </div>
                </div>
              </div>
            )}
          </div>

          {/* Chat Input */}
          <div className="border-t border-gray-100 bg-white p-6">
            <div className="group relative">
              <div className="absolute -inset-1 rounded-2xl bg-gradient-to-r from-indigo-500 to-purple-500 opacity-10 blur transition duration-1000 group-focus-within:opacity-25"></div>
              <div className="relative overflow-hidden rounded-2xl border border-gray-200 bg-white transition-all focus-within:border-indigo-500 focus-within:ring-4 focus-within:ring-indigo-50">
                <textarea
                  className="h-24 w-full resize-none bg-transparent p-4 pr-16 font-sans text-sm outline-none"
                  placeholder="请告诉我您的需求是什么..."
                  value={goal}
                  onChange={(e) => setGoal(e.target.value)}
                  onKeyDown={handleKeyDown}
                  disabled={isRunning || Boolean(pendingConfirm)}
                />
                <button
                  onClick={handleRun}
                  disabled={
                    isRunning || Boolean(pendingConfirm) || !goal.trim()
                  }
                  className={`absolute right-3 bottom-3 rounded-xl p-3 transition-all ${
                    isRunning || Boolean(pendingConfirm) || !goal.trim()
                      ? "bg-gray-100 text-gray-400"
                      : "bg-indigo-600 text-white shadow-lg shadow-indigo-200 hover:bg-indigo-700 active:scale-95"
                  }`}
                >
                  {isRunning ? (
                    <Loader2 className="h-5 w-5 animate-spin" />
                  ) : (
                    <Send className="h-5 w-5" />
                  )}
                </button>
              </div>
            </div>
            <p className="mt-3 text-center text-[10px] font-medium tracking-widest text-gray-400 uppercase">
              ReAct Protocol v1.0 · Multi-Modality Driven
            </p>
          </div>
        </div>

        {/* Right Column: Dynamic View */}
        <div className="flex w-1/2 flex-col bg-gray-50/50">
          {activeTab === "runner" ? (
            <div className="flex h-full flex-col overflow-hidden">
              <div className="flex items-center justify-between border-b border-gray-200 bg-white px-6 py-4">
                <div className="flex items-center gap-2">
                  <Terminal className="h-4 w-4 text-indigo-500" />
                  <h3 className="text-sm font-bold tracking-tight text-gray-700 uppercase">
                    执行日志
                  </h3>
                </div>
                <div className="flex items-center gap-2">
                  {tokenInfo && (
                    <div className="flex items-center gap-2 rounded-full border border-indigo-100 bg-indigo-50 px-2 py-0.5 text-[10px] font-semibold text-indigo-700">
                      <span>Prompt {tokenInfo.prompt_tokens}</span>
                      <span className="text-indigo-300">/</span>
                      <span>Completion {tokenInfo.completion_tokens}</span>
                      <span className="text-indigo-300">/</span>
                      <span>Total {tokenInfo.total_tokens}</span>
                    </div>
                  )}
                  <span className="rounded-full bg-indigo-500 px-2 py-0.5 text-[10px] font-black text-white">
                    {steps.length} LOGS
                  </span>
                </div>
              </div>
              <div
                ref={traceScrollRef}
                className="flex-1 space-y-4 overflow-y-auto p-6"
              >
                {steps.length === 0 ? (
                  <div className="flex h-full flex-col items-center justify-center opacity-20 grayscale select-none">
                    <Layers className="mb-4 h-16 w-16" />
                    <p className="text-sm font-bold tracking-widest uppercase">
                      Trace visualization inactive
                    </p>
                  </div>
                ) : (
                  steps.map((step, idx) => <StepCard key={idx} step={step} />)
                )}
              </div>
            </div>
          ) : (
            <div className="flex h-full flex-col overflow-hidden bg-gray-200">
              {/* Browser Chrome UI */}
              <div className="flex flex-col border-b border-gray-300 bg-[#e0e0e0]">
                <div className="flex items-center gap-2 p-2">
                  <div className="ml-1 flex gap-1.5">
                    <div className="h-3 w-3 rounded-full bg-red-400"></div>
                    <div className="h-3 w-3 rounded-full bg-yellow-400"></div>
                    <div className="h-3 w-3 rounded-full bg-green-400"></div>
                  </div>
                  <div className="mx-4 flex flex-1 items-center justify-between rounded-md bg-white px-3 py-1 text-xs text-gray-500 shadow-inner">
                    <div className="flex max-w-[80%] items-center gap-2 truncate">
                      <Globe className="h-3 w-3" />
                      <span className="truncate">{currentUrl}</span>
                    </div>
                    <Search className="h-3 w-3" />
                  </div>
                  <ExternalLink className="mr-2 h-4 w-4 text-gray-400" />
                </div>
              </div>
              {/* Browser Viewport */}
              <div className="relative flex flex-1 items-center justify-center overflow-hidden bg-white shadow-inner">
                {currentScreenshot ? (
                  <img
                    src={currentScreenshot}
                    alt="Browser View"
                    className="animate-in fade-in h-full w-full object-contain duration-700"
                  />
                ) : (
                  <div className="max-w-sm p-10 text-center">
                    <div className="mx-auto mb-4 flex h-16 w-16 items-center justify-center rounded-full bg-gray-100">
                      <Globe className="h-8 w-8 text-gray-300" />
                    </div>
                    <h4 className="text-xs font-bold tracking-widest text-gray-400 uppercase">
                      Waiting for Navigation
                    </h4>
                    <p className="mt-2 text-[11px] text-gray-400">
                      The agent will render the webpage view here once it uses
                      the browser tool.
                    </p>
                  </div>
                )}
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
};

// --- Helper Components ---
const TabButton = ({ active, onClick, icon: Icon, label }: any) => (
  <button
    onClick={onClick}
    className={`flex items-center gap-2 rounded-lg px-4 py-2 text-xs font-bold transition-all duration-200 ${
      active
        ? "bg-white text-indigo-600 shadow-md ring-1 ring-black/5"
        : "text-gray-500 hover:text-gray-800"
    }`}
  >
    <Icon className="h-3.5 w-3.5" />
    <span className="tracking-widest uppercase">{label}</span>
  </button>
);

type SearchItem = { title?: string; snippet?: string; link?: string };

function normalizeMaybeJSON(input: unknown): unknown {
  // 已经是对象/数组
  if (typeof input === "object" && input !== null) return input;

  // 字符串：尝试 parse JSON；失败就原样返回
  if (typeof input === "string") {
    const s = input.trim();
    if (s.startsWith("{") || s.startsWith("[")) {
      try {
        return JSON.parse(s);
      } catch {
        return input;
      }
    }
    return input;
  }

  return input;
}

// 可选：如果你确实会收到 Python dict 字符串（单引号 True/False/None）
// 注意：这不是完美解析器，但对“简单 dict/list”够用；失败则回退原字符串
function normalizeMaybePythonDict(input: unknown): unknown {
  const v = normalizeMaybeJSON(input);
  if (typeof v !== "string") return v;

  const s = v.trim();
  if (!(s.startsWith("{") || s.startsWith("["))) return v;

  // 先尝试 JSON
  try {
    return JSON.parse(s);
  } catch {
    //
  }

  // 再尝试粗略 python dict -> json
  try {
    const jsonLike = s
      .replace(/\bNone\b/g, "null")
      .replace(/\bTrue\b/g, "true")
      .replace(/\bFalse\b/g, "false")
      .replace(/'/g, '"');
    return JSON.parse(jsonLike);
  } catch {
    return v;
  }
}

function isSearchList(v: unknown): v is SearchItem[] {
  if (!Array.isArray(v)) return false;
  return v.every((x) => {
    if (!x || typeof x !== "object") return false;
    return "title" in x || "snippet" in x || "link" in x;
  });
}

const CodeBlock: React.FC<{ value: unknown }> = ({ value }) => {
  const text =
    typeof value === "string" ? value : JSON.stringify(value, null, 2);

  return (
    <pre className="overflow-x-auto rounded-xl border border-gray-800 bg-gray-900 p-3 text-[11px] whitespace-pre text-gray-300 shadow-lg">
      {text}
    </pre>
  );
};

const SearchResults: React.FC<{ items: SearchItem[] }> = ({ items }) => {
  const [expanded, setExpanded] = React.useState(false);

  const max = 5;
  const shown = expanded ? items : items.slice(0, max);

  return (
    <div className="space-y-2">
      {shown.map((it, idx) => {
        const clickable = Boolean(it.link);
        const Wrapper: any = clickable ? "a" : "div";

        return (
          <Wrapper
            key={idx}
            href={clickable ? it.link : undefined}
            target={clickable ? "_blank" : undefined}
            rel={clickable ? "noreferrer" : undefined}
            className={`block rounded-xl border p-3 transition ${
              clickable
                ? "border-gray-200 bg-white hover:bg-gray-50"
                : "border-gray-200 bg-white"
            }`}
          >
            <div className="text-[12px] leading-snug font-bold text-gray-900">
              {it.title || "(no title)"}
            </div>

            {it.snippet && (
              <div className="mt-1 text-[11px] leading-relaxed text-gray-600">
                {it.snippet}
              </div>
            )}

            {it.link && (
              <div className="mt-2 truncate font-mono text-[10px] text-indigo-600">
                {it.link}
              </div>
            )}
          </Wrapper>
        );
      })}

      {items.length > max && (
        <button
          onClick={() => setExpanded((v) => !v)}
          className="text-[10px] font-bold tracking-widest text-indigo-600 uppercase hover:text-indigo-700"
        >
          {expanded ? "收起" : `展开全部（${items.length}条）`}
        </button>
      )}
    </div>
  );
};

const ContentRenderer: React.FC<{ content: unknown }> = ({ content }) => {
  const v = normalizeMaybePythonDict(content);

  // 搜索结果数组：卡片列表
  if (isSearchList(v)) return <SearchResults items={v} />;

  // 普通数组/对象：代码块
  if (Array.isArray(v) || (typeof v === "object" && v !== null)) {
    return <CodeBlock value={v} />;
  }

  // 文本：正常展示
  return <div className="text-left whitespace-pre-wrap">{String(v ?? "")}</div>;
};

const StepCard: React.FC<{ step: AgentStep }> = ({ step }) => {
  const isThought = step.type === "thought";
  const isAction = step.type === "action";
  const isObs = step.type === "observation";
  const isFinal = step.type === "final";
  // const isError = step.type === "error";

  return (
    <div
      className={`animate-in fade-in slide-in-from-bottom-2 overflow-hidden rounded-2xl border duration-500 ${
        isThought
          ? "border-gray-100 bg-white"
          : isAction
            ? "border-indigo-100 bg-indigo-50/50"
            : isObs
              ? "border-emerald-100 bg-emerald-50/50"
              : isFinal
                ? "border-purple-100 bg-purple-50"
                : "border-rose-100 bg-rose-50"
      }`}
    >
      <div
        className={`flex items-center gap-2 border-b px-4 py-2 text-[10px] font-black tracking-[0.2em] uppercase ${
          isThought
            ? "border-gray-100 bg-gray-50 text-gray-400"
            : isAction
              ? "border-indigo-100 bg-indigo-100/30 text-indigo-600"
              : isObs
                ? "border-emerald-100 bg-emerald-100/30 text-emerald-700"
                : isFinal
                  ? "border-purple-100 bg-purple-100/30 text-purple-600"
                  : "border-rose-100 bg-rose-100/30 text-rose-600"
        }`}
      >
        {isThought && <MessageSquare className="h-3 w-3" />}
        {isAction && <Terminal className="h-3 w-3" />}
        {isObs && <Globe className="h-3 w-3" />}
        {isFinal && <Play className="h-3 w-3" />}
        {step.type}
        <span className="ml-auto font-mono tracking-normal normal-case opacity-30">
          {new Date(step.timestamp).toLocaleTimeString([], {
            hour: "2-digit",
            minute: "2-digit",
            second: "2-digit",
          })}
        </span>
      </div>

      {/* 关键：强制左对齐 + 分类型渲染 */}
      <div className="overflow-x-auto p-4 !text-left font-mono text-xs leading-relaxed text-gray-700">
        {isAction ? (
          <div className="space-y-2">
            <div className="flex items-center gap-2">
              <span className="rounded bg-indigo-600 px-1.5 py-0.5 text-[10px] font-bold text-white uppercase">
                Tool
              </span>
              <span className="font-bold text-indigo-700">{step.tool}</span>
            </div>

            {/* toolInput 也统一走 ContentRenderer：支持 dict/list/string */}
            <ContentRenderer content={step.toolInput} />
          </div>
        ) : (
          <ContentRenderer content={step.content} />
        )}
      </div>
    </div>
  );
};
