from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable

from .tool import Instance


DEFAULT_TOOL_RESULT_ROOT = "data/tool_results"


def workspace_root() -> Path:
    return Path(Instance.directory).resolve()


def tool_result_root() -> Path:
    root = os.getenv("TOOL_RESULT_ROOT_DIR", DEFAULT_TOOL_RESULT_ROOT).strip() or DEFAULT_TOOL_RESULT_ROOT
    candidate = Path(root)
    if candidate.is_absolute():
        return candidate.resolve()
    return (workspace_root() / candidate).resolve()


def _is_safe_relative(raw_path: str) -> bool:
    if not raw_path:
        return False
    p = Path(raw_path)
    if p.is_absolute():
        return False
    return not any(part == ".." for part in p.parts)


def validate_relative_input(raw_path: str, *, field_name: str = "path") -> None:
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise PermissionError(f"{field_name}_required")
    if not _is_safe_relative(raw_path):
        raise PermissionError(f"{field_name}_must_be_relative_and_without_parent_segments")


def _is_under_root(path: Path, root: Path) -> bool:
    resolved_path = path.resolve()
    resolved_root = root.resolve()
    return resolved_path == resolved_root or resolved_root in resolved_path.parents


def assert_path_in_allowed_roots(path: Path, roots: Iterable[Path]) -> None:
    if any(_is_under_root(path, root) for root in roots):
        return
    raise PermissionError(f"path_outside_allowed_roots: {path}")


def resolve_allowed_path(raw_path: str, *, field_name: str = "path") -> Path:
    validate_relative_input(raw_path, field_name=field_name)
    candidate = (workspace_root() / Path(raw_path)).resolve()
    assert_path_in_allowed_roots(candidate, (workspace_root(), tool_result_root()))
    return candidate
