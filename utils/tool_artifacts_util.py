from pathlib import Path
import shutil


def clear_tool_artifacts() -> None:
    artifact_dir = Path("data") / "tool_artifacts"
    if not artifact_dir.exists() or not artifact_dir.is_dir():
        return

    for item in artifact_dir.iterdir():
        try:
            if item.is_file() or item.is_symlink():
                item.unlink()
            elif item.is_dir():
                shutil.rmtree(item, ignore_errors=True)
        except FileNotFoundError:
            pass
