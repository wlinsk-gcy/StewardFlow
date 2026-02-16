from __future__ import annotations

import sys
import locale
import signal
import asyncio
import codecs
import json
import os
import platform
import shutil
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from .tool import Instance, Tool


DEFAULT_TIMEOUT_MS = int(os.getenv("PROC_RUN_DEFAULT_TIMEOUT_MS", str(2 * 60 * 1000)))
DEFAULT_MAX_BYTES = int(os.getenv("PROC_RUN_DEFAULT_MAX_BYTES", str(64 * 1024)))
DEFAULT_MAX_LINES = int(os.getenv("PROC_RUN_DEFAULT_MAX_LINES", "400"))
FULL_CAPTURE_MAX_BYTES = int(os.getenv("PROC_RUN_ARTIFACT_CAPTURE_MAX_BYTES", str(2 * 1024 * 1024)))

def pick_stream_encoding() -> str:
    # 非 Windows：绝大多数 shell 输出就是 UTF-8
    if not sys.platform.startswith("win"):
        return "utf-8"

    # Windows：dir/cmd 内置命令通常走 OEM code page（比如 936）
    try:
        import ctypes
        oem = ctypes.windll.kernel32.GetOEMCP()
        return f"cp{oem}"
    except Exception:
        # 兜底：系统首选编码
        return locale.getpreferredencoding(False) or "utf-8"

async def kill_process_tree(proc: asyncio.subprocess.Process) -> None:
    """
    Kill a process and its children (best-effort).
    - POSIX: kill process group
    - Windows: taskkill /T /F
    """
    if proc.returncode is not None:
        return

    sys = platform.system().lower()
    try:
        if sys != "windows":
            # If we started the process in a new process group, killpg works.
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except Exception:
                # fallback: kill only proc
                proc.kill()
        else:
            # taskkill kills child processes with /T
            await asyncio.create_subprocess_exec(
                "taskkill",
                "/PID",
                str(proc.pid),
                "/T",
                "/F",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _safe_int(value: Any, default: int, minimum: int = 1) -> int:
    try:
        v = int(value)
        if v < minimum:
            return default
        return v
    except Exception:
        return default


def _workspace_root() -> Path:
    return Path(Instance.directory).resolve()


def _resolve_workspace_path(raw_path: Optional[str]) -> Path:
    root = _workspace_root()
    candidate = Path(raw_path) if raw_path else root
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve()
    if not Instance.contains_path(str(resolved)):
        raise PermissionError(f"path_outside_workspace: {raw_path}")
    if resolved != root and root not in resolved.parents:
        raise PermissionError(f"path_outside_workspace: {raw_path}")
    return resolved


def _to_rel(path: Path) -> str:
    root = _workspace_root()
    try:
        return path.resolve().relative_to(root).as_posix()
    except Exception:
        return path.resolve().as_posix()


def _resolve_program_alias(program: str) -> str:
    raw = (program or "").strip()
    if not raw:
        return raw

    # Keep explicit paths unchanged.
    if os.path.isabs(raw) or any(sep in raw for sep in ("/", "\\")):
        return raw

    if shutil.which(raw):
        return raw

    lowered = raw.lower()
    if lowered not in {"powershell", "powershell.exe", "pwsh", "pwsh.exe"}:
        return raw

    for candidate in ("powershell", "powershell.exe", "pwsh", "pwsh.exe"):
        if shutil.which(candidate):
            return candidate

    return raw


class _StreamCapture:
    def __init__(self, max_bytes: int, max_lines: int, full_cap_bytes: int):
        self.max_bytes = max_bytes
        self.max_lines = max_lines
        self.full_cap_bytes = full_cap_bytes

        self.preview_parts: List[str] = []
        self.preview_bytes = 0
        self.preview_lines = 0

        self.total_bytes = 0
        self.total_lines = 0

        self.full_parts: List[str] = []
        self.full_bytes = 0
        self.full_capped = False
        self.truncated = False

    def _append_preview(self, text: str) -> None:
        segments = text.splitlines(keepends=True)
        if not segments:
            segments = [text]

        for seg in segments:
            if self.preview_bytes >= self.max_bytes or self.preview_lines >= self.max_lines:
                self.truncated = True
                return

            seg_bytes = seg.encode("utf-8", errors="replace")
            seg_line_count = seg.count("\n")

            if self.preview_lines + seg_line_count > self.max_lines:
                self.truncated = True
                return

            if self.preview_bytes + len(seg_bytes) <= self.max_bytes:
                self.preview_parts.append(seg)
                self.preview_bytes += len(seg_bytes)
                self.preview_lines += seg_line_count
                continue

            remain = self.max_bytes - self.preview_bytes
            if remain > 0:
                clipped = seg_bytes[:remain].decode("utf-8", errors="ignore")
                if clipped:
                    self.preview_parts.append(clipped)
                    self.preview_bytes += len(clipped.encode("utf-8", errors="replace"))
                    self.preview_lines += clipped.count("\n")
            self.truncated = True
            return

    def _append_full(self, text: str) -> None:
        raw = text.encode("utf-8", errors="replace")
        if self.full_bytes >= self.full_cap_bytes:
            self.full_capped = True
            return

        remain = self.full_cap_bytes - self.full_bytes
        if len(raw) <= remain:
            self.full_parts.append(text)
            self.full_bytes += len(raw)
            return

        clipped = raw[:remain].decode("utf-8", errors="ignore")
        if clipped:
            self.full_parts.append(clipped)
            self.full_bytes += len(clipped.encode("utf-8", errors="replace"))
        self.full_capped = True

    def ingest(self, text: str) -> None:
        if not text:
            return
        self.total_bytes += len(text.encode("utf-8", errors="replace"))
        self.total_lines += text.count("\n")
        self._append_full(text)
        self._append_preview(text)

    def preview(self) -> str:
        return "".join(self.preview_parts)

    def full(self) -> str:
        return "".join(self.full_parts)


def _build_response(
    ok: bool,
    exit_code: Optional[int],
    stdout: str = "",
    stderr: str = "",
    stdout_preview: str = "",
    stderr_preview: str = "",
    truncated: bool = False,
    error: Optional[str] = None,
) -> str:
    payload = {
        "ok": ok,
        "exit_code": exit_code,
        "stdout": stdout,
        "stderr": stderr,
        "stdout_preview": stdout_preview,
        "stderr_preview": stderr_preview,
        "truncated": truncated,
        "error": error,
    }
    return json.dumps(payload, ensure_ascii=False)


class ProcRunTool(Tool):
    def __init__(self):
        super().__init__()
        self.name = "proc_run"
        self.description = (
            "Run a program with argv (program + args[]) using exec-style subprocess; "
            "shell command strings are not allowed."
        )
        self.requires_confirmation = True

    async def execute(
        self,
        program: str,
        args: List[str],
        cwd: Optional[str] = None,
        timeout_ms: Optional[int] = None,
        env: Optional[Dict[str, str]] = None,
        max_bytes: Optional[int] = None,
        max_lines: Optional[int] = None,
        **kwargs,
    ) -> str:
        del kwargs
        if not isinstance(program, str) or not program.strip():
            return _build_response(ok=False, exit_code=None, error="invalid_program")
        if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
            return _build_response(ok=False, exit_code=None, error="invalid_args")
        program_to_run = _resolve_program_alias(program)

        try:
            cwd_path = _resolve_workspace_path(cwd or Instance.directory)
        except Exception as exc:
            return _build_response(ok=False, exit_code=None, error=str(exc))

        timeout = _safe_int(timeout_ms, DEFAULT_TIMEOUT_MS, minimum=1)
        preview_max_bytes = _safe_int(max_bytes, DEFAULT_MAX_BYTES, minimum=1)
        preview_max_lines = _safe_int(max_lines, DEFAULT_MAX_LINES, minimum=1)

        proc_env = os.environ.copy()
        if env is not None:
            if not isinstance(env, dict):
                return _build_response(ok=False, exit_code=None, error="invalid_env")
            for k, v in env.items():
                if not isinstance(k, str) or not isinstance(v, str):
                    return _build_response(ok=False, exit_code=None, error="invalid_env_entry")
            proc_env.update(env)

        spawn_kwargs: Dict[str, Any] = {
            "cwd": str(cwd_path),
            "env": proc_env,
            "stdout": asyncio.subprocess.PIPE,
            "stderr": asyncio.subprocess.PIPE,
        }
        if platform.system().lower() != "windows":
            spawn_kwargs["preexec_fn"] = os.setsid

        use_file_fallback = False
        stdout_file_handle = None
        stderr_file_handle = None
        stdout_file_path: Optional[str] = None
        stderr_file_path: Optional[str] = None

        try:
            proc = await asyncio.create_subprocess_exec(program_to_run, *args, **spawn_kwargs)
        except PermissionError as exc:
            # Some Windows runtime/sandbox environments deny asyncio PIPE handles.
            # Fallback still uses create_subprocess_exec, but redirects stdout/stderr to temp files.
            if platform.system().lower() != "windows":
                return _build_response(ok=False, exit_code=None, error=f"spawn_error: {str(exc)}")
            try:
                stdout_tmp = tempfile.NamedTemporaryFile(delete=False)
                stderr_tmp = tempfile.NamedTemporaryFile(delete=False)
                stdout_file_path = stdout_tmp.name
                stderr_file_path = stderr_tmp.name
                stdout_tmp.close()
                stderr_tmp.close()
                stdout_file_handle = open(stdout_file_path, "wb")
                stderr_file_handle = open(stderr_file_path, "wb")
                fallback_kwargs = dict(spawn_kwargs)
                fallback_kwargs["stdout"] = stdout_file_handle
                fallback_kwargs["stderr"] = stderr_file_handle
                proc = await asyncio.create_subprocess_exec(program_to_run, *args, **fallback_kwargs)
                use_file_fallback = True
            except Exception as fallback_exc:
                if stdout_file_handle:
                    stdout_file_handle.close()
                if stderr_file_handle:
                    stderr_file_handle.close()
                return _build_response(ok=False, exit_code=None, error=f"spawn_error: {str(fallback_exc)}")
        except Exception as exc:
            return _build_response(ok=False, exit_code=None, error=f"spawn_error: {str(exc)}")

        out_state = _StreamCapture(
            max_bytes=preview_max_bytes,
            max_lines=preview_max_lines,
            full_cap_bytes=FULL_CAPTURE_MAX_BYTES,
        )
        err_state = _StreamCapture(
            max_bytes=preview_max_bytes,
            max_lines=preview_max_lines,
            full_cap_bytes=FULL_CAPTURE_MAX_BYTES,
        )
        enc = pick_stream_encoding()

        async def read_stream(stream: Optional[asyncio.StreamReader], state: _StreamCapture) -> None:
            if stream is None:
                return
            decoder = codecs.getincrementaldecoder(enc)(errors="replace")
            while True:
                chunk = await stream.read(4096)
                if not chunk:
                    break
                text = decoder.decode(chunk)
                if text:
                    state.ingest(text)
            final_text = decoder.decode(b"", final=True)
            if final_text:
                state.ingest(final_text)

        out_task = None
        err_task = None
        if not use_file_fallback:
            out_task = asyncio.create_task(read_stream(proc.stdout, out_state))
            err_task = asyncio.create_task(read_stream(proc.stderr, err_state))

        timed_out = False
        error_msg: Optional[str] = None
        try:
            await asyncio.wait_for(proc.wait(), timeout=timeout / 1000.0)
        except asyncio.TimeoutError:
            timed_out = True
            error_msg = f"proc_run timeout after {timeout} ms"
            await kill_process_tree(proc)
        finally:
            if out_task and err_task:
                await asyncio.gather(out_task, err_task, return_exceptions=True)
            if proc.returncode is None:
                try:
                    await asyncio.wait_for(proc.wait(), timeout=2.0)
                except Exception:
                    pass
            if stdout_file_handle:
                try:
                    stdout_file_handle.close()
                except Exception:
                    pass
            if stderr_file_handle:
                try:
                    stderr_file_handle.close()
                except Exception:
                    pass

        if use_file_fallback:
            try:
                if stdout_file_path and os.path.exists(stdout_file_path):
                    with open(stdout_file_path, "r", encoding="utf-8", errors="replace") as f:
                        out_state.ingest(f.read())
                if stderr_file_path and os.path.exists(stderr_file_path):
                    with open(stderr_file_path, "r", encoding="utf-8", errors="replace") as f:
                        err_state.ingest(f.read())
            finally:
                if stdout_file_path and os.path.exists(stdout_file_path):
                    try:
                        os.remove(stdout_file_path)
                    except Exception:
                        pass
                if stderr_file_path and os.path.exists(stderr_file_path):
                    try:
                        os.remove(stderr_file_path)
                    except Exception:
                        pass

        exit_code = proc.returncode if proc.returncode is not None else -1
        if not timed_out and exit_code != 0:
            error_msg = f"process_exit_non_zero: {exit_code}"

        truncated = (
            out_state.truncated
            or err_state.truncated
            or out_state.full_capped
            or err_state.full_capped
        )

        return _build_response(
            ok=(not timed_out and exit_code == 0),
            exit_code=exit_code,
            stdout=out_state.full(),
            stderr=err_state.full(),
            stdout_preview=out_state.preview(),
            stderr_preview=err_state.preview(),
            truncated=truncated,
            error=error_msg,
        )

    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "program": {"type": "string", "description": "Executable name or path."},
                        "args": {"type": "array", "items": {"type": "string"}, "description": "Argument vector."},
                        "cwd": {"type": "string", "description": "Working directory (must be inside workspace)."},
                        "timeout_ms": {"type": "integer", "default": DEFAULT_TIMEOUT_MS, "minimum": 1, "maximum": 3600000},
                        "env": {
                            "type": "object",
                            "description": "Optional environment variable overrides.",
                            "additionalProperties": {"type": "string"},
                        },
                        "max_bytes": {"type": "integer", "default": DEFAULT_MAX_BYTES, "minimum": 1, "maximum": 1048576},
                        "max_lines": {"type": "integer", "default": DEFAULT_MAX_LINES, "minimum": 1, "maximum": 5000},
                    },
                    "required": ["program", "args"],
                    "additionalProperties": False,
                },
                "strict": True,
            },
        }
