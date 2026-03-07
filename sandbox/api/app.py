from __future__ import annotations

import asyncio
import os
import re
import signal
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel, ConfigDict, Field

from .tool_runtime import (
    ToolExecutionError,
    ToolInputError,
    append_bash_metadata,
    build_tool_error_result,
    edit_file,
    format_bash_result,
    grep_text,
    glob_paths,
    read_path,
    resolve_path,
    write_file,
)

DEFAULT_TIMEOUT_MS = int(os.getenv("SANDBOX_EXEC_TIMEOUT_MS", "120000"))
MAX_TIMEOUT_MS = int(os.getenv("SANDBOX_EXEC_MAX_TIMEOUT_MS", "3600000"))
UPLOAD_ROOT = Path(os.getenv("SANDBOX_UPLOAD_ROOT", "/config/uploads")).resolve()
RESULT_ARTIFACT_ROOT = Path(
    os.getenv("SANDBOX_RESULT_ARTIFACT_ROOT", "/config/tool-artifacts/results")
).resolve()

AUTO_GRANT_PERMISSIONS_ENV = "SANDBOX_BROWSER_AUTO_GRANT_PERMISSIONS"
AUTO_GRANT_PERMISSIONS_LIST_ENV = "SANDBOX_BROWSER_AUTO_GRANT_PERMISSION_LIST"
DEFAULT_AUTO_GRANT_PERMISSIONS = [
    "geolocation",
    "notifications",
    "camera",
    "microphone",
    "clipboard-read",
    "clipboard-write",
]
SUPPORTED_BROWSER_PERMISSIONS = {
    "geolocation",
    "midi",
    "midi-sysex",
    "notifications",
    "camera",
    "microphone",
    "background-sync",
    "ambient-light-sensor",
    "accelerometer",
    "gyroscope",
    "magnetometer",
    "accessibility-events",
    "clipboard-read",
    "clipboard-write",
    "payment-handler",
    "persistent-storage",
    "idle-detection",
}


def _safe_tool_name(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value or "").strip())
    normalized = normalized.strip("._-")
    return normalized or "tool"


def _artifact_path(tool_name: str, suffix: str) -> Path:
    folder = (RESULT_ARTIFACT_ROOT / _safe_tool_name(tool_name)).resolve()
    folder.mkdir(parents=True, exist_ok=True)
    filename = f"{time.time_ns()}_{os.getpid()}.{suffix}"
    return (folder / filename).resolve()


def _write_text_artifact(tool_name: str, text: str, *, suffix: str = "txt") -> str:
    out_path = _artifact_path(tool_name, suffix=suffix)
    out_path.write_text(text, encoding="utf-8")
    return str(out_path)


async def _kill_process_tree(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    if os.name != "nt":
        try:
            os.killpg(proc.pid, signal.SIGKILL)
            return
        except Exception:
            pass
    try:
        proc.kill()
    except Exception:
        pass


@asynccontextmanager
async def lifespan(_app: FastAPI):
    UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)
    RESULT_ARTIFACT_ROOT.mkdir(parents=True, exist_ok=True)
    yield


app = FastAPI(title="StewardFlow Sandbox API", version="0.3.0", lifespan=lifespan)


class StrictRequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BashRequest(StrictRequestModel):
    command: str = Field(..., min_length=1, description="Shell command string.")
    timeout: int = Field(default=DEFAULT_TIMEOUT_MS, ge=1, le=MAX_TIMEOUT_MS)
    workdir: str | None = Field(default=None, description="Working directory. Supports absolute or relative path.")
    description: str | None = Field(default=None, description="Optional command description.")


class GlobRequest(StrictRequestModel):
    pattern: str = Field(..., min_length=1)
    path: str = Field(default=".")


class ReadRequest(StrictRequestModel):
    filePath: str = Field(..., min_length=1)
    offset: int = Field(default=1)
    limit: int | None = Field(default=None)


class GrepRequest(StrictRequestModel):
    pattern: str = Field(..., min_length=1)
    path: str = Field(default=".")
    glob: str | None = None


class EditRequest(StrictRequestModel):
    filePath: str = Field(..., min_length=1)
    oldString: str
    newString: str
    replaceAll: bool = False


class WriteRequest(StrictRequestModel):
    content: str
    filePath: str = Field(..., min_length=1)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


def _tool_base_dir() -> Path:
    return Path.cwd().resolve()


def _artifact_writer(tool_name: str, text: str) -> str:
    return _write_text_artifact(tool_name, text, suffix="txt")


def _tool_error_payload(exc: Exception, *, metadata: dict[str, object] | None = None) -> dict[str, Any]:
    if isinstance(exc, ToolInputError):
        return build_tool_error_result(str(exc), metadata=metadata)
    if isinstance(exc, ToolExecutionError):
        return build_tool_error_result(str(exc), metadata=metadata)
    raise exc


async def _execute_bash_command(command: str, *, workdir: str | None, timeout_ms: int) -> dict[str, Any]:
    base_dir = _tool_base_dir()
    cwd_path = resolve_path(workdir or ".", base_dir=base_dir)
    if not cwd_path.exists() or not cwd_path.is_dir():
        raise ToolInputError(f"invalid workdir: {cwd_path}")

    spawn_kwargs: dict[str, Any] = {}
    if os.name != "nt":
        spawn_kwargs["preexec_fn"] = os.setsid

    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=str(cwd_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            **spawn_kwargs,
        )
    except Exception as exc:
        raise ToolExecutionError(f"spawn_failed: {exc}") from exc

    output_bytes = b""
    timed_out = False
    try:
        output_bytes, _ = await asyncio.wait_for(
            proc.communicate(),
            timeout=max(1, int(timeout_ms)) / 1000.0,
        )
    except asyncio.TimeoutError:
        timed_out = True
        await _kill_process_tree(proc)
        output_bytes, _ = await proc.communicate()

    output_text = output_bytes.decode("utf-8", errors="replace")
    if timed_out:
        output_text = append_bash_metadata(output_text, timeout_ms=timeout_ms)
    exit_code = None if proc.returncode is None else int(proc.returncode)
    return format_bash_result(
        output_text,
        exit_code=exit_code,
        artifact_writer=_artifact_writer,
    )


@app.post("/tools/bash")
async def tools_bash(req: BashRequest) -> dict[str, Any]:
    try:
        return await _execute_bash_command(
            req.command,
            workdir=req.workdir,
            timeout_ms=req.timeout,
        )
    except Exception as exc:
        return _tool_error_payload(exc, metadata={"exit_code": None})


@app.post("/tools/glob")
async def tools_glob(req: GlobRequest) -> dict[str, Any]:
    try:
        return await asyncio.to_thread(
            glob_paths,
            req.pattern,
            path=req.path,
            base_dir=_tool_base_dir(),
        )
    except Exception as exc:
        return _tool_error_payload(exc)


@app.post("/tools/read")
async def tools_read(req: ReadRequest) -> dict[str, Any]:
    try:
        return await asyncio.to_thread(
            read_path,
            req.filePath,
            offset=req.offset,
            limit=req.limit,
            base_dir=_tool_base_dir(),
        )
    except Exception as exc:
        return _tool_error_payload(exc)


@app.post("/tools/grep")
async def tools_grep(req: GrepRequest) -> dict[str, Any]:
    try:
        return await asyncio.to_thread(
            grep_text,
            req.pattern,
            path=req.path,
            glob=req.glob,
            base_dir=_tool_base_dir(),
        )
    except Exception as exc:
        return _tool_error_payload(exc)


@app.post("/tools/edit")
async def tools_edit(req: EditRequest) -> dict[str, Any]:
    try:
        return await asyncio.to_thread(
            edit_file,
            req.filePath,
            old_string=req.oldString,
            new_string=req.newString,
            replace_all=req.replaceAll,
            base_dir=_tool_base_dir(),
            artifact_writer=_artifact_writer,
        )
    except Exception as exc:
        return _tool_error_payload(exc)


@app.post("/tools/write")
async def tools_write(req: WriteRequest) -> dict[str, Any]:
    try:
        return await asyncio.to_thread(
            write_file,
            req.filePath,
            content=req.content,
            base_dir=_tool_base_dir(),
            artifact_writer=_artifact_writer,
        )
    except Exception as exc:
        return _tool_error_payload(exc)
