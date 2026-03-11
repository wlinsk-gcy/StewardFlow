from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path
from typing import Any, Awaitable, Callable

from .tool_runtime import (
    ToolExecutionError,
    ToolInputError,
    apply_unified_truncation,
    resolve_path,
)


TextArtifactWriter = Callable[[str, str], str]
BinaryArtifactWriter = Callable[[str, bytes, str], str]
DIALOG_OPEN_WAIT_SECONDS = 1.0
_WHITESPACE_RE = re.compile(r"\s+")
_LOGIN_SUCCESS_MARKERS = (
    "login successful",
    "logged in",
    "sign in successful",
    "signed in",
    "验证通过",
    "授权成功",
)
_CAPTCHA_MARKERS = (
    "captcha",
    "verify you are human",
    "i'm not a robot",
    "robot check",
    "security check",
    "cloudflare",
    "人机验证",
    "滑块",
)
_OTP_MARKERS = (
    "otp",
    "2fa",
    "mfa",
    "two-factor",
    "two factor",
    "one-time password",
    "one time password",
    "verification code",
    "sms code",
    "验证码",
    "短信验证码",
    "二次验证",
)
_ACCESS_DENIED_MARKERS = (
    "access denied",
    "unauthorized",
    "forbidden",
    "拒绝访问",
    "无权访问",
)
_ACCESS_DENIED_403_PATTERNS = (
    re.compile(r"(?:^|[\s([{:>])403(?:$|[\s)\]}:;,.!?<])"),
    re.compile(r"(?<![\w-])(?:http|https|status|error|code)\s*[:=-]?\s*403(?![\w-])"),
    re.compile(r"(?<![\w-])403\s+(?:forbidden|unauthorized|denied|error)(?!\w)"),
)
_LOGIN_TEXT_MARKERS = (
    "log in",
    "login",
    "sign in",
    "signin",
    "登录",
)
_PASSWORD_MARKERS = (
    "password",
    "密码",
)
_OAUTH_MARKERS = (
    "authorize",
    "authorization required",
    "grant access",
    "allow access",
    "consent",
    "授权",
)
_LOGIN_URL_MARKERS = (
    "/login",
    "/signin",
    "/sign-in",
    "/auth/login",
)
_OAUTH_URL_MARKERS = (
    "/oauth",
    "/authorize",
    "/consent",
)
DEFAULT_RESET_START_URL = os.getenv("START_URL", "chrome://new-tab-page/")
DEFAULT_EVALUATE_SCRIPT_TIMEOUT_MS = 3000
MAX_EVALUATE_SCRIPT_TIMEOUT_MS = 5000
_EVALUATE_SCRIPT_BLOCKLIST: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bfetch\s*\(", re.IGNORECASE), "fetch(...)"),
    (re.compile(r"\bXMLHttpRequest\b", re.IGNORECASE), "XMLHttpRequest"),
    (re.compile(r"\bnavigator\.sendBeacon\s*\(", re.IGNORECASE), "navigator.sendBeacon(...)"),
    (re.compile(r"\b(?:window\.)?location\s*=", re.IGNORECASE), "location assignment"),
    (
        re.compile(r"\b(?:window\.)?location\.(?:href|assign|replace)\s*(?:=|\()", re.IGNORECASE),
        "location navigation",
    ),
    (re.compile(r"(?:^|[^\w.])submit\s*\(", re.IGNORECASE), "submit(...)"),
    (re.compile(r"\.submit\s*\(", re.IGNORECASE), ".submit(...)"),
    (re.compile(r"(?:^|[^\w.])click\s*\(", re.IGNORECASE), "click(...)"),
    (re.compile(r"\.click\s*\(", re.IGNORECASE), ".click(...)"),
    (re.compile(r"\bdispatchEvent\s*\(", re.IGNORECASE), "dispatchEvent(...)"),
    (re.compile(r"\bhistory\.(?:pushState|replaceState)\s*\(", re.IGNORECASE), "history mutation"),
    (re.compile(r"\bscroll(?:To|By)\s*\(", re.IGNORECASE), "scroll mutation"),
)
_UID_RECOVERY_SCRIPT = """
({ uid, descriptor }) => {
    const normalize = (value) => String(value || "").replace(/\s+/g, " ").trim();
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
    const roleValue = (element) => normalize(element.getAttribute("role")).toLowerCase();
    const classTokens = (value) =>
        normalize(value)
            .split(/\s+/)
            .map((token) => token.trim().toLowerCase())
            .filter(
                (token) =>
                    token &&
                    token.length <= 40 &&
                    /[a-z]/.test(token) &&
                    ((token.match(/\d/g) || []).length <= 3)
            )
            .slice(0, 8);
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
    if (!descriptor || typeof descriptor !== "object") {
        return { recovered: false, reason: "missing_descriptor" };
    }

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

    const expectedTag = normalize(descriptor.tag).toLowerCase();
    const expectedRole = normalize(descriptor.role).toLowerCase();
    const expectedType = normalize(descriptor.inputType).toLowerCase();
    const expectedText = normalize(descriptor.text).slice(0, 160);
    const expectedPlaceholder = normalize(descriptor.placeholder);
    const expectedAriaLabel = normalize(descriptor.ariaLabel);
    const expectedLabelText = normalize(descriptor.labelText);
    const expectedName = normalize(descriptor.name);
    const expectedTitle = normalize(descriptor.title);
    const expectedHref = normalize(descriptor.href);
    const expectedValue = normalize(descriptor.value);
    const expectedClassTokens = Array.isArray(descriptor.classTokens)
        ? descriptor.classTokens.map((token) => normalize(token).toLowerCase()).filter(Boolean)
        : [];
    const expectedActionIndex = Number.isFinite(Number(descriptor.actionIndex)) ? Number(descriptor.actionIndex) : null;
    const expectedSignatureKey = normalize(descriptor.signatureKey).toLowerCase();
    const expectedSignatureIndex = Number.isFinite(Number(descriptor.signatureIndex)) ? Number(descriptor.signatureIndex) : null;

    const signatureCounts = new Map();
    const scored = [];
    for (let actionIndex = 0; actionIndex < interactiveElements.length; actionIndex += 1) {
        const element = interactiveElements[actionIndex];
        const tag = element.tagName.toLowerCase();
        const role = roleValue(element);
        const inputType = normalize(element.getAttribute("type")).toLowerCase();
        const text = normalize(element.innerText || element.textContent).slice(0, 160);
        const placeholder = normalize(element.getAttribute("placeholder"));
        const ariaLabel = normalize(element.getAttribute("aria-label"));
        const labels = element.labels ? Array.from(element.labels) : [];
        const labelText = normalize(labels.map((label) => label.innerText || label.textContent).join(" "))
            .slice(0, 160);
        const name = normalize(element.getAttribute("name"));
        const title = normalize(element.getAttribute("title"));
        const href = normalize(element.getAttribute("href"));
        const value = normalize(element.value);
        const candidateClassTokens = classTokens(element.className);
        const signatureText = normalize(placeholder || ariaLabel || labelText || text).slice(0, 80).toLowerCase();
        const signatureKey = [tag, role, inputType, signatureText].join("|");
        const signatureIndex = signatureCounts.get(signatureKey) || 0;
        signatureCounts.set(signatureKey, signatureIndex + 1);

        let score = 0;
        let strong = 0;
        const reasons = [];

        if (expectedTag && tag === expectedTag) {
            score += 4;
            reasons.push("tag");
        }
        if (expectedRole && role === expectedRole) {
            score += 3;
            reasons.push("role");
        }
        if (expectedType && inputType === expectedType) {
            score += 2;
            reasons.push("type");
        }
        if (expectedPlaceholder && placeholder && placeholder === expectedPlaceholder) {
            score += 7;
            strong += 1;
            reasons.push("placeholder");
        }
        if (expectedAriaLabel && ariaLabel && ariaLabel === expectedAriaLabel) {
            score += 7;
            strong += 1;
            reasons.push("aria");
        }
        if (expectedLabelText && labelText && labelText === expectedLabelText) {
            score += 5;
            strong += 1;
            reasons.push("label");
        }
        if (expectedTitle && title && title === expectedTitle) {
            score += 3;
            reasons.push("title");
        }
        if (expectedName && name && name === expectedName) {
            score += 3;
            reasons.push("name");
        }
        if (expectedHref && href && href === expectedHref) {
            score += 6;
            strong += 1;
            reasons.push("href");
        }
        if (expectedValue && value && value === expectedValue) {
            score += 2;
            reasons.push("value");
        }
        if (expectedText && text) {
            if (text === expectedText) {
                score += 8;
                strong += 1;
                reasons.push("text");
            } else if (expectedText.length >= 8 && (text.includes(expectedText) || expectedText.includes(text))) {
                score += 4;
                reasons.push("text_partial");
            }
        }
        if (expectedClassTokens.length) {
            const overlap = expectedClassTokens.filter((token) => candidateClassTokens.includes(token)).length;
            if (overlap > 0) {
                score += Math.min(overlap, 3);
                reasons.push("class");
            }
        }
        if (expectedActionIndex !== null) {
            const distance = Math.abs(actionIndex - expectedActionIndex);
            if (distance === 0) {
                score += 3;
            } else if (distance <= 2) {
                score += 2;
            } else if (distance <= 5) {
                score += 1;
            } else if (distance >= 25) {
                score -= 2;
            }
        }
        if (expectedSignatureKey && signatureKey === expectedSignatureKey && expectedSignatureIndex !== null) {
            const distance = Math.abs(signatureIndex - expectedSignatureIndex);
            if (distance === 0) {
                score += 2;
            } else if (distance <= 1) {
                score += 1;
            }
        }
        if (strong === 0 && score < 7) {
            continue;
        }
        scored.push({ element, score, strong, reasons, tag, text });
    }

    if (!scored.length) {
        return { recovered: false, reason: "no_match" };
    }

    scored.sort((left, right) => {
        if (right.score !== left.score) {
            return right.score - left.score;
        }
        if (right.strong !== left.strong) {
            return right.strong - left.strong;
        }
        return 0;
    });

    const best = scored[0];
    const second = scored[1];
    if (!best || best.score < 7 || best.strong < 1) {
        return { recovered: false, reason: "low_confidence", bestScore: best ? best.score : 0 };
    }
    if (second && best.score === second.score && best.strong === second.strong) {
        return { recovered: false, reason: "ambiguous", bestScore: best.score };
    }

    for (const existing of document.querySelectorAll(`[data-sf-uid="${uid}"]`)) {
        if (existing !== best.element) {
            existing.removeAttribute("data-sf-uid");
        }
    }
    best.element.setAttribute("data-sf-uid", uid);
    return {
        recovered: true,
        score: best.score,
        strong: best.strong,
        tag: best.tag,
        text: best.text,
        reasons: best.reasons,
    };
}
"""


def _default_artifact_writer(tool_name: str, text: str) -> str:
    _ = tool_name
    _ = text
    return ""


def _default_binary_artifact_writer(tool_name: str, data: bytes, suffix: str) -> str:
    _ = tool_name
    _ = data
    _ = suffix
    return ""


def _record_value(record: Any, key: str) -> Any:
    if isinstance(record, dict):
        return record.get(key)
    return getattr(record, key, None)


def _normalize_barrier_text(value: Any) -> str:
    return _WHITESPACE_RE.sub(" ", str(value or "")).strip().lower()


def _collect_marker_signals(source: str, haystack: str, markers: tuple[str, ...]) -> list[str]:
    signals: list[str] = []
    for marker in markers:
        normalized = _normalize_barrier_text(marker)
        if normalized and normalized in haystack:
            signals.append(f"{source}:{marker}")
    return signals


def _collect_access_denied_403_signals(source: str, haystack: str) -> list[str]:
    if not haystack:
        return []
    for pattern in _ACCESS_DENIED_403_PATTERNS:
        if pattern.search(haystack):
            return [f"{source}:403"]
    return []


def _detect_hitl_barrier(*, text: str, url: str = "", title: str = "") -> dict[str, Any] | None:
    normalized_text = _normalize_barrier_text(text)
    normalized_url = _normalize_barrier_text(url)
    normalized_title = _normalize_barrier_text(title)

    if not any((normalized_text, normalized_url, normalized_title)):
        return None
    if any(marker in normalized_text for marker in _LOGIN_SUCCESS_MARKERS):
        return None

    kinds_and_signals = (
        ("captcha", _collect_marker_signals("text", normalized_text, _CAPTCHA_MARKERS)),
        ("otp", _collect_marker_signals("text", normalized_text, _OTP_MARKERS)),
        (
            "oauth_consent",
            _collect_marker_signals("text", normalized_text, _OAUTH_MARKERS)
            + _collect_marker_signals("url", normalized_url, _OAUTH_URL_MARKERS),
        ),
        (
            "access_denied",
            _collect_marker_signals("text", normalized_text, _ACCESS_DENIED_MARKERS)
            + _collect_access_denied_403_signals("text", normalized_text),
        ),
    )
    for kind, signals in kinds_and_signals:
        if signals:
            return {
                "required": True,
                "kind": kind,
                "confidence": "high" if len(signals) > 1 else "medium",
                "signals": signals,
            }

    login_signals = _collect_marker_signals("text", normalized_text, _LOGIN_TEXT_MARKERS)
    login_signals.extend(_collect_marker_signals("url", normalized_url, _LOGIN_URL_MARKERS))
    login_signals.extend(_collect_marker_signals("title", normalized_title, _LOGIN_TEXT_MARKERS))
    password_signals = _collect_marker_signals("text", normalized_text, _PASSWORD_MARKERS)
    if password_signals and login_signals:
        signals = login_signals + password_signals
        return {
            "required": True,
            "kind": "login",
            "confidence": "high",
            "signals": signals,
        }
    return None


async def _read_page_title(page: Any) -> str:
    title_method = getattr(page, "title", None)
    if not callable(title_method):
        return ""
    title = title_method()
    if isinstance(title, Awaitable):
        title = await title
    return str(title or "")


async def _page_summary_metadata(state: Any, *, active_page_id: int) -> dict[str, Any]:
    page_map = dict(getattr(state, "page_id_to_page", {}) or {})
    pages: list[dict[str, Any]] = []
    for page_id, page in page_map.items():
        pages.append(
            {
                "pageId": int(page_id),
                "url": str(getattr(page, "url", "")),
                "title": await _read_page_title(page),
                "isActive": int(page_id) == int(active_page_id),
            }
        )
    pages.sort(key=lambda item: item["pageId"])
    return {
        "activePageId": int(active_page_id),
        "pageCount": len(pages),
        "pages": pages,
    }


async def _probe_page_barrier(page: Any) -> dict[str, Any] | None:
    evaluate = getattr(page, "evaluate", None)
    payload: dict[str, Any] = {}
    if callable(evaluate):
        try:
            raw = await evaluate(
                """
                () => ({
                    text: document?.body?.innerText || document?.body?.textContent || "",
                    title: document?.title || "",
                    url: window?.location?.href || ""
                })
                """
            )
        except Exception:
            raw = {}
        if isinstance(raw, dict):
            payload = raw

    return _detect_hitl_barrier(
        text=str(payload.get("text", "")),
        url=str(payload.get("url") or getattr(page, "url", "")),
        title=str(payload.get("title", "")) or await _read_page_title(page),
    )


async def _require_selected_page(state: Any) -> tuple[int, Any]:
    ensure_selected_page = getattr(state, "ensure_selected_page", None)
    if callable(ensure_selected_page):
        page_id, page = await ensure_selected_page()
        return int(page_id), page

    sync_pages = getattr(state, "sync_pages", None)
    if callable(sync_pages):
        sync_pages()

    page_id = getattr(state, "selected_page_id", None)
    page = None
    get_selected_page = getattr(state, "get_selected_page", None)
    if callable(get_selected_page):
        page = get_selected_page()
    elif page_id is not None:
        page = (getattr(state, "page_id_to_page", {}) or {}).get(page_id)

    if page_id is None or page is None:
        raise ToolExecutionError("browser page selection is unavailable")
    return int(page_id), page


async def _require_page(state: Any, *, page_id: int | None = None) -> tuple[int, Any]:
    if page_id is None:
        return await _require_selected_page(state)

    ensure_context = getattr(state, "ensure_context", None)
    if not callable(ensure_context):
        raise ToolExecutionError("browser context is unavailable")
    context = await ensure_context()
    pages = list(getattr(context, "pages", []))
    if not pages:
        page = await context.new_page()
        pages = [page]

    sync_pages = getattr(state, "sync_pages", None)
    if callable(sync_pages):
        sync_pages(pages)

    target_page_id = int(page_id)
    page = (getattr(state, "page_id_to_page", {}) or {}).get(target_page_id)
    if page is None:
        raise ToolInputError(f"unknown pageId: {page_id}")
    return target_page_id, page


async def _ensure_page_document_id(state: Any, *, page: Any, page_id: int) -> str:
    evaluate = getattr(page, "evaluate", None)
    if not callable(evaluate):
        raise ToolExecutionError("page evaluation is unavailable")

    try:
        raw = await evaluate(
            """
            () => {
                const root = document.documentElement;
                const normalize = (value) => String(value || "").replace(/\\s+/g, " ").trim();
                let documentId = normalize(root?.getAttribute?.("data-sf-document-id"));
                if (!documentId) {
                    documentId = `doc-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
                    root?.setAttribute?.("data-sf-document-id", documentId);
                }
                return documentId;
            }
            """
        )
    except Exception as exc:
        raise ToolExecutionError("page document id is unavailable") from exc

    document_id = str(raw or "").strip() or f"doc-{page_id}"
    page_document_ids = getattr(state, "page_document_ids", None)
    if isinstance(page_document_ids, dict):
        page_document_ids[page_id] = document_id
    return document_id


def _normalize_evaluate_script_source(script: str) -> str:
    source = str(script or "").strip()
    if not source:
        raise ToolInputError("script must not be empty")
    for pattern, label in _EVALUATE_SCRIPT_BLOCKLIST:
        if pattern.search(source):
            raise ToolInputError(
                f"evaluate_script is read-only; disallowed pattern detected: {label}"
            )
    return source


def _json_result_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _format_evaluate_script_output(
    *,
    page_id: int,
    document_id: str,
    result_type: str,
    result_text: str,
) -> str:
    return "\n".join(
        [
            f"<page_id>{page_id}</page_id>",
            f"<document_id>{document_id}</document_id>",
            f"<result_type>{result_type}</result_type>",
            "<content>",
            result_text,
            "</content>",
        ]
    )


async def _format_browser_result(
    output: str,
    *,
    state: Any,
    page_id: int,
    tool_name: str,
    artifact_writer: TextArtifactWriter,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload_metadata = await _page_summary_metadata(state, active_page_id=page_id)
    payload_metadata.update(dict(metadata or {}))
    if "hitlBarrier" not in payload_metadata:
        page = (getattr(state, "page_id_to_page", {}) or {}).get(page_id)
        if page is not None:
            hitl_barrier = await _probe_page_barrier(page)
            if hitl_barrier is not None:
                payload_metadata["hitlBarrier"] = hitl_barrier
    payload_metadata["pageId"] = int(page_id)
    return apply_unified_truncation(
        {
            "output": output,
            "metadata": payload_metadata,
        },
        tool_name=tool_name,
        artifact_writer=artifact_writer,
    )


def _latest_snapshot_record(state: Any, page_id: int) -> Any:
    snapshots = (getattr(state, "latest_snapshot_by_page", {}) or {})
    return snapshots.get(page_id)


def _validate_latest_uid(state: Any, *, page_id: int, uid: str) -> None:
    record = _latest_snapshot_record(state, page_id)
    if record is None:
        raise ToolInputError(f"snapshot is stale for pageId={page_id}; call take_snapshot again")

    current_document_id = (getattr(state, "page_document_ids", {}) or {}).get(page_id)
    snapshot_document_id = _record_value(record, "document_id")
    if current_document_id and snapshot_document_id and current_document_id != snapshot_document_id:
        raise ToolInputError(f"snapshot is stale for pageId={page_id}; call take_snapshot again")

    uids = set(_record_value(record, "uids") or [])
    if uid not in uids:
        raise ToolInputError(f"uid '{uid}' is not present in the latest snapshot; call take_snapshot again")


def _uid_selector(uid: str) -> str:
    return f'[data-sf-uid="{uid}"]'


def _descriptor_for_uid(state: Any, *, page_id: int, uid: str) -> dict[str, Any] | None:
    record = _latest_snapshot_record(state, page_id)
    if record is None:
        return None
    descriptors = _record_value(record, "descriptors") or {}
    if not isinstance(descriptors, dict):
        return None
    descriptor = descriptors.get(uid)
    return descriptor if isinstance(descriptor, dict) else None


def _raw_locator_for_uid(page: Any, uid: str) -> Any:
    locator_fn = getattr(page, "locator", None)
    if not callable(locator_fn):
        raise ToolExecutionError(f"element for uid '{uid}' is no longer available; DOM changed")
    return locator_fn(_uid_selector(uid))


async def _uid_binding_exists(page: Any, uid: str) -> bool:
    query_selector = getattr(page, "query_selector", None)
    if callable(query_selector):
        try:
            handle = await query_selector(_uid_selector(uid))
        except Exception:
            return False
        if handle is None:
            return False
        dispose = getattr(handle, "dispose", None)
        if callable(dispose):
            try:
                await dispose()
            except Exception:
                pass
        return True

    locator = _raw_locator_for_uid(page, uid)
    count = getattr(locator, "count", None)
    if not callable(count):
        return False
    try:
        return int(await count()) > 0
    except Exception:
        return False


async def _attempt_uid_recovery(page: Any, *, uid: str, descriptor: dict[str, Any]) -> bool:
    evaluate = getattr(page, "evaluate", None)
    if not callable(evaluate):
        return False
    try:
        raw = await evaluate(
            _UID_RECOVERY_SCRIPT,
            {"uid": uid, "descriptor": descriptor},
        )
    except Exception:
        return False
    return isinstance(raw, dict) and bool(raw.get("recovered"))


async def _locator_for_uid(state: Any, *, page: Any, page_id: int, uid: str) -> Any:
    if await _uid_binding_exists(page, uid):
        return _raw_locator_for_uid(page, uid)

    descriptor = _descriptor_for_uid(state, page_id=page_id, uid=uid)
    if descriptor and await _attempt_uid_recovery(page, uid=uid, descriptor=descriptor):
        if await _uid_binding_exists(page, uid):
            return _raw_locator_for_uid(page, uid)

    raise ToolExecutionError(f"element for uid '{uid}' is no longer available; DOM changed")


def _simplify_click_error(exc: Exception) -> str:
    message = str(exc or "").replace("\r\n", "\n").strip()
    lowered = message.lower()
    known_markers = (
        "element is not visible",
        "element is not enabled",
        "element is outside of the viewport",
        "element is detached from the dom",
        "element is not attached to the dom",
        "another element intercepts pointer events",
        "element does not receive pointer events",
    )
    for marker in known_markers:
        if marker in lowered:
            return marker
    if "intercepts pointer events" in lowered:
        return "another element intercepts pointer events"

    for raw_line in message.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        normalized = line.lower().lstrip("- ").strip()
        if normalized.startswith("locator.click:"):
            continue
        if normalized.startswith("call log:"):
            continue
        if normalized.startswith("waiting for "):
            continue
        if normalized.startswith("retrying click action"):
            continue
        if normalized.startswith("attempting click action"):
            continue
        return line.lstrip("- ").strip()
    return message or "click failed"


async def _click_with_dialog_support(locator: Any, *, state: Any, click_count: int) -> None:
    click_task = asyncio.create_task(locator.click(click_count=click_count))
    wait_for_dialog = getattr(state, "wait_for_dialog", None)
    if not callable(wait_for_dialog):
        try:
            await click_task
        except Exception as exc:
            raise ToolExecutionError(_simplify_click_error(exc)) from exc
        return

    dialog_task = asyncio.create_task(wait_for_dialog(DIALOG_OPEN_WAIT_SECONDS))
    try:
        done, _pending = await asyncio.wait(
            {click_task, dialog_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if click_task in done:
            try:
                await click_task
            except Exception as exc:
                raise ToolExecutionError(_simplify_click_error(exc)) from exc
            return

        dialog_opened = False
        try:
            dialog_opened = bool(dialog_task.result())
        except Exception:
            dialog_opened = False

        if dialog_opened:
            click_task.cancel()
            try:
                await click_task
            except asyncio.CancelledError:
                pass
            return

        try:
            await click_task
        except Exception as exc:
            raise ToolExecutionError(_simplify_click_error(exc)) from exc
    finally:
        if not dialog_task.done():
            dialog_task.cancel()
            try:
                await dialog_task
            except asyncio.CancelledError:
                pass


async def _append_snapshot_output(
    output: str,
    *,
    state: Any,
    include_snapshot: bool,
    artifact_writer: TextArtifactWriter,
) -> tuple[str, dict[str, Any]]:
    if not include_snapshot:
        return output, {}

    snapshot_payload = await take_snapshot(
        state=state,
        verbose=False,
        artifact_writer=artifact_writer,
    )
    metadata = {
        key: value
        for key, value in snapshot_payload.get("metadata", {}).items()
        if key in {"documentId", "snapshotId", "uidCount", "path", "outputPath", "truncated", "hitlBarrier"}
    }
    return f"{output}\n\n{snapshot_payload['output']}", metadata


async def take_snapshot(
    *,
    state: Any,
    verbose: bool,
    file_path: str | None = None,
    base_dir: Path | None = None,
    artifact_writer: TextArtifactWriter = _default_artifact_writer,
) -> dict[str, Any]:
    page_id, page = await _require_selected_page(state)
    capture_snapshot = getattr(state, "capture_snapshot", None)
    if not callable(capture_snapshot):
        raise ToolExecutionError("take_snapshot is not implemented")

    try:
        snapshot_data = await capture_snapshot(page, verbose=verbose)
    except (ToolInputError, ToolExecutionError):
        raise
    except Exception as exc:
        raise ToolExecutionError(str(exc)) from exc

    output = str(snapshot_data.get("text", ""))
    document_id = str(
        snapshot_data.get("document_id")
        or (getattr(state, "page_document_ids", {}) or {}).get(page_id)
        or f"doc-{page_id}"
    )
    uids = set(snapshot_data.get("uids") or [])
    register_snapshot = getattr(state, "register_snapshot", None)
    snapshot_id = None
    if callable(register_snapshot):
        record = register_snapshot(
            page_id=page_id,
            document_id=document_id,
            uids=uids,
            text=output,
            descriptors=snapshot_data.get("descriptors") or {},
        )
        snapshot_id = _record_value(record, "snapshot_id")

    metadata: dict[str, Any] = {
        "documentId": document_id,
        "snapshotId": snapshot_id,
        "uidCount": len(uids),
    }
    hitl_barrier = _detect_hitl_barrier(
        text=output,
        url=str(getattr(page, "url", "")),
        title=await _read_page_title(page),
    )
    if hitl_barrier is not None:
        metadata["hitlBarrier"] = hitl_barrier
    if file_path:
        target = resolve_path(file_path, base_dir=base_dir)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(output, encoding="utf-8")
        metadata["path"] = str(target)

    return await _format_browser_result(
        output,
        state=state,
        page_id=page_id,
        tool_name="tools_take_snapshot",
        artifact_writer=artifact_writer,
        metadata=metadata,
    )


async def evaluate_script(
    *,
    state: Any,
    script: str,
    page_id: int | None = None,
    document_id: str | None = None,
    timeout_ms: int = DEFAULT_EVALUATE_SCRIPT_TIMEOUT_MS,
    artifact_writer: TextArtifactWriter = _default_artifact_writer,
) -> dict[str, Any]:
    normalized_script = _normalize_evaluate_script_source(script)
    safe_timeout_ms = max(1, min(int(timeout_ms), MAX_EVALUATE_SCRIPT_TIMEOUT_MS))
    target_page_id, page = await _require_page(state, page_id=page_id)
    current_document_id = await _ensure_page_document_id(state, page=page, page_id=target_page_id)

    expected_document_id = str(document_id or "").strip()
    if expected_document_id and expected_document_id != current_document_id:
        raise ToolInputError(
            f"document is stale for pageId={target_page_id}; expected documentId={expected_document_id}, current documentId={current_document_id}"
        )

    evaluate = getattr(page, "evaluate", None)
    if not callable(evaluate):
        raise ToolExecutionError("page evaluation is unavailable")

    try:
        raw = await asyncio.wait_for(
            evaluate(
                """
                async ({ script }) => {
                    const globalEval = (0, eval);
                    let factory;
                    try {
                        factory = globalEval(script);
                    } catch (error) {
                        throw new Error(`script parse failed: ${error?.message || String(error)}`);
                    }
                    if (typeof factory !== "function") {
                        throw new Error("script must evaluate to a function");
                    }
                    let value;
                    try {
                        value = await factory();
                    } catch (error) {
                        throw new Error(`script execution failed: ${error?.message || String(error)}`);
                    }
                    try {
                        const jsonText = JSON.stringify(value === undefined ? null : value);
                        return {
                            value: JSON.parse(jsonText),
                            jsonText,
                        };
                    } catch (error) {
                        throw new Error(
                            `script result must be JSON-serializable: ${error?.message || String(error)}`
                        );
                    }
                }
                """,
                {"script": normalized_script},
            ),
            timeout=safe_timeout_ms / 1000,
        )
    except TimeoutError as exc:
        raise ToolExecutionError(
            f"evaluate_script timed out after {safe_timeout_ms} ms"
        ) from exc
    except (ToolInputError, ToolExecutionError):
        raise
    except Exception as exc:
        raise ToolExecutionError(str(exc)) from exc

    if not isinstance(raw, dict):
        raise ToolExecutionError("evaluate_script returned an invalid payload")

    result_value = raw.get("value")
    result_type = _json_result_type(result_value)
    result_text = str(raw.get("jsonText") or json.dumps(result_value, ensure_ascii=False))
    if result_type in {"object", "array"}:
        result_text = json.dumps(result_value, ensure_ascii=False, indent=2)

    output = _format_evaluate_script_output(
        page_id=target_page_id,
        document_id=current_document_id,
        result_type=result_type,
        result_text=result_text,
    )
    metadata = {
        "documentId": current_document_id,
        "resultType": result_type,
        "timeoutMs": safe_timeout_ms,
    }
    return await _format_browser_result(
        output,
        state=state,
        page_id=target_page_id,
        tool_name="tools_evaluate_script",
        artifact_writer=artifact_writer,
        metadata=metadata,
    )


async def navigate_page(
    *,
    state: Any,
    navigation_type: str,
    url: str | None = None,
    timeout_ms: int = 30000,
    wait_until: str = "domcontentloaded",
    artifact_writer: TextArtifactWriter = _default_artifact_writer,
) -> dict[str, Any]:
    page_id, page = await _require_selected_page(state)
    action = str(navigation_type or "").strip()
    if action not in {"url", "back", "forward", "reload"}:
        raise ToolInputError(f"unsupported navigate type: {action}")

    if action == "url":
        if not url:
            raise ToolInputError("url is required when type='url'")
        await page.goto(url, wait_until=wait_until, timeout=int(timeout_ms))
        output = f"Navigated pageId={page_id} to {url}"
    elif action == "back":
        await page.go_back(wait_until=wait_until, timeout=int(timeout_ms))
        output = f"Navigated back on pageId={page_id}"
    elif action == "forward":
        await page.go_forward(wait_until=wait_until, timeout=int(timeout_ms))
        output = f"Navigated forward on pageId={page_id}"
    else:
        await page.reload(wait_until=wait_until, timeout=int(timeout_ms))
        output = f"Reloaded pageId={page_id}"

    return await _format_browser_result(
        output,
        state=state,
        page_id=page_id,
        tool_name="tools_navigate_page",
        artifact_writer=artifact_writer,
    )


async def click(
    *,
    state: Any,
    uid: str,
    include_snapshot: bool = False,
    dbl_click: bool = False,
    artifact_writer: TextArtifactWriter = _default_artifact_writer,
) -> dict[str, Any]:
    page_id, page = await _require_selected_page(state)
    _validate_latest_uid(state, page_id=page_id, uid=uid)
    locator = await _locator_for_uid(state, page=page, page_id=page_id, uid=uid)
    await _click_with_dialog_support(locator, state=state, click_count=2 if dbl_click else 1)
    output, metadata = await _append_snapshot_output(
        f"Clicked uid={uid}",
        state=state,
        include_snapshot=include_snapshot,
        artifact_writer=artifact_writer,
    )
    return await _format_browser_result(
        output,
        state=state,
        page_id=page_id,
        tool_name="tools_click",
        artifact_writer=artifact_writer,
        metadata=metadata,
    )


async def fill(
    *,
    state: Any,
    uid: str,
    value: str,
    include_snapshot: bool = False,
    artifact_writer: TextArtifactWriter = _default_artifact_writer,
) -> dict[str, Any]:
    page_id, page = await _require_selected_page(state)
    _validate_latest_uid(state, page_id=page_id, uid=uid)
    locator = await _locator_for_uid(state, page=page, page_id=page_id, uid=uid)

    used_select_option = False
    select_option = getattr(locator, "select_option", None)
    if callable(select_option):
        try:
            await select_option(value)
            used_select_option = True
        except Exception:
            used_select_option = False

    if not used_select_option:
        await locator.fill(value)

    verb = "Selected option" if used_select_option else "Filled"
    output, metadata = await _append_snapshot_output(
        f"{verb} uid={uid}",
        state=state,
        include_snapshot=include_snapshot,
        artifact_writer=artifact_writer,
    )
    return await _format_browser_result(
        output,
        state=state,
        page_id=page_id,
        tool_name="tools_fill",
        artifact_writer=artifact_writer,
        metadata=metadata,
    )


async def take_screenshot(
    *,
    state: Any,
    uid: str | None = None,
    file_path: str | None = None,
    image_format: str = "png",
    full_page: bool = True,
    quality: int | None = None,
    base_dir: Path | None = None,
    binary_artifact_writer: BinaryArtifactWriter = _default_binary_artifact_writer,
) -> dict[str, Any]:
    page_id, page = await _require_selected_page(state)
    capture_target = page
    if uid is not None:
        _validate_latest_uid(state, page_id=page_id, uid=uid)
        capture_target = await _locator_for_uid(state, page=page, page_id=page_id, uid=uid)

    screenshot_fn = getattr(capture_target, "screenshot", None)
    if not callable(screenshot_fn):
        raise ToolExecutionError("screenshot target is unavailable")

    screenshot_bytes = await screenshot_fn(type=image_format, full_page=full_page, quality=quality)
    if file_path:
        target = resolve_path(file_path, base_dir=base_dir)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(screenshot_bytes)
        saved_path = str(target)
    else:
        saved_path = binary_artifact_writer("tools_take_screenshot", screenshot_bytes, image_format)

    mime_type = {
        "png": "image/png",
        "jpeg": "image/jpeg",
        "jpg": "image/jpeg",
        "webp": "image/webp",
    }.get(image_format, f"image/{image_format}")
    metadata = await _page_summary_metadata(state, active_page_id=page_id)
    metadata.update(
        {
            "pageId": page_id,
            "path": saved_path,
            "mimeType": mime_type,
            "bytes": len(screenshot_bytes),
        }
    )
    return {
        "output": f"Saved screenshot to {saved_path}",
        "metadata": metadata,
    }


async def press_key(
    *,
    state: Any,
    key: str,
    include_snapshot: bool = False,
    artifact_writer: TextArtifactWriter = _default_artifact_writer,
) -> dict[str, Any]:
    page_id, page = await _require_selected_page(state)
    keyboard = getattr(page, "keyboard", None)
    press = getattr(keyboard, "press", None)
    if not callable(press):
        raise ToolExecutionError("page keyboard is unavailable")
    await press(key)
    output, metadata = await _append_snapshot_output(
        f"Pressed key={key}",
        state=state,
        include_snapshot=include_snapshot,
        artifact_writer=artifact_writer,
    )
    return await _format_browser_result(
        output,
        state=state,
        page_id=page_id,
        tool_name="tools_press_key",
        artifact_writer=artifact_writer,
        metadata=metadata,
    )


async def handle_dialog(
    *,
    state: Any,
    action: str,
    prompt_text: str | None = None,
    artifact_writer: TextArtifactWriter = _default_artifact_writer,
) -> dict[str, Any]:
    page_id, _page = await _require_selected_page(state)
    verb = str(action or "").strip().lower()
    if verb not in {"accept", "dismiss"}:
        raise ToolInputError(f"unsupported dialog action: {action}")

    pop_dialog = getattr(state, "pop_dialog", None)
    dialog = pop_dialog() if callable(pop_dialog) else None
    if dialog is None:
        raise ToolInputError("no dialog is currently open")

    try:
        if verb == "accept":
            await dialog.accept(prompt_text)
            output = "Accepted dialog"
        else:
            await dialog.dismiss()
            output = "Dismissed dialog"
    except Exception as exc:
        raise ToolExecutionError("dialog is no longer available") from exc

    return await _format_browser_result(
        output,
        state=state,
        page_id=page_id,
        tool_name="tools_handle_dialog",
        artifact_writer=artifact_writer,
    )


async def hover(
    *,
    state: Any,
    uid: str,
    include_snapshot: bool = False,
    artifact_writer: TextArtifactWriter = _default_artifact_writer,
) -> dict[str, Any]:
    page_id, page = await _require_selected_page(state)
    _validate_latest_uid(state, page_id=page_id, uid=uid)
    locator = await _locator_for_uid(state, page=page, page_id=page_id, uid=uid)
    await locator.hover()
    output, metadata = await _append_snapshot_output(
        f"Hovered uid={uid}",
        state=state,
        include_snapshot=include_snapshot,
        artifact_writer=artifact_writer,
    )
    return await _format_browser_result(
        output,
        state=state,
        page_id=page_id,
        tool_name="tools_hover",
        artifact_writer=artifact_writer,
        metadata=metadata,
    )


async def wait_for(
    *,
    state: Any,
    text: list[str],
    timeout_ms: int = 30000,
    artifact_writer: TextArtifactWriter = _default_artifact_writer,
) -> dict[str, Any]:
    if not text:
        raise ToolInputError("text must include at least one value")

    page_id, page = await _require_selected_page(state)
    wait_for_function = getattr(page, "wait_for_function", None)
    if not callable(wait_for_function):
        raise ToolExecutionError("page wait_for_function is unavailable")

    try:
        await wait_for_function(
            """
            (targets) => {
                const bodyText = document?.body?.innerText || '';
                return targets.some((value) => bodyText.includes(value));
            }
            """,
            arg=list(text),
            timeout=int(timeout_ms),
        )
    except TimeoutError as exc:
        raise ToolExecutionError(f"wait_for timed out after {int(timeout_ms)} ms") from exc
    except Exception as exc:
        raise ToolExecutionError(str(exc)) from exc

    return await _format_browser_result(
        f"Wait completed for text: {', '.join(text)}",
        state=state,
        page_id=page_id,
        tool_name="tools_wait_for",
        artifact_writer=artifact_writer,
    )


async def upload_file(
    *,
    state: Any,
    uid: str,
    file_path: str,
    include_snapshot: bool = False,
    base_dir: Path | None = None,
    artifact_writer: TextArtifactWriter = _default_artifact_writer,
) -> dict[str, Any]:
    target = resolve_path(file_path, base_dir=base_dir)
    if not target.exists():
        raise ToolInputError(f"File not found: {target}")
    if target.is_dir():
        raise ToolInputError(f"Path is a directory, not a file: {target}")

    page_id, page = await _require_selected_page(state)
    _validate_latest_uid(state, page_id=page_id, uid=uid)
    locator = await _locator_for_uid(state, page=page, page_id=page_id, uid=uid)
    set_input_files = getattr(locator, "set_input_files", None)
    if not callable(set_input_files):
        raise ToolExecutionError(f"element for uid '{uid}' is no longer available; DOM changed")
    await set_input_files(str(target))

    output, metadata = await _append_snapshot_output(
        f"Uploaded file to uid={uid}",
        state=state,
        include_snapshot=include_snapshot,
        artifact_writer=artifact_writer,
    )
    return await _format_browser_result(
        output,
        state=state,
        page_id=page_id,
        tool_name="tools_upload_file",
        artifact_writer=artifact_writer,
        metadata=metadata,
    )


async def select_page(
    *,
    state: Any,
    page_id: int,
    bring_to_front: bool = False,
    artifact_writer: TextArtifactWriter = _default_artifact_writer,
) -> dict[str, Any]:
    target_page_id = int(page_id)
    page_map = (getattr(state, "page_id_to_page", {}) or {})
    if target_page_id not in page_map:
        raise ToolInputError(f"unknown pageId: {page_id}")
    setattr(state, "selected_page_id", target_page_id)
    page = page_map[target_page_id]
    if bring_to_front:
        bring_to_front_fn = getattr(page, "bring_to_front", None)
        if callable(bring_to_front_fn):
            await bring_to_front_fn()
    return await _format_browser_result(
        f"Selected pageId={target_page_id}",
        state=state,
        page_id=target_page_id,
        tool_name="tools_select_page",
        artifact_writer=artifact_writer,
    )


async def reset_browser(
    *,
    state: Any,
    start_url: str | None = None,
    artifact_writer: TextArtifactWriter = _default_artifact_writer,
) -> dict[str, Any]:
    context = await state.ensure_context()
    pages = list(getattr(context, "pages", []) or [])
    if not pages:
        page = await context.new_page()
        pages = [page]

    selected_page = None
    get_selected_page = getattr(state, "get_selected_page", None)
    if callable(get_selected_page):
        selected_page = get_selected_page()
    if selected_page not in pages:
        selected_page = pages[0]

    for page in pages:
        if page is selected_page:
            continue
        close_page = getattr(page, "close", None)
        if callable(close_page):
            try:
                await close_page()
            except Exception:
                continue

    synced_pages = list(getattr(context, "pages", []) or [])
    if selected_page not in synced_pages:
        if synced_pages:
            selected_page = synced_pages[0]
        else:
            selected_page = await context.new_page()
            synced_pages = [selected_page]

    sync_pages = getattr(state, "sync_pages", None)
    if callable(sync_pages):
        sync_pages(synced_pages)

    page_id = getattr(state, "get_page_id", lambda _page: None)(selected_page)
    if page_id is not None:
        setattr(state, "selected_page_id", int(page_id))

    bring_to_front = getattr(selected_page, "bring_to_front", None)
    if callable(bring_to_front):
        try:
            await bring_to_front()
        except Exception:
            pass

    target_url = str(start_url or DEFAULT_RESET_START_URL).strip() or DEFAULT_RESET_START_URL
    goto = getattr(selected_page, "goto", None)
    navigated = False
    if callable(goto):
        try:
            await goto(target_url, wait_until="domcontentloaded", timeout=15000)
            navigated = True
        except Exception:
            try:
                await goto(target_url, timeout=15000)
                navigated = True
            except Exception:
                pass

    clear_runtime_state = getattr(state, "clear_runtime_state", None)
    if callable(clear_runtime_state):
        clear_runtime_state()

    resolved_page_id, _ = await _require_selected_page(state)
    final_url = str(getattr(selected_page, "url", "") or "")
    output = f"Reset browser to pageId={resolved_page_id} url={final_url or target_url}"
    metadata = {
        "reset": {
            "pageCount": len(getattr(state, "page_id_to_page", {}) or {}),
            "requestedStartUrl": target_url,
            "finalUrl": final_url or target_url,
            "navigationApplied": navigated,
        }
    }
    return await _format_browser_result(
        output,
        state=state,
        page_id=resolved_page_id,
        tool_name="browser_reset",
        artifact_writer=artifact_writer,
        metadata=metadata,
    )
