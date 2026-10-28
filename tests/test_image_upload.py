"""图片上传的单元测试；使用仓库中的真实 PNG（doc/speed.png）作为样本。"""

import base64
import unittest
from pathlib import Path
from unittest import mock

from chatgpt_web import config
from chatgpt_web.chat_io import ChatIOMixin
from chatgpt_web.prompting import (
    ImageAttachment,
    _image_reference,
    _messages_since_last_assistant,
    _parse_data_uri,
    extract_images,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
IMAGE_FIXTURE = PROJECT_ROOT / "doc" / "speed.png"


def _data_uri(data: bytes, mime: str = "image/png") -> str:
    return f"data:{mime};base64," + base64.b64encode(data).decode("ascii")


class ImageExtractionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.image_bytes = IMAGE_FIXTURE.read_bytes()
        cls.uri = _data_uri(cls.image_bytes)

    def test_speed_png_is_extracted_as_exact_original_bytes(self):
        """真实测试图片应被解码为完全一致的 PNG 字节，而不只是文本占位符。"""
        messages = [{
            "role": "user",
            "content": [
                {"type": "text", "text": "请分析这张表格"},
                {"type": "image_url", "image_url": {"url": self.uri}},
            ],
        }]

        attachments = extract_images(messages)

        self.assertEqual(len(attachments), 1)
        self.assertEqual(attachments[0].name, "image.png")
        self.assertEqual(attachments[0].mime, "image/png")
        self.assertEqual(attachments[0].data, self.image_bytes)
        self.assertIsNone(attachments[0].url)
        self.assertTrue(attachments[0].data.startswith(b"\x89PNG\r\n\x1a\n"))

    def test_data_uri_parser_returns_mime_and_original_bytes(self):
        self.assertEqual(_parse_data_uri(self.uri), ("image/png", self.image_bytes))

    def test_invalid_or_empty_data_uri_is_skipped(self):
        for uri in (
            "data:image/png;base64,",
            "data:image/png,not-base64",
            "data:image/png;base64,!!!",
        ):
            with self.subTest(uri=uri):
                self.assertEqual(
                    extract_images([{"role": "user", "content": [
                        {"type": "image_url", "image_url": {"url": uri}}
                    ]}]),
                    [],
                )

    def test_supported_image_part_shapes(self):
        messages = [{"role": "user", "content": [
            {"type": "image_url", "image_url": self.uri},
            {"type": "input_image", "image_url": "https://example.test/picture.jpg?token=x"},
            {"url": "https://example.test/raw.webp"},
            {"image": "https://example.test/legacy.png"},
        ]}]

        attachments = extract_images(messages)

        self.assertEqual(len(attachments), 4)
        self.assertEqual(attachments[0].data, self.image_bytes)
        self.assertEqual(attachments[1].name, "picture.jpg")
        self.assertEqual(attachments[1].url, "https://example.test/picture.jpg?token=x")
        self.assertEqual(attachments[2].name, "raw.webp")
        self.assertEqual(attachments[3].name, "legacy.png")

    def test_remote_url_keeps_url_and_extracts_filename_without_query(self):
        url = "https://example.test/charts/speed.png?signature=private"
        attachments = extract_images([{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": url}}
        ]}])

        self.assertEqual(len(attachments), 1)
        self.assertEqual(attachments[0].name, "speed.png")
        self.assertEqual(attachments[0].url, url)
        self.assertIsNone(attachments[0].data)

    def test_reference_does_not_contain_base64_payload(self):
        payload = base64.b64encode(self.image_bytes).decode("ascii")
        reference = _image_reference(self.uri)

        self.assertIn("[图片]", reference)
        self.assertIn("image/png", reference)
        self.assertNotIn(payload, reference)
        self.assertNotIn(payload, str(extract_images([{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": self.uri}}
        ]}])[0].name))

    def test_only_delta_extracts_images_after_last_assistant(self):
        messages = [
            {"role": "user", "content": [{"type": "image_url", "image_url": {"url": self.uri}}]},
            {"role": "assistant", "content": "旧图片已分析"},
            {"role": "user", "content": [{"type": "image_url", "image_url": {"url": "https://example.test/new.png"}}]},
        ]

        delta = extract_images(messages, only_delta=True)

        self.assertEqual(len(delta), 1)
        self.assertEqual(delta[0].url, "https://example.test/new.png")
        self.assertEqual(_messages_since_last_assistant(messages), messages[2:])

    def test_delta_without_assistant_falls_back_to_all_messages(self):
        messages = [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": self.uri}}
        ]}]
        self.assertEqual(len(extract_images(messages, only_delta=True)), 1)

    def test_empty_data_bytes_are_not_turned_into_attachments(self):
        self.assertEqual(extract_images([{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,"}}
        ]}]), [])


class ImageCollectionTests(unittest.TestCase):
    def test_feature_disabled_returns_no_attachments(self):
        from chatgpt_web.api.chat_adapter import collect_images

        messages = [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": "https://example.test/a.png"}}
        ]}]
        with mock.patch.object(config, "IMAGE_PASTE_ENABLED", False):
            self.assertEqual(collect_images(messages), [])

    def test_zero_and_positive_limits_are_respected(self):
        from chatgpt_web.api.chat_adapter import collect_images

        messages = [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": "https://example.test/a.png"}},
            {"type": "image_url", "image_url": {"url": "https://example.test/b.png"}},
        ]}]
        with mock.patch.object(config, "IMAGE_PASTE_ENABLED", True):
            with mock.patch.object(config, "IMAGE_PASTE_MAX", 0):
                self.assertEqual(collect_images(messages), [])
            with mock.patch.object(config, "IMAGE_PASTE_MAX", 1):
                selected = collect_images(messages)
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0].name, "a.png")


class ImagePasteDispatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_speed_png_bytes_are_passed_to_page_paste_script(self):
        """确认真实样本字节到达浏览器粘贴脚本；这不替代真实网页附件验收。"""
        image_bytes = IMAGE_FIXTURE.read_bytes()
        image = ImageAttachment(name=IMAGE_FIXTURE.name, mime="image/png", data=image_bytes)
        page = mock.Mock()
        page.evaluate = mock.AsyncMock(return_value=True)
        composer = mock.Mock()
        composer.click = mock.AsyncMock(return_value=None)
        driver = object.__new__(ChatIOMixin)

        with mock.patch.object(config, "IMAGE_PASTE_ENABLED", True):
            pasted = await driver._paste_images(page, composer, [image])

        self.assertEqual(pasted, 1)
        composer.click.assert_awaited_once()
        page.evaluate.assert_awaited_once()
        script, args = page.evaluate.await_args.args
        self.assertIn("new DataTransfer()", script)
        self.assertIn("new ClipboardEvent('paste'", script)
        self.assertEqual(args, [IMAGE_FIXTURE.name, "image/png", list(image_bytes)])

    async def test_paste_dispatch_failure_is_non_fatal(self):
        image = ImageAttachment(name="speed.png", mime="image/png", data=IMAGE_FIXTURE.read_bytes())
        page = mock.Mock()
        page.evaluate = mock.AsyncMock(side_effect=RuntimeError("test dispatch failure"))
        composer = mock.Mock()
        composer.click = mock.AsyncMock(return_value=None)
        driver = object.__new__(ChatIOMixin)

        with mock.patch.object(config, "IMAGE_PASTE_ENABLED", True):
            pasted = await driver._paste_images(page, composer, [image])

        self.assertEqual(pasted, 0)
        page.evaluate.assert_awaited_once()

    async def test_disabled_feature_does_not_dispatch_paste(self):
        page = mock.Mock()
        page.evaluate = mock.AsyncMock(return_value=True)
        driver = object.__new__(ChatIOMixin)

        with mock.patch.object(config, "IMAGE_PASTE_ENABLED", False):
            pasted = await driver._paste_images(page, mock.Mock(), [
                ImageAttachment(name="speed.png", mime="image/png", data=IMAGE_FIXTURE.read_bytes())
            ])

        self.assertEqual(pasted, 0)
        page.evaluate.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
