from __future__ import annotations

from difflib import unified_diff
import fnmatch
import subprocess
from dataclasses import dataclass
from difflib import get_close_matches
from pathlib import Path
from typing import Callable

UNIFIED_MAX_LINES = 200
UNIFIED_MAX_BYTES = 10 * 1024
BACKGROUND_EARLY_OUTPUT_MAX_LINES = 40
BACKGROUND_EARLY_OUTPUT_MAX_CHARS = 4000
READ_DEFAULT_LIMIT = 200
READ_LINE_MAX_CHARS = 2000
GLOB_MAX_RESULTS = 100
GREP_MAX_RESULTS = 100
GREP_LINE_MAX_CHARS = 2000
IMAGE_SUFFIXES = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".bmp",
    ".webp",
    ".svg",
    ".ico",
    ".tif",
    ".tiff",
}


class ToolInputError(ValueError):
    pass


class ToolExecutionError(RuntimeError):
    pass


@dataclass(frozen=True)
class SearchMatch:
    path: Path
    line_no: int
    text: str
    mtime: float


def resolve_path(raw_path: str, *, base_dir: Path | None = None) -> Path:
    base = Path(base_dir or Path.cwd()).resolve()
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        candidate = base / candidate
    return candidate.resolve()


def append_bash_metadata(output: str, *, timeout_ms: int | None = None, aborted: bool = False) -> str:
    notes: list[str] = []
    if timeout_ms is not None:
        notes.append(f"bash tool terminated command after exceeding timeout {timeout_ms} ms")
    if aborted:
        notes.append("User aborted the command")
    if not notes:
        return output

    body = "\n".join(notes)
    if output:
        return f"{output}\n\n<bash_metadata>\n{body}\n</bash_metadata>"
    return f"<bash_metadata>\n{body}\n</bash_metadata>"


def format_bash_result(
    output: str,
    *,
    exit_code: int | None,
    artifact_writer: Callable[[str, str], str],
    metadata: dict[str, object] | None = None,
) -> dict[str, object]:
    merged_metadata = dict(metadata or {})
    merged_metadata["exit_code"] = exit_code
    return apply_unified_truncation(
        {
            "output": output,
            "metadata": merged_metadata,
        },
        tool_name="tools_bash",
        artifact_writer=artifact_writer,
    )


def shape_background_bash_early_output(output: str) -> str:
    text = str(output or "")
    if not text:
        return ""

    limited_lines = text.splitlines()[:BACKGROUND_EARLY_OUTPUT_MAX_LINES]
    shaped = "\n".join(limited_lines).strip()
    if len(shaped) > BACKGROUND_EARLY_OUTPUT_MAX_CHARS:
        shaped = shaped[: BACKGROUND_EARLY_OUTPUT_MAX_CHARS - 3].rstrip() + "..."
    return shaped


def format_background_bash_result(
    *,
    status: str,
    command: str,
    workdir: str,
    job_id: str,
    pid: int | None,
    log_path: str,
    exit_code: int | None,
    artifact_writer: Callable[[str, str], str],
    early_output: str | None = None,
) -> dict[str, object]:
    output = _build_background_bash_output(
        status=status,
        command=command,
        workdir=workdir,
        job_id=job_id,
        pid=pid,
        log_path=log_path,
        early_output=early_output,
    )
    return format_bash_result(
        output,
        exit_code=exit_code,
        artifact_writer=artifact_writer,
        metadata={
            "mode": "background",
            "status": status,
            "job_id": job_id,
            "pid": pid,
            "logPath": log_path,
        },
    )


def _append_tagged_block(lines: list[str], tag: str, value: str | None) -> None:
    if value is None:
        return
    text = str(value)
    if "\n" in text:
        lines.append(f"<{tag}>")
        lines.extend(text.splitlines())
        lines.append(f"</{tag}>")
        return
    lines.append(f"<{tag}>{text}</{tag}>")


def _build_background_bash_output(
    *,
    status: str,
    command: str,
    workdir: str,
    job_id: str,
    pid: int | None,
    log_path: str,
    early_output: str | None,
) -> str:
    lines: list[str] = []
    _append_tagged_block(lines, "status", status)
    _append_tagged_block(lines, "command", command)
    _append_tagged_block(lines, "workdir", workdir)
    _append_tagged_block(lines, "job_id", job_id)
    if pid is not None:
        _append_tagged_block(lines, "pid", str(pid))
    _append_tagged_block(lines, "log_path", log_path)

    if status == "launched_unverified":
        _append_tagged_block(
            lines,
            "message",
            "The command was launched and returned early by design.\n"
            "Do not assume the service is ready yet.\n"
            "Use a follow-up check if readiness matters.",
        )
    elif status == "launch_timeout":
        _append_tagged_block(
            lines,
            "message",
            "The command did not reach the expected quick-return state.",
        )
    elif status in {"failed_to_launch", "exited_early"}:
        shaped = shape_background_bash_early_output(early_output or "")
        if shaped:
            _append_tagged_block(lines, "early_output", shaped)

    return "\n".join(lines)


def apply_unified_truncation(
    payload: dict[str, object],
    *,
    tool_name: str,
    artifact_writer: Callable[[str, str], str],
) -> dict[str, object]:
    metadata = dict(payload.get("metadata") or {})
    if metadata.get("truncated") is True:
        return {"output": str(payload.get("output", "")), "metadata": metadata}

    output = str(payload.get("output", ""))
    if _looks_like_image_base64(output):
        return {"output": output, "metadata": metadata}

    preview, truncated, output_path = _truncate_with_artifact(
        output,
        tool_name=tool_name,
        artifact_writer=artifact_writer,
    )
    if truncated:
        metadata["truncated"] = True
        metadata["outputPath"] = output_path
    return {"output": preview, "metadata": metadata}


def build_tool_error_result(
    message: str,
    *,
    metadata: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "output": f"ErrorInfo:\n{message}",
        "metadata": dict(metadata or {}),
    }


def read_path(
    file_path: str,
    *,
    offset: int = 1,
    limit: int | None = None,
    base_dir: Path | None = None,
) -> dict[str, object]:
    if offset < 1:
        raise ToolInputError("offset must be greater than or equal to 1")
    resolved_limit = READ_DEFAULT_LIMIT if limit is None else int(limit)
    if resolved_limit < 1:
        raise ToolInputError("limit must be greater than or equal to 1")

    target = resolve_path(file_path, base_dir=base_dir)
    if not target.exists():
        raise ToolInputError(_build_missing_file_message(target))

    if target.is_dir():
        return _read_directory(target, offset=offset, limit=resolved_limit)

    suffix = target.suffix.lower()
    if suffix in IMAGE_SUFFIXES:
        return {"output": "The current system does not support reading images", "metadata": {"truncated": False}}
    if suffix == ".pdf":
        return {"output": "The current system does not support reading PDF files", "metadata": {"truncated": False}}
    if _is_binary_file(target):
        raise ToolInputError(f"Cannot read binary file: {target}")

    return _read_text_file(target, offset=offset, limit=resolved_limit)


def glob_paths(
    pattern: str,
    *,
    path: str = ".",
    base_dir: Path | None = None,
) -> dict[str, object]:
    target = resolve_path(path or ".", base_dir=base_dir)
    matches = _collect_glob_matches(pattern, target)
    total = len(matches)
    truncated = total > GLOB_MAX_RESULTS
    shown = matches[:GLOB_MAX_RESULTS]

    if not shown:
        output = "No files found"
    else:
        output = "\n".join(str(item) for item in shown)
        if truncated:
            output += (
                "\n\n(Results are truncated: showing first 100 results. "
                "Consider using a more specific path or pattern.)"
            )

    return {
        "output": output,
        "metadata": {
            "count": total,
            "truncated": truncated,
        },
    }


def grep_text(
    pattern: str,
    *,
    path: str = ".",
    glob: str | None = None,
    base_dir: Path | None = None,
) -> dict[str, object]:
    target = resolve_path(path or ".", base_dir=base_dir)
    if not target.exists():
        return {"output": "No files found", "metadata": {"matches": 0, "truncated": False}}

    if target.is_dir():
        cwd = target
        search_target = "."
    else:
        cwd = target.parent
        search_target = target.name

    argv = [
        "rg",
        "-nH",
        "--hidden",
        "--no-messages",
        "--field-match-separator=|",
        "--regexp",
        pattern,
    ]
    if isinstance(glob, str) and glob.strip():
        argv.extend(["--glob", glob.strip()])
    argv.append(search_target)

    proc = _run_command(argv, cwd=cwd)
    if proc.returncode not in {0, 1, 2}:
        raise ToolExecutionError(proc.stderr.strip() or f"rg exited with code {proc.returncode}")

    matches = _parse_search_matches(proc.stdout, cwd=cwd)
    if not matches:
        return {"output": "No files found", "metadata": {"matches": 0, "truncated": False}}

    matches.sort(key=lambda item: (-item.mtime, str(item.path), item.line_no))
    total = len(matches)
    truncated = total > GREP_MAX_RESULTS
    shown = matches[:GREP_MAX_RESULTS]

    lines = [f"Found {total} matches" + (" (showing first 100)" if truncated else "")]
    current_path: Path | None = None
    for match in shown:
        if current_path != match.path:
            if current_path is not None:
                lines.append("")
            current_path = match.path
            lines.append(f"  {match.path}:")
        lines.append(f"    Line {match.line_no}: {match.text}")

    if truncated:
        hidden = total - GREP_MAX_RESULTS
        lines.extend(
            [
                "",
                (
                    f"(Results truncated: showing 100 of {total} matches ({hidden} hidden). "
                    "Consider using a more specific path or pattern.)"
                ),
            ]
        )

    if proc.returncode == 2:
        lines.extend(["", "(Some paths were inaccessible and skipped)"])

    return {
        "output": "\n".join(lines),
        "metadata": {
            "matches": total,
            "truncated": truncated,
        },
    }


def edit_file(
    file_path: str,
    *,
    old_string: str,
    new_string: str,
    replace_all: bool = False,
    base_dir: Path | None = None,
    artifact_writer: Callable[[str, str], str],
) -> dict[str, object]:
    if not file_path:
        raise ToolInputError("filePath is required")
    if old_string == new_string:
        raise ToolInputError("No changes to apply: oldString and newString are identical.")

    target = resolve_path(file_path, base_dir=base_dir)
    if not target.exists():
        raise ToolInputError(f"File {target} not found")
    if target.is_dir():
        raise ToolInputError(f"Path is a directory, not a file: {target}")

    try:
        before = target.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise ToolExecutionError(str(exc)) from exc
    after = _replace_text(before, old_string, new_string, replace_all=replace_all)
    if after == before:
        raise ToolInputError("No changes to apply: oldString and newString are identical.")

    try:
        target.write_text(after, encoding="utf-8")
    except OSError as exc:
        raise ToolExecutionError(str(exc)) from exc
    diff_text = _make_unified_diff(target, before, after)
    additions, deletions = _count_diff_changes(diff_text)
    payload = {
        "output": "Edit applied successfully.",
        "metadata": {
            "diagnostics": {},
            "diff": diff_text,
            "filediff": {
                "file": str(target.resolve()),
                "before": before,
                "after": after,
                "additions": additions,
                "deletions": deletions,
            },
        },
    }
    return apply_unified_truncation(payload, tool_name="tools_edit", artifact_writer=artifact_writer)


def write_file(
    file_path: str,
    *,
    content: str,
    base_dir: Path | None = None,
    artifact_writer: Callable[[str, str], str],
) -> dict[str, object]:
    if not file_path:
        raise ToolInputError("filePath is required")

    target = resolve_path(file_path, base_dir=base_dir)
    if target.exists() and target.is_dir():
        raise ToolInputError(f"Path is a directory, not a file: {target}")

    exists = target.exists()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    except OSError as exc:
        raise ToolExecutionError(str(exc)) from exc
    payload = {
        "output": "Wrote file successfully.",
        "metadata": {
            "diagnostics": {},
            "filepath": str(target.resolve()),
            "exists": exists,
        },
    }
    return apply_unified_truncation(payload, tool_name="tools_write", artifact_writer=artifact_writer)


def _truncate_with_artifact(
    text: str,
    *,
    tool_name: str,
    artifact_writer: Callable[[str, str], str],
) -> tuple[str, bool, str | None]:
    encoded = text.encode("utf-8", errors="replace")
    lines = text.splitlines()
    if len(lines) <= UNIFIED_MAX_LINES and len(encoded) <= UNIFIED_MAX_BYTES:
        return text, False, None

    if len(lines) > UNIFIED_MAX_LINES:
        preview = "\n".join(lines[:UNIFIED_MAX_LINES])
        omitted_label = f"{len(lines) - UNIFIED_MAX_LINES} lines"
    else:
        clipped = encoded[:UNIFIED_MAX_BYTES]
        preview = clipped.decode("utf-8", errors="ignore")
        omitted_label = f"{max(0, len(encoded) - len(clipped))} bytes"

    output_path = artifact_writer(tool_name, text)
    rendered = "\n".join(
        part
        for part in (
            preview,
            f"  ...{omitted_label} truncated...",
            f"  The tool call succeeded but the output was truncated. Full output saved to: {output_path}",
            "  Use Grep to search the full content or Read with offset/limit to view specific sections.",
        )
        if part
    )
    return rendered, True, output_path


def _looks_like_image_base64(output: str) -> bool:
    stripped = output.strip()
    return stripped.startswith("data:image/")


def _build_missing_file_message(target: Path) -> str:
    message = f"File not found: {target}"
    parent = target.parent
    if not parent.exists() or not parent.is_dir():
        return message

    siblings = sorted(item.name for item in parent.iterdir())
    suggestions = get_close_matches(target.name, siblings, n=3, cutoff=0.1)
    if not suggestions:
        return message
    return message + "\n\nDid you mean one of these?\n" + "\n".join(suggestions)


def _read_directory(target: Path, *, offset: int, limit: int) -> dict[str, object]:
    entries = sorted(_format_directory_entry(item) for item in target.iterdir())
    start = offset - 1
    shown = entries[start : start + limit]
    truncated = start + len(shown) < len(entries)

    lines = [
        f"<path>{target.resolve()}</path>",
        "<type>directory</type>",
        "<entries>",
        *shown,
    ]
    if truncated:
        next_offset = start + len(shown) + 1
        lines.append(
            f"(Showing {len(shown)} of {len(entries)} entries. Use 'offset' parameter to read beyond entry {next_offset - 1})"
        )
    else:
        lines.append(f"({len(entries)} entries)")
    lines.append("</entries>")
    return {"output": "\n".join(lines), "metadata": {"truncated": truncated}}


def _read_text_file(target: Path, *, offset: int, limit: int) -> dict[str, object]:
    rendered_lines: list[str] = []
    total_lines = 0
    shown_start: int | None = None
    shown_end: int | None = None
    shown_count = 0
    byte_count = 0
    byte_capped = False
    has_more_lines = False

    with target.open("r", encoding="utf-8", errors="replace") as handle:
        for line_no, raw_line in enumerate(handle, start=1):
            total_lines = line_no
            if line_no < offset:
                continue
            if byte_capped:
                continue
            if shown_count >= limit:
                has_more_lines = True
                continue

            text = raw_line.rstrip("\r\n")
            if len(text) > READ_LINE_MAX_CHARS:
                text = text[: READ_LINE_MAX_CHARS - 3] + "..."
            rendered = f"{line_no}: {text}"
            extra_bytes = len(rendered.encode("utf-8", errors="replace"))
            if rendered_lines:
                extra_bytes += 1
            if byte_count + extra_bytes > UNIFIED_MAX_BYTES:
                byte_capped = True
                continue

            rendered_lines.append(rendered)
            byte_count += extra_bytes
            shown_count += 1
            if shown_start is None:
                shown_start = line_no
            shown_end = line_no

    if shown_start is None and total_lines > 0 and offset > total_lines:
        raise ToolInputError(f"Offset {offset} is out of range for this file ({total_lines} lines)")
    if total_lines == 0 and offset > 1:
        raise ToolInputError(f"Offset {offset} is out of range for this file (0 lines)")

    truncated = has_more_lines or byte_capped
    lines = [
        f"<path>{target.resolve()}</path>",
        "<type>file</type>",
        "<content>",
        *rendered_lines,
    ]

    if byte_capped and shown_start is not None and shown_end is not None:
        lines.extend(
            [
                "",
                (
                    f"(Output capped at 10 KB. Showing lines {shown_start}-{shown_end}. "
                    f"Use offset={shown_end + 1} to continue.)"
                ),
            ]
        )
    elif truncated and shown_start is not None and shown_end is not None:
        lines.extend(
            [
                "",
                (
                    f"(Showing lines {shown_start}-{shown_end} of {total_lines}. "
                    f"Use offset={shown_end + 1} to continue.)"
                ),
            ]
        )
    else:
        lines.extend(["", f"(End of file - total {total_lines} lines)"])
    lines.append("</content>")
    return {"output": "\n".join(lines), "metadata": {"truncated": truncated}}


def _format_directory_entry(path: Path) -> str:
    return path.name + ("/" if path.is_dir() else "")


def _is_binary_file(target: Path) -> bool:
    sample = target.read_bytes()[:8192]
    if b"\x00" in sample:
        return True
    try:
        sample.decode("utf-8")
    except UnicodeDecodeError:
        return True
    return False


def _collect_glob_matches(pattern: str, target: Path) -> list[Path]:
    if not target.exists():
        return []
    if target.is_file():
        return [target.resolve()] if fnmatch.fnmatch(target.name, pattern) else []

    proc = _run_command(["rg", "--files", "-g", pattern], cwd=target)
    if proc.returncode not in {0, 1}:
        raise ToolExecutionError(proc.stderr.strip() or f"rg exited with code {proc.returncode}")

    matches = [(target / line.strip()).resolve() for line in proc.stdout.splitlines() if line.strip()]
    matches.sort(key=lambda item: (-_safe_mtime(item), str(item)))
    return matches


def _parse_search_matches(stdout: str, *, cwd: Path) -> list[SearchMatch]:
    matches: list[SearchMatch] = []
    for raw_line in stdout.splitlines():
        parts = raw_line.split("|", 2)
        if len(parts) != 3:
            continue
        file_part, line_part, text_part = parts
        try:
            line_no = int(line_part)
        except ValueError:
            continue

        candidate = Path(file_part)
        if not candidate.is_absolute():
            candidate = (cwd / candidate).resolve()

        text = text_part
        if len(text) > GREP_LINE_MAX_CHARS:
            text = text[: GREP_LINE_MAX_CHARS - 3] + "..."

        matches.append(
            SearchMatch(
                path=candidate.resolve(),
                line_no=line_no,
                text=text,
                mtime=_safe_mtime(candidate),
            )
        )
    return matches


def _safe_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _run_command(argv: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            argv,
            cwd=str(cwd),
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError as exc:
        raise ToolExecutionError(f"Required executable not found: {argv[0]}") from exc


def _replace_text(source: str, old: str, new: str, *, replace_all: bool) -> str:
    exact_count = source.count(old)
    if exact_count == 1:
        return source.replace(old, new, 1)
    if exact_count > 1:
        if replace_all:
            return source.replace(old, new)
        raise ToolInputError(
            "Found multiple matches for oldString. Provide more surrounding context to make the match unique."
        )

    normalized_source = source.replace("\r\n", "\n")
    normalized_old = old.replace("\r\n", "\n")
    normalized_new = new.replace("\r\n", "\n")
    normalized_count = normalized_source.count(normalized_old)
    if normalized_count == 1:
        return normalized_source.replace(normalized_old, normalized_new, 1)
    if normalized_count > 1:
        if replace_all:
            return normalized_source.replace(normalized_old, normalized_new)
        raise ToolInputError(
            "Found multiple matches for oldString. Provide more surrounding context to make the match unique."
        )

    raise ToolInputError(
        "Could not find oldString in the file. It must match exactly, including whitespace, indentation, and line endings."
    )


def _make_unified_diff(target: Path, before: str, after: str) -> str:
    return "".join(
        unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=str(target.resolve()),
            tofile=str(target.resolve()),
        )
    )


def _count_diff_changes(diff_text: str) -> tuple[int, int]:
    additions = 0
    deletions = 0
    for line in diff_text.splitlines():
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            additions += 1
        elif line.startswith("-"):
            deletions += 1
    return additions, deletions
