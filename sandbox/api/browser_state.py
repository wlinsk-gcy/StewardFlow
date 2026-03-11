from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass, field
from typing import Any, Iterable


DEFAULT_CDP_URL = os.getenv("SANDBOX_BROWSER_CDP_URL", "http://127.0.0.1:9222")
SNAPSHOT_EVALUATE_SCRIPT = """
({ verbose }) => {
  const normalize = (value) => String(value || "").replace(/\\s+/g, " ").trim();
  const root = document.documentElement;
  let documentId = normalize(root.getAttribute("data-sf-document-id"));
  if (!documentId) {
    documentId = `doc-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
    root.setAttribute("data-sf-document-id", documentId);
  }

  let nextUid = Number.parseInt(root.getAttribute("data-sf-uid-seq") || "1", 10);
  if (!Number.isFinite(nextUid) || nextUid < 1) {
    nextUid = 1;
  }

  const actionableTags = new Set(["a", "button", "input", "textarea", "select", "summary"]);
  const actionableRoles = new Set([
    "button",
    "link",
    "checkbox",
    "radio",
    "switch",
    "tab",
    "menuitem",
    "menuitemcheckbox",
    "menuitemradio",
    "option",
    "combobox",
  ]);
  const ignoredRoles = new Set(["presentation", "none"]);

  const isVisible = (element) => {
    if (!(element instanceof Element)) {
      return false;
    }
    const style = window.getComputedStyle(element);
    if (
      !style ||
      style.display === "none" ||
      style.visibility === "hidden" ||
      style.visibility === "collapse" ||
      style.opacity === "0" ||
      style.pointerEvents === "none"
    ) {
      return false;
    }
    const rect = element.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) {
      return false;
    }
    const viewportWidth = window.innerWidth || document.documentElement.clientWidth || 0;
    const viewportHeight = window.innerHeight || document.documentElement.clientHeight || 0;
    const samplePoints = [
      [rect.left + rect.width / 2, rect.top + rect.height / 2],
      [rect.left + 1, rect.top + 1],
      [rect.right - 1, rect.top + 1],
      [rect.left + 1, rect.bottom - 1],
      [rect.right - 1, rect.bottom - 1],
    ];
    for (const [rawX, rawY] of samplePoints) {
      const x = Math.min(Math.max(rawX, 0), viewportWidth - 1);
      const y = Math.min(Math.max(rawY, 0), viewportHeight - 1);
      if (!Number.isFinite(x) || !Number.isFinite(y)) {
        continue;
      }
      const hit = document.elementFromPoint(x, y);
      if (hit && (hit === element || element.contains(hit))) {
        return true;
      }
    }
    return false;
  };

  const uniqueLines = [];
  const seenLineKeys = new Set();
  const pushLine = (line) => {
    const normalized = normalize(line);
    if (!normalized) {
      return;
    }
    const key = normalized.toLowerCase();
    if (seenLineKeys.has(key)) {
      return;
    }
    seenLineKeys.add(key);
    uniqueLines.push(normalized);
  };

  const roleValue = (element) => normalize(element.getAttribute("role")).toLowerCase();
  const hasText = (element) => normalize(element.innerText || element.textContent || element.value).length > 0;
  const hasClickablePointer = (element) => {
    const style = window.getComputedStyle(element);
    return !!style && style.pointerEvents !== "none" && style.cursor === "pointer";
  };
  const hasUsableTabIndex = (element) => {
    const raw = element.getAttribute("tabindex");
    if (raw === null) {
      return false;
    }
    const parsed = Number(raw);
    return Number.isFinite(parsed) ? parsed >= 0 : true;
  };
  const isStandardActionTag = (element) => {
    const tagName = element.tagName.toLowerCase();
    if (!actionableTags.has(tagName)) {
      return false;
    }
    if (tagName === "input") {
      const inputType = normalize(element.getAttribute("type")).toLowerCase();
      return inputType !== "hidden";
    }
    return true;
  };
  const isActionableElement = (element) => {
    if (!(element instanceof Element) || !isVisible(element)) {
      return false;
    }
    const role = roleValue(element);
    if (ignoredRoles.has(role)) {
      return false;
    }
    if (isStandardActionTag(element)) {
      return true;
    }
    if (actionableRoles.has(role)) {
      return true;
    }
    if (element.matches("[contenteditable='true']")) {
      return true;
    }
    if (hasUsableTabIndex(element)) {
      return true;
    }
    return hasClickablePointer(element) && hasText(element);
  };
  const normalizeActionTarget = (element) => {
    if (!(element instanceof Element)) {
      return null;
    }
    const tagName = element.tagName.toLowerCase();
    if (tagName === "option") {
      return element.closest("select") || element;
    }
    if (tagName === "label") {
      return null;
    }
    return element;
  };
  const resolveActionTarget = (element) => {
    let current = element;
    while (current instanceof Element) {
      const target = normalizeActionTarget(current);
      if (target && isActionableElement(target)) {
        return target;
      }
      current = current.parentElement;
    }
    return null;
  };

  const describeElement = (element) => {
    const values = [];
    const seenValues = new Set();
    const pushValue = (value) => {
      const normalized = normalize(value);
      if (!normalized) {
        return;
      }
      const key = normalized.toLowerCase();
      if (seenValues.has(key)) {
        return;
      }
      seenValues.add(key);
      values.push(normalized);
    };

    const labels = element.labels ? Array.from(element.labels) : [];
    pushValue(element.tagName.toLowerCase());
    pushValue(element.getAttribute("type"));
    pushValue(element.getAttribute("role"));
    for (const label of labels) {
      pushValue(label.innerText || label.textContent);
    }
    pushValue(element.getAttribute("aria-label"));
    pushValue(element.getAttribute("placeholder"));
    pushValue(element.innerText || element.textContent);
    if (verbose) {
      pushValue(element.value);
      pushValue(element.getAttribute("title"));
      pushValue(element.getAttribute("name"));
      pushValue(element.id);
    }
    return values.join(" | ");
  };

  const descriptorClassTokens = (value) =>
    normalize(value)
      .split(/\s+/)
      .map((token) => token.trim())
      .filter(
        (token) =>
          token &&
          token.length <= 40 &&
          /[a-zA-Z]/.test(token) &&
          ((token.match(/\d/g) || []).length <= 3)
      )
      .slice(0, 8);
  const descriptorText = (element) => normalize(element.innerText || element.textContent).slice(0, 160);
  const descriptorLabelText = (element) => {
    const labels = element.labels ? Array.from(element.labels) : [];
    return normalize(labels.map((label) => label.innerText || label.textContent).join(" "))
      .slice(0, 160);
  };
  const descriptorSignatureText = (descriptor) =>
    normalize(descriptor.placeholder || descriptor.ariaLabel || descriptor.labelText || descriptor.text)
      .slice(0, 80)
      .toLowerCase();

  const interactiveSelector = [
    "a",
    "button",
    "input",
    "textarea",
    "select",
    "summary",
    "[role]",
    "[tabindex]",
    "[contenteditable='true']",
    "[onclick]",
    "div",
    "span",
  ].join(",");
  const interactiveElements = [];
  const seenElements = new Set();
  for (const element of document.querySelectorAll(interactiveSelector)) {
    if (!isVisible(element)) {
      continue;
    }
    const target = resolveActionTarget(element);
    if (!target || seenElements.has(target)) {
      continue;
    }
    seenElements.add(target);
    interactiveElements.push(target);
  }

  const uids = [];
  const descriptors = {};
  const signatureCounts = new Map();
  for (let actionIndex = 0; actionIndex < interactiveElements.length; actionIndex += 1) {
    const element = interactiveElements[actionIndex];
    let uid = normalize(element.getAttribute("data-sf-uid"));
    if (!uid) {
      uid = `sf-${nextUid}`;
      nextUid += 1;
      element.setAttribute("data-sf-uid", uid);
    }
    uids.push(uid);
    const descriptor = {
      tag: element.tagName.toLowerCase(),
      role: roleValue(element),
      inputType: normalize(element.getAttribute("type")).toLowerCase(),
      text: descriptorText(element),
      placeholder: normalize(element.getAttribute("placeholder")),
      ariaLabel: normalize(element.getAttribute("aria-label")),
      labelText: descriptorLabelText(element),
      name: normalize(element.getAttribute("name")),
      title: normalize(element.getAttribute("title")),
      href: normalize(element.getAttribute("href")),
      value: normalize(element.value),
      classTokens: descriptorClassTokens(element.className),
      actionIndex,
    };
    const signatureKey = [
      descriptor.tag,
      descriptor.role,
      descriptor.inputType,
      descriptorSignatureText(descriptor),
    ].join("|");
    const signatureIndex = signatureCounts.get(signatureKey) || 0;
    signatureCounts.set(signatureKey, signatureIndex + 1);
    descriptor.signatureKey = signatureKey;
    descriptor.signatureIndex = signatureIndex;
    descriptors[uid] = descriptor;
    pushLine(`[uid=${uid}] ${describeElement(element)}`);
  }
  root.setAttribute("data-sf-uid-seq", String(nextUid));

  const textSelector = verbose
    ? "h1,h2,h3,h4,h5,h6,p,li,dt,dd,blockquote,pre,code,main,article,section,div,span"
    : "h1,h2,h3,p,li,main,article,section";
  for (const element of document.querySelectorAll(textSelector)) {
    if (seenElements.has(element) || !isVisible(element)) {
      continue;
    }
    const text = normalize(element.innerText || element.textContent);
    if (!text) {
      continue;
    }
    pushLine(text.slice(0, 240));
  }

  if (!uniqueLines.length) {
    pushLine(document.body ? document.body.innerText || document.body.textContent : "");
  }

  return {
    documentId,
    lines: uniqueLines,
    uids,
    descriptors,
  };
}
"""


@dataclass(slots=True)
class SnapshotRecord:
    snapshot_id: int
    page_id: int
    document_id: str
    created_at: float
    uids: set[str] = field(default_factory=set)
    text: str = ""
    descriptors: dict[str, dict[str, Any]] = field(default_factory=dict)


class BrowserState:
    def __init__(self, *, cdp_url: str | None = None) -> None:
        self.cdp_url = cdp_url or DEFAULT_CDP_URL
        self._lock = asyncio.Lock()
        self.playwright: Any | None = None
        self.browser: Any | None = None
        self.context: Any | None = None

        self.page_id_to_page: dict[int, Any] = {}
        self.page_key_to_id: dict[int, int] = {}
        self.selected_page_id: int | None = None
        self.next_page_id = 1

        self.page_document_ids: dict[int, str] = {}
        self.snapshots_by_id: dict[int, SnapshotRecord] = {}
        self.latest_snapshot_by_page: dict[int, SnapshotRecord] = {}
        self.next_snapshot_id = 1

        self.dialog_queue: list[Any] = []
        self._dialog_event = asyncio.Event()
        self._dialog_hooked_page_keys: set[int] = set()
        self._context_page_listener_registered = False

    def _queue_dialog(self, dialog: Any) -> None:
        self.dialog_queue.append(dialog)
        self._dialog_event.set()

    def _hook_page_events(self, page: Any) -> None:
        page_key = id(page)
        if page_key in self._dialog_hooked_page_keys:
            return
        page_on = getattr(page, "on", None)
        if not callable(page_on):
            return
        page_on("dialog", self._queue_dialog)
        self._dialog_hooked_page_keys.add(page_key)

    def _handle_context_page(self, page: Any) -> None:
        context_pages = list(getattr(self.context, "pages", []) or [])
        if page not in context_pages:
            context_pages.append(page)
        self.sync_pages(context_pages)

    def sync_pages(self, pages: Iterable[Any]) -> dict[int, Any]:
        current_pages = list(pages)
        next_mapping: dict[int, Any] = {}
        live_page_ids: set[int] = set()

        for page in current_pages:
            self._hook_page_events(page)
            page_key = id(page)
            page_id = self.page_key_to_id.get(page_key)
            if page_id is None:
                page_id = self.next_page_id
                self.next_page_id += 1
                self.page_key_to_id[page_key] = page_id
            next_mapping[page_id] = page
            live_page_ids.add(page_id)

        for page_key, page_id in list(self.page_key_to_id.items()):
            if page_id not in live_page_ids:
                self.page_key_to_id.pop(page_key, None)
                self._dialog_hooked_page_keys.discard(page_key)

        for page_id in list(self.page_document_ids):
            if page_id not in live_page_ids:
                self.page_document_ids.pop(page_id, None)

        for page_id in list(self.latest_snapshot_by_page):
            if page_id not in live_page_ids:
                self.latest_snapshot_by_page.pop(page_id, None)

        for snapshot_id, record in list(self.snapshots_by_id.items()):
            if record.page_id not in live_page_ids:
                self.snapshots_by_id.pop(snapshot_id, None)

        self.page_id_to_page = next_mapping
        if self.selected_page_id not in self.page_id_to_page:
            self.selected_page_id = next(iter(self.page_id_to_page), None)
        return dict(self.page_id_to_page)

    def get_page_id(self, page: Any) -> int | None:
        return self.page_key_to_id.get(id(page))

    def get_selected_page(self) -> Any | None:
        if self.selected_page_id is None:
            return None
        return self.page_id_to_page.get(self.selected_page_id)

    def register_snapshot(
        self,
        *,
        page_id: int,
        document_id: str,
        uids: set[str],
        text: str,
        descriptors: dict[str, dict[str, Any]] | None = None,
    ) -> SnapshotRecord:
        record = SnapshotRecord(
            snapshot_id=self.next_snapshot_id,
            page_id=page_id,
            document_id=document_id,
            created_at=time.time(),
            uids=set(uids),
            text=text,
            descriptors=dict(descriptors or {}),
        )
        self.next_snapshot_id += 1
        self.page_document_ids[page_id] = document_id
        self.snapshots_by_id[record.snapshot_id] = record
        self.latest_snapshot_by_page[page_id] = record
        return record

    def pop_dialog(self) -> Any | None:
        if not self.dialog_queue:
            return None
        dialog = self.dialog_queue.pop(0)
        if not self.dialog_queue:
            self._dialog_event.clear()
        return dialog

    def clear_runtime_state(self) -> None:
        self.page_document_ids.clear()
        self.snapshots_by_id.clear()
        self.latest_snapshot_by_page.clear()
        self.dialog_queue.clear()
        self._dialog_event = asyncio.Event()

    async def wait_for_dialog(self, timeout: float) -> bool:
        if self.dialog_queue:
            return True
        try:
            await asyncio.wait_for(self._dialog_event.wait(), timeout=timeout)
        except TimeoutError:
            return False
        return True

    async def ensure_context(self) -> Any:
        async with self._lock:
            if self.context is not None:
                return self.context

            from playwright.async_api import async_playwright

            if self.playwright is None:
                self.playwright = await async_playwright().start()
            if self.browser is None:
                self.browser = await self.playwright.chromium.connect_over_cdp(self.cdp_url)

            contexts = list(getattr(self.browser, "contexts", []))
            if contexts:
                self.context = contexts[0]
                for extra_context in contexts[1:]:
                    try:
                        await extra_context.close()
                    except Exception:
                        continue
            else:
                self.context = await self.browser.new_context()

            if not self._context_page_listener_registered:
                context_on = getattr(self.context, "on", None)
                if callable(context_on):
                    context_on("page", self._handle_context_page)
                    self._context_page_listener_registered = True

            self.sync_pages(list(getattr(self.context, "pages", [])))
            return self.context

    async def ensure_selected_page(self) -> tuple[int, Any]:
        context = await self.ensure_context()
        pages = list(getattr(context, "pages", []))
        if not pages:
            page = await context.new_page()
            pages = [page]
        self.sync_pages(pages)
        page = self.get_selected_page()
        if page is None:
            page = pages[0]
            self.selected_page_id = self.get_page_id(page)
        if self.selected_page_id is None:
            raise RuntimeError("browser page selection is unavailable")
        return self.selected_page_id, page

    async def capture_snapshot(self, page: Any, *, verbose: bool) -> dict[str, Any]:
        evaluate = getattr(page, "evaluate", None)
        if not callable(evaluate):
            raise RuntimeError("page snapshot evaluation is unavailable")

        snapshot = await evaluate(
            SNAPSHOT_EVALUATE_SCRIPT,
            {"verbose": bool(verbose)},
        )
        if not isinstance(snapshot, dict):
            snapshot = {}

        lines = [str(line).strip() for line in (snapshot.get("lines") or []) if str(line).strip()]
        document_id = str(snapshot.get("documentId") or snapshot.get("document_id") or "")
        if not document_id:
            page_id = self.get_page_id(page)
            document_id = f"doc-{page_id or int(time.time())}"

        return {
            "text": "\n".join(lines),
            "document_id": document_id,
            "uids": {str(uid) for uid in (snapshot.get("uids") or []) if str(uid).strip()},
            "descriptors": {
                str(uid): value
                for uid, value in dict(snapshot.get("descriptors") or {}).items()
                if str(uid).strip() and isinstance(value, dict)
            },
        }

    async def shutdown(self) -> None:
        async with self._lock:
            if self.browser is not None:
                try:
                    await self.browser.close()
                except Exception:
                    pass
            if self.playwright is not None:
                try:
                    await self.playwright.stop()
                except Exception:
                    pass

            self.playwright = None
            self.browser = None
            self.context = None
            self.page_id_to_page.clear()
            self.page_key_to_id.clear()
            self.selected_page_id = None
            self.page_document_ids.clear()
            self.snapshots_by_id.clear()
            self.latest_snapshot_by_page.clear()
            self.dialog_queue.clear()
            self._dialog_event = asyncio.Event()
            self._dialog_hooked_page_keys.clear()
            self._context_page_listener_registered = False


_BROWSER_STATE: BrowserState | None = None


def get_browser_state() -> BrowserState:
    global _BROWSER_STATE
    if _BROWSER_STATE is None:
        _BROWSER_STATE = BrowserState()
    return _BROWSER_STATE
