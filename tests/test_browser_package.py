"""PI-903：browser 子包拆分后的模块级回归测试。

验证：

* selectors / dom_adapter / diagnostics 可独立导入（无循环依赖）；
* ``chatgpt_web.dom_adapter`` facade 与包内 ChatGPTDOMAdapter 是同一对象；
* adapter 仍以类属性暴露 JS/选择器兼容契约（GENERATING_JS / STOP_TOKEN_PATTERN 等）；
* selector version 存在；
* 生命周期 mixin 提供 launch/new page/close，且 driver 组合了它。
"""

import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from chatgpt_web import dom_adapter as facade  # noqa: E402
from chatgpt_web.browser import ChatGPTDOMAdapter, DiagnosticsMixin, selectors  # noqa: E402
from chatgpt_web.browser.driver import BrowserLifecycleMixin  # noqa: E402
from chatgpt_web.driver import ChatGPTWebDriver  # noqa: E402


class PackageImportTest(unittest.TestCase):
    def test_submodules_import(self):
        from chatgpt_web.browser import dom_adapter, diagnostics, selectors as sel  # noqa: F401

        self.assertIsNotNone(sel.SELECTOR_VERSION)

    def test_facade_reexports(self):
        self.assertIs(facade.ChatGPTDOMAdapter, ChatGPTDOMAdapter)
        self.assertEqual(facade.STOP_TOKEN_PATTERN, selectors.STOP_TOKEN_PATTERN)

    def test_selector_version_present(self):
        self.assertTrue(selectors.SELECTOR_VERSION)


class AdapterContractTest(unittest.TestCase):
    def test_class_attribute_contract(self):
        a = ChatGPTDOMAdapter()
        # tests/test_selectors.py 直接读取这些类属性
        self.assertTrue(a.STOP_TOKEN_PATTERN)
        self.assertIn("stop", a.GENERATING_JS.lower())
        self.assertIn("matches", a.STOP_CANDIDATES_JS)
        self.assertEqual(a.NEW_CHAT_SEARCH_TIMEOUT_S, 8.0)
        self.assertEqual(a.NEW_CHAT_POLL_INTERVAL_S, 0.4)

    def test_diagnostics_mixin_composed(self):
        self.assertIsInstance(ChatGPTDOMAdapter(), DiagnosticsMixin)
        for name in ("reply_diagnostics", "stop_diagnostics", "selector_diagnostics"):
            self.assertTrue(hasattr(ChatGPTDOMAdapter, name), name)

    def test_stop_control_selector_not_empty(self):
        self.assertIn("stop", selectors.STOP_CONTROL_SELECTOR)


class LifecycleTest(unittest.TestCase):
    def test_driver_composes_lifecycle_mixin(self):
        self.assertTrue(issubclass(ChatGPTWebDriver, BrowserLifecycleMixin))
        driver = ChatGPTWebDriver(user_data_dir="/tmp/chatgpt-test-noprofile")
        for name in ("launch_persistent_context", "new_browser_page", "close"):
            self.assertTrue(hasattr(driver, name), name)

    def test_new_page_requires_context(self):
        driver = ChatGPTWebDriver(user_data_dir="/tmp/chatgpt-test-noprofile")
        driver.context = None
        with self.assertRaises(RuntimeError):
            asyncio.run(driver.new_browser_page())

    def test_close_is_noop_when_not_started(self):
        driver = ChatGPTWebDriver(user_data_dir="/tmp/chatgpt-test-noprofile")
        driver.context = None
        driver.playwright = None
        asyncio.run(driver.close())  # 不应抛异常


if __name__ == "__main__":
    unittest.main()
