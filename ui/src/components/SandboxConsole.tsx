import React, { useMemo, useState } from "react";
import { Activity, ClipboardList, RefreshCw, Server } from "lucide-react";

type SandboxHealthResponse = {
  sandbox_id: string;
  ok: boolean;
  reason?: string;
  error?: string;
  sandbox_status?: string;
  api_port?: number | null;
  status_code?: number;
  url?: string;
  body?: unknown;
};

type SandboxLogsResponse = {
  sandbox_id: string;
  tail: number;
  logs: string;
};

type SandboxListItem = {
  sandbox_id: string;
  status?: string;
};

type SandboxListResponse = {
  count: number;
  items: SandboxListItem[];
};

function normalizeErrorMessage(error: unknown): string {
  if (error instanceof Error) return error.message;
  if (typeof error === "string") return error;
  return "Unknown request error";
}

export const SandboxConsole: React.FC = () => {
  const apiBase = useMemo(() => {
    const configured = import.meta.env.VITE_API_BASE as string | undefined;
    const base = configured?.trim() ? configured.trim() : "http://localhost:8000";
    return base.endsWith("/") ? base.slice(0, -1) : base;
  }, []);

  const [sandboxId, setSandboxId] = useState("");
  const [tail, setTail] = useState(200);

  const [health, setHealth] = useState<SandboxHealthResponse | null>(null);
  const [logs, setLogs] = useState("");

  const [healthLoading, setHealthLoading] = useState(false);
  const [logsLoading, setLogsLoading] = useState(false);
  const [healthError, setHealthError] = useState<string | null>(null);
  const [logsError, setLogsError] = useState<string | null>(null);

  const resolveSandboxId = async (): Promise<string> => {
    const direct = sandboxId.trim();
    if (direct) return direct;

    const response = await fetch(`${apiBase}/sandboxes?include_exited=false`);
    const payload = (await response.json()) as SandboxListResponse | { detail?: string };
    if (!response.ok) {
      const detail = "detail" in payload ? payload.detail : undefined;
      throw new Error(detail || `Sandbox list request failed (${response.status})`);
    }
    const data = payload as SandboxListResponse;
    if (!Array.isArray(data.items) || data.items.length === 0) {
      throw new Error("No running sandbox found");
    }
    const preferred = data.items.find((item) => item.status === "running") || data.items[0];
    const resolved = preferred.sandbox_id?.trim();
    if (!resolved) {
      throw new Error("Sandbox id is missing");
    }
    setSandboxId(resolved);
    return resolved;
  };

  const fetchHealth = async () => {
    setHealthLoading(true);
    setHealthError(null);
    try {
      const id = await resolveSandboxId();
      const response = await fetch(`${apiBase}/sandboxes/${encodeURIComponent(id)}/health`);
      const payload = (await response.json()) as SandboxHealthResponse | { detail?: string };
      if (!response.ok) {
        const detail = "detail" in payload ? payload.detail : undefined;
        throw new Error(detail || `Health request failed (${response.status})`);
      }
      setHealth(payload as SandboxHealthResponse);
    } catch (error) {
      setHealthError(normalizeErrorMessage(error));
    } finally {
      setHealthLoading(false);
    }
  };

  const fetchLogs = async () => {
    setLogsLoading(true);
    setLogsError(null);
    try {
      const id = await resolveSandboxId();
      const safeTail = Number.isFinite(tail) ? Math.max(1, Math.min(5000, tail)) : 200;
      const response = await fetch(
        `${apiBase}/sandboxes/${encodeURIComponent(id)}/logs?tail=${safeTail}`,
      );
      const payload = (await response.json()) as SandboxLogsResponse | { detail?: string };
      if (!response.ok) {
        const detail = "detail" in payload ? payload.detail : undefined;
        throw new Error(detail || `Logs request failed (${response.status})`);
      }
      const data = payload as SandboxLogsResponse;
      setLogs(data.logs || "");
    } catch (error) {
      setLogsError(normalizeErrorMessage(error));
    } finally {
      setLogsLoading(false);
    }
  };

  return (
    <div className="flex h-full flex-col overflow-hidden rounded-2xl border border-gray-200 bg-white shadow-xl">
      <div className="flex items-center justify-between border-b border-gray-100 px-6 py-4">
        <div className="flex items-center gap-3">
          <div className="rounded-xl bg-slate-700 p-2.5 shadow-lg shadow-slate-200">
            <Server className="h-5 w-5 text-white" />
          </div>
          <div>
            <h2 className="font-bold text-gray-900">Sandbox Console</h2>
            <p className="text-[11px] text-gray-500">Health check and logs only</p>
          </div>
        </div>
      </div>

      <div className="grid flex-1 grid-cols-1 gap-4 overflow-hidden p-4 lg:grid-cols-[360px_minmax(0,1fr)]">
        <section className="flex flex-col gap-4 overflow-auto rounded-xl border border-gray-200 bg-gray-50 p-4">
          <div>
            <label className="mb-2 block text-[11px] font-bold tracking-wide text-gray-600 uppercase">
              Sandbox ID
            </label>
            <input
              value={sandboxId}
              onChange={(event) => setSandboxId(event.target.value)}
              placeholder="Leave empty to auto-detect running sandbox"
              className="w-full rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm outline-none focus:border-indigo-500 focus:ring-2 focus:ring-indigo-100"
            />
          </div>

          <div>
            <div className="mb-2 flex items-center gap-2 text-[11px] font-bold tracking-wide text-gray-600 uppercase">
              <Activity className="h-3.5 w-3.5" />
              Health
            </div>
            <button
              type="button"
              onClick={() => void fetchHealth()}
              disabled={healthLoading}
              className="inline-flex items-center gap-2 rounded-lg bg-indigo-600 px-3 py-2 text-xs font-bold text-white transition hover:bg-indigo-700 disabled:cursor-not-allowed disabled:bg-gray-300"
            >
              <RefreshCw className={`h-3.5 w-3.5 ${healthLoading ? "animate-spin" : ""}`} />
              {healthLoading ? "Checking..." : "Check Health"}
            </button>

            {healthError && (
              <div className="mt-3 rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-xs text-rose-700">
                {healthError}
              </div>
            )}

            {health && !healthError && (
              <div className="mt-3 space-y-2 rounded-lg border border-gray-200 bg-white p-3 text-xs">
                <div className="flex items-center justify-between">
                  <span className="font-semibold text-gray-600">Result</span>
                  <span
                    className={`rounded px-2 py-0.5 font-bold ${
                      health.ok
                        ? "bg-emerald-100 text-emerald-700"
                        : "bg-rose-100 text-rose-700"
                    }`}
                  >
                    {health.ok ? "HEALTHY" : "UNHEALTHY"}
                  </span>
                </div>
                <div className="text-gray-700">sandbox: {health.sandbox_id}</div>
                {health.sandbox_status && (
                  <div className="text-gray-700">container: {health.sandbox_status}</div>
                )}
                {typeof health.api_port === "number" && (
                  <div className="text-gray-700">api_port: {health.api_port}</div>
                )}
                {typeof health.status_code === "number" && (
                  <div className="text-gray-700">status_code: {health.status_code}</div>
                )}
                {health.reason && <div className="text-rose-700">reason: {health.reason}</div>}
                {health.error && <div className="text-rose-700">error: {health.error}</div>}
              </div>
            )}
          </div>

          <div>
            <div className="mb-2 flex items-center gap-2 text-[11px] font-bold tracking-wide text-gray-600 uppercase">
              <ClipboardList className="h-3.5 w-3.5" />
              Log Options
            </div>
            <label className="mb-2 block text-[11px] text-gray-600">Tail lines</label>
            <input
              type="number"
              min={1}
              max={5000}
              value={tail}
              onChange={(event) => setTail(Number(event.target.value))}
              className="w-full rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm outline-none focus:border-indigo-500 focus:ring-2 focus:ring-indigo-100"
            />
            <button
              type="button"
              onClick={() => void fetchLogs()}
              disabled={logsLoading}
              className="mt-3 inline-flex items-center gap-2 rounded-lg bg-slate-700 px-3 py-2 text-xs font-bold text-white transition hover:bg-slate-800 disabled:cursor-not-allowed disabled:bg-gray-300"
            >
              <RefreshCw className={`h-3.5 w-3.5 ${logsLoading ? "animate-spin" : ""}`} />
              {logsLoading ? "Loading..." : "Load Logs"}
            </button>

            {logsError && (
              <div className="mt-3 rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-xs text-rose-700">
                {logsError}
              </div>
            )}
          </div>
        </section>

        <section className="min-h-0 overflow-hidden rounded-xl border border-gray-200 bg-gray-950">
          <div className="border-b border-gray-800 px-4 py-2 text-[11px] font-bold tracking-wide text-gray-300 uppercase">
            Sandbox Logs
          </div>
          <pre className="h-full overflow-auto p-4 font-mono text-[12px] leading-relaxed text-gray-200">
            {logs || "# no logs loaded"}
          </pre>
        </section>
      </div>
    </div>
  );
};
