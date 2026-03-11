import React, {
  useState,
  useRef,
  useEffect,
  useCallback,
  useMemo,
} from "react";
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
  Wrench,
  Server,
  X,
  RefreshCw,
  Maximize2,
  Minimize2,
} from "lucide-react";
import {
  type AgentStep,
  type ChatMessage,
  type RegistrySummary,
} from "../types";

import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { useLayoutEffect } from "react";

function normalizeText(s: string) {
  return s.replace(/\\n/g, "\n").replace(/\\t/g, "\t");
}

function joinClasses(...values: Array<string | undefined | false | null>) {
  return values.filter(Boolean).join(" ");
}

const MessageContent: React.FC<{ content: string }> = ({ content }) => {
  return (
    <div className="sf-markdown max-w-none text-[14px] leading-7">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          h1: ({ className, ...props }) => (
            <h1
              {...props}
              className={joinClasses("sf-markdown-heading sf-markdown-h1", className)}
            />
          ),
          h2: ({ className, ...props }) => (
            <h2
              {...props}
              className={joinClasses("sf-markdown-heading sf-markdown-h2", className)}
            />
          ),
          h3: ({ className, ...props }) => (
            <h3
              {...props}
              className={joinClasses("sf-markdown-heading sf-markdown-h3", className)}
            />
          ),
          h4: ({ className, ...props }) => (
            <h4
              {...props}
              className={joinClasses("sf-markdown-heading sf-markdown-h4", className)}
            />
          ),
          p: ({ className, ...props }) => (
            <p {...props} className={joinClasses("sf-markdown-paragraph", className)} />
          ),
          blockquote: ({ className, ...props }) => (
            <blockquote
              {...props}
              className={joinClasses("sf-markdown-blockquote", className)}
            />
          ),
          hr: ({ className, ...props }) => (
            <hr {...props} className={joinClasses("sf-markdown-divider", className)} />
          ),
          a: ({ className, ...props }) => (
            <a
              {...props}
              className={joinClasses("sf-markdown-link", className)}
              target="_blank"
              rel="noreferrer"
            />
          ),
          pre: ({ className, ...props }) => (
            <pre {...props} className={joinClasses("sf-markdown-pre", className)} />
          ),
          code: ({ inline, className, ...props }: any) =>
            inline ? (
              <code
                {...props}
                className={joinClasses("sf-markdown-inline-code", className)}
              />
            ) : (
              <code
                {...props}
                className={joinClasses("sf-markdown-code-block", className)}
              />
            ),
          table: ({ className, ...props }) => (
            <div className="sf-markdown-table-wrap">
              <table
                {...props}
                className={joinClasses("sf-markdown-table", className)}
              />
            </div>
          ),
          thead: ({ className, ...props }) => (
            <thead
              {...props}
              className={joinClasses("sf-markdown-table-head", className)}
            />
          ),
          tbody: ({ className, ...props }) => (
            <tbody
              {...props}
              className={joinClasses("sf-markdown-table-body", className)}
            />
          ),
          tr: ({ className, ...props }) => (
            <tr {...props} className={joinClasses("sf-markdown-table-row", className)} />
          ),
          th: ({ className, ...props }) => (
            <th
              {...props}
              className={joinClasses("sf-markdown-table-cell sf-markdown-table-th", className)}
            />
          ),
          td: ({ className, ...props }) => (
            <td
              {...props}
              className={joinClasses("sf-markdown-table-cell sf-markdown-table-td", className)}
            />
          ),
          ul: ({ className, ...props }) => (
            <ul {...props} className={joinClasses("sf-markdown-list", className)} />
          ),
          ol: ({ className, ...props }) => (
            <ol {...props} className={joinClasses("sf-markdown-list", className)} />
          ),
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
  msgId?: string;
};

type SandboxListItem = {
  sandbox_id: string;
  status?: string;
  urls?: {
    novnc?: string | null;
  };
};

type SandboxListResponse = {
  count: number;
  items: SandboxListItem[];
};

function getConfirmCommand(pending: PendingConfirm | null): string | null {
  if (!pending) return null;
  const args = pending.args as { command?: unknown } | undefined;
  if (pending.toolName === "exec" && typeof args?.command === "string") {
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

function truncateWithEllipsis(text: string, maxChars = 48): string {
  const chars = Array.from(text);
  if (chars.length <= maxChars) return text;
  return `${chars.slice(0, maxChars).join("")}...`;
}

type BrowserViewportRect = {
  top: number;
  left: number;
  width: number;
  height: number;
};

const BROWSER_VIEWPORT_TRANSITION_MS = 320;
const BROWSER_VIEWPORT_TRANSITION_EASING = "cubic-bezier(0.22, 1, 0.36, 1)";

export const AgentWorkbench: React.FC = () => {
  const apiBase = useMemo(() => {
    const configured = import.meta.env.VITE_API_BASE as string | undefined;
    const base = configured?.trim()
      ? configured.trim()
      : "http://localhost:8000";
    return base.endsWith("/") ? base.slice(0, -1) : base;
  }, []);

  const wsBase = useMemo(() => {
    try {
      const parsed = new URL(apiBase);
      const protocol = parsed.protocol === "https:" ? "wss:" : "ws:";
      return `${protocol}//${parsed.host}`;
    } catch {
      return "ws://localhost:8000";
    }
  }, [apiBase]);

  const [goal, setGoal] = useState("");
  const [chatHistory, setChatHistory] = useState<ChatMessage[]>([]);
  const [isRunning, setIsRunning] = useState(false);
  const [isStopping, setIsStopping] = useState(false);
  const [steps, setSteps] = useState<AgentStep[]>([]);
  const [activeTab, setActiveTab] = useState<"runner" | "browser">("runner");
  const [isBrowserExpanded, setIsBrowserExpanded] = useState(false);
  const [isBrowserAnimating, setIsBrowserAnimating] = useState(false);
  const [isBrowserVisualExpanded, setIsBrowserVisualExpanded] = useState(false);
  const [browserViewportRect, setBrowserViewportRect] =
    useState<BrowserViewportRect | null>(null);
  const [novncUrl, setNovncUrl] = useState<string | null>(null);
  const [tokenInfo, setTokenInfo] = useState<{
    cache_tokens: number;
    prompt_tokens: number;
    completion_tokens: number;
    total_tokens: number;
  } | null>(null);
  const [wsStatus, setWsStatus] = useState<
    "online" | "offline" | "reconnecting"
  >("offline");
  const [pendingConfirm, setPendingConfirm] = useState<PendingConfirm | null>(
    null,
  );
  const [registryOpen, setRegistryOpen] = useState(false);
  const [registryLoading, setRegistryLoading] = useState(false);
  const [registryError, setRegistryError] = useState<string | null>(null);
  const [registrySummary, setRegistrySummary] =
    useState<RegistrySummary | null>(null);
  const [isResettingSession, setIsResettingSession] = useState(false);

  // --- 新增：WebSocket 相关状态 ---
  const [clientId] = useState(() => uuidv4());
  const [traceId, setTraceId] = useState<string | null>(null);
  const socketRef = useRef<WebSocket | null>(null);
  const chatScrollRef = useRef<HTMLDivElement>(null);
  const traceScrollRef = useRef<HTMLDivElement>(null);
  const workbenchBodyRef = useRef<HTMLDivElement>(null);
  const browserDockRef = useRef<HTMLDivElement>(null);
  const browserTransitionFrameRef = useRef<number | null>(null);
  const browserTransitionTimerRef = useRef<number | null>(null);
  const normalizedNoVncUrl = useMemo(() => {
    if (!novncUrl) return null;
    const candidate = novncUrl.trim();
    if (!/^https?:\/\//i.test(candidate)) return null;
    return candidate;
  }, [novncUrl]);

  const refreshNoVncUrl = useCallback(async () => {
    try {
      const response = await fetch(`${apiBase}/sandboxes?include_exited=false`);
      if (!response.ok) return;
      const payload = (await response.json()) as SandboxListResponse;
      const items = Array.isArray(payload?.items) ? payload.items : [];
      const running =
        items.find((item) => item.status === "running") || items[0];
      const next = running?.urls?.novnc?.trim() || null;
      setNovncUrl(next);
    } catch {
      // ignore transient fetch errors for browser panel URL refresh.
    }
  }, [apiBase]);

  const clearBrowserTransition = useCallback(() => {
    if (browserTransitionFrameRef.current !== null) {
      window.cancelAnimationFrame(browserTransitionFrameRef.current);
      browserTransitionFrameRef.current = null;
    }
    if (browserTransitionTimerRef.current !== null) {
      window.clearTimeout(browserTransitionTimerRef.current);
      browserTransitionTimerRef.current = null;
    }
  }, []);

  const measureBrowserViewportRect = useCallback(
    (expanded: boolean): BrowserViewportRect | null => {
      const workbenchBody = workbenchBodyRef.current;
      if (!workbenchBody) return null;

      if (expanded) {
        return {
          top: 0,
          left: 0,
          width: workbenchBody.clientWidth,
          height: workbenchBody.clientHeight,
        };
      }

      const browserDock = browserDockRef.current;
      if (!browserDock) return null;

      const bodyRect = workbenchBody.getBoundingClientRect();
      const dockRect = browserDock.getBoundingClientRect();
      return {
        top: dockRect.top - bodyRect.top,
        left: dockRect.left - bodyRect.left,
        width: dockRect.width,
        height: dockRect.height,
      };
    },
    [],
  );

  const finishBrowserTransition = useCallback(
    (expanded: boolean) => {
      clearBrowserTransition();
      setIsBrowserAnimating(false);
      const finalRect = measureBrowserViewportRect(expanded);
      if (finalRect) {
        setBrowserViewportRect(finalRect);
      }
      if (!expanded) {
        setIsBrowserVisualExpanded(false);
      }
    },
    [clearBrowserTransition, measureBrowserViewportRect],
  );

  const animateBrowserViewport = useCallback(
    (expand: boolean) => {
      if (!normalizedNoVncUrl) return;

      const targetRect = measureBrowserViewportRect(expand);
      const originRect =
        browserViewportRect ?? measureBrowserViewportRect(!expand);
      if (!targetRect || !originRect) {
        clearBrowserTransition();
        setIsBrowserAnimating(false);
        setIsBrowserExpanded(expand);
        setIsBrowserVisualExpanded(expand);
        setBrowserViewportRect(targetRect ?? originRect ?? null);
        return;
      }

      clearBrowserTransition();
      if (expand) {
        setActiveTab("browser");
        setIsBrowserVisualExpanded(true);
      }

      setIsBrowserAnimating(true);
      setBrowserViewportRect(originRect);
      setIsBrowserExpanded(expand);

      browserTransitionFrameRef.current = window.requestAnimationFrame(() => {
        browserTransitionFrameRef.current = window.requestAnimationFrame(() => {
          setBrowserViewportRect(targetRect);
        });
      });

      browserTransitionTimerRef.current = window.setTimeout(() => {
        finishBrowserTransition(expand);
      }, BROWSER_VIEWPORT_TRANSITION_MS);
    },
    [
      browserViewportRect,
      clearBrowserTransition,
      finishBrowserTransition,
      measureBrowserViewportRect,
      normalizedNoVncUrl,
    ],
  );

  // --- 新增：初始化 WebSocket ---
  const handleIncomingEvent = useCallback(
    (event: any) => {
      const { event_type, data, timestamp, msg_id } = event;
      // console.log('incoming event', event);

      if (event_type === "token_info") {
        setTokenInfo({
          cache_tokens: Number(data?.cache_tokens ?? 0),
          prompt_tokens: Number(data?.prompt_tokens ?? 0),
          completion_tokens: Number(data?.completion_tokens ?? 0),
          total_tokens: Number(data?.total_tokens ?? 0),
        });
        return;
      }

      // 1. 处理执行日志 (Execution Trace)
      // 同一步骤可能收到多次状态快照，前端需要按 step 维度更新而不是盲目追加。
      if (["thought", "action", "observation", "final"].includes(event_type)) {
        // console.log('receive execute trace', event);
        const MAX_LEN = 500;
        const actionBatch =
          event_type === "action" && Array.isArray(data?.actions)
            ? data.actions
            : undefined;
        const observationBatch =
          event_type === "observation" && Array.isArray(data?.observations)
            ? data.observations
            : undefined;
        const rawContent =
          data?.content ??
          (actionBatch
            ? `Action batch (${actionBatch.length})`
            : observationBatch
              ? `Observation batch (${observationBatch.length})`
              : "");
        const content =
          rawContent.length > MAX_LEN
            ? rawContent.slice(0, MAX_LEN) + "..."
            : rawContent;
        const stepMsgId = typeof msg_id === "string" ? msg_id : undefined;
        const newStep: AgentStep = {
          stepId: stepMsgId,
          msgId: stepMsgId,
          type: event_type,
          content: content,
          tool: data.tool_name,
          toolInput: data.args,
          actions: actionBatch,
          observations: observationBatch,
          timestamp: new Date(timestamp).getTime(),
        };
        setSteps((prev) => {
          if (!stepMsgId) {
            return [...prev, newStep];
          }
          const existingIndex = prev.findIndex(
            (item) => item.type === event_type && item.msgId === stepMsgId,
          );
          if (existingIndex < 0) {
            return [...prev, newStep];
          }
          const next = [...prev];
          next[existingIndex] = {
            ...next[existingIndex],
            ...newStep,
          };
          return next;
        });

        if (event_type === "observation") {
          const obsHasBrowserAction =
            typeof data?.tool_name === "string"
              ? data.tool_name.startsWith("browser_")
              : false;
          const batchHasBrowserAction =
            Array.isArray(observationBatch) &&
            observationBatch.some(
              (item: any) =>
                typeof item?.tool_name === "string" &&
                item.tool_name.startsWith("browser_"),
            );
          if (obsHasBrowserAction || batchHasBrowserAction) {
            setActiveTab("browser");
            void refreshNoVncUrl();
          }
        }

        // 如果是 action 且涉及到浏览器（根据你的业务逻辑），可以切换 Tab
        if (
          event_type === "action" &&
          ((Array.isArray(actionBatch) &&
            actionBatch.some(
              (a: any) =>
                typeof a?.tool_name === "string" &&
                a.tool_name.startsWith("browser_"),
            )) ||
            (typeof data?.tool_name === "string" &&
              data.tool_name.startsWith("browser_")))
        ) {
          setActiveTab("browser");
          void refreshNoVncUrl();
        }
      }

      // --- 2. 处理流式消息 (Answer & HITL Request) ---
      if (event_type === "answer" || event_type === "hitl_request") {
        // console.log('receive answer or hitl_request');
        setIsRunning((prev) => (prev ? false : prev));
        setIsStopping(false);
        setChatHistory((prev) => {
          const isHitlRequest = event_type === "hitl_request";
          const lastIndex = prev.length - 1;
          const lastMessage = lastIndex >= 0 ? prev[lastIndex] : undefined;
          const canAppendToLast =
            !!msg_id &&
            !!lastMessage &&
            lastMessage.role === "assistant" &&
            lastMessage.msg_id === msg_id &&
            ((isHitlRequest && lastMessage.hitlType === "request") ||
              (!isHitlRequest && !lastMessage.isHitl));

          // 仅当连续分片到达且目标是最后一条 assistant 消息时才拼接
          if (canAppendToLast && lastMessage) {
            const newHistory = [...prev];
            const targetMsg = newHistory[lastIndex];
            newHistory[lastIndex] = {
              ...targetMsg,
              content: targetMsg.content + (data.content || ""),
              // 如果是 hitl_request，确保标记状态
              isHitl: event_type.startsWith("hitl") ? true : targetMsg.isHitl,
              hitlType:
                event_type === "hitl_request" ? "request" : targetMsg.hitlType,
            };
            return newHistory;
          } else {
            // 如果是该 msg_id 的第一块数据，创建新消息
            return [
              ...prev,
              {
                id: uuidv4(),
                role: "assistant",
                content: data.content || "",
                timestamp: new Date(timestamp).getTime(),
                msg_id: msg_id,
                isHitl: event_type.startsWith("hitl"),
                hitlType: event_type === "hitl_request" ? "request" : undefined,
              },
            ];
          }
        });
      }

      // 3. 处理 Final 收尾消息（尤其是 code-first barrier handoff 场景）
      if (event_type === "final") {
        setIsRunning(false);
        setIsStopping(false);
        const finalText = typeof data?.content === "string" ? data.content : "";
        if (finalText.trim()) {
          setChatHistory((prev) => {
            const last = prev.length > 0 ? prev[prev.length - 1] : undefined;
            // Avoid duplicate bubble when answer already rendered the same text.
            if (
              last &&
              last.role === "assistant" &&
              String(last.content ?? "").trim() === finalText.trim()
            ) {
              return prev;
            }
            return [
              ...prev,
              {
                id: uuidv4(),
                role: "assistant",
                content: finalText,
                timestamp: new Date(timestamp).getTime(),
                msg_id: msg_id,
              },
            ];
          });
        }
        return;
      }

      if (event_type === "hitl_confirm") {
        setIsRunning(false);
        setIsStopping(false);
        const promptText = data?.prompt || "Please confirm the action.";
        const requestId = data?.request_id || uuidv4();
        setPendingConfirm({
          requestId,
          prompt: promptText,
          toolName: data?.tool_name,
          args: data?.args,
          msgId: msg_id,
        });
        setChatHistory((prev) => [
          ...prev,
          {
            id: uuidv4(),
            role: "assistant",
            content: promptText,
            timestamp: new Date(timestamp).getTime(),
            msg_id: msg_id,
            requestId,
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
          // 如果是 hitl_confirm，可在这里扩展 type 以便后续展示按钮
          isHitl: event_type.startsWith("hitl"),
        };
        setChatHistory((prev) => [...prev, newMessage]);
        setIsRunning(false);
        setIsStopping(false);
      }

      // 4. 统一结束标记（后端可能在不同路径发送 end）
      if (event_type === "end") {
        setIsRunning(false);
        setIsStopping(false);
        return;
      }
    },
    [refreshNoVncUrl],
  );

  const fetchRegistrySummary = useCallback(async () => {
    setRegistryLoading(true);
    setRegistryError(null);
    try {
      const response = await fetch(`${apiBase}/agent/registry-summary`);
      if (!response.ok)
        throw new Error("Failed to fetch tool registry summary");
      const data = (await response.json()) as RegistrySummary;
      setRegistrySummary(data);
    } catch (error) {
      console.error(error);
      setRegistryError("工具清单加载失败");
    } finally {
      setRegistryLoading(false);
    }
  }, [apiBase]);

  useEffect(() => {
    void fetchRegistrySummary();
  }, [fetchRegistrySummary]);

  useEffect(() => {
    let closedByUser = false;
    let retryCount = 0;
    let retryTimer: number | undefined;

    const connect = () => {
      const socket = new WebSocket(`${wsBase}/ws/${clientId}`);
      socketRef.current = socket;

      socket.onopen = () => {
        retryCount = 0;
        setWsStatus("online");
        console.log("Connected to WS:", clientId);
      };

      socket.onmessage = (event) => {
        const serverEvent = JSON.parse(event.data);
        handleIncomingEvent(serverEvent);
      };

      socket.onerror = () => {
        // Error 通常会紧跟 close，这里主动关闭一次确保状态一致
        setWsStatus("offline");
        try {
          socket.close();
        } catch {
          // ignore
        }
      };

      socket.onclose = () => {
        console.log("WS Disconnected");
        setWsStatus("offline");
        if (closedByUser) return;
        const delay = Math.min(1000 * 2 ** retryCount, 10000);
        retryCount += 1;
        setWsStatus("reconnecting");
        retryTimer = window.setTimeout(connect, delay);
        console.log(`[ws] reconnect in ${delay}ms`);
      };
    };

    connect();

    return () => {
      closedByUser = true;
      setWsStatus("offline");
      if (retryTimer) {
        clearTimeout(retryTimer);
      }
      if (socketRef.current && socketRef.current.readyState <= 1) {
        socketRef.current.close();
      }
    };
  }, [clientId, handleIncomingEvent, wsBase]);

  useEffect(() => {
    if (activeTab === "browser") {
      void refreshNoVncUrl();
    }
  }, [activeTab, refreshNoVncUrl]);

  useEffect(() => {
    if (!isBrowserVisualExpanded) return;
    const handleEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        animateBrowserViewport(false);
      }
    };
    window.addEventListener("keydown", handleEscape);
    return () => {
      window.removeEventListener("keydown", handleEscape);
    };
  }, [animateBrowserViewport, isBrowserVisualExpanded]);

  useLayoutEffect(() => {
    if (!normalizedNoVncUrl || isBrowserAnimating) return;
    const syncBrowserViewport = () => {
      const nextRect = measureBrowserViewportRect(isBrowserExpanded);
      if (nextRect) {
        setBrowserViewportRect(nextRect);
      }
    };
    syncBrowserViewport();
    window.addEventListener("resize", syncBrowserViewport);
    return () => {
      window.removeEventListener("resize", syncBrowserViewport);
    };
  }, [
    isBrowserAnimating,
    isBrowserExpanded,
    measureBrowserViewportRect,
    normalizedNoVncUrl,
  ]);

  useEffect(() => {
    if (normalizedNoVncUrl) return;
    clearBrowserTransition();
    setIsBrowserAnimating(false);
    setIsBrowserExpanded(false);
    setIsBrowserVisualExpanded(false);
    setBrowserViewportRect(null);
  }, [clearBrowserTransition, normalizedNoVncUrl]);

  useEffect(() => {
    return () => {
      clearBrowserTransition();
    };
  }, [clearBrowserTransition]);

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
    if (!traceId || isRunning || isStopping) return;

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
      const response = await fetch(`${apiBase}/agent/run`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          client_id: clientId,
          task: decision,
          ...(traceId && { trace_id: traceId }),
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
    if (
      !goal.trim() ||
      isRunning ||
      isStopping ||
      isResettingSession ||
      pendingConfirm
    )
      return;

    // 1. UI 反馈
    const userMessage: ChatMessage = {
      id: uuidv4(),
      role: "user",
      content: goal,
      timestamp: Date.now(),
    };

    setChatHistory((prev) => [...prev, userMessage]);
    setSteps([]); // 清空旧日志
    if (!traceId) setTokenInfo(null);
    setIsRunning(true);
    const currentGoal = goal;
    setGoal("");

    // 2. 调用后端 API 触发任务
    try {
      const response = await fetch(`${apiBase}/agent/run`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          client_id: clientId,
          task: currentGoal,
          // --- 新增：如果已经有 trace_id，则携带它 ---
          ...(traceId && { trace_id: traceId }),
        }),
      });

      if (!response.ok) throw new Error("Failed to start agent");

      const data = await response.json();
      if (data.trace_id) {
        setTraceId(data.trace_id);
        console.log("Current Agent ID session:", data.trace_id);
      }
    } catch (e) {
      console.error(e);
      setChatHistory((prev) => [
        ...prev,
        {
          id: uuidv4(),
          role: "assistant",
          content: "连接服务失败，请稍后再试。",
          timestamp: Date.now(),
        },
      ]);
      setIsRunning(false);
    }
  };

  const handleStop = async () => {
    if (!traceId || !isRunning || pendingConfirm || isStopping) return;

    setIsStopping(true);
    try {
      const response = await fetch(`${apiBase}/agent/stop`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ trace_id: traceId }),
      });

      if (!response.ok) throw new Error("Failed to stop agent");

      setIsRunning(false);
    } catch (e) {
      console.error(e);
      setChatHistory((prev) => [
        ...prev,
        {
          id: uuidv4(),
          role: "assistant",
          content: "停止当前运行失败，请稍后重试。",
          timestamp: Date.now(),
        },
      ]);
    } finally {
      setIsStopping(false);
    }
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleRun();
    }
  };

  const handleNewSession = async () => {
    if (isRunning || isStopping || pendingConfirm || isResettingSession) return;

    setIsResettingSession(true);
    try {
      const response = await fetch(`${apiBase}/sandboxes/browser/reset`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({}),
      });
      if (!response.ok) {
        throw new Error("Failed to reset sandbox browser");
      }
    } catch (error) {
      console.error(error);
      setChatHistory((prev) => [
        ...prev,
        {
          id: uuidv4(),
          role: "assistant",
          content: "浏览器重置失败，当前会话未清空，请稍后重试。",
          timestamp: Date.now(),
        },
      ]);
      setIsResettingSession(false);
      return;
    }

    clearBrowserTransition();
    setChatHistory([]);
    setSteps([]);
    setPendingConfirm(null);
    setIsRunning(false);
    setIsStopping(false);
    setIsBrowserExpanded(false);
    setIsBrowserAnimating(false);
    setIsBrowserVisualExpanded(false);
    setBrowserViewportRect(null);
    setGoal("");
    setTokenInfo(null);
    setTraceId(null);
    setNovncUrl(null);
    setActiveTab("runner");
    setIsResettingSession(false);
  };

  const openBrowserExpanded = useCallback(() => {
    if (!normalizedNoVncUrl) return;
    animateBrowserViewport(true);
  }, [animateBrowserViewport, normalizedNoVncUrl]);

  const closeBrowserExpanded = useCallback(() => {
    if (!isBrowserVisualExpanded && !isBrowserAnimating) return;
    animateBrowserViewport(false);
  }, [animateBrowserViewport, isBrowserAnimating, isBrowserVisualExpanded]);

  const builtInCount = registrySummary?.counts.built_in_tools ?? 0;
  const mcpServerCount = registrySummary?.counts.mcp_servers ?? 0;
  const mcpToolCount = registrySummary?.counts.mcp_tools ?? 0;
  const showStopButton = isRunning && !pendingConfirm;
  const isAwaitingTraceId = showStopButton && !traceId;
  const sendDisabled =
    isRunning ||
    isStopping ||
    isResettingSession ||
    Boolean(pendingConfirm) ||
    !goal.trim();
  const stopDisabled = isStopping || isResettingSession || isAwaitingTraceId;
  const canExpandBrowser = Boolean(normalizedNoVncUrl);
  const shouldRenderBrowserViewport = Boolean(
    normalizedNoVncUrl && browserViewportRect,
  );
  const wsStatusLabel =
    wsStatus === "online"
      ? "System Online"
      : wsStatus === "reconnecting"
        ? "Offline · Reconnecting"
        : "System Offline";
  const wsStatusChipClass =
    wsStatus === "online"
      ? "sf-chip sf-chip-green"
      : wsStatus === "reconnecting"
        ? "sf-chip sf-chip-amber"
        : "sf-chip sf-chip-red";
  const isBrowserViewportVisible =
    activeTab === "browser" || isBrowserVisualExpanded || isBrowserAnimating;
  const browserViewportStyle: React.CSSProperties | undefined =
    browserViewportRect
      ? {
          top: `${browserViewportRect.top}px`,
          left: `${browserViewportRect.left}px`,
          width: `${browserViewportRect.width}px`,
          height: `${browserViewportRect.height}px`,
          opacity: isBrowserViewportVisible ? 1 : 0,
          visibility: isBrowserViewportVisible ? "visible" : "hidden",
          pointerEvents: isBrowserViewportVisible ? "auto" : "none",
          transition: [
            `top ${BROWSER_VIEWPORT_TRANSITION_MS}ms ${BROWSER_VIEWPORT_TRANSITION_EASING}`,
            `left ${BROWSER_VIEWPORT_TRANSITION_MS}ms ${BROWSER_VIEWPORT_TRANSITION_EASING}`,
            `width ${BROWSER_VIEWPORT_TRANSITION_MS}ms ${BROWSER_VIEWPORT_TRANSITION_EASING}`,
            `height ${BROWSER_VIEWPORT_TRANSITION_MS}ms ${BROWSER_VIEWPORT_TRANSITION_EASING}`,
            "opacity 180ms ease",
            `box-shadow ${BROWSER_VIEWPORT_TRANSITION_MS}ms ${BROWSER_VIEWPORT_TRANSITION_EASING}`,
            "background-color 220ms ease",
          ].join(", "),
          boxShadow: isBrowserExpanded
            ? "0 24px 80px rgba(15, 23, 42, 0.34)"
            : "0 8px 28px rgba(15, 23, 42, 0.12)",
        }
      : undefined;

  return (
    <div className="sf-panel relative flex h-full flex-col overflow-hidden">
      {/* Tab Header */}
      <div className="sf-panel-header flex shrink-0 items-center justify-between px-6 py-5">
        <div className="flex items-center gap-3">
          <div className="sf-icon-badge sf-icon-badge-accent h-11 w-11">
            <Cpu className="h-5 w-5" />
          </div>
          <div>
            <h2 className="text-base font-extrabold tracking-tight text-[var(--sf-ink)]">
              Agent Workspace
            </h2>
            <div className="mt-2 flex items-center gap-2">
              <span className={wsStatusChipClass}>
                <span
                  className={`h-1.5 w-1.5 rounded-full ${
                    wsStatus === "online"
                      ? "bg-[var(--sf-success)]"
                      : wsStatus === "reconnecting"
                        ? "bg-[var(--sf-warning)]"
                        : "bg-[var(--sf-danger)]"
                  }`}
                />
                {wsStatusLabel}
              </span>
            </div>
          </div>
        </div>

        <div className="sf-tab-group">
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

      <div
        ref={workbenchBodyRef}
        className="relative flex flex-1 divide-x divide-[var(--sf-border)] overflow-hidden"
      >
        {shouldRenderBrowserViewport && (
          <>
            <div
              className={`absolute inset-0 z-20 transition-opacity ${
                isBrowserExpanded
                  ? "pointer-events-auto opacity-100"
                  : "pointer-events-none opacity-0"
              }`}
              style={{
                transitionDuration: `${BROWSER_VIEWPORT_TRANSITION_MS}ms`,
              }}
            >
              <button
                onClick={closeBrowserExpanded}
                className="h-full w-full bg-[rgba(15,23,42,0.22)] backdrop-blur-[1px]"
                aria-label="关闭 VNC 全屏遮罩"
                tabIndex={isBrowserExpanded ? 0 : -1}
              />
            </div>

            <div
              className="absolute z-30 overflow-hidden rounded-[26px] bg-[rgba(255,255,255,0.82)]"
              style={browserViewportStyle}
            >
              <div
                className={`relative h-full w-full overflow-hidden transition-colors duration-300 ${
                  isBrowserVisualExpanded
                    ? "bg-[var(--sf-panel-dark)]"
                    : "bg-[rgba(255,255,255,0.92)]"
                }`}
              >
                <iframe
                  src={normalizedNoVncUrl ?? undefined}
                  title="VNC Browser View"
                  className="h-full w-full border-0 bg-white"
                  allow="clipboard-read; clipboard-write; fullscreen"
                  referrerPolicy="no-referrer"
                />
                <div
                  className={`pointer-events-none absolute inset-x-0 top-0 z-10 flex items-start justify-between gap-3 transition-all duration-300 ${
                    isBrowserVisualExpanded
                      ? "border-b border-white/10 bg-[rgba(15,23,42,0.84)] px-5 py-3 backdrop-blur-md"
                      : "px-4 py-4"
                  }`}
                >
                  <div
                    className={`pointer-events-auto flex items-center gap-2 transition-all duration-300 ${
                      isBrowserVisualExpanded
                        ? "text-sm font-bold tracking-wide text-slate-100"
                        : "rounded-full border border-white/70 bg-white/[0.88] px-3 py-1.5 text-[10px] font-black tracking-[0.18em] text-[var(--sf-ink-soft)] uppercase shadow-[0_14px_32px_rgba(15,23,42,0.08)] backdrop-blur"
                    }`}
                  >
                    <Globe
                      className={`h-4 w-4 ${
                        isBrowserVisualExpanded
                          ? "text-blue-300"
                          : "text-[var(--sf-ink-soft)]"
                      }`}
                    />
                    <span>
                      {isBrowserVisualExpanded
                        ? "VNC Browser View"
                        : "Live VNC"}
                    </span>
                  </div>
                  <div className="pointer-events-auto flex items-center gap-2">
                    <button
                      onClick={() => void refreshNoVncUrl()}
                      className={`sf-btn cursor-pointer px-3 py-2 text-[10px] ${
                        isBrowserVisualExpanded
                          ? "border border-white/10 bg-white/[0.08] text-slate-100 hover:border-white/20 hover:bg-white/[0.12]"
                          : "sf-btn-secondary text-[var(--sf-ink-soft)]"
                      }`}
                      title="刷新 VNC"
                    >
                      <RefreshCw className="h-3.5 w-3.5" />
                      刷新
                    </button>
                    <button
                      onClick={
                        isBrowserVisualExpanded
                          ? closeBrowserExpanded
                          : openBrowserExpanded
                      }
                      className={`sf-btn cursor-pointer px-3 py-2 text-[10px] ${
                        isBrowserVisualExpanded
                          ? "border border-blue-300/20 bg-blue-400/10 text-blue-100 hover:border-blue-300/30 hover:bg-blue-400/15"
                          : "sf-btn-primary"
                      }`}
                      title={
                        isBrowserVisualExpanded
                          ? "退出全屏"
                          : "在工作台内全屏显示 VNC"
                      }
                    >
                      {isBrowserVisualExpanded ? (
                        <Minimize2 className="h-3.5 w-3.5" />
                      ) : (
                        <Maximize2 className="h-3.5 w-3.5" />
                      )}
                      {isBrowserVisualExpanded ? "退出全屏" : "全屏"}
                    </button>
                  </div>
                </div>
              </div>
            </div>
          </>
        )}

        {/* Left Column: Chat History & Input */}
        <div className="flex w-1/2 min-w-0 flex-col bg-transparent">
          <div className="flex items-center justify-between border-b border-[var(--sf-border)] px-6 py-4">
            <div>
              <span className="text-[10px] font-bold tracking-[0.18em] text-[var(--sf-ink-muted)] uppercase">
                Agent Session
              </span>
            </div>
            <button
              onClick={handleNewSession}
              disabled={
                isRunning ||
                isStopping ||
                Boolean(pendingConfirm) ||
                isResettingSession
              }
              className="sf-btn sf-btn-secondary px-3 py-2 text-[10px]"
            >
              {isResettingSession ? "Resetting..." : "New Session"}
            </button>
          </div>
          {/* Chat Messages */}
          <div
            ref={chatScrollRef}
            className="flex-1 space-y-6 overflow-y-auto overflow-x-hidden bg-[rgba(247,249,252,0.42)] p-6"
          >
            {chatHistory.length === 0 && (
              <div className="sf-empty-state flex h-full flex-col items-center justify-center px-10 text-center">
                <div className="sf-icon-badge sf-icon-badge-dark mb-6 flex h-20 w-20 items-center justify-center rounded-[28px]">
                  <Bot className="h-10 w-10 text-blue-200" />
                </div>
                <h3 className="mb-2 text-xl font-extrabold tracking-tight text-[var(--sf-ink)]">
                  Hello, I'm your personal AI Steward
                </h3>
                <p className="max-w-md text-sm leading-7 text-[var(--sf-ink-muted)]">
                  Web page automation, retrieval, program execution, and manual confirmation are completed in a unified workbench, making the execution process more visible and controllable.
                </p>
              </div>
            )}
            {chatHistory.map((msg) => (
              <div
                key={msg.id}
                className={`flex min-w-0 ${msg.role === "user" ? "justify-end" : "justify-start"}`}
              >
                <div
                  className={`flex min-w-0 gap-3 ${
                    msg.role === "user"
                      ? "max-w-[85%] flex-row-reverse"
                      : "w-full max-w-[48rem] flex-row"
                  }`}
                >
                  <div
                    className={`flex h-8 w-8 shrink-0 items-center justify-center rounded-2xl shadow-sm ${
                      msg.role === "user"
                        ? "sf-icon-badge sf-icon-badge-accent text-white"
                        : "border border-[var(--sf-border)] bg-white/[0.88] text-[var(--sf-ink-soft)] shadow-[0_10px_24px_rgba(15,23,42,0.06)]"
                    }`}
                  >
                    {msg.role === "user" ? (
                      <User className="h-4 w-4" />
                    ) : (
                      <Bot className="h-4 w-4" />
                    )}
                  </div>
                  <div
                    className={`min-w-0 overflow-hidden rounded-[24px] p-4 text-left text-sm leading-relaxed break-words shadow-[0_14px_28px_rgba(15,23,42,0.05)] ${
                      msg.role === "user"
                        ? "rounded-tr-none bg-[linear-gradient(180deg,var(--sf-accent),var(--sf-accent-strong))] text-white"
                        : "flex-1 rounded-tl-none border border-[var(--sf-border)] bg-white/[0.88] text-[var(--sf-ink)]"
                    }`}
                  >
                    <MessageContent content={String(msg.content ?? "")} />
                    {msg.role === "assistant" &&
                      msg.hitlType === "confirm" &&
                      pendingConfirm &&
                      msg.requestId &&
                      msg.requestId === pendingConfirm.requestId && (
                        <div className="mt-3 rounded-[18px] border border-[rgba(49,94,251,0.16)] bg-[rgba(232,239,255,0.72)] p-3 shadow-inner">
                          <div className="flex items-center gap-2 text-[10px] font-black tracking-[0.16em] text-[var(--sf-accent-strong)] uppercase">
                            <span className="h-1.5 w-1.5 rounded-full bg-[var(--sf-accent)]"></span>
                            Tool Call Confirmation
                          </div>
                          {getConfirmCommand(pendingConfirm) && (
                            <div className="sf-terminal-panel mt-2 rounded-[16px] px-3 py-2 font-mono text-[11px] text-slate-100">
                              $ {getConfirmCommand(pendingConfirm)}
                            </div>
                          )}
                          <div className="mt-3 flex gap-2">
                            <button
                              onClick={() => handleConfirm("confirm")}
                              className="sf-btn sf-btn-primary px-3 py-2 text-[10px]"
                            >
                              Approve
                            </button>
                            <button
                              onClick={() => handleConfirm("reject")}
                              className="sf-btn border border-[rgba(207,63,83,0.14)] bg-[var(--sf-danger-soft)] px-3 py-2 text-[10px] text-[var(--sf-danger)]"
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
                  <div className="flex h-8 w-8 items-center justify-center rounded-2xl border border-[var(--sf-border)] bg-white/[0.88] text-[var(--sf-ink-soft)] shadow-[0_10px_24px_rgba(15,23,42,0.06)]">
                    <Bot className="h-4 w-4" />
                  </div>
                  <div className="flex items-center gap-2 rounded-[24px] rounded-tl-none border border-[var(--sf-border)] bg-white/[0.88] p-4 shadow-[0_14px_28px_rgba(15,23,42,0.05)]">
                    <div className="flex gap-1">
                      <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-blue-300"></span>
                      <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-blue-400 [animation-delay:0.2s]"></span>
                      <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-blue-500 [animation-delay:0.4s]"></span>
                    </div>
                    <span className="ml-2 text-xs font-bold tracking-[0.16em] text-[var(--sf-ink-muted)] uppercase">
                      Agent is thinking...
                    </span>
                  </div>
                </div>
              </div>
            )}
          </div>

          {/* Chat Input */}
          <div className="border-t border-[var(--sf-border)] bg-[rgba(255,255,255,0.38)] p-6">
            <div className="relative mb-3 flex justify-end">
              <button
                onClick={() => {
                  setRegistryOpen((prev) => !prev);
                  if (!registrySummary && !registryLoading) {
                    void fetchRegistrySummary();
                  }
                }}
                className={`sf-btn cursor-pointer px-3 py-2 text-[11px] ${
                  registryOpen
                    ? "border border-[rgba(49,94,251,0.18)] bg-[rgba(232,239,255,0.84)] text-[var(--sf-accent-strong)]"
                    : "sf-btn-secondary text-[var(--sf-accent-strong)]"
                }`}
              >
                <Wrench className="h-4 w-4" />
                <span>Tool {builtInCount}</span>
                <span className="text-blue-300">|</span>
                <span>MCP {mcpServerCount}</span>
              </button>

              {registryOpen && (
                <div className="sf-floating-panel absolute right-0 bottom-full z-40 mb-2 w-[22rem] max-w-[calc(100vw-3rem)] overflow-hidden">
                  <div className="flex items-center justify-between border-b border-[var(--sf-border)] bg-[rgba(247,249,252,0.8)] px-3 py-3">
                    <div className="flex items-center gap-2 text-[11px] font-bold text-[var(--sf-ink-soft)]">
                      <Server className="h-3.5 w-3.5" />
                      <span>Registered tools and MCP</span>
                    </div>
                    <div className="flex items-center gap-1">
                      <button
                        onClick={() => void fetchRegistrySummary()}
                        className="sf-btn sf-btn-ghost cursor-pointer px-2 py-1 text-[10px]"
                        title="刷新"
                      >
                        <RefreshCw className="h-3.5 w-3.5" />
                      </button>
                      <button
                        onClick={() => setRegistryOpen(false)}
                        className="sf-btn sf-btn-ghost cursor-pointer px-2 py-1 text-[10px]"
                        title="关闭"
                      >
                        <X className="h-3.5 w-3.5" />
                      </button>
                    </div>
                  </div>

                  <div className="max-h-[60vh] space-y-4 overflow-y-auto p-4 text-xs">
                    {registryLoading ? (
                      <div className="rounded-[18px] border border-[var(--sf-border)] bg-[rgba(247,249,252,0.8)] p-3 text-[var(--sf-ink-soft)]">
                        正在加载工具清单...
                      </div>
                    ) : registryError ? (
                      <div className="rounded-[18px] border border-[rgba(207,63,83,0.14)] bg-[var(--sf-danger-soft)] p-3 text-[var(--sf-danger)]">
                        {registryError}
                      </div>
                    ) : registrySummary ? (
                      <>
                        <div className="grid grid-cols-3 gap-2 text-center">
                          <div className="rounded-[16px] border border-[var(--sf-border)] bg-[rgba(247,249,252,0.8)] p-2">
                            <div className="text-[10px] text-[var(--sf-ink-muted)]">
                              Built-in tools
                            </div>
                            <div className="mt-1 font-mono text-sm font-bold text-[var(--sf-ink)]">
                              {builtInCount}
                            </div>
                          </div>
                          <div className="rounded-[16px] border border-[var(--sf-border)] bg-[rgba(247,249,252,0.8)] p-2">
                            <div className="text-[10px] text-[var(--sf-ink-muted)]">
                              MCP Servers
                            </div>
                            <div className="mt-1 font-mono text-sm font-bold text-[var(--sf-ink)]">
                              {mcpServerCount}
                            </div>
                          </div>
                          <div className="rounded-[16px] border border-[var(--sf-border)] bg-[rgba(247,249,252,0.8)] p-2">
                            <div className="text-[10px] text-[var(--sf-ink-muted)]">
                              MCP Tools
                            </div>
                            <div className="mt-1 font-mono text-sm font-bold text-[var(--sf-ink)]">
                              {mcpToolCount}
                            </div>
                          </div>
                        </div>

                        <section className="space-y-2">
                          <div className="text-[11px] font-black tracking-wide text-[var(--sf-ink-muted)] uppercase">
                            Built-in tools
                          </div>
                          {registrySummary.built_in_tools.length === 0 ? (
                            <div className="rounded-[16px] border border-[var(--sf-border)] bg-[rgba(247,249,252,0.8)] p-2 text-[var(--sf-ink-muted)]">
                              There are no built-in tools yet
                            </div>
                          ) : (
                            registrySummary.built_in_tools.map((tool) => (
                              <div
                                key={`builtin-${tool.name}`}
                                className="rounded-[16px] border border-[var(--sf-border)] bg-white/[0.82] p-2"
                              >
                                <div className="font-mono text-[11px] font-bold text-[var(--sf-ink)]">
                                  {tool.name}
                                </div>
                                {tool.description && (
                                  <div
                                    className="mt-1 text-[10px] leading-relaxed text-[var(--sf-ink-muted)]"
                                    title={tool.description}
                                  >
                                    {truncateWithEllipsis(tool.description, 48)}
                                  </div>
                                )}
                              </div>
                            ))
                          )}
                        </section>

                        <section className="space-y-2">
                          <div className="text-[11px] font-black tracking-wide text-[var(--sf-ink-muted)] uppercase">
                            MCP
                          </div>
                          {registrySummary.mcp_servers.length === 0 ? (
                            <div className="rounded-[16px] border border-[var(--sf-border)] bg-[rgba(247,249,252,0.8)] p-2 text-[var(--sf-ink-muted)]">
                              There is no MCP configuration at this time
                            </div>
                          ) : (
                            registrySummary.mcp_servers.map((server) => (
                              <div
                                key={`mcp-${server.name}`}
                                className="rounded-[16px] border border-[var(--sf-border)] bg-white/[0.82] p-2"
                              >
                                <div className="flex items-center justify-between gap-2">
                                  <div className="font-mono text-[11px] font-bold text-[var(--sf-ink)]">
                                    {server.name}
                                  </div>
                                  <div className="flex items-center gap-2 text-[10px]">
                                    <span
                                      className={`h-2 w-2 rounded-full ${
                                        server.connected
                                          ? "bg-[var(--sf-success)]"
                                          : "bg-slate-300"
                                      }`}
                                    ></span>
                                    <span className="text-[var(--sf-ink-muted)]">
                                      {server.connected ? "已连接" : "未连接"}
                                    </span>
                                    <span className="font-mono text-[var(--sf-ink-soft)]">
                                      {server.tool_count} tools
                                    </span>
                                  </div>
                                </div>
                                {server.tools.length > 0 && (
                                  <div className="mt-2 space-y-1">
                                    {server.tools.map((tool) => (
                                      <div
                                        key={`mcp-${server.name}-${tool.name}`}
                                        className="rounded-[12px] bg-[rgba(247,249,252,0.86)] px-2 py-1"
                                      >
                                        <div className="font-mono text-[10px] font-bold text-[var(--sf-ink-soft)]">
                                          {tool.name}
                                        </div>
                                        {tool.description && (
                                          <div
                                            className="mt-0.5 text-[10px] leading-relaxed text-[var(--sf-ink-muted)]"
                                            title={tool.description}
                                          >
                                            {truncateWithEllipsis(
                                              tool.description,
                                              48,
                                            )}
                                          </div>
                                        )}
                                      </div>
                                    ))}
                                  </div>
                                )}
                              </div>
                            ))
                          )}
                        </section>
                      </>
                    ) : (
                      <div className="rounded-[18px] border border-[var(--sf-border)] bg-[rgba(247,249,252,0.8)] p-3 text-[var(--sf-ink-muted)]">
                        暂无工具数据
                      </div>
                    )}
                  </div>
                </div>
              )}
            </div>

            <div className="group relative">
              <div className="absolute -inset-1 rounded-[26px] bg-[radial-gradient(circle_at_top_left,rgba(49,94,251,0.16),transparent_55%)] opacity-70 blur-xl transition duration-500 group-focus-within:opacity-100"></div>
              <div className="sf-panel-muted relative overflow-hidden transition-all">
                <textarea
                  className="sf-input h-24 resize-none border-0 bg-transparent p-4 pr-16 text-sm outline-none"
                  placeholder="Describe the task you want StewardFlow to execute..."
                  value={goal}
                  onChange={(e) => setGoal(e.target.value)}
                  onKeyDown={handleKeyDown}
                  disabled={isRunning || isStopping || Boolean(pendingConfirm)}
                />
                <button
                  onClick={showStopButton ? handleStop : handleRun}
                  disabled={showStopButton ? stopDisabled : sendDisabled}
                  className={`absolute right-3 bottom-3 rounded-[16px] p-3 transition-all ${
                    showStopButton
                      ? isAwaitingTraceId
                        ? "bg-slate-100 text-slate-400"
                        : stopDisabled
                          ? "bg-[var(--sf-danger-soft)] text-[rgba(207,63,83,0.45)]"
                          : "bg-[var(--sf-danger)] text-white shadow-[0_16px_34px_rgba(207,63,83,0.2)] hover:brightness-95 active:scale-95"
                      : sendDisabled
                        ? "bg-slate-100 text-slate-400"
                        : "bg-[linear-gradient(180deg,var(--sf-accent),var(--sf-accent-strong))] text-white shadow-[0_16px_34px_rgba(49,94,251,0.2)] hover:brightness-105 active:scale-95"
                  }`}
                >
                  {showStopButton ? (
                    isAwaitingTraceId || isStopping ? (
                      <Loader2 className="h-5 w-5 animate-spin" />
                    ) : (
                      <X className="h-5 w-5" />
                    )
                  ) : (
                    <Send className="h-5 w-5" />
                  )}
                </button>
              </div>
            </div>

          </div>
        </div>

        {/* Right Column: Dynamic View */}
        <div className="relative flex w-1/2 min-w-0 flex-col bg-[rgba(247,249,252,0.2)]">
          <div
            className={`absolute inset-0 flex h-full flex-col overflow-hidden transition-opacity duration-150 ${
              activeTab === "runner"
                ? "pointer-events-auto opacity-100"
                : "pointer-events-none opacity-0"
            }`}
          >
            <div className="flex items-center justify-between border-b border-[var(--sf-border)] px-6 py-4">
              <div className="flex items-center gap-3">
                <div className="flex h-10 w-10 items-center justify-center rounded-2xl border border-[rgba(49,94,251,0.12)] bg-[rgba(232,239,255,0.78)] text-[var(--sf-accent-strong)]">
                  <Terminal className="h-4 w-4" />
                </div>
                <div>
                  <h3 className="text-sm font-semibold text-[var(--sf-ink)]">
                    Execution Trace
                  </h3>

                </div>
              </div>
              <div className="flex items-center gap-2">
                {tokenInfo && (
                  <div className="sf-chip sf-chip-blue gap-2 text-[10px]">
                    <span>Cache {tokenInfo.cache_tokens}</span>
                    <span className="text-blue-300">/</span>
                    <span>Prompt {tokenInfo.prompt_tokens}</span>
                    <span className="text-blue-300">/</span>
                    <span>Completion {tokenInfo.completion_tokens}</span>
                    <span className="text-blue-300">/</span>
                    <span>Total {tokenInfo.total_tokens}</span>
                  </div>
                )}
                <span className="sf-chip sf-chip-blue text-[10px]">
                  {steps.length} LOGS
                </span>
              </div>
            </div>
            <div
              ref={traceScrollRef}
              className="flex-1 space-y-4 overflow-y-auto bg-[rgba(247,249,252,0.42)] p-6"
            >
              {steps.length === 0 ? (
                <div className="sf-empty-state flex h-full flex-col items-center justify-center select-none">
                  <div className="mb-4 flex h-20 w-20 items-center justify-center rounded-[28px] border border-[var(--sf-border)] bg-white/80 shadow-[0_16px_34px_rgba(15,23,42,0.05)]">
                    <Layers className="h-10 w-10 opacity-60" />
                  </div>
                  <p className="text-sm font-bold tracking-[0.16em] uppercase">
                    No trace yet
                  </p>
                  <p className="mt-2 max-w-sm text-center text-sm leading-6 text-[var(--sf-ink-muted)]">
                    No execution trace has been created yet. Start a task to see reasoning steps, tool calls, and observations appear here in real time.
                  </p>
                </div>
              ) : (
                steps.map((step, idx) => (
                  <StepCard
                    key={`${step.type}-${step.msgId ?? step.stepId ?? idx}`}
                    step={step}
                  />
                ))
              )}
            </div>
          </div>

          <div
            className={`absolute inset-0 flex h-full flex-col overflow-hidden bg-[rgba(247,249,252,0.42)] transition-opacity duration-150 ${
              activeTab === "browser"
                ? "pointer-events-auto opacity-100"
                : "pointer-events-none opacity-0"
            }`}
          >
            <div
              ref={normalizedNoVncUrl ? browserDockRef : null}
              className="relative flex flex-1 items-center justify-center overflow-hidden bg-[rgba(255,255,255,0.68)] shadow-inner"
            >
              {!canExpandBrowser && (
                <div className="max-w-md p-10 text-center">
                  <div className="mx-auto mb-4 flex h-[4.5rem] w-[4.5rem] items-center justify-center rounded-[26px] border border-[var(--sf-border)] bg-white/[0.86] shadow-[0_18px_36px_rgba(15,23,42,0.06)]">
                    <Globe className="h-8 w-8 text-[var(--sf-ink-muted)]" />
                  </div>
                  <h4 className="text-xs font-bold tracking-[0.18em] text-[var(--sf-ink-muted)] uppercase">
                    Waiting for VNC Session
                  </h4>
                  <p className="mt-3 text-sm leading-6 text-[var(--sf-ink-muted)]">
                    Browser tools will refresh sandbox noVNC URL from
                    `/sandboxes`, then render the remote browser here.
                  </p>
                </div>
              )}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
};

// --- Helper Components ---
const TabButton = ({ active, onClick, icon: Icon, label }: any) => (
  <button
    onClick={onClick}
    className="sf-tab-button"
    data-active={active ? "true" : "false"}
  >
    <Icon className="h-3.5 w-3.5" />
    <span className="tracking-[0.14em] uppercase">{label}</span>
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

// 可选：如果你确实会收到 Python dict 字符串（单引号、True/False/None）
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
    <pre className="sf-terminal-panel overflow-x-auto p-4 font-mono text-[11px] whitespace-pre text-slate-200">
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
            className={`block rounded-[18px] border p-3 transition ${
              clickable
                ? "border-[var(--sf-border)] bg-white/80 hover:border-[rgba(49,94,251,0.18)] hover:bg-white"
                : "border-[var(--sf-border)] bg-white/80"
            }`}
          >
            <div className="text-[12px] leading-snug font-semibold text-[var(--sf-ink)]">
              {it.title || "(no title)"}
            </div>

            {it.snippet && (
              <div className="mt-1 text-[11px] leading-relaxed text-[var(--sf-ink-muted)]">
                {it.snippet}
              </div>
            )}

            {it.link && (
              <div className="mt-2 truncate font-mono text-[10px] text-[var(--sf-accent-strong)]">
                {it.link}
              </div>
            )}
          </Wrapper>
        );
      })}

      {items.length > max && (
        <button
          onClick={() => setExpanded((v) => !v)}
          className="text-[10px] font-bold tracking-[0.16em] text-[var(--sf-accent-strong)] uppercase hover:opacity-80"
        >
          {expanded ? "收起" : `展开全部（${items.length}条）`}
        </button>
      )}
    </div>
  );
};

const OBSERVATION_PREVIEW_LIMIT = 200;
const OBSERVATION_PREVIEW_NOTICE = "[内容过长，已截断，仅供预览]";

function formatObservationPreview(content: unknown): unknown {
  if (typeof content !== "string") return content;
  const chars = Array.from(content);
  if (chars.length <= OBSERVATION_PREVIEW_LIMIT) return content;
  return `${chars.slice(0, OBSERVATION_PREVIEW_LIMIT).join("")}\n${OBSERVATION_PREVIEW_NOTICE}`;
}

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
  const tone = isThought
    ? {
        shell: "border-[var(--sf-border)] bg-white/[0.86]",
        header:
          "border-[var(--sf-border)] bg-[rgba(247,249,252,0.9)] text-[var(--sf-ink-muted)]",
        nested: "border-[var(--sf-border)] bg-white/90",
        pill: "bg-[rgba(15,23,42,0.07)] text-[var(--sf-ink-soft)]",
        title: "text-[var(--sf-ink)]",
        meta: "text-[var(--sf-ink-muted)]",
      }
    : isAction
      ? {
          shell: "border-[rgba(49,94,251,0.12)] bg-[rgba(232,239,255,0.58)]",
          header:
            "border-[rgba(49,94,251,0.12)] bg-[rgba(232,239,255,0.92)] text-[var(--sf-accent-strong)]",
          nested: "border-[rgba(49,94,251,0.12)] bg-white/[0.88]",
          pill: "bg-[var(--sf-accent)] text-white",
          title: "text-[var(--sf-accent-strong)]",
          meta: "text-[rgba(35,71,207,0.72)]",
        }
      : isObs
        ? {
            shell: "border-[rgba(15,143,99,0.12)] bg-[rgba(232,248,241,0.72)]",
            header:
              "border-[rgba(15,143,99,0.12)] bg-[rgba(232,248,241,0.92)] text-[#0a6d4b]",
            nested: "border-[rgba(15,143,99,0.12)] bg-white/[0.88]",
            pill: "bg-[var(--sf-success)] text-white",
            title: "text-[#0a6d4b]",
            meta: "text-[rgba(10,109,75,0.72)]",
          }
        : isFinal
          ? {
              shell: "border-[rgba(15,23,42,0.1)] bg-[rgba(245,247,251,0.94)]",
              header:
                "border-[rgba(15,23,42,0.1)] bg-[rgba(255,255,255,0.86)] text-[var(--sf-ink-soft)]",
              nested: "border-[var(--sf-border)] bg-white/90",
              pill: "bg-[var(--sf-ink)] text-white",
              title: "text-[var(--sf-ink)]",
              meta: "text-[var(--sf-ink-muted)]",
            }
          : {
              shell:
                "border-[rgba(207,63,83,0.14)] bg-[rgba(255,240,242,0.92)]",
              header:
                "border-[rgba(207,63,83,0.14)] bg-[rgba(255,240,242,0.96)] text-[var(--sf-danger)]",
              nested: "border-[rgba(207,63,83,0.14)] bg-white/90",
              pill: "bg-[var(--sf-danger)] text-white",
              title: "text-[var(--sf-danger)]",
              meta: "text-[rgba(164,47,64,0.74)]",
            };

  return (
    <div
      className={`animate-in fade-in slide-in-from-bottom-2 overflow-hidden rounded-[24px] border shadow-[0_14px_32px_rgba(15,23,42,0.05)] duration-500 ${tone.shell}`}
    >
      <div
        className={`flex items-center gap-2 border-b px-4 py-3 text-[10px] font-extrabold tracking-[0.18em] uppercase ${tone.header}`}
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

      {/* 关键：强制左对齐 + 分类渲染 */}
      <div className="overflow-x-auto p-4 !text-left text-sm leading-6 text-[var(--sf-ink-soft)]">
        {isAction ? (
          Array.isArray(step.actions) && step.actions.length > 0 ? (
            <div className="space-y-3">
              {step.actions.map((action, idx) => {
                const toolName =
                  typeof action.tool_name === "string"
                    ? action.tool_name
                    : "(unknown)";
                return (
                  <div
                    key={`${toolName}-${idx}`}
                    className={`rounded-[18px] border p-3 ${tone.nested}`}
                  >
                    <div className="mb-2 flex items-center gap-2">
                      <span
                        className={`rounded-full px-2 py-1 text-[10px] font-bold uppercase ${tone.pill}`}
                      >
                        Tool #{idx + 1}
                      </span>
                      <span
                        className={`font-mono text-[11px] font-semibold ${tone.title}`}
                      >
                        {toolName}
                      </span>
                    </div>
                    <ContentRenderer content={action.args} />
                  </div>
                );
              })}
            </div>
          ) : (
            <div className="space-y-2">
              <div className="flex items-center gap-2">
                <span
                  className={`rounded-full px-2 py-1 text-[10px] font-bold uppercase ${tone.pill}`}
                >
                  Tool
                </span>
                <span
                  className={`font-mono text-[11px] font-semibold ${tone.title}`}
                >
                  {step.tool}
                </span>
              </div>
              <ContentRenderer content={step.toolInput} />
            </div>
          )
        ) : isObs ? (
          Array.isArray(step.observations) && step.observations.length > 0 ? (
            <div className="space-y-3">
              {step.observations.map((observation, idx) => {
                const obsType =
                  typeof observation.type === "string"
                    ? observation.type
                    : "observation";
                return (
                  <div
                    key={`${obsType}-${idx}`}
                    className={`rounded-[18px] border p-3 ${tone.nested}`}
                  >
                    <div className="mb-2 flex items-center gap-2">
                      <span
                        className={`rounded-full px-2 py-1 text-[10px] font-bold uppercase ${tone.pill}`}
                      >
                        Obs #{idx + 1}
                      </span>
                      <span
                        className={`font-mono text-[11px] font-semibold ${tone.title}`}
                      >
                        {obsType}
                      </span>
                      {typeof observation.action_id === "string" && (
                        <span className={`font-mono text-[10px] ${tone.meta}`}>
                          action_id={observation.action_id}
                        </span>
                      )}
                    </div>
                    <ContentRenderer
                      content={formatObservationPreview(observation.content)}
                    />
                  </div>
                );
              })}
            </div>
          ) : (
            <ContentRenderer content={formatObservationPreview(step.content)} />
          )
        ) : (
          <ContentRenderer content={step.content} />
        )}
      </div>
    </div>
  );
};
