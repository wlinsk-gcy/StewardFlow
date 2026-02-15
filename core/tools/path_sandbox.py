from __future__ import annotations

from pathlib import Path
from typing import Iterable

from core.runtime_settings import RuntimeSettings, get_runtime_settings


def workspace_root(settings: RuntimeSettings | None = None) -> Path:
    return (settings or get_runtime_settings()).workspace_root


def tool_result_root(settings: RuntimeSettings | None = None) -> Path:
    return (settings or get_runtime_settings()).tool_result_root


def allowed_roots(settings: RuntimeSettings | None = None) -> tuple[Path, Path]:
    return (settings or get_runtime_settings()).allowed_roots


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


def resolve_allowed_path(
    raw_path: str,
    *,
    field_name: str = "path",
    settings: RuntimeSettings | None = None,
) -> Path:
    resolved_settings = settings or get_runtime_settings()
    validate_relative_input(raw_path, field_name=field_name)
    candidate = (resolved_settings.workspace_root / Path(raw_path)).resolve()
    assert_path_in_allowed_roots(candidate, resolved_settings.allowed_roots)
    return candidate
