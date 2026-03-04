from __future__ import annotations

import time
from typing import Any
from uuid import uuid4

import requests

try:
    import docker
    from docker.errors import APIError, DockerException, ImageNotFound, NotFound
except Exception:  # pragma: no cover - handled at runtime when docker SDK missing
    docker = None
    APIError = Exception
    DockerException = Exception
    ImageNotFound = Exception
    NotFound = Exception


class SandboxManagerError(RuntimeError):
    def __init__(self, message: str, *, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


class SandboxManager:
    def __init__(
        self,
        *,
        default_image: str,
        default_public_host: str | None = None,
        docker_base_url: str | None = None,
        healthcheck_host: str = "127.0.0.1",
    ) -> None:
        self.default_image = default_image
        self.default_public_host = default_public_host
        self.docker_base_url = docker_base_url
        self.healthcheck_host = healthcheck_host
        self._client = None

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None

    def _client_or_raise(self):
        if docker is None:
            raise SandboxManagerError(
                "docker SDK is not installed. Please add `docker` to requirements.",
                status_code=500,
            )
        if self._client is None:
            try:
                if self.docker_base_url:
                    self._client = docker.DockerClient(base_url=self.docker_base_url)
                else:
                    self._client = docker.from_env()
                self._client.ping()
            except Exception as exc:
                raise SandboxManagerError(
                    f"Failed to connect Docker daemon: {exc}",
                    status_code=503,
                ) from exc
        return self._client

    @staticmethod
    def _resolve_host_port(ports: dict[str, Any], container_port: str) -> int | None:
        entries = ports.get(container_port)
        if not entries:
            return None
        try:
            return int(entries[0]["HostPort"])
        except Exception:
            return None

    def _to_payload(self, container: Any, *, public_host: str | None = None) -> dict[str, Any]:
        container.reload()
        attrs = container.attrs or {}
        net = attrs.get("NetworkSettings", {}) or {}
        ports = net.get("Ports", {}) or {}
        labels = (attrs.get("Config", {}) or {}).get("Labels", {}) or {}
        mounts = attrs.get("Mounts", []) or []

        host = public_host or self.default_public_host or self.healthcheck_host
        novnc_port = self._resolve_host_port(ports, "5800/tcp")
        vnc_port = self._resolve_host_port(ports, "5900/tcp")
        api_port = self._resolve_host_port(ports, "8899/tcp")

        urls = {
            "novnc": f"http://{host}:{novnc_port}" if novnc_port else None,
            "api": f"http://{host}:{api_port}" if api_port else None,
            "vnc": f"{host}:{vnc_port}" if vnc_port else None,
        }

        return {
            "sandbox_id": container.name,
            "container_id": container.id,
            "status": container.status,
            "image": ((attrs.get("Config", {}) or {}).get("Image") or ""),
            "created": attrs.get("Created"),
            "ports": {
                "novnc": novnc_port,
                "vnc": vnc_port,
                "api": api_port,
                "raw": ports,
            },
            "urls": urls,
            "labels": labels,
            "mounts": mounts,
        }

    def _wait_api_ready(self, *, api_port: int | None, timeout_sec: int) -> tuple[bool, str | None]:
        if not api_port:
            return False, "api port not published"
        deadline = time.time() + max(1, timeout_sec)
        last_error: str | None = None
        url = f"http://{self.healthcheck_host}:{api_port}/health"
        while time.time() < deadline:
            try:
                response = requests.get(url, timeout=1.0)
                if response.status_code == 200:
                    return True, None
                last_error = f"health returned {response.status_code}"
            except Exception as exc:
                last_error = str(exc)
            time.sleep(0.5)
        return False, last_error or "timeout"

    def create(
        self,
        *,
        sandbox_id: str | None,
        image: str | None,
        start_url: str,
        display_width: int,
        display_height: int,
        user_id: int,
        group_id: int,
        keep_app_running: bool,
        novnc_port: int | None,
        vnc_port: int | None,
        api_port: int | None,
        extra_env: dict[str, str] | None,
        restart_policy: str,
        wait_ready: bool,
        ready_timeout_sec: int,
        public_host: str | None = None,
    ) -> dict[str, Any]:
        client = self._client_or_raise()
        name = (sandbox_id or f"sandbox-{uuid4().hex[:8]}").strip()
        if not name:
            raise SandboxManagerError("sandbox_id is empty")

        try:
            existing = client.containers.get(name)
            raise SandboxManagerError(
                f"Sandbox already exists: {name} (status={existing.status})",
                status_code=409,
            )
        except NotFound:
            pass
        except Exception as exc:
            raise SandboxManagerError(
                f"Failed to check existing sandbox '{name}': {exc}",
                status_code=500,
            ) from exc

        ports = {
            "5800/tcp": novnc_port if novnc_port else None,
            "5900/tcp": vnc_port if vnc_port else None,
            "8899/tcp": api_port if api_port else None,
        }
        env = {
            "USER_ID": str(user_id),
            "GROUP_ID": str(group_id),
            "KEEP_APP_RUNNING": "1" if keep_app_running else "0",
            "DISPLAY_WIDTH": str(display_width),
            "DISPLAY_HEIGHT": str(display_height),
            "START_URL": start_url,
        }
        if extra_env:
            env.update({str(k): str(v) for k, v in extra_env.items()})

        labels = {
            "stewardflow.managed": "true",
            "stewardflow.sandbox_id": name,
        }
        restart = None
        if restart_policy == "unless-stopped":
            restart = {"Name": "unless-stopped"}
        elif restart_policy == "always":
            restart = {"Name": "always"}

        run_kwargs: dict[str, Any] = {
            "image": image or self.default_image,
            "name": name,
            "detach": True,
            "ports": ports,
            "environment": env,
            "labels": labels,
        }
        if restart is not None:
            run_kwargs["restart_policy"] = restart

        try:
            container = client.containers.run(**run_kwargs)
        except ImageNotFound as exc:
            raise SandboxManagerError(
                f"Image not found: {image or self.default_image}",
                status_code=404,
            ) from exc
        except (APIError, DockerException) as exc:
            raise SandboxManagerError(f"Failed to create sandbox: {exc}", status_code=500) from exc

        payload = self._to_payload(container, public_host=public_host)
        if wait_ready:
            ready, reason = self._wait_api_ready(
                api_port=payload["ports"]["api"],
                timeout_sec=ready_timeout_sec,
            )
            payload["ready"] = ready
            payload["ready_error"] = reason
        return payload

    def list(self, *, include_exited: bool) -> list[dict[str, Any]]:
        client = self._client_or_raise()
        try:
            containers = client.containers.list(
                all=include_exited,
                filters={"label": "stewardflow.managed=true"},
            )
        except Exception as exc:
            raise SandboxManagerError(f"Failed to list sandboxes: {exc}", status_code=500) from exc
        return [self._to_payload(c) for c in containers]

    def get(self, sandbox_id: str) -> dict[str, Any]:
        client = self._client_or_raise()
        try:
            container = client.containers.get(sandbox_id)
        except NotFound as exc:
            raise SandboxManagerError(f"Sandbox not found: {sandbox_id}", status_code=404) from exc
        except Exception as exc:
            raise SandboxManagerError(f"Failed to get sandbox: {exc}", status_code=500) from exc
        return self._to_payload(container)

    def start(self, sandbox_id: str) -> dict[str, Any]:
        client = self._client_or_raise()
        try:
            container = client.containers.get(sandbox_id)
            container.start()
            return self._to_payload(container)
        except NotFound as exc:
            raise SandboxManagerError(f"Sandbox not found: {sandbox_id}", status_code=404) from exc
        except Exception as exc:
            raise SandboxManagerError(f"Failed to start sandbox: {exc}", status_code=500) from exc

    def stop(self, sandbox_id: str, *, timeout_sec: int = 10) -> dict[str, Any]:
        client = self._client_or_raise()
        try:
            container = client.containers.get(sandbox_id)
            container.stop(timeout=max(1, timeout_sec))
            return self._to_payload(container)
        except NotFound as exc:
            raise SandboxManagerError(f"Sandbox not found: {sandbox_id}", status_code=404) from exc
        except Exception as exc:
            raise SandboxManagerError(f"Failed to stop sandbox: {exc}", status_code=500) from exc

    def delete(self, sandbox_id: str, *, force: bool = True) -> dict[str, Any]:
        client = self._client_or_raise()
        try:
            container = client.containers.get(sandbox_id)
            container.remove(force=force)
        except NotFound as exc:
            raise SandboxManagerError(f"Sandbox not found: {sandbox_id}", status_code=404) from exc
        except Exception as exc:
            raise SandboxManagerError(f"Failed to delete sandbox: {exc}", status_code=500) from exc
        return {
            "deleted": True,
            "sandbox_id": sandbox_id,
        }

    def logs(self, sandbox_id: str, *, tail: int = 200) -> dict[str, Any]:
        client = self._client_or_raise()
        try:
            container = client.containers.get(sandbox_id)
            text = container.logs(tail=max(1, tail)).decode("utf-8", errors="replace")
            return {"sandbox_id": sandbox_id, "tail": tail, "logs": text}
        except NotFound as exc:
            raise SandboxManagerError(f"Sandbox not found: {sandbox_id}", status_code=404) from exc
        except Exception as exc:
            raise SandboxManagerError(f"Failed to get logs: {exc}", status_code=500) from exc

    def health(self, sandbox_id: str, *, timeout_sec: int = 3) -> dict[str, Any]:
        payload = self.get(sandbox_id)
        api_port = payload.get("ports", {}).get("api")
        status = payload.get("status")
        if status != "running":
            return {
                "sandbox_id": sandbox_id,
                "ok": False,
                "reason": "sandbox_not_running",
                "sandbox_status": status,
                "api_port": api_port,
            }
        if not api_port:
            return {
                "sandbox_id": sandbox_id,
                "ok": False,
                "reason": "sandbox_api_port_unpublished",
                "sandbox_status": status,
                "api_port": None,
            }

        url = f"http://{self.healthcheck_host}:{api_port}/health"
        try:
            response = requests.get(url, timeout=max(1, timeout_sec))
            body: Any
            try:
                body = response.json()
            except Exception:
                body = response.text
            return {
                "sandbox_id": sandbox_id,
                "ok": response.status_code == 200,
                "url": url,
                "status_code": response.status_code,
                "body": body,
                "sandbox_status": status,
                "api_port": api_port,
            }
        except Exception as exc:
            return {
                "sandbox_id": sandbox_id,
                "ok": False,
                "url": url,
                "reason": "health_request_failed",
                "error": str(exc),
                "sandbox_status": status,
                "api_port": api_port,
            }
