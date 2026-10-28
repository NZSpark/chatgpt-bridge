"""真实 Chromium 测试：验证 doc/speed.png 通过合成 paste 事件进入网页编辑器。

不访问 ChatGPT 网络或登录账户。该测试验证浏览器事件接收方真实拿到 File 及
原始字节，不把“事件派发成功”等同于生产网站已接受附件。
"""

import asyncio
import unittest
from pathlib import Path
from unittest import mock

from playwright.async_api import async_playwright

from chatgpt_web import config
from chatgpt_web.chat_io import ChatIOMixin
from chatgpt_web.prompting import ImageAttachment

PROJECT_ROOT = Path(__file__).resolve().parents[2]
IMAGE_FIXTURE = PROJECT_ROOT / "doc" / "speed.png"


class ImagePasteBrowserE2ETests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not IMAGE_FIXTURE.is_file():
            raise unittest.SkipTest(f"缺少测试图片：{IMAGE_FIXTURE}")
        cls._loop = asyncio.new_event_loop()
        cls._pw = None
        cls._browser = None
        try:
            cls._pw = cls._loop.run_until_complete(async_playwright().start())
            cls._browser = cls._loop.run_until_complete(cls._pw.chromium.launch())
        except Exception as exc:  # noqa: BLE001
            cls._teardown_browser()
            raise unittest.SkipTest(f"Playwright Chromium 不可用：{exc!r}")

    @classmethod
    def _teardown_browser(cls):
        if cls._browser is not None:
            try:
                cls._loop.run_until_complete(cls._browser.close())
            except Exception:  # noqa: BLE001
                pass
            cls._browser = None
        if cls._pw is not None:
            try:
                cls._loop.run_until_complete(cls._pw.stop())
            except Exception:  # noqa: BLE001
                pass
            cls._pw = None

    @classmethod
    def tearDownClass(cls):
        cls._teardown_browser()
        cls._loop.close()

    def test_speed_png_file_and_exact_bytes_reach_paste_listener(self):
        image_bytes = IMAGE_FIXTURE.read_bytes()

        async def run_case():
            page = await self._browser.new_page()
            try:
                await page.set_content("""
                    <!doctype html>
                    <html lang="zh-CN">
                    <meta charset="utf-8">
                    <title>Image paste fixture</title>
                    <div id="composer" contenteditable="true" role="textbox"
                         aria-label="Message"></div>
                    <script>
                      const composer = document.querySelector('#composer');
                      window.__pasteReport = null;
                      composer.addEventListener('paste', async (event) => {
                        // 真实浏览器中的监听器读取 ClipboardEvent.files。
                        const files = Array.from(event.clipboardData?.files || []);
                        event.preventDefault();
                        const report = {
                          textBeforePaste: composer.innerText,
                          files: await Promise.all(files.map(async (file) => ({
                            name: file.name,
                            type: file.type,
                            size: file.size,
                            bytes: Array.from(new Uint8Array(await file.arrayBuffer()))
                          })))
                        };
                        window.__pasteReport = report;
                      });
                    </script>
                    </html>
                """)
                composer = page.locator("#composer")
                await composer.fill("请分析这张图片")
                attachment = ImageAttachment(
                    name=IMAGE_FIXTURE.name,
                    mime="image/png",
                    data=image_bytes,
                )
                driver = object.__new__(ChatIOMixin)
                with mock.patch.object(config, "IMAGE_PASTE_ENABLED", True):
                    dispatched = await driver._paste_images(page, composer, [attachment])

                self.assertEqual(dispatched, 1, "粘贴层没有成功派发图片事件")
                await page.wait_for_function(
                    "() => window.__pasteReport !== null",
                    timeout=5000,
                )
                report = await page.evaluate("() => window.__pasteReport")
                self.assertEqual(
                    report["textBeforePaste"],
                    "请分析这张图片",
                    "粘贴图片时，文本应已存在于同一个 composer 中",
                )
                self.assertEqual(len(report["files"]), 1, "paste 监听器应收到一张图片")
                uploaded = report["files"][0]
                self.assertEqual(uploaded["name"], "speed.png")
                self.assertEqual(uploaded["type"], "image/png")
                self.assertEqual(uploaded["size"], len(image_bytes))
                self.assertEqual(
                    uploaded["bytes"],
                    list(image_bytes),
                    "浏览器收到的 File 字节必须与 doc/speed.png 完全一致",
                )
            finally:
                await page.close()

        # 在 Playwright 所属的同一 event loop 执行，避免跨 loop 使用浏览器对象。
        self._loop.run_until_complete(run_case())


if __name__ == "__main__":
    unittest.main(verbosity=2)
