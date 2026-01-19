import json
import unittest
import subprocess
from unittest.mock import patch, MagicMock
from core.tools.bash import BashTool


class TestBashTool(unittest.TestCase):
    def setUp(self):
        self.tool = BashTool()

    def test_simple_cmd(self):
        import platform

        commands = ["ls", "pwd", "ip a"]
        if platform.system().lower() == "windows":
            commands = ["dir", "cd", "ipconfig"]

        for command in commands:
            try:
                output = self.tool.execute(command)
                print(output)
            except RuntimeError as exc:
                print(str(exc))

    @patch("platform.system", return_value="Windows")
    @patch("shutil.which")
    def test_resolve_windows_native_cmd_goes_cmd(self, mock_which, mock_system):
        # Windows 原生命令：不需要 bash
        mock_which.side_effect = lambda x: None  # bash/wsl 都没有也无所谓（这里会走 cmd）
        cmd, uses_wsl, shell_kind = self.tool._resolve_bash("ipconfig")

        self.assertEqual(cmd, ["cmd.exe", "/c", "ipconfig"])
        self.assertFalse(uses_wsl)
        self.assertEqual(shell_kind, "cmd")

    @patch("platform.system", return_value="Windows")
    @patch("shutil.which")
    def test_resolve_windows_bash_cmd_prefers_bash(self, mock_which, mock_system):
        # 需要 bash 的命令：ls 属于 UNIX_CMDS
        def which_side_effect(name):
            if name == "bash":
                return r"C:\Program Files\Git\bin\bash.exe"
            if name == "wsl":
                return r"C:\Windows\System32\wsl.exe"
            return None

        mock_which.side_effect = which_side_effect
        cmd, uses_wsl, shell_kind = self.tool._resolve_bash("ls ~/Desktop")

        self.assertEqual(cmd, [r"C:\Program Files\Git\bin\bash.exe", "-lc", "ls ~/Desktop"])
        self.assertFalse(uses_wsl)
        self.assertEqual(shell_kind, "bash")

    @patch("platform.system", return_value="Windows")
    @patch("shutil.which")
    def test_resolve_windows_bash_cmd_fallback_wsl(self, mock_which, mock_system):
        # 需要 bash，但本地没有 bash，有 wsl
        def which_side_effect(name):
            if name == "bash":
                return None
            if name == "wsl":
                return r"C:\Windows\System32\wsl.exe"
            return None

        mock_which.side_effect = which_side_effect
        cmd, uses_wsl, shell_kind = self.tool._resolve_bash("grep foo file.txt")

        self.assertEqual(cmd, [r"C:\Windows\System32\wsl.exe", "bash", "-lc", "grep foo file.txt"])
        self.assertTrue(uses_wsl)
        self.assertEqual(shell_kind, "wsl")

    @patch("platform.system", return_value="Windows")
    @patch("shutil.which", return_value=None)
    def test_execute_windows_bash_missing_raises(self, mock_which, mock_system):
        # 需要 bash，但 bash/wsl 都找不到
        with self.assertRaises(RuntimeError) as ctx:
            self.tool.execute("ls ~/Desktop")
        self.assertIn("bash_unavailable", str(ctx.exception))

    @patch("platform.system", return_value="Windows")
    @patch("shutil.which")
    def test_execute_wsl_with_cwd_raises(self, mock_which, mock_system):
        # 需要 bash，且只走 wsl；传 cwd 会报错
        def which_side_effect(name):
            if name == "bash":
                return None
            if name == "wsl":
                return r"C:\Windows\System32\wsl.exe"
            return None

        mock_which.side_effect = which_side_effect

        with self.assertRaises(RuntimeError) as ctx:
            self.tool.execute("ls", cwd=r"C:\temp")
        self.assertIn("cwd is not supported", str(ctx.exception))

    @patch("platform.system", return_value="Linux")
    @patch("shutil.which", return_value=None)
    def test_resolve_non_windows_default_bin_bash(self, mock_which, mock_system):
        cmd, uses_wsl, shell_kind = self.tool._resolve_bash("echo hello")
        self.assertEqual(cmd, ["/bin/bash", "-lc", "echo hello"])
        self.assertFalse(uses_wsl)
        self.assertEqual(shell_kind, "bash")

    @patch("platform.system", return_value="Windows")
    @patch("shutil.which")
    @patch("subprocess.run")
    def test_execute_returns_json_structure(self, mock_run, mock_which, mock_system):
        # Windows 原生命令：走 cmd
        mock_which.side_effect = lambda x: None

        fake_completed = MagicMock()
        fake_completed.returncode = 0
        fake_completed.stdout = "OK\n"
        fake_completed.stderr = ""
        mock_run.return_value = fake_completed

        out = self.tool.execute("ipconfig")
        data = json.loads(out)

        self.assertEqual(data["platform"], "Windows")
        self.assertEqual(data["shell"], "cmd")
        self.assertEqual(data["exit_code"], 0)
        self.assertEqual(data["stdout"], "OK\n")
        self.assertEqual(data["stderr"], "")

        # 确认 subprocess.run 的调用参数正确
        mock_run.assert_called_once()
        called_cmd = mock_run.call_args.kwargs["args"] if "args" in mock_run.call_args.kwargs else \
        mock_run.call_args.args[0]
        self.assertEqual(called_cmd, ["cmd.exe", "/c", "ipconfig"])

    @patch("platform.system", return_value="Linux")
    @patch("shutil.which", return_value="/usr/bin/bash")
    @patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="bash", timeout=1))
    def test_execute_timeout_raises_runtimeerror(self, mock_run, mock_which, mock_system):
        with self.assertRaises(RuntimeError) as ctx:
            self.tool.execute("sleep 10", timeout_sec=1)
        self.assertIn("timed out", str(ctx.exception).lower())
