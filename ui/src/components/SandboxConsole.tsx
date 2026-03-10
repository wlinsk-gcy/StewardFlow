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
    const base = configured?.trim()
      ? configured.trim()
      : "http://localhost:8000";
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
    const payload = (await response.json()) as
      | SandboxListResponse
      | { detail?: string };
    if (!response.ok) {
      const detail = "detail" in payload ? payload.detail : undefined;
      throw new Error(
        detail || `Sandbox list request failed (${response.status})`,
      );
    }
    const data = payload as SandboxListResponse;
    if (!Array.isArray(data.items) || data.items.length === 0) {
      throw new Error("No running sandbox found");
    }
    const preferred =
      data.items.find((item) => item.status === "running") || data.items[0];
    const resolved = preferred.sandbox_id?.trim();
    if (!resolved) {
      throw new Error("Sandbox id is missing");
    }
    return resolved;
  };

  const fetchHealth = async () => {
    const requestedSandboxId = sandboxId.trim();
    const shouldClearInput = requestedSandboxId.length > 0;
    setHealthLoading(true);
    setHealthError(null);
    try {
      const params = new URLSearchParams();
      if (requestedSandboxId) {
        params.set("sandbox_id", requestedSandboxId);
      }
      const query = params.toString();
      const url = `${apiBase}/sandboxes/health${query ? `?${query}` : ""}`;
      const response = await fetch(url);
      const payload = (await response.json()) as
        | SandboxHealthResponse
        | { detail?: string };
      if (!response.ok) {
        const detail = "detail" in payload ? payload.detail : undefined;
        throw new Error(detail || `Health request failed (${response.status})`);
      }
      setHealth(payload as SandboxHealthResponse);
    } catch (error) {
      setHealthError(normalizeErrorMessage(error));
    } finally {
      if (shouldClearInput) {
        setSandboxId("");
      }
      setHealthLoading(false);
    }
  };

  const fetchLogs = async () => {
    setLogsLoading(true);
    setLogsError(null);
    try {
      const id = await resolveSandboxId();
      const safeTail = Number.isFinite(tail)
        ? Math.max(1, Math.min(5000, tail))
        : 200;
      const response = await fetch(
        `${apiBase}/sandboxes/${encodeURIComponent(id)}/logs?tail=${safeTail}`,
      );
      const payload = (await response.json()) as
        | SandboxLogsResponse
        | { detail?: string };
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
    <div className="sf-panel flex h-full flex-col overflow-hidden">
      <div className="sf-panel-header flex items-center justify-between px-6 py-5">
        <div className="flex items-center gap-3">
          <div className="sf-icon-badge sf-icon-badge-dark h-11 w-11">
            <Server className="h-5 w-5" />
          </div>
          <div>
            <h2 className="text-base font-extrabold tracking-tight text-[var(--sf-ink)]">
              Sandbox Console
            </h2>
            <p className="mt-1 text-[11px] text-[var(--sf-ink-muted)]">
              Container health, API reachability, and runtime logs
            </p>
          </div>
        </div>
      </div>

      <div className="grid flex-1 grid-cols-1 gap-4 overflow-hidden p-4 lg:grid-cols-[360px_minmax(0,1fr)]">
        <section className="sf-panel-muted flex flex-col gap-4 overflow-auto p-4">
          <div>
            <label className="mb-2 block text-[11px] font-bold tracking-[0.16em] text-[var(--sf-ink-muted)] uppercase">
              Sandbox ID
            </label>
            <input
              value={sandboxId}
              onChange={(event) => setSandboxId(event.target.value)}
              placeholder="Leave empty to auto-detect running sandbox"
              className="sf-input px-3 py-2 text-sm"
            />
          </div>

          <div>
            <div className="mb-2 flex items-center gap-2 text-[11px] font-bold tracking-[0.16em] text-[var(--sf-ink-muted)] uppercase">
              <Activity className="h-3.5 w-3.5" />
              Health
            </div>
            <button
              type="button"
              onClick={() => void fetchHealth()}
              disabled={healthLoading}
              className="sf-btn sf-btn-primary px-3 py-2 text-[10px]"
            >
              <RefreshCw
                className={`h-3.5 w-3.5 ${healthLoading ? "animate-spin" : ""}`}
              />
              {healthLoading ? "Checking..." : "Check Health"}
            </button>

            {healthError && (
              <div className="mt-3 rounded-[16px] border border-[rgba(207,63,83,0.14)] bg-[var(--sf-danger-soft)] px-3 py-2 text-xs text-[var(--sf-danger)]">
                {healthError}
              </div>
            )}

            {health && !healthError && (
              <div className="mt-3 space-y-2 rounded-[18px] border border-[var(--sf-border)] bg-white/[0.82] p-3 text-xs">
                <div className="flex items-center justify-between">
                  <span className="font-semibold text-[var(--sf-ink-soft)]">
                    Result
                  </span>
                  <span
                    className={`sf-chip px-2 py-1 text-[10px] ${
                      health.ok ? "sf-chip-green" : "sf-chip-red"
                    }`}
                  >
                    {health.ok ? "HEALTHY" : "UNHEALTHY"}
                  </span>
                </div>
                <div className="text-[var(--sf-ink-soft)]">
                  sandbox: {health.sandbox_id}
                </div>
                {health.sandbox_status && (
                  <div className="text-[var(--sf-ink-soft)]">
                    container: {health.sandbox_status}
                  </div>
                )}
                {typeof health.api_port === "number" && (
                  <div className="text-[var(--sf-ink-soft)]">
                    api_port: {health.api_port}
                  </div>
                )}
                {typeof health.status_code === "number" && (
                  <div className="text-[var(--sf-ink-soft)]">
                    status_code: {health.status_code}
                  </div>
                )}
                {health.reason && (
                  <div className="text-[var(--sf-danger)]">
                    reason: {health.reason}
                  </div>
                )}
                {health.error && (
                  <div className="text-[var(--sf-danger)]">
                    error: {health.error}
                  </div>
                )}
              </div>
            )}
          </div>

          <div>
            <div className="mb-2 flex items-center gap-2 text-[11px] font-bold tracking-[0.16em] text-[var(--sf-ink-muted)] uppercase">
              <ClipboardList className="h-3.5 w-3.5" />
              Log Options
            </div>
            <label className="mb-2 block text-[11px] text-[var(--sf-ink-muted)]">
              Tail lines
            </label>
            <input
              type="number"
              min={1}
              max={5000}
              value={tail}
              onChange={(event) => setTail(Number(event.target.value))}
              className="sf-input px-3 py-2 text-sm"
            />
            <button
              type="button"
              onClick={() => void fetchLogs()}
              disabled={logsLoading}
              className="sf-btn sf-btn-secondary mt-3 px-3 py-2 text-[10px]"
            >
              <RefreshCw
                className={`h-3.5 w-3.5 ${logsLoading ? "animate-spin" : ""}`}
              />
              {logsLoading ? "Loading..." : "Load Logs"}
            </button>

            {logsError && (
              <div className="mt-3 rounded-[16px] border border-[rgba(207,63,83,0.14)] bg-[var(--sf-danger-soft)] px-3 py-2 text-xs text-[var(--sf-danger)]">
                {logsError}
              </div>
            )}
          </div>
        </section>

        <section className="sf-terminal-panel min-h-0 overflow-hidden">
          <div className="border-b border-white/10 px-4 py-3 text-[11px] font-bold tracking-[0.16em] text-slate-300 uppercase">
            Sandbox Logs
          </div>
          <pre className="h-full overflow-auto p-4 font-mono text-[12px] leading-relaxed text-slate-200">
            {logs || "# no logs loaded"}
          </pre>
        </section>
      </div>
    </div>
  );
};
