from __future__ import annotations

import base64
import logging
import os
import shlex
import threading
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
from dataclasses import asdict, is_dataclass
from typing import Any, Callable, Mapping

logger = logging.getLogger(__name__)

_RUNNING_STATES = {"started", "running", "ready", "active", "up"}
_URL_KEYS = ("vnc_url", "url", "preview_url", "link")
_TOKEN_KEYS = ("token", "preview_token")


def _to_plain(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (bytes, bytearray)):
        return {
            "encoding": "base64",
            "content": base64.b64encode(bytes(value)).decode("ascii"),
        }
    if isinstance(value, Mapping):
        return {str(k): _to_plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_to_plain(v) for v in value]
    if is_dataclass(value):
        return _to_plain(asdict(value))
    if hasattr(value, "model_dump"):
        try:
            return _to_plain(value.model_dump())
        except Exception:
            pass
    if hasattr(value, "dict"):
        try:
            return _to_plain(value.dict())
        except Exception:
            pass
    if hasattr(value, "to_dict"):
        try:
            return _to_plain(value.to_dict())
        except Exception:
            pass
    if hasattr(value, "__dict__"):
        return {
            str(k): _to_plain(v)
            for k, v in vars(value).items()
            if not str(k).startswith("_")
        }
    return str(value)


def _extract_value(payload: Any, keys: tuple[str, ...]) -> Any:
    obj = _to_plain(payload)
    if isinstance(obj, Mapping):
        for key in keys:
            val = obj.get(key)
            if val:
                return val
        for val in obj.values():
            nested = _extract_value(val, keys)
            if nested:
                return nested
    if isinstance(obj, list):
        for item in obj:
            nested = _extract_value(item, keys)
            if nested:
                return nested
    return None


def _normalize_state(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip().lower()
    if hasattr(value, "value"):
        return str(getattr(value, "value")).strip().lower()
    if isinstance(value, Mapping):
        for key in ("state", "status"):
            if key in value:
                return _normalize_state(value.get(key))
    return str(value).strip().lower()


def _is_component_running(status: Any) -> bool:
    if status is None:
        return False
    if isinstance(status, bool):
        return status
    if isinstance(status, Mapping):
        for key in ("running", "is_running", "started", "active", "ready"):
            if key in status and bool(status.get(key)):
                return True
        state = _normalize_state(status)
        return state in _RUNNING_STATES
    for key in ("running", "is_running", "started", "active", "ready"):
        if hasattr(status, key) and bool(getattr(status, key)):
            return True
    state = _normalize_state(status)
    return state in _RUNNING_STATES


def _call_with_optional_kwargs(method: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    filtered = {k: v for k, v in kwargs.items() if v is not None}
    if not filtered:
        return method(*args)
    try:
        return method(*args, **filtered)
    except TypeError:
        return method(*args)


class DaytonaSandboxManager:
    def __init__(
        self,
        *,
        daytona_client: Any | None = None,
        auto_stop_minutes: int = 15,
        vnc_port: int = 6080,
        vnc_url_ttl_seconds: int = 3600,
        api_key: str | None = None,
        target: str | None = None,
        server_url: str | None = None,
    ) -> None:
        self.auto_stop_minutes = max(1, int(auto_stop_minutes))
        self.vnc_port = int(vnc_port)
        self.vnc_url_ttl_seconds = max(60, int(vnc_url_ttl_seconds))
        self.api_key = api_key
        self.target = target
        self.server_url = server_url
        self._client = daytona_client
        self._trace_to_sandbox: dict[str, str] = {}
        self._lock = threading.RLock()

    def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            from daytona import Daytona  # type: ignore
        except Exception as exc:
            raise RuntimeError(
                "daytona sdk is not installed. Run: pip install daytona"
            ) from exc

        if self.api_key and not os.getenv("DAYTONA_API_KEY"):
            os.environ["DAYTONA_API_KEY"] = self.api_key
        if self.target and not os.getenv("DAYTONA_TARGET"):
            os.environ["DAYTONA_TARGET"] = self.target
        if self.server_url and not os.getenv("DAYTONA_SERVER_URL"):
            os.environ["DAYTONA_SERVER_URL"] = self.server_url

        self._client = Daytona()
        return self._client

    def _set_autostop(self, sandbox: Any) -> None:
        try:
            sandbox.set_autostop_interval(self.auto_stop_minutes)
        except Exception as exc:
            logger.warning("Failed to set sandbox auto-stop interval: %s", exc)

    def _find_sandbox(self, sandbox_id: str) -> Any | None:
        client = self._ensure_client()
        try:
            return client.find_one(sandbox_id)
        except Exception:
            return None

    def _is_sandbox_running(self, sandbox: Any) -> bool:
        return _normalize_state(getattr(sandbox, "state", None)) in _RUNNING_STATES

    def _ensure_running(self, sandbox: Any) -> Any:
        if self._is_sandbox_running(sandbox):
            return sandbox
        sandbox.start()
        return sandbox

    def _create_sandbox(self) -> Any:
        client = self._ensure_client()
        sandbox = client.create()
        self._set_autostop(sandbox)
        return sandbox

    def _resolve_sandbox_for_trace(self, trace_id: str) -> Any:
        sandbox_id = self._trace_to_sandbox.get(trace_id)
        if sandbox_id:
            sandbox = self._find_sandbox(sandbox_id)
            if sandbox is not None:
                return sandbox
            self._trace_to_sandbox.pop(trace_id, None)

        sandbox = self._create_sandbox()
        sandbox_id = getattr(sandbox, "id", None)
        if not sandbox_id:
            raise RuntimeError("Daytona sandbox has no id")
        self._trace_to_sandbox[trace_id] = str(sandbox_id)
        return sandbox

    def _ensure_computer_use_started(self, sandbox: Any) -> None:
        computer_use = getattr(sandbox, "computer_use", None)
        if computer_use is None:
            raise RuntimeError("Daytona sandbox does not expose computer_use")

        status = None
        try:
            status = computer_use.get_status()
        except Exception:
            status = None

        if _is_component_running(status):
            return

        try:
            computer_use.start()
        except Exception:
            status = computer_use.get_status()
            if not _is_component_running(status):
                raise

    def ensure_sandbox(self, trace_id: str, *, require_computer_use: bool = False) -> Any:
        if not trace_id:
            raise RuntimeError("trace_id is required for Daytona sandbox operations")
        with self._lock:
            sandbox = self._resolve_sandbox_for_trace(trace_id)
            sandbox = self._ensure_running(sandbox)
            self._set_autostop(sandbox)
            if require_computer_use:
                self._ensure_computer_use_started(sandbox)
            return sandbox

    def _build_vnc_view(self, sandbox: Any) -> dict[str, Any]:
        signed_payload = None
        try:
            signed_payload = sandbox.create_signed_preview_url(
                self.vnc_port,
                expires_in_seconds=self.vnc_url_ttl_seconds,
            )
        except Exception:
            signed_payload = None

        vnc_url = _extract_value(signed_payload, _URL_KEYS)
        vnc_token = _extract_value(signed_payload, _TOKEN_KEYS)
        if not vnc_url:
            preview_payload = sandbox.get_preview_link(self.vnc_port)
            vnc_url = _extract_value(preview_payload, _URL_KEYS)
            vnc_token = vnc_token or _extract_value(preview_payload, _TOKEN_KEYS)

        if not vnc_url:
            raise RuntimeError("Failed to get Daytona VNC preview URL")

        try:
            parsed = urlparse(vnc_url)
            if parsed.scheme in {"http", "https"} and (parsed.path in {"", "/"}):
                parsed = parsed._replace(path="/vnc_auto.html")
            query = dict(parse_qsl(parsed.query, keep_blank_values=True))
            query.setdefault("autoconnect", "true")
            query.setdefault("reconnect", "true")
            query.setdefault("resize", "remote")
            parsed = parsed._replace(query=urlencode(query, doseq=True))
            vnc_url = urlunparse(parsed)
        except Exception:
            pass

        payload = {
            "sandbox_id": getattr(sandbox, "id", None),
            "vnc_url": vnc_url,
            "vnc_port": self.vnc_port,
            "vnc_url_ttl_seconds": self.vnc_url_ttl_seconds,
            "auto_stop_minutes": self.auto_stop_minutes,
        }
        if vnc_token:
            payload["vnc_token"] = vnc_token
        return payload

    def get_vnc_view(self, trace_id: str) -> dict[str, Any]:
        sandbox = self.ensure_sandbox(trace_id, require_computer_use=True)
        payload = self._build_vnc_view(sandbox)
        payload["trace_id"] = trace_id
        return payload

    def cleanup(self, *, timeout_seconds: float = 60.0) -> dict[str, Any]:
        timeout = max(1.0, float(timeout_seconds))
        with self._lock:
            trace_mappings = list(self._trace_to_sandbox.items())
            self._trace_to_sandbox.clear()

            result = {
                "attempted": len(trace_mappings),
                "deleted": 0,
                "failed": 0,
                "not_found": 0,
                "deleted_sandbox_ids": [],
                "failed_sandbox_ids": [],
                "not_found_sandbox_ids": [],
            }
            if not trace_mappings:
                return result

            client = self._ensure_client()
            for trace_id, sandbox_id in trace_mappings:
                sandbox = self._find_sandbox(sandbox_id)
                if sandbox is None:
                    result["not_found"] += 1
                    result["not_found_sandbox_ids"].append(sandbox_id)
                    continue
                try:
                    client.delete(sandbox, timeout=timeout)
                    result["deleted"] += 1
                    result["deleted_sandbox_ids"].append(sandbox_id)
                except Exception as exc:
                    result["failed"] += 1
                    result["failed_sandbox_ids"].append(sandbox_id)
                    logger.warning(
                        "Failed to delete Daytona sandbox id=%s trace_id=%s: %s",
                        sandbox_id,
                        trace_id,
                        exc,
                    )

            return result

    def fs_list(self, trace_id: str, path: str) -> dict[str, Any]:
        sandbox = self.ensure_sandbox(trace_id)
        entries = sandbox.fs.list_files(path)
        return {
            "sandbox_id": getattr(sandbox, "id", None),
            "path": path,
            "entries": _to_plain(entries),
        }

    def fs_read(self, trace_id: str, path: str) -> dict[str, Any]:
        sandbox = self.ensure_sandbox(trace_id)
        content = sandbox.fs.download_file(path)
        if isinstance(content, (bytes, bytearray)):
            decoded = bytes(content).decode("utf-8", errors="replace")
        else:
            decoded = str(content)
        return {
            "sandbox_id": getattr(sandbox, "id", None),
            "path": path,
            "content": decoded,
        }

    def fs_write(self, trace_id: str, path: str, content: str) -> dict[str, Any]:
        sandbox = self.ensure_sandbox(trace_id)
        payload = content.encode("utf-8")
        result = sandbox.fs.upload_file(payload, path)
        return {
            "sandbox_id": getattr(sandbox, "id", None),
            "path": path,
            "written_chars": len(content),
            "result": _to_plain(result),
        }

    def fs_stat(self, trace_id: str, path: str) -> dict[str, Any]:
        sandbox = self.ensure_sandbox(trace_id)
        info = sandbox.fs.get_file_info(path)
        return {
            "sandbox_id": getattr(sandbox, "id", None),
            "path": path,
            "info": _to_plain(info),
        }

    def git_clone(self, trace_id: str, url: str, path: str) -> dict[str, Any]:
        sandbox = self.ensure_sandbox(trace_id)
        result = sandbox.git.clone(url, path)
        return {
            "sandbox_id": getattr(sandbox, "id", None),
            "url": url,
            "path": path,
            "result": _to_plain(result),
        }

    def git_status(self, trace_id: str, path: str) -> dict[str, Any]:
        sandbox = self.ensure_sandbox(trace_id)
        result = sandbox.git.status(path)
        return {
            "sandbox_id": getattr(sandbox, "id", None),
            "path": path,
            "status": _to_plain(result),
        }

    def git_add(self, trace_id: str, path: str, files: list[str]) -> dict[str, Any]:
        sandbox = self.ensure_sandbox(trace_id)
        result = sandbox.git.add(path, files)
        return {
            "sandbox_id": getattr(sandbox, "id", None),
            "path": path,
            "files": files,
            "result": _to_plain(result),
        }

    def git_commit(
        self,
        trace_id: str,
        path: str,
        message: str,
        author: str | None = None,
        email: str | None = None,
    ) -> dict[str, Any]:
        sandbox = self.ensure_sandbox(trace_id)
        effective_author = author or "StewardFlow Agent"
        effective_email = email or "agent@stewardflow.local"
        result = sandbox.git.commit(
            path=path,
            message=message,
            author=effective_author,
            email=effective_email,
        )
        return {
            "sandbox_id": getattr(sandbox, "id", None),
            "path": path,
            "message": message,
            "author": effective_author,
            "email": effective_email,
            "result": _to_plain(result),
        }

    def git_pull(self, trace_id: str, path: str) -> dict[str, Any]:
        sandbox = self.ensure_sandbox(trace_id)
        result = sandbox.git.pull(path)
        return {
            "sandbox_id": getattr(sandbox, "id", None),
            "path": path,
            "result": _to_plain(result),
        }

    def git_push(self, trace_id: str, path: str) -> dict[str, Any]:
        sandbox = self.ensure_sandbox(trace_id)
        result = sandbox.git.push(path)
        return {
            "sandbox_id": getattr(sandbox, "id", None),
            "path": path,
            "result": _to_plain(result),
        }

    def computer_start(self, trace_id: str) -> dict[str, Any]:
        sandbox = self.ensure_sandbox(trace_id, require_computer_use=True)
        status = sandbox.computer_use.get_status()
        payload = {
            "status": _to_plain(status),
            **self._build_vnc_view(sandbox),
        }
        return payload

    def computer_stop(self, trace_id: str) -> dict[str, Any]:
        sandbox = self.ensure_sandbox(trace_id, require_computer_use=True)
        result = sandbox.computer_use.stop()
        return {
            "sandbox_id": getattr(sandbox, "id", None),
            "result": _to_plain(result),
        }

    def computer_status(self, trace_id: str) -> dict[str, Any]:
        sandbox = self.ensure_sandbox(trace_id)
        status = sandbox.computer_use.get_status()
        payload = {
            "sandbox_id": getattr(sandbox, "id", None),
            "status": _to_plain(status),
        }
        if _is_component_running(status):
            payload.update(self._build_vnc_view(sandbox))
        return payload

    def _build_browser_launch_command(self, url: str, browser: str | None = None) -> str:
        browser_hint = (browser or "").strip()
        script = "\n".join(
            [
                f"URL={shlex.quote(url)}",
                f"BROWSER_HINT={shlex.quote(browser_hint)}",
                "LOG_FILE=/tmp/daytona-browser-launch.log",
                ": > \"$LOG_FILE\"",
                "if [ -z \"${DISPLAY:-}\" ]; then",
                "  for display in :0 :1 :99; do",
                "    display_num=\"${display#:}\"",
                "    if [ -S \"/tmp/.X11-unix/X${display_num}\" ]; then",
                "      export DISPLAY=\"$display\"",
                "      break",
                "    fi",
                "  done",
                "fi",
                "if [ -z \"${DISPLAY:-}\" ]; then",
                "  echo \"Missing X server or DISPLAY\" >> \"$LOG_FILE\"",
                "  echo \"no_display_available\"",
                "  exit 126",
                "fi",
                "if [ -n \"$BROWSER_HINT\" ]; then",
                "  CANDIDATES=\"$BROWSER_HINT chromium chromium-browser google-chrome google-chrome-stable firefox brave-browser xdg-open\"",
                "else",
                "  CANDIDATES=\"chromium chromium-browser google-chrome google-chrome-stable firefox brave-browser xdg-open\"",
                "fi",
                "CHROMIUM_FLAGS_BASE=\"--new-window --disable-dev-shm-usage --disable-gpu --no-first-run --no-default-browser-check --user-data-dir=/tmp/daytona-chromium-profile\"",
                "if [ \"$(id -u)\" = \"0\" ]; then",
                "  CHROMIUM_FLAGS=\"$CHROMIUM_FLAGS_BASE --no-sandbox\"",
                "else",
                "  CHROMIUM_FLAGS=\"$CHROMIUM_FLAGS_BASE\"",
                "fi",
                "for cmd in $CANDIDATES; do",
                "  if command -v \"$cmd\" >/dev/null 2>&1; then",
                "    if [ \"$cmd\" = \"xdg-open\" ]; then",
                "      \"$cmd\" \"$URL\" >> \"$LOG_FILE\" 2>&1 &",
                "    elif [ \"$cmd\" = \"firefox\" ]; then",
                "      \"$cmd\" \"$URL\" >> \"$LOG_FILE\" 2>&1 &",
                "    else",
                "      \"$cmd\" $CHROMIUM_FLAGS \"$URL\" >> \"$LOG_FILE\" 2>&1 &",
                "    fi",
                "    pid=$!",
                "    sleep 1",
                "    if kill -0 \"$pid\" >/dev/null 2>&1; then",
                "      echo \"launched:$cmd:$DISPLAY\"",
                "      exit 0",
                "    fi",
                "    echo \"launch_failed:$cmd\" >> \"$LOG_FILE\"",
                "  fi",
                "done",
                "echo \"no_browser_found_or_failed\"",
                "exit 127",
            ]
        )
        return f"bash -lc {shlex.quote(script)}"

    def browser_navigate(self, trace_id: str, url: str, browser: str | None = None) -> dict[str, Any]:
        normalized_url = (url or "").strip()
        if not normalized_url:
            raise ValueError("url is required")

        sandbox = self.ensure_sandbox(trace_id, require_computer_use=True)
        process = getattr(sandbox, "process", None)
        if process is None:
            raise RuntimeError("Daytona sandbox does not expose process API")

        launch_command = self._build_browser_launch_command(normalized_url, browser)
        launch_result = process.exec(launch_command, timeout=45)
        plain_result = _to_plain(launch_result)

        if isinstance(plain_result, Mapping):
            exit_code = plain_result.get("exit_code")
            if isinstance(exit_code, int) and exit_code != 0:
                raise RuntimeError(
                    f"Failed to launch browser in sandbox (exit_code={exit_code}): {plain_result}"
                )

        payload = {
            "sandbox_id": getattr(sandbox, "id", None),
            "url": normalized_url,
            "browser": (browser or "").strip() or None,
            "result": plain_result,
            **self._build_vnc_view(sandbox),
        }
        return payload

    def computer_mouse(
        self,
        trace_id: str,
        *,
        action: str,
        x: int | None = None,
        y: int | None = None,
        button: str | None = None,
        start_x: int | None = None,
        start_y: int | None = None,
        end_x: int | None = None,
        end_y: int | None = None,
        direction: str | None = None,
        amount: int | None = None,
        double: bool = False,
    ) -> dict[str, Any]:
        sandbox = self.ensure_sandbox(trace_id, require_computer_use=True)
        computer_use = sandbox.computer_use
        normalized_action = (action or "").strip().lower()

        if normalized_action == "move":
            if x is None or y is None:
                raise ValueError("x and y are required for mouse move")
            result = computer_use.mouse.move(x, y)
        elif normalized_action == "click":
            if x is None or y is None:
                raise ValueError("x and y are required for mouse click")
            result = computer_use.mouse.click(x=x, y=y, button=button or "left", double=bool(double))
        elif normalized_action == "drag":
            if None in (start_x, start_y, end_x, end_y):
                raise ValueError("start_x, start_y, end_x, end_y are required for mouse drag")
            result = computer_use.mouse.drag(
                start_x=start_x,
                start_y=start_y,
                end_x=end_x,
                end_y=end_y,
                button=button or "left",
            )
        elif normalized_action == "scroll":
            if x is None or y is None:
                raise ValueError("x and y are required for mouse scroll")
            result = computer_use.mouse.scroll(
                x=x,
                y=y,
                direction=direction or "down",
                amount=int(amount or 1),
            )
        else:
            raise ValueError(f"Unsupported mouse action: {action}")

        return {
            "sandbox_id": getattr(sandbox, "id", None),
            "action": normalized_action,
            "result": _to_plain(result),
            **self._build_vnc_view(sandbox),
        }

    def computer_keyboard(
        self,
        trace_id: str,
        *,
        action: str,
        text: str | None = None,
        key: str | None = None,
        keys: list[str] | str | None = None,
        modifiers: list[str] | None = None,
    ) -> dict[str, Any]:
        sandbox = self.ensure_sandbox(trace_id, require_computer_use=True)
        computer_use = sandbox.computer_use
        normalized_action = (action or "").strip().lower()

        if normalized_action == "type":
            if text is None:
                raise ValueError("text is required for keyboard type")
            result = computer_use.keyboard.type(text=text)
        elif normalized_action == "press":
            if key is None:
                raise ValueError("key is required for keyboard press")
            result = computer_use.keyboard.press(key=key, modifiers=modifiers or [])
        elif normalized_action == "hotkey":
            if not keys:
                raise ValueError("keys are required for keyboard hotkey")
            if isinstance(keys, list):
                hotkey = "+".join(keys)
            else:
                hotkey = keys
            result = computer_use.keyboard.hotkey(hotkey)
        else:
            raise ValueError(f"Unsupported keyboard action: {action}")

        return {
            "sandbox_id": getattr(sandbox, "id", None),
            "action": normalized_action,
            "result": _to_plain(result),
            **self._build_vnc_view(sandbox),
        }

    def computer_screenshot(
        self,
        trace_id: str,
        *,
        mode: str = "full",
        x: int | None = None,
        y: int | None = None,
        width: int | None = None,
        height: int | None = None,
        quality: int = 75,
        image_format: str = "jpeg",
    ) -> dict[str, Any]:
        from daytona.common.computer_use import ScreenshotOptions, ScreenshotRegion  # type: ignore

        sandbox = self.ensure_sandbox(trace_id, require_computer_use=True)
        computer_use = sandbox.computer_use
        normalized_mode = (mode or "full").strip().lower()

        if normalized_mode == "full":
            result = computer_use.screenshot.take_full_screen()
        elif normalized_mode == "region":
            if None in (x, y, width, height):
                raise ValueError("x, y, width, height are required for region screenshot")
            result = computer_use.screenshot.take_region(ScreenshotRegion(x=x, y=y, width=width, height=height))
        elif normalized_mode == "compressed":
            options = ScreenshotOptions(quality=quality, format=image_format)
            result = computer_use.screenshot.take_compressed(options)
        elif normalized_mode == "compressed_region":
            if None in (x, y, width, height):
                raise ValueError("x, y, width, height are required for compressed_region screenshot")
            options = ScreenshotOptions(quality=quality, format=image_format)
            result = computer_use.screenshot.take_compressed_region(
                region=ScreenshotRegion(x=x, y=y, width=width, height=height),
                options=options,
            )
        else:
            raise ValueError(f"Unsupported screenshot mode: {mode}")

        return {
            "sandbox_id": getattr(sandbox, "id", None),
            "mode": normalized_mode,
            "result": _to_plain(result),
            **self._build_vnc_view(sandbox),
        }


_manager_singleton: DaytonaSandboxManager | None = None
_manager_singleton_lock = threading.Lock()


def get_daytona_manager(config: Mapping[str, Any] | None = None) -> DaytonaSandboxManager:
    global _manager_singleton
    with _manager_singleton_lock:
        if _manager_singleton is not None:
            return _manager_singleton

        raw = dict(config or {})
        _manager_singleton = DaytonaSandboxManager(
            auto_stop_minutes=int(raw.get("auto_stop_minutes", 15)),
            vnc_port=int(raw.get("vnc_port", 6080)),
            vnc_url_ttl_seconds=int(raw.get("vnc_url_ttl_seconds", 3600)),
            api_key=raw.get("api_key"),
            target=raw.get("target"),
            server_url=raw.get("server_url"),
        )
        return _manager_singleton


def get_daytona_manager_if_initialized() -> DaytonaSandboxManager | None:
    with _manager_singleton_lock:
        return _manager_singleton


def set_daytona_manager(manager: DaytonaSandboxManager | None) -> None:
    global _manager_singleton
    with _manager_singleton_lock:
        _manager_singleton = manager
