from __future__ import annotations

import asyncio
import os
import re
import signal
import subprocess
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel, ConfigDict, Field

from . import browser_runtime
from .browser_state import get_browser_state
from .tool_runtime import (
    ToolExecutionError,
    ToolInputError,
    append_bash_metadata,
    build_tool_error_result,
    edit_file,
    format_background_bash_result,
    format_bash_result,
    shape_background_bash_early_output,
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
BACKGROUND_LAUNCH_TIMEOUT_MS = 2000
BACKGROUND_EARLY_EXIT_WINDOW_MS = 300


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


def _write_binary_artifact(tool_name: str, data: bytes, *, suffix: str) -> str:
    out_path = _artifact_path(tool_name, suffix=suffix)
    out_path.write_bytes(data)
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
    _background_job_root().mkdir(parents=True, exist_ok=True)
    try:
        yield
    finally:
        await get_browser_state().shutdown()


app = FastAPI(title="StewardFlow Sandbox API", version="0.3.0", lifespan=lifespan)


class StrictRequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BashRequest(StrictRequestModel):
    command: str = Field(..., min_length=1, description="Shell command string.")
    timeout: int = Field(default=DEFAULT_TIMEOUT_MS, ge=1, le=MAX_TIMEOUT_MS)
    workdir: str | None = Field(default=None, description="Working directory. Supports absolute or relative path.")
    description: str | None = Field(default=None, description="Optional command description.")
    background: bool = Field(default=False, description="Launch in background mode and return early.")


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


class NavigatePageRequest(StrictRequestModel):
    type: str = Field(default="url")
    url: str | None = Field(default=None)
    timeout: int = Field(default=30000, ge=1, le=MAX_TIMEOUT_MS)
    waitUntil: str = Field(default="domcontentloaded")


class TakeSnapshotRequest(StrictRequestModel):
    verbose: bool = Field(default=False)
    filePath: str | None = Field(default=None)


class EvaluateScriptRequest(StrictRequestModel):
    script: str = Field(..., min_length=1)
    pageId: int | None = Field(default=None, ge=0)
    documentId: str | None = Field(default=None)
    timeout: int = Field(default=browser_runtime.DEFAULT_EVALUATE_SCRIPT_TIMEOUT_MS, ge=1, le=MAX_TIMEOUT_MS)


class ClickRequest(StrictRequestModel):
    uid: str = Field(..., min_length=1)
    dblClick: bool = Field(default=False)
    includeSnapshot: bool = Field(default=False)


class FillRequest(StrictRequestModel):
    uid: str = Field(..., min_length=1)
    value: str
    includeSnapshot: bool = Field(default=False)


class WaitForRequest(StrictRequestModel):
    text: list[str] = Field(..., min_length=1)
    timeout: int = Field(default=30000, ge=1, le=MAX_TIMEOUT_MS)


class TakeScreenshotRequest(StrictRequestModel):
    uid: str | None = Field(default=None)
    filePath: str | None = Field(default=None)
    format: str = Field(default="png")
    fullPage: bool = Field(default=True)
    quality: int | None = Field(default=None, ge=0, le=100)


class PressKeyRequest(StrictRequestModel):
    key: str = Field(..., min_length=1)
    includeSnapshot: bool = Field(default=False)


class HandleDialogRequest(StrictRequestModel):
    action: str = Field(default="accept")
    promptText: str | None = Field(default=None)


class HoverRequest(StrictRequestModel):
    uid: str = Field(..., min_length=1)
    includeSnapshot: bool = Field(default=False)


class UploadFileRequest(StrictRequestModel):
    uid: str = Field(..., min_length=1)
    filePath: str = Field(..., min_length=1)
    includeSnapshot: bool = Field(default=False)


class SelectPageRequest(StrictRequestModel):
    pageId: int = Field(..., ge=0)
    bringToFront: bool = Field(default=False)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


def _tool_base_dir() -> Path:
    return Path.cwd().resolve()


def _artifact_writer(tool_name: str, text: str) -> str:
    return _write_text_artifact(tool_name, text, suffix="txt")


def _binary_artifact_writer(tool_name: str, data: bytes, suffix: str) -> str:
    return _write_binary_artifact(tool_name, data, suffix=suffix)


def _background_job_root() -> Path:
    return (RESULT_ARTIFACT_ROOT / "jobs").resolve()


def _background_job_id() -> str:
    return f"job_{time.time_ns()}_{uuid.uuid4().hex[:8]}"


def _background_log_path(job_id: str) -> Path:
    return (_background_job_root() / f"{job_id}.log").resolve()


def _read_background_log_excerpt(log_path: Path) -> str:
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return shape_background_bash_early_output(text)


def _spawn_background_process(command: str, *, cwd: Path, log_path: Path) -> tuple[subprocess.Popen[str], Any]:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_handle = open(log_path, "a", encoding="utf-8")
    kwargs: dict[str, Any] = {
        "shell": True,
        "cwd": str(cwd),
        "stdin": subprocess.DEVNULL,
        "stdout": log_handle,
        "stderr": subprocess.STDOUT,
        "text": True,
    }
    if os.name == "nt":
        kwargs["creationflags"] = (
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "DETACHED_PROCESS", 0)
        )
    else:
        kwargs["start_new_session"] = True
    try:
        proc = subprocess.Popen(command, **kwargs)
    except Exception:
        log_handle.close()
        raise
    return proc, log_handle


def _tool_error_payload(exc: Exception, *, metadata: dict[str, object] | None = None) -> dict[str, Any]:
    return build_tool_error_result(str(exc), metadata=metadata)
    # if isinstance(exc, ToolInputError):
    #     return build_tool_error_result(str(exc), metadata=metadata)
    # if isinstance(exc, ToolExecutionError):
    #     return build_tool_error_result(str(exc), metadata=metadata)
    # raise exc


async def _execute_foreground_bash_command(command: str, *, cwd_path: Path, timeout_ms: int) -> dict[str, Any]:
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


async def _execute_background_bash_command(command: str, *, cwd_path: Path) -> dict[str, Any]:
    job_id = _background_job_id()
    log_path = _background_log_path(job_id)
    try:
        proc, log_handle = await asyncio.wait_for(
            asyncio.to_thread(_spawn_background_process, command, cwd=cwd_path, log_path=log_path),
            timeout=BACKGROUND_LAUNCH_TIMEOUT_MS / 1000.0,
        )
    except asyncio.TimeoutError:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.touch(exist_ok=True)
        return format_background_bash_result(
            status="launch_timeout",
            command=command,
            workdir=str(cwd_path),
            job_id=job_id,
            pid=None,
            log_path=str(log_path),
            exit_code=None,
            artifact_writer=_artifact_writer,
        )
    except Exception as exc:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(str(exc), encoding="utf-8")
        return format_background_bash_result(
            status="failed_to_launch",
            command=command,
            workdir=str(cwd_path),
            job_id=job_id,
            pid=None,
            log_path=str(log_path),
            exit_code=None,
            artifact_writer=_artifact_writer,
            early_output=str(exc),
        )

    log_handle.close()
    await asyncio.sleep(BACKGROUND_EARLY_EXIT_WINDOW_MS / 1000.0)
    exit_code = proc.poll()
    if exit_code is None:
        return format_background_bash_result(
            status="launched_unverified",
            command=command,
            workdir=str(cwd_path),
            job_id=job_id,
            pid=proc.pid,
            log_path=str(log_path),
            exit_code=None,
            artifact_writer=_artifact_writer,
        )

    return format_background_bash_result(
        status="exited_early",
        command=command,
        workdir=str(cwd_path),
        job_id=job_id,
        pid=proc.pid,
        log_path=str(log_path),
        exit_code=int(exit_code),
        artifact_writer=_artifact_writer,
        early_output=_read_background_log_excerpt(log_path),
    )


async def _execute_bash_command(
    command: str,
    *,
    workdir: str | None,
    timeout_ms: int,
    background: bool = False,
) -> dict[str, Any]:
    base_dir = _tool_base_dir()
    cwd_path = resolve_path(workdir or ".", base_dir=base_dir)
    if not cwd_path.exists() or not cwd_path.is_dir():
        raise ToolInputError(f"invalid workdir: {cwd_path}")
    if background:
        return await _execute_background_bash_command(command, cwd_path=cwd_path)
    return await _execute_foreground_bash_command(command, cwd_path=cwd_path, timeout_ms=timeout_ms)


@app.post("/tools/bash")
async def tools_bash(req: BashRequest) -> dict[str, Any]:
    try:
        return await _execute_bash_command(
            req.command,
            workdir=req.workdir,
            timeout_ms=req.timeout,
            background=req.background,
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


@app.post("/tools/navigate_page")
async def tools_navigate_page(req: NavigatePageRequest) -> dict[str, Any]:
    try:
        return await browser_runtime.navigate_page(
            state=get_browser_state(),
            navigation_type=req.type,
            url=req.url,
            timeout_ms=req.timeout,
            wait_until=req.waitUntil,
            artifact_writer=_artifact_writer,
        )
    except Exception as exc:
        return _tool_error_payload(exc)


@app.post("/tools/take_snapshot")
async def tools_take_snapshot(req: TakeSnapshotRequest) -> dict[str, Any]:
    try:
        return await browser_runtime.take_snapshot(
            state=get_browser_state(),
            verbose=req.verbose,
            file_path=req.filePath,
            base_dir=_tool_base_dir(),
            artifact_writer=_artifact_writer,
        )
    except Exception as exc:
        return _tool_error_payload(exc)


@app.post("/tools/evaluate_script")
async def tools_evaluate_script(req: EvaluateScriptRequest) -> dict[str, Any]:
    try:
        return await browser_runtime.evaluate_script(
            state=get_browser_state(),
            script=req.script,
            page_id=req.pageId,
            document_id=req.documentId,
            timeout_ms=req.timeout,
            artifact_writer=_artifact_writer,
        )
    except Exception as exc:
        return _tool_error_payload(exc)


@app.post("/tools/click")
async def tools_click(req: ClickRequest) -> dict[str, Any]:
    try:
        return await browser_runtime.click(
            state=get_browser_state(),
            uid=req.uid,
            include_snapshot=req.includeSnapshot,
            dbl_click=req.dblClick,
            artifact_writer=_artifact_writer,
        )
    except Exception as exc:
        return _tool_error_payload(exc)


@app.post("/tools/fill")
async def tools_fill(req: FillRequest) -> dict[str, Any]:
    try:
        return await browser_runtime.fill(
            state=get_browser_state(),
            uid=req.uid,
            value=req.value,
            include_snapshot=req.includeSnapshot,
            artifact_writer=_artifact_writer,
        )
    except Exception as exc:
        return _tool_error_payload(exc)


@app.post("/tools/wait_for")
async def tools_wait_for(req: WaitForRequest) -> dict[str, Any]:
    try:
        return await browser_runtime.wait_for(
            state=get_browser_state(),
            text=req.text,
            timeout_ms=req.timeout,
            artifact_writer=_artifact_writer,
        )
    except Exception as exc:
        return _tool_error_payload(exc)


@app.post("/tools/take_screenshot")
async def tools_take_screenshot(req: TakeScreenshotRequest) -> dict[str, Any]:
    try:
        return await browser_runtime.take_screenshot(
            state=get_browser_state(),
            uid=req.uid,
            file_path=req.filePath,
            image_format=req.format,
            full_page=req.fullPage,
            quality=req.quality,
            base_dir=_tool_base_dir(),
            binary_artifact_writer=_binary_artifact_writer,
        )
    except Exception as exc:
        return _tool_error_payload(exc)


@app.post("/tools/press_key")
async def tools_press_key(req: PressKeyRequest) -> dict[str, Any]:
    try:
        return await browser_runtime.press_key(
            state=get_browser_state(),
            key=req.key,
            include_snapshot=req.includeSnapshot,
            artifact_writer=_artifact_writer,
        )
    except Exception as exc:
        return _tool_error_payload(exc)


@app.post("/tools/handle_dialog")
async def tools_handle_dialog(req: HandleDialogRequest) -> dict[str, Any]:
    try:
        return await browser_runtime.handle_dialog(
            state=get_browser_state(),
            action=req.action,
            prompt_text=req.promptText,
            artifact_writer=_artifact_writer,
        )
    except Exception as exc:
        return _tool_error_payload(exc)


@app.post("/tools/hover")
async def tools_hover(req: HoverRequest) -> dict[str, Any]:
    try:
        return await browser_runtime.hover(
            state=get_browser_state(),
            uid=req.uid,
            include_snapshot=req.includeSnapshot,
            artifact_writer=_artifact_writer,
        )
    except Exception as exc:
        return _tool_error_payload(exc)


@app.post("/tools/upload_file")
async def tools_upload_file(req: UploadFileRequest) -> dict[str, Any]:
    try:
        return await browser_runtime.upload_file(
            state=get_browser_state(),
            uid=req.uid,
            file_path=req.filePath,
            include_snapshot=req.includeSnapshot,
            base_dir=_tool_base_dir(),
            artifact_writer=_artifact_writer,
        )
    except Exception as exc:
        return _tool_error_payload(exc)


@app.post("/tools/select_page")
async def tools_select_page(req: SelectPageRequest) -> dict[str, Any]:
    try:
        return await browser_runtime.select_page(
            state=get_browser_state(),
            page_id=req.pageId,
            bring_to_front=req.bringToFront,
            artifact_writer=_artifact_writer,
        )
    except Exception as exc:
        return _tool_error_payload(exc)


@app.post("/browser/reset")
async def browser_reset() -> dict[str, Any]:
    try:
        return await browser_runtime.reset_browser(
            state=get_browser_state(),
            artifact_writer=_artifact_writer,
        )
    except Exception as exc:
        return _tool_error_payload(exc)
