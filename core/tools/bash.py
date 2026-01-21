from pathlib import Path
import os
import platform
from typing import Optional
import asyncio
import sys
import locale
import codecs
import signal
import json

from .tool import Tool

DEFAULT_TIMEOUT_MS = int(os.getenv("BASH_DEFAULT_TIMEOUT_MS", str(2 * 60 * 1000)))




class Instance:
    # Change this to your sandbox dir
    directory: str = str(Path.cwd())

    @staticmethod
    def contains_path(p: str) -> bool:
        """
        Whether path is inside sandbox root.
        Replace with your real policy.
        """
        root = Path(Instance.directory).resolve()
        try:
            return root in Path(p).resolve().parents or Path(p).resolve() == root
        except Exception:
            return False

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

class BashTool(Tool):
    def __init__(self):
        super().__init__()
        self.name = "bash"
        os_name = platform.system()
        self.description = (
            f"Execute a bash command on the local machine (os={os_name}). "
            "On Windows, runs cmd/PowerShell for Windows-native commands; "
            "uses bash/WSL only when needed. "
        )
        self.requires_confirmation = True

    async def execute(self, command: str, cwd: Optional[str] = None, **kwargs) -> str:
        cwd = cwd or Instance.directory
        timeout_ms = DEFAULT_TIMEOUT_MS

        sys = platform.system().lower()
        preexec_fn = os.setsid if sys != "windows" else None

        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                cwd=cwd,
                env=os.environ.copy(),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                preexec_fn=preexec_fn,  # type: ignore[arg-type]
            )
        except Exception as e:
            print(str(e))
            raise e
        output = ""
        timed_out = False
        async def read_stream(stream: Optional[asyncio.StreamReader], encoding: str) -> None:
            nonlocal output
            if stream is None:
                return

            decoder = codecs.getincrementaldecoder(encoding)(errors="replace")
            while True:
                chunk = await stream.read(4096)
                if not chunk:
                    break
                output += decoder.decode(chunk)
            # flush 可能残留的半个字符
            output += decoder.decode(b"", final=True)

        enc = pick_stream_encoding()
        # Concurrent stdout/stderr
        out_task = asyncio.create_task(read_stream(proc.stdout,enc))
        err_task = asyncio.create_task(read_stream(proc.stderr,enc))

        try:
            await asyncio.wait_for(proc.wait(), timeout=timeout_ms / 1000.0)
        except asyncio.TimeoutError:
            timed_out = True
            await kill_process_tree(proc)
        finally:
            # ensure streams drained
            await asyncio.gather(out_task, err_task, return_exceptions=True)
        err_msg = ""
        if timed_out:
            err_msg = f"bash tool terminated command after exceeding timeout {timeout_ms} ms"

        return json.dumps({"output": output, "error": err_msg})

    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "command": {
                            "type": "string",
                            "description": "The bash command to execute."
                        },
                        "cwd": {
                            "type": "string",
                            "description": "Optional working directory for the command."
                        }
                    },
                    "required": ["command"],
                    "additionalProperties": False
                },
                "strict": True
            }
        }
