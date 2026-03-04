import './App.css'
import {AgentWorkbench} from "./components/AgentWorkbench.tsx";
import {SandboxConsole} from "./components/SandboxConsole.tsx";
import {LayoutPanelLeft, MessageSquareCode, Server} from "lucide-react";
import {useState} from "react";

type NavKey = "workbench" | "sandbox";

export default function App() {
    const [activeNav, setActiveNav] = useState<NavKey>("workbench");

    return (
        <div className="h-screen w-full bg-[#f8f9fc] p-4">
            <div className="mx-auto flex h-[calc(100vh-2rem)] w-full max-w-[1700px] gap-4">
              <aside className="flex w-[250px] shrink-0 flex-col overflow-hidden rounded-2xl border border-gray-200 bg-white shadow-xl">
                <div className="flex items-center gap-2 border-b border-gray-100 px-4 py-4">
                  <div className="rounded-lg bg-slate-800 p-2">
                    <LayoutPanelLeft className="h-4 w-4 text-white" />
                  </div>
                  <div>
                    <div className="text-sm font-bold text-gray-900">StewardFlow</div>
                    <div className="text-[11px] text-gray-500">Navigation</div>
                  </div>
                </div>

                <nav className="flex flex-1 flex-col gap-2 p-3">
                  <button
                    type="button"
                    onClick={() => setActiveNav("workbench")}
                    className={`flex items-center gap-3 rounded-xl px-3 py-3 text-left text-sm font-semibold transition ${
                      activeNav === "workbench"
                        ? "bg-indigo-600 text-white shadow-lg shadow-indigo-200"
                        : "bg-gray-50 text-gray-700 hover:bg-gray-100"
                    }`}
                  >
                    <MessageSquareCode className="h-4 w-4" />
                    <div>
                      <div>Agent Workspace</div>
                      <div className={`text-[11px] ${activeNav === "workbench" ? "text-indigo-100" : "text-gray-500"}`}>
                        Chat + TRACE
                      </div>
                    </div>
                  </button>

                  <button
                    type="button"
                    onClick={() => setActiveNav("sandbox")}
                    className={`flex items-center gap-3 rounded-xl px-3 py-3 text-left text-sm font-semibold transition ${
                      activeNav === "sandbox"
                        ? "bg-slate-700 text-white shadow-lg shadow-slate-200"
                        : "bg-gray-50 text-gray-700 hover:bg-gray-100"
                    }`}
                  >
                    <Server className="h-4 w-4" />
                    <div>
                      <div>Sandbox Console</div>
                      <div className={`text-[11px] ${activeNav === "sandbox" ? "text-slate-200" : "text-gray-500"}`}>
                        Health + Logs
                      </div>
                    </div>
                  </button>
                </nav>
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
