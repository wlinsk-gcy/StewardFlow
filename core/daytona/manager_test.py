from types import SimpleNamespace
import unittest
from urllib.parse import parse_qs, urlparse

from core.daytona.manager import DaytonaSandboxManager


class FakeComputerUse:
    def __init__(self):
        self.started = False
        self.start_calls = 0

    def start(self):
        self.started = True
        self.start_calls += 1
        return {"ok": True}

    def get_status(self):
        return {"running": self.started}


class FakeProcess:
    def __init__(self):
        self.exec_calls: list[tuple[str, float | None]] = []

    def exec(self, command: str, timeout: float | None = None):
        self.exec_calls.append((command, timeout))
        return {"exit_code": 0, "result": "launched"}


class FakeSandbox:
    def __init__(self, sandbox_id: str, *, signed_supported: bool = True):
        self.id = sandbox_id
        self.state = "stopped"
        self.start_calls = 0
        self.auto_stop_minutes = None
        self.computer_use = FakeComputerUse()
        self.process = FakeProcess()
        self._signed_supported = signed_supported

    def start(self):
        self.state = "started"
        self.start_calls += 1
        return {"ok": True}

    def set_autostop_interval(self, minutes: int):
        self.auto_stop_minutes = minutes
        return {"ok": True}

    def create_signed_preview_url(self, port: int, expires_in_seconds: int):
        if not self._signed_supported:
            raise RuntimeError("signed preview not supported")
        return SimpleNamespace(url=f"https://preview.local/signed/{self.id}/{port}?ttl={expires_in_seconds}")

    def get_preview_link(self, port: int):
        return SimpleNamespace(url=f"https://preview.local/plain/{self.id}/{port}", token="preview-token")


class FakeDaytona:
    def __init__(self, *, signed_supported: bool = True, delete_fails_for: set[str] | None = None):
        self._counter = 0
        self._signed_supported = signed_supported
        self.sandboxes = {}
        self.delete_calls: list[tuple[str, float]] = []
        self._delete_fails_for = set(delete_fails_for or set())

    def create(self):
        self._counter += 1
        sandbox = FakeSandbox(f"sb-{self._counter}", signed_supported=self._signed_supported)
        self.sandboxes[sandbox.id] = sandbox
        return sandbox

    def find_one(self, sandbox_id: str):
        if sandbox_id not in self.sandboxes:
            raise KeyError(sandbox_id)
        return self.sandboxes[sandbox_id]

    def delete(self, sandbox, timeout: float = 60):
        sandbox_id = sandbox.id
        self.delete_calls.append((sandbox_id, timeout))
        if sandbox_id in self._delete_fails_for:
            raise RuntimeError(f"failed to delete {sandbox_id}")
        self.sandboxes.pop(sandbox_id, None)


class DaytonaManagerTests(unittest.TestCase):
    def test_ensure_sandbox_reuses_trace_sandbox_and_restarts_when_stopped(self):
        client = FakeDaytona()
        manager = DaytonaSandboxManager(
            daytona_client=client,
            auto_stop_minutes=15,
            vnc_port=6080,
            vnc_url_ttl_seconds=3600,
        )

        first = manager.ensure_sandbox("trace-1")
        self.assertEqual(first.id, "sb-1")
        self.assertEqual(first.start_calls, 1)
        self.assertEqual(first.auto_stop_minutes, 15)

        first.state = "stopped"
        second = manager.ensure_sandbox("trace-1")
        self.assertEqual(second.id, first.id)
        self.assertEqual(second.start_calls, 2)
        self.assertEqual(len(client.sandboxes), 1)

    def test_get_vnc_view_returns_signed_preview_and_starts_computer_use(self):
        manager = DaytonaSandboxManager(
            daytona_client=FakeDaytona(),
            auto_stop_minutes=15,
            vnc_port=6080,
            vnc_url_ttl_seconds=3600,
        )

        view = manager.get_vnc_view("trace-2")
        self.assertEqual(view["sandbox_id"], "sb-1")
        parsed = urlparse(view["vnc_url"])
        qs = parse_qs(parsed.query)
        self.assertIn("signed/sb-1/6080", parsed.path)
        self.assertEqual(qs.get("autoconnect"), ["true"])
        self.assertEqual(qs.get("reconnect"), ["true"])
        self.assertEqual(qs.get("resize"), ["remote"])
        sandbox = manager.ensure_sandbox("trace-2", require_computer_use=False)
        self.assertEqual(sandbox.computer_use.start_calls, 1)

    def test_get_vnc_view_falls_back_to_preview_link_when_signed_unavailable(self):
        manager = DaytonaSandboxManager(
            daytona_client=FakeDaytona(signed_supported=False),
            auto_stop_minutes=15,
            vnc_port=6080,
            vnc_url_ttl_seconds=3600,
        )

        view = manager.get_vnc_view("trace-3")
        self.assertEqual(view["sandbox_id"], "sb-1")
        parsed = urlparse(view["vnc_url"])
        qs = parse_qs(parsed.query)
        self.assertEqual(parsed.path, "/plain/sb-1/6080")
        self.assertEqual(qs.get("autoconnect"), ["true"])
        self.assertEqual(qs.get("reconnect"), ["true"])
        self.assertEqual(qs.get("resize"), ["remote"])
        self.assertEqual(view["vnc_token"], "preview-token")

    def test_cleanup_deletes_all_trace_sandboxes_and_clears_mapping(self):
        client = FakeDaytona()
        manager = DaytonaSandboxManager(
            daytona_client=client,
            auto_stop_minutes=15,
            vnc_port=6080,
            vnc_url_ttl_seconds=3600,
        )
        manager.ensure_sandbox("trace-cleanup-1")
        manager.ensure_sandbox("trace-cleanup-2")

        result = manager.cleanup(timeout_seconds=25)

        self.assertEqual(result["attempted"], 2)
        self.assertEqual(result["deleted"], 2)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(result["not_found"], 0)
        self.assertEqual(sorted(result["deleted_sandbox_ids"]), ["sb-1", "sb-2"])
        self.assertEqual(client.delete_calls, [("sb-1", 25.0), ("sb-2", 25.0)])
        self.assertEqual(manager._trace_to_sandbox, {})

    def test_browser_navigate_executes_launch_command_and_returns_vnc_view(self):
        manager = DaytonaSandboxManager(
            daytona_client=FakeDaytona(),
            auto_stop_minutes=15,
            vnc_port=6080,
            vnc_url_ttl_seconds=3600,
        )

        result = manager.browser_navigate("trace-browser", "https://www.xiaohongshu.com")
        sandbox = manager.ensure_sandbox("trace-browser", require_computer_use=False)

        self.assertEqual(result["sandbox_id"], "sb-1")
        self.assertEqual(result["url"], "https://www.xiaohongshu.com")
        self.assertIn("vnc_url", result)
        self.assertEqual(len(sandbox.process.exec_calls), 1)
        launch_command, timeout = sandbox.process.exec_calls[0]
        self.assertIn("https://www.xiaohongshu.com", launch_command)
        self.assertIn("/tmp/.X11-unix/X", launch_command)
        self.assertIn("no_display_available", launch_command)
        self.assertEqual(timeout, 45)


if __name__ == "__main__":
    unittest.main()
