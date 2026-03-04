from __future__ import annotations

from typing import Any

import requests

from core.services.sandbox_manager import SandboxManager


class DockerSandboxClient:
    """Thin client for invoking the managed docker-sandbox API."""

    def __init__(
        self,
        *,
        default_image: str,
        docker_base_url: str | None,
        public_host: str | None,
        healthcheck_host: str,
        sandbox_id: str | None = None,
    ) -> None:
        self.manager = SandboxManager(
            default_image=default_image,
            default_public_host=public_host,
            docker_base_url=docker_base_url,
            healthcheck_host=healthcheck_host,
        )
        self.healthcheck_host = healthcheck_host
        self.sandbox_id = sandbox_id

    def close(self) -> None:
        self.manager.close()

    def set_sandbox_id(self, sandbox_id: str | None) -> None:
        self.sandbox_id = sandbox_id.strip() if isinstance(sandbox_id, str) and sandbox_id.strip() else None

    def _pick_sandbox_id(self, sandbox_id: str | None) -> str:
        if sandbox_id and sandbox_id.strip():
            return sandbox_id.strip()
        if self.sandbox_id:
            return self.sandbox_id

        items = self.manager.list(include_exited=False)
        running = [item for item in items if item.get("status") == "running"]
        if not running:
            raise RuntimeError("no_running_sandbox")
        running.sort(key=lambda item: str(item.get("created") or ""), reverse=True)

        chosen = running[0].get("sandbox_id")
        if not chosen:
            raise RuntimeError("invalid_sandbox_payload_missing_sandbox_id")
        return str(chosen)

    def resolve(self, sandbox_id: str | None = None) -> dict[str, Any]:
        sid = self._pick_sandbox_id(sandbox_id)
        payload = self.manager.get(sid)
        return payload

    def list(self, *, include_exited: bool = False) -> dict[str, Any]:
        items = self.manager.list(include_exited=include_exited)
        return {"count": len(items), "items": items}

    def health(self, *, sandbox_id: str | None = None, timeout_sec: int = 3) -> dict[str, Any]:
        sid = self._pick_sandbox_id(sandbox_id)
        return self.manager.health(sid, timeout_sec=timeout_sec)

    def logs(self, *, sandbox_id: str | None = None, tail: int = 200) -> dict[str, Any]:
        sid = self._pick_sandbox_id(sandbox_id)
        return self.manager.logs(sid, tail=tail)

    def _sandbox_api_base(self, sandbox_id: str | None) -> tuple[str, dict[str, Any]]:
        payload = self.resolve(sandbox_id=sandbox_id)
        sid = str(payload.get("sandbox_id") or "")
        if payload.get("status") != "running":
            raise RuntimeError(f"sandbox_not_running: {sid}")

        api_port = payload.get("ports", {}).get("api")
        if not api_port:
            raise RuntimeError(f"sandbox_api_port_unpublished: {sid}")

        return f"http://{self.healthcheck_host}:{int(api_port)}", payload

    @staticmethod
    def _parse_response(response: requests.Response) -> Any:
        try:
            data = response.json()
        except Exception:
            data = {"raw": response.text}

        if response.status_code >= 400:
            raise RuntimeError(f"http_{response.status_code}: {data}")
        return data

    def api_get(
        self,
        *,
        sandbox_id: str | None,
        path: str,
        timeout_sec: int = 30,
    ) -> dict[str, Any]:
        base_url, resolved = self._sandbox_api_base(sandbox_id)
        url = f"{base_url}{path}"
        response = requests.get(url, timeout=max(1, timeout_sec))
        data = self._parse_response(response)
        return {
            "sandbox_id": resolved.get("sandbox_id"),
            "api_url": url,
            "result": data,
        }

    def api_post(
        self,
        *,
        sandbox_id: str | None,
        path: str,
        payload: dict[str, Any],
        timeout_sec: int = 30,
    ) -> dict[str, Any]:
        base_url, resolved = self._sandbox_api_base(sandbox_id)
        url = f"{base_url}{path}"
        response = requests.post(url, json=payload, timeout=max(1, timeout_sec))
        data = self._parse_response(response)
        return {
            "sandbox_id": resolved.get("sandbox_id"),
            "api_url": url,
            "result": data,
        }
