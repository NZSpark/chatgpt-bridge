"""E2E harness 的纯单元测试：不需要网络、登录或浏览器。

覆盖 doc/tasks.md：

* **T2.2** —— ``BridgeServer.ensure_started`` 的任何失败路径都必须回收自己
  拉起的子进程（``setUpModule`` 抛错时 unittest 不调用 ``tearDownModule``），
  否则残留 uvicorn + Chromium 会独占 ``user_data``、污染后续所有运行。
"""

import os
import socket
import subprocess
import sys
import unittest
from unittest import mock

from tests.e2e import bridge


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


class StartupFailureCleanupTests(unittest.TestCase):
    """T2.2：启动失败必须回收子进程 / 释放端口。"""

    def _server(self, port: int) -> bridge.BridgeServer:
        return bridge.BridgeServer(f"http://127.0.0.1:{port}")

    def test_timeout_reaps_child_and_releases_state(self) -> None:
        """超时（探活一直失败）分支：子进程被 terminate/kill，句柄与日志被释放。"""
        server = self._server(65531)
        real_popen = subprocess.Popen
        spawned = {}

        def fake_popen(cmd, **kwargs):  # noqa: ANN001
            # 用真实长眠进程替代 uvicorn，避免测试里拉起浏览器
            proc = real_popen(
                [sys.executable, "-c", "import time; time.sleep(120)"], **kwargs
            )
            spawned["pid"] = proc.pid
            return proc

        with mock.patch.object(bridge, "STARTUP_TIMEOUT_S", 1.0), \
                mock.patch.object(bridge, "reap_orphan_bridges", return_value=[]), \
                mock.patch.object(bridge.subprocess, "Popen", side_effect=fake_popen), \
                mock.patch.object(bridge.BridgeServer, "healthz", return_value=None):
            with self.assertRaises(RuntimeError):
                server.ensure_started()

        self.assertIn("pid", spawned)
        self.assertIsNone(server.proc)
        self.assertFalse(server.started_by_us)
        self.assertIsNone(server._log_file)
        self.assertFalse(_pid_alive(spawned["pid"]), "超时后子进程仍存活（泄漏）")

    def test_early_exit_reaps_child(self) -> None:
        server = self._server(65532)
        real_popen = subprocess.Popen
        spawned = {}

        def fake_popen(cmd, **kwargs):  # noqa: ANN001
            proc = real_popen([sys.executable, "-c", "raise SystemExit(3)"], **kwargs)
            spawned["pid"] = proc.pid
            return proc

        with mock.patch.object(bridge, "reap_orphan_bridges", return_value=[]), \
                mock.patch.object(bridge.subprocess, "Popen", side_effect=fake_popen), \
                mock.patch.object(bridge.BridgeServer, "healthz", return_value=None):
            with self.assertRaises(RuntimeError):
                server.ensure_started()

        self.assertIsNone(server.proc)
        self.assertFalse(server.started_by_us)
        self.assertFalse(_pid_alive(spawned["pid"]))

    def test_stop_is_idempotent(self) -> None:
        server = self._server(65533)
        server.stop()  # 从未 start 过也不能抛错
        server.stop()


class PortReleaseHelperTests(unittest.TestCase):
    def test_wait_port_closed(self) -> None:
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        # backlog 要够大：wait_port_closed 会反复 connect 而不 accept，
        # backlog 满了之后 connect 会失败（macOS 报 EWOULDBLOCK），看起来像“端口已关”。
        listener.listen(128)
        port = listener.getsockname()[1]
        try:
            self.assertFalse(bridge.wait_port_closed("127.0.0.1", port, timeout_s=0.6))
        finally:
            listener.close()
        self.assertTrue(bridge.wait_port_closed("127.0.0.1", port, timeout_s=3.0))


class ReapOrphanTests(unittest.TestCase):
    def test_only_orphans_are_killed(self) -> None:
        """非孤儿（用户自己在跑的 bridge）绝不能被误杀。"""
        with mock.patch.object(bridge, "_bridge_pids", return_value=[12345]), \
                mock.patch.object(bridge, "is_orphan", return_value=False), \
                mock.patch.object(bridge, "_terminate_pid") as killer:
            self.assertEqual(bridge.reap_orphan_bridges(8002), [])
            killer.assert_not_called()

    def test_orphan_of_other_port_is_kept(self) -> None:
        with mock.patch.object(bridge, "_bridge_pids", return_value=[12345]), \
                mock.patch.object(bridge, "is_orphan", return_value=True), \
                mock.patch.object(bridge.subprocess, "run") as run, \
                mock.patch.object(bridge, "_terminate_pid") as killer:
            run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="python -m uvicorn chatgpt_api_server:app --port 9999"
            )
            self.assertEqual(bridge.reap_orphan_bridges(8002), [])
            killer.assert_not_called()

    def test_orphan_of_same_port_is_killed(self) -> None:
        with mock.patch.object(bridge, "_bridge_pids", return_value=[12345]), \
                mock.patch.object(bridge, "is_orphan", return_value=True), \
                mock.patch.object(bridge.subprocess, "run") as run, \
                mock.patch.object(bridge, "_terminate_pid", return_value=True) as killer:
            run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="python -m uvicorn chatgpt_api_server:app --port 8002"
            )
            self.assertEqual(bridge.reap_orphan_bridges(8002), [12345])
            killer.assert_called_once_with(12345)


if __name__ == "__main__":
    unittest.main(verbosity=2)
