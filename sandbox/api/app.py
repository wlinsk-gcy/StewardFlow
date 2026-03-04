from __future__ import annotations

import asyncio
import hashlib
import json
import os
import signal
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)
from pydantic import BaseModel, Field

DEFAULT_TIMEOUT_MS = int(os.getenv("SANDBOX_EXEC_TIMEOUT_MS", "120000"))
MAX_TIMEOUT_MS = int(os.getenv("SANDBOX_EXEC_MAX_TIMEOUT_MS", "3600000"))
DEFAULT_PREVIEW_BYTES = int(os.getenv("SANDBOX_EXEC_PREVIEW_BYTES", "4096"))
DEFAULT_CAPTURE_LIMIT_BYTES = int(
    os.getenv("SANDBOX_EXEC_CAPTURE_LIMIT_BYTES", str(50 * 1024 * 1024))
)

ARTIFACT_ROOT = Path(os.getenv("SANDBOX_ARTIFACT_ROOT", "/config/tool-artifacts")).resolve()
EXEC_ARTIFACT_ROOT = (ARTIFACT_ROOT / "exec").resolve()
BROWSER_ARTIFACT_ROOT = (ARTIFACT_ROOT / "browser").resolve()
UPLOAD_ROOT = Path(os.getenv("SANDBOX_UPLOAD_ROOT", "/config/uploads")).resolve()


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id(prefix: str) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return f"{prefix}-{ts}-{uuid4().hex[:8]}"


def _resolve_any_path(raw_path: str | None, base_dir: Path) -> Path:
    if raw_path is None or not str(raw_path).strip():
        return base_dir
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        candidate = base_dir / candidate
    return candidate.resolve()


def _resolve_cwd(raw_cwd: str | None) -> Path:
    if raw_cwd is None or not str(raw_cwd).strip():
        return Path("/").resolve()
    candidate = Path(raw_cwd).expanduser()
    if not candidate.is_absolute():
        candidate = (Path.cwd() / candidate).resolve()
    return candidate.resolve()


def _artifact_file_path(base_dir: Path, requested: str | None, suffix: str, stem_prefix: str) -> Path:
    if requested is None or not requested.strip():
        base_dir.mkdir(parents=True, exist_ok=True)
        return (base_dir / f"{_new_id(stem_prefix)}.{suffix}").resolve()

    candidate = _resolve_any_path(requested, base_dir)
    if requested.endswith("/") or candidate.suffix == "":
        candidate.mkdir(parents=True, exist_ok=True)
        candidate = candidate / f"{_new_id(stem_prefix)}.{suffix}"
    candidate.parent.mkdir(parents=True, exist_ok=True)
    return candidate.resolve()


def _escape_css_attr(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


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


async def _capture_stream(
    stream: asyncio.StreamReader | None,
    output_file: Path,
    *,
    preview_bytes: int,
    capture_limit_bytes: int,
) -> dict[str, Any]:
    preview = bytearray()
    total_bytes = 0
    stored_bytes = 0
    truncated = False

    output_file.parent.mkdir(parents=True, exist_ok=True)
    with output_file.open("wb") as handle:
        if stream is None:
            return {
                "path": str(output_file),
                "total_bytes": 0,
                "stored_bytes": 0,
                "truncated": False,
                "preview": "",
            }

        while True:
            chunk = await stream.read(65536)
            if not chunk:
                break
            total_bytes += len(chunk)

            if len(preview) < preview_bytes:
                remain = preview_bytes - len(preview)
                preview.extend(chunk[:remain])

            if stored_bytes < capture_limit_bytes:
                remain = capture_limit_bytes - stored_bytes
                part = chunk[:remain]
                if part:
                    handle.write(part)
                    stored_bytes += len(part)
                if len(part) < len(chunk):
                    truncated = True
            else:
                truncated = True

    return {
        "path": str(output_file),
        "total_bytes": total_bytes,
        "stored_bytes": stored_bytes,
        "truncated": truncated,
        "preview": preview.decode("utf-8", errors="replace"),
    }


class BrowserManager:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._dialog_queue: asyncio.Queue[Any] = asyncio.Queue()
        self._dialog_listener_page: Page | None = None
        self._permission_probe_context_id: int | None = None

        cdp_port = os.getenv("CHROME_REMOTE_DEBUGGING_PORT", "9222").strip() or "9222"
        self._cdp_url = os.getenv("SANDBOX_CHROME_CDP_URL", f"http://127.0.0.1:{cdp_port}")
        self._connect_timeout_sec = max(
            1.0,
            float(os.getenv("SANDBOX_CHROME_CONNECT_TIMEOUT_SEC", "20")),
        )
        self._connect_poll_sec = 0.25

    @staticmethod
    def _permission_probe_init_script() -> str:
        return """
(() => {
  const root = window;
  if (root.__SF_PERMISSION_PROBE_INSTALLED) return;
  root.__SF_PERMISSION_PROBE_INSTALLED = true;
  if (!Array.isArray(root.__SF_PERMISSION_EVENTS)) {
    root.__SF_PERMISSION_EVENTS = [];
  }
  const push = (kind, detail) => {
    try {
      root.__SF_PERMISSION_EVENTS.push({
        ts: Date.now(),
        kind: String(kind || ""),
        detail: String(detail || "").slice(0, 200),
        href: String(location.href || ""),
      });
      const cap = 200;
      if (root.__SF_PERMISSION_EVENTS.length > cap) {
        root.__SF_PERMISSION_EVENTS.splice(0, root.__SF_PERMISSION_EVENTS.length - cap);
      }
    } catch (_) {}
  };
  const wrap = (obj, key, kind) => {
    try {
      if (!obj) return;
      const original = obj[key];
      if (typeof original !== "function") return;
      if (original.__sf_permission_wrapped) return;
      const wrapped = function(...args) {
        push(kind, key);
        return original.apply(this, args);
      };
      wrapped.__sf_permission_wrapped = true;
      obj[key] = wrapped;
    } catch (_) {}
  };

  try {
    if (navigator.geolocation) {
      wrap(navigator.geolocation, "getCurrentPosition", "geolocation");
      wrap(navigator.geolocation, "watchPosition", "geolocation");
    }
  } catch (_) {}
  try {
    if (navigator.mediaDevices) {
      wrap(navigator.mediaDevices, "getUserMedia", "media");
    }
  } catch (_) {}
  try {
    if (window.Notification) {
      wrap(window.Notification, "requestPermission", "notification");
    }
  } catch (_) {}
  try {
    if (navigator.clipboard) {
      wrap(navigator.clipboard, "read", "clipboard");
      wrap(navigator.clipboard, "readText", "clipboard");
      wrap(navigator.clipboard, "write", "clipboard");
      wrap(navigator.clipboard, "writeText", "clipboard");
    }
  } catch (_) {}
})();
"""

    async def _install_permission_probe_locked(self, page: Page) -> None:
        if self._context is not None:
            ctx_id = id(self._context)
            if self._permission_probe_context_id != ctx_id:
                try:
                    await self._context.add_init_script(script=self._permission_probe_init_script())
                    self._permission_probe_context_id = ctx_id
                except Exception:
                    pass
        try:
            await page.evaluate(self._permission_probe_init_script())
        except Exception:
            pass

    async def _permission_marker_locked(self, page: Page) -> dict[str, Any]:
        try:
            payload = await page.evaluate(
                """
                async () => {
                  const permissionNames = [
                    "geolocation",
                    "notifications",
                    "camera",
                    "microphone",
                    "clipboard-read",
                    "clipboard-write",
                  ];
                  const states = {};
                  const canQuery = !!(navigator.permissions && navigator.permissions.query);
                  for (const name of permissionNames) {
                    if (!canQuery) {
                      states[name] = "unsupported";
                      continue;
                    }
                    try {
                      const status = await navigator.permissions.query({ name });
                      states[name] = status.state;
                    } catch (_) {
                      states[name] = "unsupported";
                    }
                  }

                  const rawEvents = Array.isArray(window.__SF_PERMISSION_EVENTS)
                    ? window.__SF_PERMISSION_EVENTS
                    : [];
                  const now = Date.now();
                  const recentRequests = rawEvents
                    .filter((item) => item && typeof item.ts === "number" && now - item.ts <= 180000)
                    .slice(-20);

                  const promptPermissions = Object.entries(states)
                    .filter(([_, state]) => state === "prompt")
                    .map(([name]) => name);
                  const deniedPermissions = Object.entries(states)
                    .filter(([_, state]) => state === "denied")
                    .map(([name]) => name);

                  const kinds = Array.from(
                    new Set(
                      recentRequests
                        .map((item) => String(item.kind || ""))
                        .filter((item) => item.length > 0),
                    ),
                  );
                  const expectedKinds = {
                    geolocation: ["geolocation"],
                    notifications: ["notification"],
                    camera: ["media"],
                    microphone: ["media"],
                    "clipboard-read": ["clipboard"],
                    "clipboard-write": ["clipboard"],
                  };

                  const detectedPermissions = [];
                  for (const perm of promptPermissions) {
                    const expected = expectedKinds[perm] || [];
                    if (expected.some((kind) => kinds.includes(kind))) {
                      detectedPermissions.push(perm);
                    }
                  }

                  return {
                    states,
                    prompt_permissions: promptPermissions,
                    denied_permissions: deniedPermissions,
                    recent_requests: recentRequests,
                    permission_prompt_detected: detectedPermissions.length > 0,
                    permission_prompt_permissions: detectedPermissions,
                  };
                }
                """
            )
            if isinstance(payload, dict):
                return payload
        except Exception:
            pass
        return {
            "states": {},
            "prompt_permissions": [],
            "denied_permissions": [],
            "recent_requests": [],
            "permission_prompt_detected": False,
            "permission_prompt_permissions": [],
        }

    async def permission_marker(self) -> dict[str, Any]:
        async with self._lock:
            await self._start_locked()
            page = self._require_page_locked()
            await self._install_permission_probe_locked(page)
            return await self._permission_marker_locked(page)

    async def _connect_browser_locked(self) -> None:
        if self._playwright is None:
            self._playwright = await async_playwright().start()

        deadline = asyncio.get_running_loop().time() + self._connect_timeout_sec
        last_error = "unknown"
        while True:
            try:
                # Connect to existing GUI Chrome. Do not launch any new browser process.
                self._browser = await self._playwright.chromium.connect_over_cdp(self._cdp_url)
                return
            except Exception as exc:
                last_error = str(exc)
                if asyncio.get_running_loop().time() >= deadline:
                    raise HTTPException(
                        status_code=503,
                        detail=f"chrome_cdp_unavailable: {last_error}",
                    ) from exc
                await asyncio.sleep(self._connect_poll_sec)

    @staticmethod
    def _normalize_wait_until(wait_until: str) -> Literal["load", "domcontentloaded", "networkidle", "commit"]:
        allowed = {"load", "domcontentloaded", "networkidle", "commit"}
        normalized = (wait_until or "domcontentloaded").strip().lower()
        if normalized not in allowed:
            return "domcontentloaded"
        return normalized  # type: ignore[return-value]

    async def _safe_page_title(self, page: Page) -> str:
        try:
            return await page.title()
        except Exception:
            return ""

    def _install_dialog_listener_locked(self, page: Page) -> None:
        if self._dialog_listener_page is page:
            return
        self._dialog_listener_page = page

        def _on_dialog(dialog: Any) -> None:
            try:
                self._dialog_queue.put_nowait(dialog)
            except Exception:
                pass

        page.on("dialog", _on_dialog)

    async def _ensure_single_context_locked(self) -> None:
        if self._browser is None:
            raise HTTPException(status_code=500, detail="browser_not_connected")

        contexts = list(self._browser.contexts)
        if not contexts:
            self._context = await self._browser.new_context(ignore_https_errors=True)
            self._page = await self._context.new_page()
            self._install_dialog_listener_locked(self._page)
            await self._install_permission_probe_locked(self._page)
            return

        primary = contexts[0]
        for extra in contexts[1:]:
            try:
                await extra.close()
            except Exception:
                pass
        self._context = primary

        pages = list(primary.pages)
        if pages:
            self._page = pages[0]
        else:
            self._page = await primary.new_page()
        self._install_dialog_listener_locked(self._page)
        await self._install_permission_probe_locked(self._page)

    async def _start_locked(self) -> None:
        if self._browser is None:
            await self._connect_browser_locked()

        try:
            await self._ensure_single_context_locked()
        except Exception:
            await self._disconnect_locked()
            await self._connect_browser_locked()
            await self._ensure_single_context_locked()

    def _require_page_locked(self) -> Page:
        if self._page is None:
            raise HTTPException(status_code=400, detail="browser_not_started")
        return self._page

    async def _disconnect_locked(self) -> None:
        self._context = None
        self._page = None
        self._browser = None
        self._dialog_listener_page = None
        while not self._dialog_queue.empty():
            try:
                self._dialog_queue.get_nowait()
            except Exception:
                break
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None

    def _resolve_locator(self, page: Page, *, uid: str | None, selector: str | None):
        if uid and uid.strip():
            safe_uid = _escape_css_attr(uid.strip())
            resolved = f'[data-sf-uid="{safe_uid}"]'
            return page.locator(resolved).first, resolved
        if selector and selector.strip():
            resolved = selector.strip()
            return page.locator(resolved).first, resolved
        raise HTTPException(status_code=400, detail="target_required: provide uid or selector")

    async def _tabs_payload_locked(self) -> dict[str, Any]:
        if self._context is None:
            raise HTTPException(status_code=500, detail="browser_context_unavailable")

        pages = list(self._context.pages)
        active = self._page
        items: list[dict[str, Any]] = []
        active_index = 0 if pages else -1
        for idx, page in enumerate(pages):
            is_active = page is active
            if is_active:
                active_index = idx
            items.append(
                {
                    "index": idx,
                    "url": page.url,
                    "title": await self._safe_page_title(page),
                    "is_active": is_active,
                }
            )

        return {
            "single_browser_process": True,
            "single_context_enforced": True,
            "context_count": len(self._browser.contexts) if self._browser is not None else 0,
            "tab_count": len(items),
            "active_index": active_index,
            "tabs": items,
        }

    async def _ensure_snapshot_uids(self, page: Page, *, max_elements: int) -> dict[str, Any]:
        return await page.evaluate(
            """
            (maxElements) => {
              const root = window;
              if (!root.__SF_UID_SEQ || typeof root.__SF_UID_SEQ !== "number") {
                root.__SF_UID_SEQ = 1;
              }
              const selectors = [
                "a", "button", "input", "textarea", "select", "option",
                "label", "summary", "[role]", "[tabindex]",
                "[contenteditable='true']", "[onclick]"
              ];
              const nodes = Array.from(document.querySelectorAll(selectors.join(",")));
              const elements = [];
              for (const node of nodes) {
                if (!(node instanceof HTMLElement)) continue;
                const style = window.getComputedStyle(node);
                const rect = node.getBoundingClientRect();
                const visible = rect.width > 0 && rect.height > 0 &&
                  style.display !== "none" && style.visibility !== "hidden";
                if (!visible) continue;

                let uid = node.getAttribute("data-sf-uid");
                if (!uid) {
                  uid = `sf-${root.__SF_UID_SEQ++}`;
                  node.setAttribute("data-sf-uid", uid);
                }

                const text = ((node.innerText || node.textContent || "").trim()).replace(/\\\\s+/g, " ").slice(0, 200);
                const ariaLabel = (node.getAttribute("aria-label") || "").trim();
                const placeholder = (node.getAttribute("placeholder") || "").trim();
                const role = (node.getAttribute("role") || "").trim();
                const tag = node.tagName.toLowerCase();
                const href = tag === "a" ? (node.getAttribute("href") || "") : "";

                elements.push({
                  uid,
                  tag,
                  role,
                  text,
                  aria_label: ariaLabel,
                  placeholder,
                  id: node.id || "",
                  name: node.getAttribute("name") || "",
                  href,
                  x: Math.round(rect.x),
                  y: Math.round(rect.y),
                  width: Math.round(rect.width),
                  height: Math.round(rect.height),
                });
              }
              return {
                url: location.href,
                title: document.title || "",
                viewport: { width: window.innerWidth, height: window.innerHeight },
                element_count: elements.length,
                elements: elements.slice(0, Math.max(1, maxElements)),
              };
            }
            """,
            max(1, min(int(max_elements), 2000)),
        )

    async def state(self) -> dict[str, Any]:
        async with self._lock:
            await self._start_locked()
            page = self._require_page_locked()
            tabs = await self._tabs_payload_locked()
            permission_marker = await self._permission_marker_locked(page)
            return {
                "started": True,
                "headless": False,
                "url": page.url,
                "title": await self._safe_page_title(page),
                "cdp_url": self._cdp_url,
                "permission_marker": permission_marker,
                "needs_user_authorization": bool(permission_marker.get("permission_prompt_detected")),
                **tabs,
            }

    async def navigate(self, *, url: str, timeout_ms: int, wait_until: str) -> dict[str, Any]:
        async with self._lock:
            await self._start_locked()
            page = self._require_page_locked()
            resolved_wait = self._normalize_wait_until(wait_until)
            await page.goto(url, timeout=timeout_ms, wait_until=resolved_wait)
            return {
                "url": page.url,
                "title": await self._safe_page_title(page),
                "wait_until": resolved_wait,
            }

    async def navigate_back(self, *, timeout_ms: int, wait_until: str) -> dict[str, Any]:
        async with self._lock:
            await self._start_locked()
            page = self._require_page_locked()
            resolved_wait = self._normalize_wait_until(wait_until)
            response = await page.go_back(timeout=timeout_ms, wait_until=resolved_wait)
            return {
                "navigated": response is not None,
                "url": page.url,
                "title": await self._safe_page_title(page),
                "wait_until": resolved_wait,
            }

    async def click(
        self,
        *,
        uid: str | None,
        selector: str | None,
        button: Literal["left", "right", "middle"],
        click_count: int,
        timeout_ms: int,
    ) -> dict[str, Any]:
        async with self._lock:
            await self._start_locked()
            page = self._require_page_locked()
            locator, target = self._resolve_locator(page, uid=uid, selector=selector)
            clicks = max(1, click_count)
            pending_before = self._dialog_queue.qsize()

            def _ok_response(**extra: Any) -> dict[str, Any]:
                payload = {
                    "clicked": target,
                    "button": button,
                    "click_count": clicks,
                    "url": page.url,
                    "dialog_pending": self._dialog_queue.qsize() > pending_before,
                }
                payload.update(extra)
                return payload

            # For alert/prompt/confirm flows, Playwright click can remain blocked until
            # the modal is handled. Run click as a task and short-circuit as soon as a
            # new dialog is observed.
            click_task = asyncio.create_task(
                locator.click(
                    button=button,
                    click_count=clicks,
                    timeout=timeout_ms,
                    no_wait_after=True,
                )
            )

            while not click_task.done():
                if self._dialog_queue.qsize() > pending_before:
                    click_task.cancel()
                    # Best-effort cleanup: avoid waiting for the full click timeout.
                    with suppress(asyncio.CancelledError, asyncio.TimeoutError, PlaywrightTimeoutError):
                        await asyncio.wait_for(click_task, timeout=0.2)
                    return _ok_response(
                        dialog_pending=True,
                        click_short_circuited=True,
                        reason="dialog_detected_early",
                    )
                await asyncio.sleep(0.02)

            try:
                await click_task
                return _ok_response()
            except PlaywrightTimeoutError:
                if self._dialog_queue.qsize() > pending_before:
                    return _ok_response(
                        dialog_pending=True,
                        click_timeout_ignored=True,
                        reason="dialog_blocked_click_completion",
                    )
                raise

    async def hover(self, *, uid: str | None, selector: str | None, timeout_ms: int) -> dict[str, Any]:
        async with self._lock:
            await self._start_locked()
            page = self._require_page_locked()
            locator, target = self._resolve_locator(page, uid=uid, selector=selector)
            await locator.hover(timeout=timeout_ms)
            return {"hovered": target, "url": page.url}

    async def drag(
        self,
        *,
        from_uid: str | None,
        from_selector: str | None,
        to_uid: str | None,
        to_selector: str | None,
        timeout_ms: int,
    ) -> dict[str, Any]:
        async with self._lock:
            await self._start_locked()
            page = self._require_page_locked()
            source, source_target = self._resolve_locator(page, uid=from_uid, selector=from_selector)
            target, target_target = self._resolve_locator(page, uid=to_uid, selector=to_selector)
            await source.drag_to(target, timeout=timeout_ms)
            return {"dragged_from": source_target, "dragged_to": target_target, "url": page.url}

    async def evaluate(self, *, expression: str, arg: Any) -> dict[str, Any]:
        async with self._lock:
            await self._start_locked()
            page = self._require_page_locked()
            result = await page.evaluate(expression, arg)
            return {"result": result}

    async def type_text(
        self,
        *,
        text: str,
        uid: str | None,
        selector: str | None,
        clear_before: bool,
        delay_ms: int,
        timeout_ms: int,
    ) -> dict[str, Any]:
        async with self._lock:
            await self._start_locked()
            page = self._require_page_locked()
            if (uid and uid.strip()) or (selector and selector.strip()):
                locator, target = self._resolve_locator(page, uid=uid, selector=selector)
                if clear_before:
                    await locator.fill("", timeout=timeout_ms)
                await locator.type(text, delay=max(0, delay_ms), timeout=timeout_ms)
                return {
                    "typed": len(text),
                    "target": target,
                    "clear_before": clear_before,
                    "url": page.url,
                }

            await page.keyboard.type(text, delay=max(0, delay_ms))
            return {
                "typed": len(text),
                "target": "active_element",
                "clear_before": clear_before,
                "url": page.url,
            }

    async def fill_form(
        self,
        *,
        fields: list[dict[str, Any]],
        timeout_ms: int,
        submit: bool,
    ) -> dict[str, Any]:
        async with self._lock:
            await self._start_locked()
            page = self._require_page_locked()
            applied: list[dict[str, Any]] = []
            for item in fields:
                uid = str(item.get("uid") or "").strip() or None
                selector = str(item.get("selector") or "").strip() or None
                value = str(item.get("value") or "")
                locator, target = self._resolve_locator(page, uid=uid, selector=selector)
                await locator.fill(value, timeout=timeout_ms)
                applied.append({"target": target, "length": len(value)})
            if submit:
                await page.keyboard.press("Enter")
            return {
                "filled_count": len(applied),
                "submit": submit,
                "applied": applied,
                "url": page.url,
            }

    async def press_key(
        self,
        *,
        key: str,
        uid: str | None,
        selector: str | None,
        timeout_ms: int,
    ) -> dict[str, Any]:
        async with self._lock:
            await self._start_locked()
            page = self._require_page_locked()
            if (uid and uid.strip()) or (selector and selector.strip()):
                locator, target = self._resolve_locator(page, uid=uid, selector=selector)
                await locator.press(key, timeout=timeout_ms)
                return {"pressed": key, "target": target, "url": page.url}
            await page.keyboard.press(key)
            return {"pressed": key, "target": "active_element", "url": page.url}

    async def select_option(
        self,
        *,
        uid: str | None,
        selector: str | None,
        values: list[str],
        labels: list[str],
        indexes: list[int],
        timeout_ms: int,
    ) -> dict[str, Any]:
        if not values and not labels and not indexes:
            raise HTTPException(status_code=400, detail="select_option_requires_values_labels_or_indexes")
        async with self._lock:
            await self._start_locked()
            page = self._require_page_locked()
            locator, target = self._resolve_locator(page, uid=uid, selector=selector)
            selected = await locator.select_option(
                value=values or None,
                label=labels or None,
                index=indexes or None,
                timeout=timeout_ms,
            )
            return {"target": target, "selected": selected, "url": page.url}

    async def wait_for(
        self,
        *,
        texts: list[str],
        uid: str | None,
        selector: str | None,
        state: Literal["visible", "hidden", "attached", "detached"],
        timeout_ms: int,
    ) -> dict[str, Any]:
        async with self._lock:
            await self._start_locked()
            page = self._require_page_locked()

            if (uid and uid.strip()) or (selector and selector.strip()):
                locator, target = self._resolve_locator(page, uid=uid, selector=selector)
                await locator.wait_for(state=state, timeout=timeout_ms)
                return {"waited_for": "locator", "target": target, "state": state, "url": page.url}

            candidates = [item for item in texts if isinstance(item, str) and item.strip()]
            if candidates:
                await page.wait_for_function(
                    """
                    (needles) => {
                      const body = (document.body && document.body.innerText) || "";
                      return needles.some((needle) => body.includes(needle));
                    }
                    """,
                    arg=candidates,
                    timeout=timeout_ms,
                )
                matched = await page.evaluate(
                    """
                    (needles) => {
                      const body = (document.body && document.body.innerText) || "";
                      return needles.find((needle) => body.includes(needle)) || null;
                    }
                    """,
                    candidates,
                )
                return {"waited_for": "text", "matched": matched, "url": page.url}

            await page.wait_for_timeout(timeout_ms)
            return {"waited_for": "timeout", "timeout_ms": timeout_ms, "url": page.url}

    async def snapshot(self, *, max_elements: int, output_path: str | None) -> dict[str, Any]:
        async with self._lock:
            await self._start_locked()
            page = self._require_page_locked()
            payload = await self._ensure_snapshot_uids(page, max_elements=max_elements)
            target = _artifact_file_path(
                BROWSER_ARTIFACT_ROOT,
                output_path,
                suffix="json",
                stem_prefix="snapshot",
            )
            target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            return {
                "path": str(target),
                "url": payload.get("url"),
                "title": payload.get("title"),
                "viewport": payload.get("viewport"),
                "element_count": payload.get("element_count"),
                "elements": payload.get("elements"),
            }

    async def take_screenshot(
        self,
        *,
        uid: str | None,
        selector: str | None,
        output_path: str | None,
        full_page: bool,
        image_format: Literal["png", "jpeg", "webp"],
        quality: int | None,
    ) -> dict[str, Any]:
        async with self._lock:
            await self._start_locked()
            page = self._require_page_locked()
            suffix = "jpg" if image_format == "jpeg" else image_format
            target = _artifact_file_path(
                BROWSER_ARTIFACT_ROOT,
                output_path,
                suffix=suffix,
                stem_prefix="screenshot",
            )
            kwargs: dict[str, Any] = {
                "path": str(target),
                "type": image_format,
            }
            if quality is not None and image_format in {"jpeg", "webp"}:
                kwargs["quality"] = max(0, min(int(quality), 100))

            target_desc = "page"
            if (uid and uid.strip()) or (selector and selector.strip()):
                locator, target_desc = self._resolve_locator(page, uid=uid, selector=selector)
                await locator.screenshot(**kwargs)
            else:
                kwargs["full_page"] = bool(full_page)
                await page.screenshot(**kwargs)

            return {
                "path": str(target),
                "target": target_desc,
                "format": image_format,
                "full_page": bool(full_page),
                "url": page.url,
            }

    async def tabs(
        self,
        *,
        action: Literal["list", "new", "activate", "close", "close_others"],
        index: int | None,
        url: str | None,
        timeout_ms: int,
        wait_until: str,
    ) -> dict[str, Any]:
        async with self._lock:
            await self._start_locked()
            if self._context is None:
                raise HTTPException(status_code=500, detail="browser_context_unavailable")

            resolved_wait = self._normalize_wait_until(wait_until)
            pages = list(self._context.pages)
            active_page = self._page
            active_index = 0
            for idx, page in enumerate(pages):
                if page is active_page:
                    active_index = idx
                    break

            if action == "new":
                new_page = await self._context.new_page()
                self._page = new_page
                self._install_dialog_listener_locked(new_page)
                await new_page.bring_to_front()
                if url:
                    await new_page.goto(url, timeout=timeout_ms, wait_until=resolved_wait)

            elif action == "activate":
                if index is None:
                    raise HTTPException(status_code=400, detail="tabs_activate_requires_index")
                if index < 0 or index >= len(pages):
                    raise HTTPException(status_code=404, detail=f"tab_index_out_of_range: {index}")
                self._page = pages[index]
                self._install_dialog_listener_locked(self._page)
                await self._page.bring_to_front()

            elif action == "close":
                if not pages:
                    raise HTTPException(status_code=400, detail="no_tabs_to_close")
                target_index = index if index is not None else active_index
                if target_index < 0 or target_index >= len(pages):
                    raise HTTPException(status_code=404, detail=f"tab_index_out_of_range: {target_index}")
                target_page = pages[target_index]
                await target_page.close()
                remaining = list(self._context.pages)
                if not remaining:
                    self._page = await self._context.new_page()
                else:
                    fallback_index = min(target_index, len(remaining) - 1)
                    self._page = remaining[fallback_index]
                self._install_dialog_listener_locked(self._page)
                await self._page.bring_to_front()

            elif action == "close_others":
                if not pages:
                    raise HTTPException(status_code=400, detail="no_tabs_to_close")
                keep_index = index if index is not None else active_index
                if keep_index < 0 or keep_index >= len(pages):
                    raise HTTPException(status_code=404, detail=f"tab_index_out_of_range: {keep_index}")
                keep_page = pages[keep_index]
                for page in pages:
                    if page is keep_page:
                        continue
                    await page.close()
                self._page = keep_page
                self._install_dialog_listener_locked(self._page)
                await self._page.bring_to_front()

            elif action != "list":
                raise HTTPException(status_code=400, detail=f"unsupported_tabs_action: {action}")

            result = await self._tabs_payload_locked()
            result["action"] = action
            return result

    async def mouse_click_xy(
        self,
        *,
        x: float,
        y: float,
        button: Literal["left", "right", "middle"],
        click_count: int,
        delay_ms: int,
    ) -> dict[str, Any]:
        async with self._lock:
            await self._start_locked()
            page = self._require_page_locked()
            await page.mouse.click(
                x=float(x),
                y=float(y),
                button=button,
                click_count=max(1, int(click_count)),
                delay=max(0, int(delay_ms)),
            )
            return {"clicked": {"x": float(x), "y": float(y)}, "button": button, "url": page.url}

    async def mouse_down(self, *, button: Literal["left", "right", "middle"]) -> dict[str, Any]:
        async with self._lock:
            await self._start_locked()
            page = self._require_page_locked()
            await page.mouse.down(button=button)
            return {"mouse_down": True, "button": button, "url": page.url}

    async def mouse_move_xy(self, *, x: float, y: float, steps: int) -> dict[str, Any]:
        async with self._lock:
            await self._start_locked()
            page = self._require_page_locked()
            await page.mouse.move(x=float(x), y=float(y), steps=max(1, int(steps)))
            return {"moved_to": {"x": float(x), "y": float(y)}, "steps": max(1, int(steps)), "url": page.url}

    async def mouse_drag_xy(
        self,
        *,
        start_x: float,
        start_y: float,
        end_x: float,
        end_y: float,
        steps: int,
        button: Literal["left", "right", "middle"],
    ) -> dict[str, Any]:
        async with self._lock:
            await self._start_locked()
            page = self._require_page_locked()
            await page.mouse.move(x=float(start_x), y=float(start_y))
            await page.mouse.down(button=button)
            await page.mouse.move(x=float(end_x), y=float(end_y), steps=max(1, int(steps)))
            await page.mouse.up(button=button)
            return {
                "dragged": {
                    "start": {"x": float(start_x), "y": float(start_y)},
                    "end": {"x": float(end_x), "y": float(end_y)},
                },
                "steps": max(1, int(steps)),
                "button": button,
                "url": page.url,
            }

    async def mouse_up(self, *, button: Literal["left", "right", "middle"]) -> dict[str, Any]:
        async with self._lock:
            await self._start_locked()
            page = self._require_page_locked()
            await page.mouse.up(button=button)
            return {"mouse_up": True, "button": button, "url": page.url}

    async def mouse_wheel(self, *, delta_x: float, delta_y: float) -> dict[str, Any]:
        async with self._lock:
            await self._start_locked()
            page = self._require_page_locked()
            await page.mouse.wheel(delta_x=float(delta_x), delta_y=float(delta_y))
            return {"wheel": {"delta_x": float(delta_x), "delta_y": float(delta_y)}, "url": page.url}

    async def file_upload(
        self,
        *,
        uid: str | None,
        selector: str | None,
        files: list[str],
        timeout_ms: int,
    ) -> dict[str, Any]:
        normalized: list[str] = []
        for item in files:
            if not isinstance(item, str) or not item.strip():
                continue
            file_path = Path(item).expanduser().resolve()
            if not file_path.exists() or not file_path.is_file():
                raise HTTPException(status_code=400, detail=f"file_not_found: {file_path}")
            normalized.append(str(file_path))
        if not normalized:
            raise HTTPException(status_code=400, detail="files_required")

        async with self._lock:
            await self._start_locked()
            page = self._require_page_locked()
            locator, target = self._resolve_locator(page, uid=uid, selector=selector)
            await locator.set_input_files(normalized, timeout=timeout_ms)
            return {"target": target, "files": normalized, "count": len(normalized), "url": page.url}

    async def handle_dialog(
        self,
        *,
        action: Literal["accept", "dismiss"],
        prompt_text: str | None,
        timeout_ms: int,
    ) -> dict[str, Any]:
        async with self._lock:
            await self._start_locked()
            self._require_page_locked()

            dialog = None
            while not self._dialog_queue.empty():
                try:
                    dialog = self._dialog_queue.get_nowait()
                except Exception:
                    break

            if dialog is None:
                try:
                    dialog = await asyncio.wait_for(
                        self._dialog_queue.get(),
                        timeout=max(1, timeout_ms) / 1000.0,
                    )
                except asyncio.TimeoutError as exc:
                    raise HTTPException(status_code=408, detail="dialog_not_found") from exc

            payload = {
                "type": getattr(dialog, "type", "unknown"),
                "message": getattr(dialog, "message", ""),
                "default_value": getattr(dialog, "default_value", ""),
                "action": action,
            }
            if action == "accept":
                await dialog.accept(prompt_text if prompt_text is not None else "")
            else:
                await dialog.dismiss()
            return payload

    async def close(self) -> dict[str, Any]:
        async with self._lock:
            was_connected = self._browser is not None
            await self._disconnect_locked()
            return {
                "closed": was_connected,
                "detached_only": True,
                "note": "Only the CDP session is disconnected. GUI Chrome process keeps running.",
            }

    async def shutdown(self) -> None:
        async with self._lock:
            await self._disconnect_locked()


browser_manager = BrowserManager()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    EXEC_ARTIFACT_ROOT.mkdir(parents=True, exist_ok=True)
    BROWSER_ARTIFACT_ROOT.mkdir(parents=True, exist_ok=True)
    UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)
    yield
    await browser_manager.shutdown()


app = FastAPI(title="StewardFlow Sandbox API", version="0.3.0", lifespan=lifespan)


class ExecRequest(BaseModel):
    command: str = Field(..., min_length=1, description="Shell command string.")
    cwd: str | None = Field(default=None, description="Working directory. Supports absolute or relative path.")
    timeout_ms: int = Field(default=DEFAULT_TIMEOUT_MS, ge=1, le=MAX_TIMEOUT_MS)
    env: dict[str, str] | None = Field(default=None, description="Extra environment variables.")
    preview_bytes: int = Field(default=DEFAULT_PREVIEW_BYTES, ge=128, le=65536)
    capture_limit_bytes: int = Field(default=DEFAULT_CAPTURE_LIMIT_BYTES, ge=1024, le=512 * 1024 * 1024)
    shell_executable: str | None = Field(default=None, description="Optional shell executable path.")


class BrowserNavigateRequest(BaseModel):
    url: str
    timeout_ms: int = Field(default=30000, ge=1, le=300000)
    wait_until: str = Field(default="domcontentloaded")


class BrowserNavigateBackRequest(BaseModel):
    timeout_ms: int = Field(default=30000, ge=1, le=300000)
    wait_until: str = Field(default="domcontentloaded")


class BrowserClickRequest(BaseModel):
    uid: str | None = None
    selector: str | None = None
    button: Literal["left", "right", "middle"] = "left"
    click_count: int = Field(default=1, ge=1, le=10)
    timeout_ms: int = Field(default=30000, ge=1, le=300000)


class BrowserHoverRequest(BaseModel):
    uid: str | None = None
    selector: str | None = None
    timeout_ms: int = Field(default=30000, ge=1, le=300000)


class BrowserDragRequest(BaseModel):
    from_uid: str | None = None
    from_selector: str | None = None
    to_uid: str | None = None
    to_selector: str | None = None
    timeout_ms: int = Field(default=30000, ge=1, le=300000)


class BrowserEvaluateRequest(BaseModel):
    expression: str
    arg: Any = None


class BrowserFileUploadRequest(BaseModel):
    uid: str | None = None
    selector: str | None = None
    files: list[str] = Field(default_factory=list, min_length=1)
    timeout_ms: int = Field(default=30000, ge=1, le=300000)


class BrowserFormField(BaseModel):
    uid: str | None = None
    selector: str | None = None
    value: str


class BrowserFillFormRequest(BaseModel):
    fields: list[BrowserFormField] = Field(default_factory=list, min_length=1)
    timeout_ms: int = Field(default=30000, ge=1, le=300000)
    submit: bool = False


class BrowserHandleDialogRequest(BaseModel):
    action: Literal["accept", "dismiss"] = "accept"
    prompt_text: str | None = None
    timeout_ms: int = Field(default=10000, ge=1, le=300000)


class BrowserPressKeyRequest(BaseModel):
    key: str
    uid: str | None = None
    selector: str | None = None
    timeout_ms: int = Field(default=30000, ge=1, le=300000)


class BrowserSelectOptionRequest(BaseModel):
    uid: str | None = None
    selector: str | None = None
    values: list[str] = Field(default_factory=list)
    labels: list[str] = Field(default_factory=list)
    indexes: list[int] = Field(default_factory=list)
    timeout_ms: int = Field(default=30000, ge=1, le=300000)


class BrowserSnapshotRequest(BaseModel):
    max_elements: int = Field(default=200, ge=1, le=2000)
    output_path: str | None = None


class BrowserTakeScreenshotRequest(BaseModel):
    uid: str | None = None
    selector: str | None = None
    output_path: str | None = None
    full_page: bool = True
    format: Literal["png", "jpeg", "webp"] = "png"
    quality: int | None = Field(default=None, ge=0, le=100)


class BrowserTypeRequest(BaseModel):
    text: str
    uid: str | None = None
    selector: str | None = None
    clear_before: bool = False
    delay_ms: int = Field(default=0, ge=0, le=2000)
    timeout_ms: int = Field(default=30000, ge=1, le=300000)


class BrowserWaitForRequest(BaseModel):
    text: list[str] = Field(default_factory=list)
    uid: str | None = None
    selector: str | None = None
    state: Literal["visible", "hidden", "attached", "detached"] = "visible"
    timeout_ms: int = Field(default=30000, ge=1, le=300000)


class BrowserTabsRequest(BaseModel):
    action: Literal["list", "new", "activate", "close", "close_others"] = "list"
    index: int | None = Field(default=None, ge=0)
    url: str | None = None
    timeout_ms: int = Field(default=30000, ge=1, le=300000)
    wait_until: str = Field(default="domcontentloaded")


class BrowserMouseClickXYRequest(BaseModel):
    x: float
    y: float
    button: Literal["left", "right", "middle"] = "left"
    click_count: int = Field(default=1, ge=1, le=10)
    delay_ms: int = Field(default=0, ge=0, le=5000)


class BrowserMouseDownRequest(BaseModel):
    button: Literal["left", "right", "middle"] = "left"


class BrowserMouseDragXYRequest(BaseModel):
    start_x: float
    start_y: float
    end_x: float
    end_y: float
    steps: int = Field(default=20, ge=1, le=1000)
    button: Literal["left", "right", "middle"] = "left"


class BrowserMouseMoveXYRequest(BaseModel):
    x: float
    y: float
    steps: int = Field(default=1, ge=1, le=1000)


class BrowserMouseUpRequest(BaseModel):
    button: Literal["left", "right", "middle"] = "left"


class BrowserMouseWheelRequest(BaseModel):
    delta_x: float = 0
    delta_y: float = 0


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/tools/exec")
async def tools_exec(req: ExecRequest) -> dict[str, Any]:
    run_id = _new_id("exec")
    run_dir = (EXEC_ARTIFACT_ROOT / run_id).resolve()
    run_dir.mkdir(parents=True, exist_ok=True)

    stdout_file = run_dir / "stdout.txt"
    stderr_file = run_dir / "stderr.txt"
    meta_file = run_dir / "meta.json"

    cwd_path = _resolve_cwd(req.cwd)
    if not cwd_path.exists() or not cwd_path.is_dir():
        raise HTTPException(status_code=400, detail=f"invalid cwd: {cwd_path}")

    env = os.environ.copy()
    if req.env:
        env.update(req.env)

    spawn_kwargs: dict[str, Any] = {}
    if os.name != "nt":
        spawn_kwargs["preexec_fn"] = os.setsid

    started_at = _utc_now_iso()
    try:
        proc = await asyncio.create_subprocess_shell(
            req.command,
            cwd=str(cwd_path),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            executable=req.shell_executable,
            **spawn_kwargs,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"spawn_failed: {exc}") from exc

    stdout_task = asyncio.create_task(
        _capture_stream(
            proc.stdout,
            stdout_file,
            preview_bytes=req.preview_bytes,
            capture_limit_bytes=req.capture_limit_bytes,
        )
    )
    stderr_task = asyncio.create_task(
        _capture_stream(
            proc.stderr,
            stderr_file,
            preview_bytes=req.preview_bytes,
            capture_limit_bytes=req.capture_limit_bytes,
        )
    )

    timed_out = False
    try:
        await asyncio.wait_for(proc.wait(), timeout=req.timeout_ms / 1000.0)
    except asyncio.TimeoutError:
        timed_out = True
        await _kill_process_tree(proc)
        await proc.wait()

    stdout_info, stderr_info = await asyncio.gather(stdout_task, stderr_task)
    exit_code = -1 if proc.returncode is None else int(proc.returncode)
    finished_at = _utc_now_iso()

    meta = {
        "run_id": run_id,
        "command": req.command,
        "cwd": str(cwd_path),
        "timeout_ms": req.timeout_ms,
        "timed_out": timed_out,
        "exit_code": exit_code,
        "started_at": started_at,
        "finished_at": finished_at,
        "stdout": {k: v for k, v in stdout_info.items() if k != "preview"},
        "stderr": {k: v for k, v in stderr_info.items() if k != "preview"},
    }
    meta_file.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    return {
        "ok": (not timed_out and exit_code == 0),
        "run_id": run_id,
        "exit_code": exit_code,
        "timed_out": timed_out,
        "cwd": str(cwd_path),
        "artifacts": {
            "run_dir": str(run_dir),
            "meta_json": str(meta_file),
            "stdout_path": str(stdout_file),
            "stderr_path": str(stderr_file),
        },
        "stdout_preview": stdout_info["preview"],
        "stderr_preview": stderr_info["preview"],
        "stdout_truncated": stdout_info["truncated"],
        "stderr_truncated": stderr_info["truncated"],
        "query_hints": [
            f"rg -n \"pattern\" {stdout_file}",
            f"sed -n '1,120p' {stdout_file}",
            f"tail -n 80 {stderr_file}",
        ],
    }


@app.get("/tools/exec/{run_id}")
def tools_exec_meta(run_id: str) -> dict[str, Any]:
    safe_id = run_id.strip()
    if not safe_id or "/" in safe_id or "\\" in safe_id:
        raise HTTPException(status_code=400, detail="invalid_run_id")
    meta_file = (EXEC_ARTIFACT_ROOT / safe_id / "meta.json").resolve()
    if not meta_file.exists():
        raise HTTPException(status_code=404, detail="run_id_not_found")
    return json.loads(meta_file.read_text(encoding="utf-8"))


async def _attach_permission_marker(payload: dict[str, Any]) -> dict[str, Any]:
    marker = await browser_manager.permission_marker()
    enriched = dict(payload)
    enriched["permission_marker"] = marker
    enriched["needs_user_authorization"] = bool(marker.get("permission_prompt_detected"))
    return enriched


@app.get("/browser/state")
async def browser_state() -> dict[str, Any]:
    return await browser_manager.state()


@app.post("/browser/click")
async def browser_click(req: BrowserClickRequest) -> dict[str, Any]:
    payload = await browser_manager.click(
        uid=req.uid,
        selector=req.selector,
        button=req.button,
        click_count=req.click_count,
        timeout_ms=req.timeout_ms,
    )
    return await _attach_permission_marker(payload)


@app.post("/browser/close")
async def browser_close() -> dict[str, Any]:
    return await browser_manager.close()


@app.post("/browser/drag")
async def browser_drag(req: BrowserDragRequest) -> dict[str, Any]:
    payload = await browser_manager.drag(
        from_uid=req.from_uid,
        from_selector=req.from_selector,
        to_uid=req.to_uid,
        to_selector=req.to_selector,
        timeout_ms=req.timeout_ms,
    )
    return await _attach_permission_marker(payload)


@app.post("/browser/evaluate")
async def browser_evaluate(req: BrowserEvaluateRequest) -> dict[str, Any]:
    payload = await browser_manager.evaluate(expression=req.expression, arg=req.arg)
    return await _attach_permission_marker(payload)


@app.post("/browser/file_upload")
async def browser_file_upload(req: BrowserFileUploadRequest) -> dict[str, Any]:
    payload = await browser_manager.file_upload(
        uid=req.uid,
        selector=req.selector,
        files=req.files,
        timeout_ms=req.timeout_ms,
    )
    return await _attach_permission_marker(payload)


@app.post("/browser/fill_form")
async def browser_fill_form(req: BrowserFillFormRequest) -> dict[str, Any]:
    payload = await browser_manager.fill_form(
        fields=[item.model_dump() for item in req.fields],
        timeout_ms=req.timeout_ms,
        submit=req.submit,
    )
    return await _attach_permission_marker(payload)


@app.post("/browser/handle_dialog")
async def browser_handle_dialog(req: BrowserHandleDialogRequest) -> dict[str, Any]:
    payload = await browser_manager.handle_dialog(
        action=req.action,
        prompt_text=req.prompt_text,
        timeout_ms=req.timeout_ms,
    )
    return await _attach_permission_marker(payload)


@app.post("/browser/hover")
async def browser_hover(req: BrowserHoverRequest) -> dict[str, Any]:
    payload = await browser_manager.hover(uid=req.uid, selector=req.selector, timeout_ms=req.timeout_ms)
    return await _attach_permission_marker(payload)


@app.post("/browser/navigate")
async def browser_navigate(req: BrowserNavigateRequest) -> dict[str, Any]:
    payload = await browser_manager.navigate(
        url=req.url,
        timeout_ms=req.timeout_ms,
        wait_until=req.wait_until,
    )
    return await _attach_permission_marker(payload)


@app.post("/browser/navigate_back")
async def browser_navigate_back(req: BrowserNavigateBackRequest) -> dict[str, Any]:
    payload = await browser_manager.navigate_back(
        timeout_ms=req.timeout_ms,
        wait_until=req.wait_until,
    )
    return await _attach_permission_marker(payload)


@app.post("/browser/press_key")
async def browser_press_key(req: BrowserPressKeyRequest) -> dict[str, Any]:
    payload = await browser_manager.press_key(
        key=req.key,
        uid=req.uid,
        selector=req.selector,
        timeout_ms=req.timeout_ms,
    )
    return await _attach_permission_marker(payload)


@app.post("/browser/select_option")
async def browser_select_option(req: BrowserSelectOptionRequest) -> dict[str, Any]:
    payload = await browser_manager.select_option(
        uid=req.uid,
        selector=req.selector,
        values=req.values,
        labels=req.labels,
        indexes=req.indexes,
        timeout_ms=req.timeout_ms,
    )
    return await _attach_permission_marker(payload)


@app.post("/browser/snapshot")
async def browser_snapshot(req: BrowserSnapshotRequest) -> dict[str, Any]:
    payload = await browser_manager.snapshot(max_elements=req.max_elements, output_path=req.output_path)
    return await _attach_permission_marker(payload)


@app.post("/browser/take_screenshot")
async def browser_take_screenshot(req: BrowserTakeScreenshotRequest) -> dict[str, Any]:
    payload = await browser_manager.take_screenshot(
        uid=req.uid,
        selector=req.selector,
        output_path=req.output_path,
        full_page=req.full_page,
        image_format=req.format,
        quality=req.quality,
    )
    return await _attach_permission_marker(payload)


@app.post("/browser/type")
async def browser_type(req: BrowserTypeRequest) -> dict[str, Any]:
    payload = await browser_manager.type_text(
        text=req.text,
        uid=req.uid,
        selector=req.selector,
        clear_before=req.clear_before,
        delay_ms=req.delay_ms,
        timeout_ms=req.timeout_ms,
    )
    return await _attach_permission_marker(payload)


@app.post("/browser/wait_for")
async def browser_wait_for(req: BrowserWaitForRequest) -> dict[str, Any]:
    payload = await browser_manager.wait_for(
        texts=req.text,
        uid=req.uid,
        selector=req.selector,
        state=req.state,
        timeout_ms=req.timeout_ms,
    )
    return await _attach_permission_marker(payload)


@app.post("/browser/tabs")
async def browser_tabs(req: BrowserTabsRequest) -> dict[str, Any]:
    payload = await browser_manager.tabs(
        action=req.action,
        index=req.index,
        url=req.url,
        timeout_ms=req.timeout_ms,
        wait_until=req.wait_until,
    )
    return await _attach_permission_marker(payload)


@app.post("/browser/mouse_click_xy")
async def browser_mouse_click_xy(req: BrowserMouseClickXYRequest) -> dict[str, Any]:
    payload = await browser_manager.mouse_click_xy(
        x=req.x,
        y=req.y,
        button=req.button,
        click_count=req.click_count,
        delay_ms=req.delay_ms,
    )
    return await _attach_permission_marker(payload)


@app.post("/browser/mouse_down")
async def browser_mouse_down(req: BrowserMouseDownRequest) -> dict[str, Any]:
    payload = await browser_manager.mouse_down(button=req.button)
    return await _attach_permission_marker(payload)


@app.post("/browser/mouse_drag_xy")
async def browser_mouse_drag_xy(req: BrowserMouseDragXYRequest) -> dict[str, Any]:
    payload = await browser_manager.mouse_drag_xy(
        start_x=req.start_x,
        start_y=req.start_y,
        end_x=req.end_x,
        end_y=req.end_y,
        steps=req.steps,
        button=req.button,
    )
    return await _attach_permission_marker(payload)


@app.post("/browser/mouse_move_xy")
async def browser_mouse_move_xy(req: BrowserMouseMoveXYRequest) -> dict[str, Any]:
    payload = await browser_manager.mouse_move_xy(x=req.x, y=req.y, steps=req.steps)
    return await _attach_permission_marker(payload)


@app.post("/browser/mouse_up")
async def browser_mouse_up(req: BrowserMouseUpRequest) -> dict[str, Any]:
    payload = await browser_manager.mouse_up(button=req.button)
    return await _attach_permission_marker(payload)


@app.post("/browser/mouse_wheel")
async def browser_mouse_wheel(req: BrowserMouseWheelRequest) -> dict[str, Any]:
    payload = await browser_manager.mouse_wheel(delta_x=req.delta_x, delta_y=req.delta_y)
    return await _attach_permission_marker(payload)


@app.post("/files/upload")
async def files_upload(
    file: UploadFile = File(...),
    target_path: str | None = Form(default=None),
    destination_dir: str = Form(default="/config/uploads"),
    overwrite: bool = Form(default=True),
) -> dict[str, Any]:
    if target_path and target_path.strip():
        out_path = _resolve_any_path(target_path, UPLOAD_ROOT)
    else:
        if not file.filename:
            raise HTTPException(status_code=400, detail="filename_missing")
        out_path = (_resolve_any_path(destination_dir, UPLOAD_ROOT) / Path(file.filename).name).resolve()

    if out_path.exists() and not overwrite:
        raise HTTPException(status_code=409, detail=f"target_exists: {out_path}")

    out_path.parent.mkdir(parents=True, exist_ok=True)

    size = 0
    digest = hashlib.sha256()
    with out_path.open("wb") as handle:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            handle.write(chunk)
            digest.update(chunk)
            size += len(chunk)
    await file.close()

    return {
        "ok": True,
        "filename": file.filename,
        "content_type": file.content_type,
        "path": str(out_path),
        "size_bytes": size,
        "sha256": digest.hexdigest(),
    }


