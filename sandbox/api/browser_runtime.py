from __future__ import annotations

import asyncio
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


def _locator_for_uid(page: Any, uid: str) -> Any:
    locator_fn = getattr(page, "locator", None)
    if not callable(locator_fn):
        raise ToolExecutionError(f"element for uid '{uid}' is no longer available; DOM changed")
    return locator_fn(_uid_selector(uid))


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
        if key in {"documentId", "snapshotId", "uidCount", "path", "outputPath", "truncated"}
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
        )
        snapshot_id = _record_value(record, "snapshot_id")

    metadata: dict[str, Any] = {
        "documentId": document_id,
        "snapshotId": snapshot_id,
        "uidCount": len(uids),
    }
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
    locator = _locator_for_uid(page, uid)
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
    locator = _locator_for_uid(page, uid)

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
        capture_target = _locator_for_uid(page, uid)

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
    locator = _locator_for_uid(page, uid)
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
    locator = _locator_for_uid(page, uid)
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
