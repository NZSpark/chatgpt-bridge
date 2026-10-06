"""非回环暴露的告警与「零配置免鉴权」设计决定的回归锁（doc/tasks.md T3.2 / §9）。

用户的设计约束：**本项目不需要 API Key**——客户端（Pi / Codex）`api_key`
随便填即可，`/v1/models` 与 `/v1/chat/completions` 对任意（甚至缺失）认证头
都必须照常服务。所以这里做的是**反向回归**：一旦有人“顺手”加了鉴权，
这些用例会立刻失败，而不是让缺陷悄悄上线。
"""

import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from chatgpt_web import config
from chatgpt_web.server import _warn_if_exposed, app

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class LoopbackDetectionTests(unittest.TestCase):
    def test_loopback_hosts(self) -> None:
        for host in ("127.0.0.1", "127.1.2.3", "::1", "[::1]", "localhost", "LOCALHOST", ""):
            with self.subTest(host=host):
                self.assertTrue(config.is_loopback_host(host))

    def test_exposed_hosts(self) -> None:
        for host in ("0.0.0.0", "192.168.1.5", "example.com", "::", "10.0.0.1"):
            with self.subTest(host=host):
                self.assertFalse(config.is_loopback_host(host))


class ExposureWarningTests(unittest.TestCase):
    def test_warns_on_non_loopback(self) -> None:
        with self.assertLogs("chatgpt_web.server", level="WARNING") as captured:
            self.assertTrue(_warn_if_exposed("0.0.0.0"))
        message = captured.records[-1].getMessage()
        self.assertIn("不提供任何鉴权", message)
        self.assertIn("0.0.0.0", message)

    def test_no_warning_on_loopback(self) -> None:
        with mock.patch("chatgpt_web.server.logger") as logger:
            self.assertFalse(_warn_if_exposed("127.0.0.1"))
            logger.warning.assert_not_called()

    def test_default_config_is_loopback(self) -> None:
        """仓库内建默认值必须是回环地址（.env.example 亦同）。"""
        self.assertTrue(config.is_loopback_host(config.HOST))
        example = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")
        self.assertIn("\nHOST=127.0.0.1\n", example)


class NoAuthRegressionTests(unittest.TestCase):
    """反向回归：没有认证头也不得返回 401/403。"""

    def setUp(self) -> None:
        self.client = TestClient(app)

    def test_models_without_credentials(self) -> None:
        res = self.client.get("/v1/models")  # 不带任何 Authorization
        self.assertNotIn(res.status_code, (401, 403), res.text)
        self.assertEqual(res.status_code, 200)
        self.assertIn("chatgpt-chat", [m["id"] for m in res.json()["data"]])

    def test_models_with_bogus_api_key(self) -> None:
        res = self.client.get("/v1/models", headers={"Authorization": "Bearer whatever"})
        self.assertEqual(res.status_code, 200)

    def test_chat_completions_without_credentials_is_not_auth_error(self) -> None:
        payload = {"model": "chatgpt-chat", "messages": [{"role": "user", "content": "hi"}]}
        res = self.client.post("/v1/chat/completions", json=payload, headers={})
        self.assertNotIn(
            res.status_code, (401, 403),
            f"未提供 API Key 不应被拒绝（这是设计需求）：{res.status_code} {res.text}",
        )

    def test_responses_without_credentials_is_not_auth_error(self) -> None:
        payload = {"model": "chatgpt-chat", "input": "hi"}
        res = self.client.post("/v1/responses", json=payload, headers={})
        self.assertNotIn(res.status_code, (401, 403), res.text)


class NoApiKeyConfigTests(unittest.TestCase):
    def test_library_has_no_api_key_auth(self) -> None:
        offenders = []
        for path in sorted((PROJECT_ROOT / "chatgpt_web").glob("*.py")):
            text = path.read_text(encoding="utf-8")
            for token in ("API_KEY", "AUTHORIZATION", "Bearer"):
                if token in text:
                    offenders.append(f"{path.name}: {token}")
        self.assertFalse(
            offenders,
            "库代码里出现了鉴权痕迹（本项目设计上不需要 API Key）：\n"
            + "\n".join(offenders),
        )

    def test_env_example_declares_no_auth_design(self) -> None:
        example = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")
        self.assertIn("不提供鉴权", example)


if __name__ == "__main__":
    unittest.main(verbosity=2)
