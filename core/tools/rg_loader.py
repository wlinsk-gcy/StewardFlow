from __future__ import annotations

import json
import os
import platform
import shutil
import tarfile
import time
import urllib.request
import zipfile
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


class RgInstallError(RuntimeError):
    """Raised when rg installation cannot be completed."""


def _detect_platform_key() -> str:
    system = platform.system().lower()
    machine = platform.machine().lower()

    x86_64_aliases = {"x86_64", "amd64", "x64"}
    arm64_aliases = {"arm64", "aarch64"}

    if system == "windows":
        if machine in x86_64_aliases:
            return "windows-x86_64"
        if machine in arm64_aliases:
            return "windows-aarch64"
    elif system == "linux":
        if machine in x86_64_aliases:
            return "linux-x86_64"
        if machine in arm64_aliases:
            return "linux-aarch64"
    elif system == "darwin":
        if machine in x86_64_aliases:
            return "macos-x86_64"
        if machine in arm64_aliases:
            return "macos-aarch64"

    raise RgInstallError(
        f"unsupported platform: system={system} machine={machine}"
    )


def _load_manifest(manifest_path: Path) -> dict[str, Any]:
    if not manifest_path.exists():
        raise RgInstallError(f"manifest not found: manifest_path={manifest_path}")

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RgInstallError(f"manifest parse failed: manifest_path={manifest_path}") from exc

    if manifest.get("name") != "rg":
        raise RgInstallError(
            f"invalid manifest name: manifest_path={manifest_path} name={manifest.get('name')}"
        )
    return manifest


def _acquire_install_lock(lock_path: Path, timeout_seconds: int = 60) -> int:
    start = time.monotonic()
    while True:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(
                fd,
                f"pid={os.getpid()} started_at={int(time.time())}\n".encode("utf-8"),
            )
            return fd
        except FileExistsError:
            if time.monotonic() - start >= timeout_seconds:
                raise RgInstallError(
                    f"install lock timeout: lock_path={lock_path} timeout_seconds={timeout_seconds}"
                )
            time.sleep(0.2)


def _release_install_lock(lock_fd: int, lock_path: Path) -> None:
    try:
        os.close(lock_fd)
    finally:
        lock_path.unlink(missing_ok=True)


def _download_file(url: str, destination: Path, platform_key: str) -> None:
    temp_path = destination.with_name(destination.name + ".part")
    try:
        with urllib.request.urlopen(url) as response, temp_path.open("wb") as f:
            shutil.copyfileobj(response, f)
        temp_path.replace(destination)
    except Exception as exc:
        raise RgInstallError(
            f"download failed: platform_key={platform_key} url={url} destination={destination}"
        ) from exc
    finally:
        temp_path.unlink(missing_ok=True)


def _compute_blake3_digest(file_path: Path) -> str:
    try:
        import blake3  # type: ignore
    except ModuleNotFoundError as exc:
        raise RgInstallError("missing dependency: blake3") from exc

    hasher = blake3.blake3()
    with file_path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _safe_extract_tar_gz(archive_path: Path, target_dir: Path) -> None:
    base_dir = target_dir.resolve()
    with tarfile.open(archive_path, mode="r:gz") as tar:
        for member in tar.getmembers():
            member_path = (target_dir / member.name).resolve()
            try:
                member_path.relative_to(base_dir)
            except ValueError as exc:
                raise RgInstallError(
                    f"unsafe tar member path: archive={archive_path} member={member.name}"
                ) from exc
        tar.extractall(path=target_dir)


def ensure_rg(project_root: Path | None = None) -> tuple[Path, bool]:
    root = (
        project_root.resolve()
        if project_root is not None
        else Path(__file__).resolve().parents[2]
    )
    manifest_path = root / "core" / "tools" / "bin" / "rg.json"
    manifest = _load_manifest(manifest_path)
    platform_key = _detect_platform_key()

    platforms = manifest.get("platforms")
    if not isinstance(platforms, dict):
        raise RgInstallError(f"invalid manifest platforms: manifest_path={manifest_path}")

    platform_manifest = platforms.get(platform_key)
    if not isinstance(platform_manifest, dict):
        raise RgInstallError(
            f"platform manifest missing: platform_key={platform_key} manifest_path={manifest_path}"
        )

    archive_format = platform_manifest.get("format")
    relative_exec_path = platform_manifest.get("path")
    providers = platform_manifest.get("providers") or []
    digest_expected = str(platform_manifest.get("digest", "")).lower()
    hash_algorithm = platform_manifest.get("hash")

    if hash_algorithm != "blake3":
        raise RgInstallError(
            f"unsupported hash algorithm: platform_key={platform_key} hash={hash_algorithm}"
        )
    if archive_format not in {"zip", "tar.gz"}:
        raise RgInstallError(
            f"unsupported archive format: platform_key={platform_key} format={archive_format}"
        )
    if not relative_exec_path:
        raise RgInstallError(f"missing executable path: platform_key={platform_key}")
    if not providers or not isinstance(providers[0], dict) or not providers[0].get("url"):
        raise RgInstallError(f"missing provider url: platform_key={platform_key}")
    if not digest_expected:
        raise RgInstallError(f"missing digest: platform_key={platform_key}")

    url = str(providers[0]["url"])
    install_dir = root / "tools_bin" / "rg" / platform_key
    install_dir.mkdir(parents=True, exist_ok=True)
    executable_path = install_dir / str(relative_exec_path)

    if executable_path.exists():
        return executable_path, False

    lock_path = install_dir / ".install.lock"
    lock_fd = _acquire_install_lock(lock_path, timeout_seconds=60)
    try:
        # Another process may finish installation while we are waiting for the lock.
        if executable_path.exists():
            return executable_path, False

        parsed_url = urlparse(url)
        archive_name = Path(parsed_url.path).name
        if not archive_name:
            archive_name = "rg.zip" if archive_format == "zip" else "rg.tar.gz"
        archive_path = install_dir / archive_name

        _download_file(url=url, destination=archive_path, platform_key=platform_key)

        digest_actual = _compute_blake3_digest(archive_path).lower()
        if digest_actual != digest_expected:
            raise RgInstallError(
                "digest mismatch: "
                f"platform_key={platform_key} url={url} expected={digest_expected} actual={digest_actual}"
            )

        if archive_format == "zip":
            with zipfile.ZipFile(archive_path, mode="r") as zf:
                zf.extractall(path=install_dir)
        else:
            _safe_extract_tar_gz(archive_path=archive_path, target_dir=install_dir)

        if not executable_path.exists():
            raise RgInstallError(
                f"executable missing after extraction: platform_key={platform_key} executable_path={executable_path}"
            )

        if os.name != "nt":
            executable_path.chmod(executable_path.stat().st_mode | 0o111)

        return executable_path, True
    finally:
        _release_install_lock(lock_fd, lock_path)
