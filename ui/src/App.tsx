import "./App.css";
import { AgentWorkbench } from "./components/AgentWorkbench.tsx";
import { SandboxConsole } from "./components/SandboxConsole.tsx";
import {
  LayoutPanelLeft,
  MessageSquareCode,
  Orbit,
  Server,
} from "lucide-react";
import { useState } from "react";

type NavKey = "workbench" | "sandbox";

export default function App() {
  const [activeNav, setActiveNav] = useState<NavKey>("workbench");

  const navButtonClass = (active: boolean) =>
    [
      "group flex w-full items-start gap-3 rounded-[20px] border px-4 py-4 text-left transition-all duration-200",
      active
        ? "border-white/10 bg-white/[0.12] text-white shadow-[inset_0_1px_0_rgba(255,255,255,0.08),0_18px_36px_rgba(15,23,42,0.22)]"
        : "border-transparent bg-transparent text-slate-300 hover:border-white/10 hover:bg-white/[0.06] hover:text-white",
    ].join(" ");

  return (
    <div className="h-screen w-full px-4 py-4 lg:px-5 lg:py-5">
      <div className="mx-auto flex h-[calc(100vh-2rem)] w-full max-w-[1760px] gap-5">
        <aside className="sf-terminal-panel flex w-[272px] shrink-0 flex-col overflow-hidden text-white">
          <div className="border-b border-white/10 px-5 py-5">
            <div className="flex items-center gap-3">
              <div className="sf-icon-badge sf-icon-badge-accent h-11 w-11">
                <LayoutPanelLeft className="h-5 w-5" />
              </div>
              <div>
                <div className="text-sm font-extrabold tracking-tight text-white">
                  StewardFlow
                </div>
                <div className="mt-1 text-[11px] text-slate-400">
                  Precision Workspace
                </div>
              </div>
            </div>
            <div className="mt-5 rounded-[18px] border border-white/10 bg-white/[0.06] px-3 py-3">
              <div className="flex items-center gap-2 text-[11px] font-semibold text-slate-200">
                <Orbit className="h-3.5 w-3.5 text-blue-300" />
                Unified operator surface
              </div>
              <p className="mt-2 text-[11px] leading-5 text-slate-400">
                One consistent shell for chat, execution trace, VNC browser
                control, and sandbox diagnostics.
              </p>
            </div>
          </div>

          <nav className="flex flex-1 flex-col gap-2 p-3">
            <button
              type="button"
              onClick={() => setActiveNav("workbench")}
              className={navButtonClass(activeNav === "workbench")}
            >
              <div
                className={`mt-0.5 flex h-10 w-10 shrink-0 items-center justify-center rounded-2xl border ${
                  activeNav === "workbench"
                    ? "border-white/10 bg-white/[0.12] text-white"
                    : "border-white/10 bg-white/[0.04] text-slate-300 group-hover:bg-white/[0.08]"
                }`}
              >
                <MessageSquareCode className="h-4 w-4" />
              </div>
              <div className="min-w-0">
                <div className="text-sm font-semibold">Agent Workspace</div>
                <div
                  className={`mt-1 text-[11px] leading-5 ${
                    activeNav === "workbench"
                      ? "text-slate-300"
                      : "text-slate-400"
                  }`}
                >
                  Chat, execution trace, and browser orchestration
                </div>
              </div>
            </button>

            <button
              type="button"
              onClick={() => setActiveNav("sandbox")}
              className={navButtonClass(activeNav === "sandbox")}
            >
              <div
                className={`mt-0.5 flex h-10 w-10 shrink-0 items-center justify-center rounded-2xl border ${
                  activeNav === "sandbox"
                    ? "border-white/10 bg-white/[0.12] text-white"
                    : "border-white/10 bg-white/[0.04] text-slate-300 group-hover:bg-white/[0.08]"
                }`}
              >
                <Server className="h-4 w-4" />
              </div>
              <div className="min-w-0">
                <div className="text-sm font-semibold">Sandbox Console</div>
                <div
                  className={`mt-1 text-[11px] leading-5 ${
                    activeNav === "sandbox"
                      ? "text-slate-300"
                      : "text-slate-400"
                  }`}
                >
                  Health inspection, log tailing, and runtime visibility
                </div>
              </div>
            </button>
          </nav>

          <div className="border-t border-white/10 px-4 py-4">
            <div className="rounded-[18px] border border-white/10 bg-white/[0.05] px-3 py-3">
              <div className="text-[10px] font-bold tracking-[0.22em] text-slate-500 uppercase">
                username
              </div>
              <div className="mt-2 text-sm font-semibold text-white">
                Current Plan: Free
              </div>
            </div>
          </div>
        </aside>

        <main className="min-w-0 flex-1">
          <section
            className={activeNav === "workbench" ? "h-full" : "hidden h-full"}
            aria-hidden={activeNav !== "workbench"}
          >
            <AgentWorkbench />
          </section>
          <section
            className={activeNav === "sandbox" ? "h-full" : "hidden h-full"}
            aria-hidden={activeNav !== "sandbox"}
          >
            <SandboxConsole />
          </section>
        </main>
      </div>
    </div>
  );
}
