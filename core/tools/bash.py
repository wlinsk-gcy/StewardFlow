import re
import json
import platform
import shutil
import subprocess
from typing import Optional, Tuple, List

from .tool import Tool

UNIX_CMDS = {
    "ls","cat","grep","awk","sed","cut","find","xargs","head","tail",
    "cp","mv","rm","chmod","chown","which","ps","kill","tar","ssh"
}

WIN_HINT_CMDS = {
    "dir","cd","ipconfig","tasklist","whoami","systeminfo","type","copy","move","del"
}

def needs_bash(command: str) -> bool:
    s = command.strip()

    # 明显的 bash 语法/路径/变量
    bash_signals = [
        "~/" , "/etc/", "/usr/", "/bin/", "/var/",
        "$HOME", "$PATH", "${", "$(",
        ">/dev/null", "2>&1", "&&", "||", "|", ";", "source ", ". ",
        "chmod ", "chown ", "./", ".sh"
    ]
    if any(sig in s for sig in bash_signals):
        return True

    # 以 unix 命令开头
    first = re.split(r"\s+", s, maxsplit=1)[0]
    if first in UNIX_CMDS:
        return True

    return False

def looks_like_windows_cmd(command: str) -> bool:
    s = command.strip().lower()
    first = re.split(r"\s+", s, maxsplit=1)[0]
    return first in WIN_HINT_CMDS or s.startswith("powershell") or s.startswith("pwsh")




class BashTool(Tool):
    def __init__(self):
        super().__init__()
        self.name = "bash"
        self.description = (
            "Execute a bash command on the local machine. "
            "On Windows, runs cmd/PowerShell for Windows-native commands; "
            "uses bash/WSL only when needed."
            "Requires explicit user confirmation for every command."
        )
        self.requires_confirmation = True

    def _resolve_bash(self, command: str) -> Tuple[Optional[List[str]], bool, str]:
        """
        :param command:
        :return: cmd, uses_wsl, shell_kind
        """
        system = platform.system().lower()
        if system == "windows":
            # 1) 如果不像 bash 命令，直接走 Windows shell（更合理、更稳定）
            if not needs_bash(command) and looks_like_windows_cmd(command):
                # 用 cmd.exe 执行（支持内置命令 dir/cd 等）
                return ["cmd.exe", "/c", command], False, "cmd"

            # 2) 需要 bash：优先找 Git Bash / MSYS bash
            bash_path = shutil.which("bash")
            if bash_path:
                return [bash_path, "-lc", command], False, "bash"

            # 3) 没有本地 bash 就走 WSL
            wsl_path = shutil.which("wsl")
            if wsl_path:
                return [wsl_path, "bash", "-lc", command], True, "wsl"

            # 4) 既不是明显 Windows 命令，又没 bash/wsl：给出缺失
            return None, False, "missing"

            # 非 Windows：直接 bash
        bash_path = shutil.which("bash") or "/bin/bash"
        return [bash_path, "-lc", command], False, "bash"

    def execute(self, command: str, cwd: Optional[str] = None, timeout_sec: Optional[int] = 60, **kwargs) -> str:
        cmd, uses_wsl, shell_kind = self._resolve_bash(command)
        if not cmd:
            raise RuntimeError(
                "bash_unavailable: No bash/wsl found for a bash-like command. "
                "Install Git Bash or enable WSL, or use a Windows-native command."
            )

        if uses_wsl and cwd:
            raise RuntimeError("cwd is not supported when running via WSL in this tool.")

        try:
            completed = subprocess.run(
                cmd,
                cwd=cwd,
                capture_output=True,
                text=False,
                timeout=timeout_sec,
                check=False
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"Command timed out after {timeout_sec} seconds.")
        except Exception as e:
            raise RuntimeError(f"Failed to execute bash command: {str(e)}")

        stdout_text, stderr_text = self._decode_output(completed.stdout, completed.stderr, shell_kind)
        result = {
            "platform": platform.system(),
            "shell": shell_kind,
            "exit_code": completed.returncode,
            "stdout": stdout_text,
            "stderr": stderr_text,
        }
        return json.dumps(result, ensure_ascii=False)

    def _decode_bytes_smart(self, data: bytes, encodings: list[str]) -> str:
        if not data:
            return ""
        for enc in encodings:
            try:
                return data.decode(enc)
            except UnicodeDecodeError:
                continue
        # 最后兜底：不报错，替换非法字符
        return data.decode(encodings[0], errors="replace")

    def _decode_output(self, stdout: bytes, stderr: bytes, shell_kind: str):
        if shell_kind == "wsl":
            # wsl 常见：utf-16le（有时也可能 utf-8）
            encs = ["utf-16le", "utf-8", "gbk"]
        elif shell_kind == "cmd":
            # cmd 常见：gbk，但有时是 utf-8（chcp 65001）
            encs = ["gbk", "utf-8", "utf-16le"]
        else:
            # bash 通常 utf-8
            encs = ["utf-8", "gbk", "utf-16le"]

        return (
            self._decode_bytes_smart(stdout, encs),
            self._decode_bytes_smart(stderr, encs),
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
                        "command": {
                            "type": "string",
                            "description": "The bash command to execute."
                        },
                        "cwd": {
                            "type": "string",
                            "description": "Optional working directory for the command."
                        },
                        "timeout_sec": {
                            "type": "integer",
                            "description": "Optional timeout in seconds."
                        }
                    },
                    "required": ["command"],
                    "additionalProperties": False
                },
                "strict": True
            }
        }
