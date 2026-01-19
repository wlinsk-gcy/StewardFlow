import React, {useState, useRef, useEffect, useCallback} from 'react';
import {v4 as uuidv4} from 'uuid';
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
    ExternalLink
} from 'lucide-react';
import {type AgentStep, type ChatMessage} from '../types';

import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

function normalizeText(s: string) {
    return s.replace(/\\n/g, "\n").replace(/\\t/g, "\t");
}

const MessageContent: React.FC<{ content: string }> = ({ content }) => {
    return (
        <div className="prose prose-sm max-w-none prose-p:my-2 prose-li:my-1 prose-ul:my-2 prose-ol:my-2 prose-a:text-indigo-600 prose-a:no-underline hover:prose-a:underline">
            <ReactMarkdown
                remarkPlugins={[remarkGfm]}
                components={{
                    a: (props) => (
                        <a {...props} target="_blank" rel="noreferrer" />
                    ),
                }}
            >
                {normalizeText(content || "")}
            </ReactMarkdown>
        </div>
    );
};

export const AgentWorkbench: React.FC = () => {

    const [goal, setGoal] = useState("");
    const [chatHistory, setChatHistory] = useState<ChatMessage[]>([]);
    const [isRunning, setIsRunning] = useState(false);
    const [steps, setSteps] = useState<AgentStep[]>([]);
    const [activeTab, setActiveTab] = useState<'runner' | 'browser'>('runner');
    const [currentUrl, setCurrentUrl] = useState("about:blank");
    const [currentScreenshot, setCurrentScreenshot] = useState<string | null>(null);

    // --- 新增：WebSocket 相关状态 ---
    const [clientId] = useState(() => uuidv4()); // 保持整个生命周期 clientId 唯一
    const [agentId, setAgentId] = useState<string | null>(null); // 新增：存储后端返回的 agent_id
    const socketRef = useRef<WebSocket | null>(null);
    const chatScrollRef = useRef<HTMLDivElement>(null);
    const traceScrollRef = useRef<HTMLDivElement>(null);

    // --- 新增：初始化 WebSocket ---
    useEffect(() => {
        const socket = new WebSocket(`ws://localhost:8000/ws/${clientId}`);

        socket.onopen = () => console.log("Connected to WS:", clientId);

        socket.onmessage = (event) => {
            const serverEvent = JSON.parse(event.data);
            handleIncomingEvent(serverEvent);
        };

        socket.onclose = () => console.log("WS Disconnected");
        socketRef.current = socket;

        return () => socket.close();
    }, [clientId]);

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
    const handleIncomingEvent = useCallback((event: any) => {
        const {event_type, data, timestamp, turn_id} = event;
        // console.log('incoming event', event);

        // 1. 处理执行日志 (Execution Trace)
        // 日志通常不需要流式输出，直接追加到 steps 数组即可
        if (['thought', 'action', 'observation', 'final'].includes(event_type)) {
            // console.log('receive execute trace', event);
            const newStep: AgentStep = {
                type: event_type,
                content: data.content || "",
                tool: data.tool_name,
                toolInput: data.args,
                timestamp: timestamp
            };
            setSteps(prev => [...prev, newStep]);

            // 如果是 action 且涉及到浏览器（根据你的业务逻辑），可以切换 Tab
            if (event_type === 'action' && data.tool_name?.includes('browser')) {
                // 这里可以根据实际 data 里的 args 解析出 URL
                // setCurrentUrl(...)
            }
        }

        // --- 2. 处理流式消息 (Answer & HITL Request) ---
        if (event_type === 'answer' || event_type === 'hitl_request') {
            // console.log('receive answer or hitl_request');
            setIsRunning(prev => (prev ? false : prev));
            setChatHistory(prev => {
                // 查找是否已经存在相同 turn_id 且角色为 assistant 的消息
                const existingMsgIndex = prev.findIndex(
                    m => m.turnId === turn_id && m.role === 'assistant'
                );
                if (existingMsgIndex !== -1) {
                    // 如果消息已存在，在原有内容基础上追加 text
                    const newHistory = [...prev];
                    const targetMsg = newHistory[existingMsgIndex];
                    newHistory[existingMsgIndex] = {
                        ...targetMsg,
                        content: targetMsg.content + (data.content || ""),
                        // 如果是 hitl_request，确保标记状态
                        isHitl: event_type.startsWith('hitl')
                    };
                    return newHistory;
                } else {
                    // 如果是该 turn_id 的第一块数据，创建新消息
                    return [...prev, {
                        id: uuidv4(),
                        role: 'assistant',
                        content: data.content || "",
                        timestamp: new Date(timestamp).getTime(),
                        turnId: turn_id,
                        isHitl: event_type.startsWith('hitl')
                    }];
                }
            })
        }

        if (event_type === 'hitl_confirm') {
            // TODO 需要用户请求需要弹确认框，要怎么做？
        }

        // 2. 处理聊天窗口消息 (Chat Window)
        if (event_type === 'error') {
            const newMessage: ChatMessage = {
                id: uuidv4(),
                role: 'assistant',
                content: data.content,
                timestamp: new Date(timestamp).getTime(),
                // 如果是 hitl_confirm，可以在这里扩展 type 以后展示按钮
                isHitl: event_type.startsWith('hitl')
            };
            setChatHistory(prev => [...prev, newMessage]);
            setIsRunning(false);
        }

        // if (event_type === 'end' || event_type === 'error') {
        //     // console.log('stop running')
        //     setIsRunning(false);
        // }
    }, []);

    const handleRun = async () => {
        if (!goal.trim() || isRunning) return;

        // 1. UI 反馈
        const userMessage: ChatMessage = {
            id: uuidv4(),
            role: 'user',
            content: goal,
            timestamp: Date.now(),
        };

        setChatHistory(prev => [...prev, userMessage]);
        setSteps([]); // 清空旧日志
        setIsRunning(true);
        const currentGoal = goal;
        setGoal("");

        // 2. 调用后端 API 触发任务
        try {
            const response = await fetch('http://localhost:8000/agent/run', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({
                    client_id: clientId,
                    task: currentGoal,
                    // --- 新增：如果已经有 agent_id，则携带它 ---
                    ...(agentId && {agent_id: agentId})
                })
            });

            if (!response.ok) throw new Error("Failed to start agent");

            const data = await response.json();
            if (data.agent_id) {
                setAgentId(data.agent_id);
                console.log("Current Agent ID session:", data.agent_id);
            }
        } catch (e) {
            console.error(e);
            setChatHistory(prev => [...prev, {
                id: uuidv4(),
                role: 'assistant',
                content: "连接服务器失败，请稍后再试。",
                timestamp: Date.now()
            }]);
            setIsRunning(false);
        }
    };

    const handleKeyDown = (e: React.KeyboardEvent) => {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            handleRun();
        }
    };

    return (
        <div className="flex flex-col h-full bg-white rounded-2xl shadow-xl border border-gray-200 overflow-hidden">
            {/* Tab Header */}
            <div className="flex items-center justify-between px-6 py-4 bg-white border-b border-gray-100 shrink-0">
                <div className="flex items-center gap-3">
                    <div className="p-2.5 bg-indigo-600 rounded-xl shadow-lg shadow-indigo-200">
                        <Cpu className="w-5 h-5 text-white"/>
                    </div>
                    <div>
                        <h2 className="font-bold text-gray-900">ReAct Agent Studio</h2>
                        <div className="flex items-center gap-2 mt-0.5">
                            <span className="flex h-2 w-2 rounded-full bg-green-500"></span>
                            <p className="text-[10px] text-gray-500 uppercase tracking-widest font-bold">System
                                Online</p>
                        </div>
                    </div>
                </div>

                <div className="flex gap-1.5 bg-gray-100 p-1 rounded-xl">
                    <TabButton active={activeTab === 'runner'} onClick={() => setActiveTab('runner')} icon={Terminal}
                               label="Execution Trace"/>
                    <TabButton active={activeTab === 'browser'} onClick={() => setActiveTab('browser')} icon={Globe}
                               label="Browser View"/>
                </div>
            </div>

            <div className="flex-1 flex overflow-hidden divide-x divide-gray-100">
                {/* Left Column: Chat History & Input */}
                <div className="w-1/2 flex flex-col bg-white">
                    {/* Chat Messages */}
                    <div ref={chatScrollRef} className="flex-1 overflow-y-auto p-6 space-y-6 bg-gray-50/30">
                        {chatHistory.length === 0 && (
                            <div className="h-full flex flex-col items-center justify-center text-center px-10">
                                <div
                                    className="w-20 h-20 bg-indigo-50 rounded-3xl flex items-center justify-center mb-6">
                                    <Bot className="w-10 h-10 text-indigo-500 opacity-60"/>
                                </div>
                                <h3 className="text-xl font-bold text-gray-900 mb-2">你好，我是你的私人AI助理</h3>
                                <p className="text-gray-500 text-sm leading-relaxed">
                                    让我浏览网页、进行复杂计算，或用ReAct框架解决逻辑谜题。
                                </p>
                            </div>
                        )}
                        {chatHistory.map(msg => (
                            <div key={msg.id}
                                 className={`flex ${msg.role === 'user' ? 'justify-end' : 'justify-start'}`}>
                                <div
                                    className={`flex gap-3 max-w-[85%] ${msg.role === 'user' ? 'flex-row-reverse' : 'flex-row'}`}>
                                    <div
                                        className={`w-8 h-8 rounded-lg flex items-center justify-center shrink-0 shadow-sm ${
                                            msg.role === 'user' ? 'bg-indigo-600 text-white' : 'bg-white border border-gray-200 text-indigo-600'
                                        }`}>
                                        {msg.role === 'user' ? <User className="w-4 h-4"/> : <Bot className="w-4 h-4"/>}
                                    </div>
                                    <div className={`p-4 rounded-2xl text-left text-sm leading-relaxed shadow-sm break-words ${
                                        msg.role === 'user'
                                            ? 'bg-indigo-600 text-white rounded-tr-none'
                                            : 'bg-white border border-gray-100 text-gray-800 rounded-tl-none'
                                    }`}>
                                        <MessageContent content={String(msg.content ?? "")} />
                                    </div>
                                </div>
                            </div>
                        ))}
                        {isRunning && (
                            <div className="flex justify-start animate-in fade-in slide-in-from-left-2">
                                <div className="flex gap-3">
                                    <div
                                        className="w-8 h-8 rounded-lg bg-white border border-gray-200 text-indigo-600 flex items-center justify-center">
                                        <Bot className="w-4 h-4"/>
                                    </div>
                                    <div
                                        className="bg-white border border-gray-100 p-4 rounded-2xl rounded-tl-none shadow-sm flex items-center gap-2">
                                        <div className="flex gap-1">
                                            <span
                                                className="w-1.5 h-1.5 bg-indigo-300 rounded-full animate-bounce"></span>
                                            <span
                                                className="w-1.5 h-1.5 bg-indigo-400 rounded-full animate-bounce [animation-delay:0.2s]"></span>
                                            <span
                                                className="w-1.5 h-1.5 bg-indigo-500 rounded-full animate-bounce [animation-delay:0.4s]"></span>
                                        </div>
                                        <span
                                            className="text-xs font-bold text-gray-400 uppercase tracking-widest ml-2">Agent is thinking...</span>
                                    </div>
                                </div>
                            </div>
                        )}
                    </div>

                    {/* Chat Input */}
                    <div className="p-6 border-t border-gray-100 bg-white">
                        <div className="relative group">
                            <div
                                className="absolute -inset-1 bg-gradient-to-r from-indigo-500 to-purple-500 rounded-2xl blur opacity-10 group-focus-within:opacity-25 transition duration-1000"></div>
                            <div
                                className="relative bg-white border border-gray-200 rounded-2xl overflow-hidden focus-within:border-indigo-500 focus-within:ring-4 focus-within:ring-indigo-50 transition-all">
                  <textarea
                      className="w-full h-24 p-4 pr-16 text-sm bg-transparent outline-none resize-none font-sans"
                      placeholder="Describe a task for the agent..."
                      value={goal}
                      onChange={(e) => setGoal(e.target.value)}
                      onKeyDown={handleKeyDown}
                      disabled={isRunning}
                  />
                                <button
                                    onClick={handleRun}
                                    disabled={isRunning || !goal.trim()}
                                    className={`absolute right-3 bottom-3 p-3 rounded-xl transition-all ${
                                        isRunning || !goal.trim()
                                            ? 'bg-gray-100 text-gray-400'
                                            : 'bg-indigo-600 text-white hover:bg-indigo-700 shadow-lg shadow-indigo-200 active:scale-95'
                                    }`}
                                >
                                    {isRunning ? <Loader2 className="w-5 h-5 animate-spin"/> :
                                        <Send className="w-5 h-5"/>}
                                </button>
                            </div>
                        </div>
                        <p className="mt-3 text-center text-[10px] text-gray-400 font-medium uppercase tracking-widest">
                            ReAct Protocol v1.0 · Multi-Modality Driven
                        </p>
                    </div>
                </div>

                {/* Right Column: Dynamic View */}
                <div className="w-1/2 flex flex-col bg-gray-50/50">
                    {activeTab === 'runner' ? (
                        <div className="flex flex-col h-full overflow-hidden">
                            <div
                                className="px-6 py-4 border-b border-gray-200 bg-white flex justify-between items-center">
                                <div className="flex items-center gap-2">
                                    <Terminal className="w-4 h-4 text-indigo-500"/>
                                    <h3 className="font-bold text-sm text-gray-700 uppercase tracking-tight">执行日志</h3>
                                </div>
                                <span
                                    className="text-[10px] font-black text-white bg-indigo-500 px-2 py-0.5 rounded-full">{steps.length} LOGS</span>
                            </div>
                            <div ref={traceScrollRef} className="flex-1 overflow-y-auto p-6 space-y-4">
                                {steps.length === 0 ? (
                                    <div
                                        className="h-full flex flex-col items-center justify-center opacity-20 select-none grayscale">
                                        <Layers className="w-16 h-16 mb-4"/>
                                        <p className="text-sm font-bold uppercase tracking-widest">Trace visualization
                                            inactive</p>
                                    </div>
                                ) : (
                                    steps.map((step, idx) => <StepCard key={idx} step={step}/>)
                                )}
                            </div>
                        </div>
                    ) : (
                        <div className="flex flex-col h-full overflow-hidden bg-gray-200">
                            {/* Browser Chrome UI */}
                            <div className="flex flex-col bg-[#e0e0e0] border-b border-gray-300">
                                <div className="flex items-center gap-2 p-2">
                                    <div className="flex gap-1.5 ml-1">
                                        <div className="w-3 h-3 bg-red-400 rounded-full"></div>
                                        <div className="w-3 h-3 bg-yellow-400 rounded-full"></div>
                                        <div className="w-3 h-3 bg-green-400 rounded-full"></div>
                                    </div>
                                    <div
                                        className="flex-1 mx-4 bg-white rounded-md py-1 px-3 text-xs flex items-center justify-between text-gray-500 shadow-inner">
                                        <div className="flex items-center gap-2 truncate max-w-[80%]">
                                            <Globe className="w-3 h-3"/>
                                            <span className="truncate">{currentUrl}</span>
                                        </div>
                                        <Search className="w-3 h-3"/>
                                    </div>
                                    <ExternalLink className="w-4 h-4 text-gray-400 mr-2"/>
                                </div>
                            </div>
                            {/* Browser Viewport */}
                            <div
                                className="flex-1 relative overflow-hidden bg-white shadow-inner flex items-center justify-center">
                                {currentScreenshot ? (
                                    <img
                                        src={currentScreenshot}
                                        alt="Browser View"
                                        className="w-full h-full object-contain animate-in fade-in duration-700"
                                    />
                                ) : (
                                    <div className="text-center p-10 max-w-sm">
                                        <div
                                            className="w-16 h-16 bg-gray-100 rounded-full flex items-center justify-center mx-auto mb-4">
                                            <Globe className="w-8 h-8 text-gray-300"/>
                                        </div>
                                        <h4 className="font-bold text-gray-400 uppercase tracking-widest text-xs">Waiting
                                            for Navigation</h4>
                                        <p className="text-[11px] text-gray-400 mt-2">The agent will render the webpage
                                            view here once it uses the browser tool.</p>
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
const TabButton = ({active, onClick, icon: Icon, label}: any) => (
    <button
        onClick={onClick}
        className={`flex items-center gap-2 px-4 py-2 rounded-lg text-xs font-bold transition-all duration-200 ${
            active
                ? 'bg-white text-indigo-600 shadow-md ring-1 ring-black/5'
                : 'text-gray-500 hover:text-gray-800'
        }`}
    >
        <Icon className="w-3.5 h-3.5"/>
        <span className="uppercase tracking-widest">{label}</span>
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
        return ("title" in x) || ("snippet" in x) || ("link" in x);
    });
}

const CodeBlock: React.FC<{ value: unknown }> = ({value}) => {
    const text =
        typeof value === "string" ? value : JSON.stringify(value, null, 2);

    return (
        <pre
            className="bg-gray-900 rounded-xl p-3 text-gray-300 text-[11px] border border-gray-800 shadow-lg overflow-x-auto whitespace-pre">
      {text}
    </pre>
    );
};

const SearchResults: React.FC<{ items: SearchItem[] }> = ({items}) => {
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
                        <div className="text-[12px] font-bold text-gray-900 leading-snug">
                            {it.title || "(no title)"}
                        </div>

                        {it.snippet && (
                            <div className="mt-1 text-[11px] text-gray-600 leading-relaxed">
                                {it.snippet}
                            </div>
                        )}

                        {it.link && (
                            <div className="mt-2 text-[10px] text-indigo-600 font-mono truncate">
                                {it.link}
                            </div>
                        )}
                    </Wrapper>
                );
            })}

            {items.length > max && (
                <button
                    onClick={() => setExpanded((v) => !v)}
                    className="text-[10px] font-bold uppercase tracking-widest text-indigo-600 hover:text-indigo-700"
                >
                    {expanded ? "收起" : `展开全部（${items.length}条）`}
                </button>
            )}
        </div>
    );
};

const ContentRenderer: React.FC<{ content: unknown }> = ({content}) => {
    const v = normalizeMaybePythonDict(content);

    // 搜索结果数组：卡片列表
    if (isSearchList(v)) return <SearchResults items={v}/>;

    // 普通数组/对象：代码块
    if (Array.isArray(v) || (typeof v === "object" && v !== null)) {
        return <CodeBlock value={v}/>;
    }

    // 文本：正常展示
    return <div className="whitespace-pre-wrap text-left">{String(v ?? "")}</div>;
};

const StepCard: React.FC<{ step: AgentStep }> = ({step}) => {
    const isThought = step.type === "thought";
    const isAction = step.type === "action";
    const isObs = step.type === "observation";
    const isFinal = step.type === "final";
    // const isError = step.type === "error";

    return (
        <div
            className={`rounded-2xl border overflow-hidden animate-in fade-in slide-in-from-bottom-2 duration-500 ${
                isThought
                    ? "bg-white border-gray-100"
                    : isAction
                        ? "bg-indigo-50/50 border-indigo-100"
                        : isObs
                            ? "bg-emerald-50/50 border-emerald-100"
                            : isFinal
                                ? "bg-purple-50 border-purple-100"
                                : "bg-rose-50 border-rose-100"
            }`}
        >
            <div
                className={`px-4 py-2 text-[10px] font-black uppercase tracking-[0.2em] flex items-center gap-2 border-b ${
                    isThought
                        ? "text-gray-400 bg-gray-50 border-gray-100"
                        : isAction
                            ? "text-indigo-600 bg-indigo-100/30 border-indigo-100"
                            : isObs
                                ? "text-emerald-700 bg-emerald-100/30 border-emerald-100"
                                : isFinal
                                    ? "text-purple-600 bg-purple-100/30 border-purple-100"
                                    : "text-rose-600 bg-rose-100/30 border-rose-100"
                }`}
            >
                {isThought && <MessageSquare className="w-3 h-3"/>}
                {isAction && <Terminal className="w-3 h-3"/>}
                {isObs && <Globe className="w-3 h-3"/>}
                {isFinal && <Play className="w-3 h-3"/>}
                {step.type}
                <span className="ml-auto opacity-30 font-mono normal-case tracking-normal">
          {new Date(step.timestamp).toLocaleTimeString([], {
              hour: "2-digit",
              minute: "2-digit",
              second: "2-digit",
          })}
        </span>
            </div>

            {/* 关键：强制左对齐 + 分类型渲染 */}
            <div className="p-4 !text-left text-xs font-mono text-gray-700 leading-relaxed overflow-x-auto">
                {isAction ? (
                    <div className="space-y-2">
                        <div className="flex items-center gap-2">
              <span className="px-1.5 py-0.5 bg-indigo-600 text-white rounded font-bold text-[10px] uppercase">
                Tool
              </span>
                            <span className="font-bold text-indigo-700">{step.tool}</span>
                        </div>

                        {/* toolInput 也统一走 ContentRenderer：支持 dict/list/string */}
                        <ContentRenderer content={step.toolInput}/>
                    </div>
                ) : (
                    <ContentRenderer content={step.content}/>
                )}
            </div>
        </div>
    );
};

