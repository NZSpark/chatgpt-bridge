"""Tests for stable session bucket selection and optional cwd scoping."""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from chatgpt_web import config
from chatgpt_web.models import ChatCompletionRequest, ChatMessage
from chatgpt_web.responses import ResponsesRequest
from chatgpt_web.server import _client_from_ua, _sanitize_session_key, _session_key
from chatgpt_web.session_scoping import (
    cwd_bucket_key,
    cwd_candidate_texts,
    extract_cwd_path,
    normalize_cwd_path,
)


class CwdCandidateTests(unittest.TestCase):
    def test_system_developer_and_only_first_user_are_collected(self):
        request = ChatCompletionRequest(messages=[
            ChatMessage(role="system", content="system metadata"),
            ChatMessage(role="developer", content="developer metadata"),
            ChatMessage(role="user", content="Working Directory: /workspace/first"),
            ChatMessage(role="user", content="Working Directory: /workspace/later"),
        ])
        self.assertEqual(cwd_candidate_texts(request), [
            "system metadata", "developer metadata", "Working Directory: /workspace/first",
        ])
        self.assertEqual(extract_cwd_path(cwd_candidate_texts(request)), "/workspace/first")

    def test_responses_instructions_take_precedence_over_input(self):
        request = ResponsesRequest(
            instructions="<cwd>/workspace/instructions</cwd>",
            input=[
                {"type": "message", "role": "user", "content": "Working Directory: /workspace/input"},
                {"type": "message", "role": "user", "content": "Working Directory: /workspace/later"},
            ],
        )
        self.assertEqual(extract_cwd_path(cwd_candidate_texts(request)), "/workspace/instructions")

    def test_responses_input_string_and_multimodal_text(self):
        request = ResponsesRequest(input="<cwd>/workspace/input-string</cwd>")
        self.assertEqual(extract_cwd_path(cwd_candidate_texts(request)), "/workspace/input-string")
        message = SimpleNamespace(role="user", content=[
            {"type": "input_text", "text": "Working Directory: /workspace/multimodal"},
            {"type": "input_image", "image_url": "not-a-path"},
        ])
        self.assertEqual(extract_cwd_path(cwd_candidate_texts(SimpleNamespace(messages=[message]))), "/workspace/multimodal")


class CwdParsingTests(unittest.TestCase):
    def test_supported_metadata_formats(self):
        cases = (
            ("<cwd> /workspace/repo </cwd>", "/workspace/repo"),
            ("Working Directory: '/workspace/repo'", "/workspace/repo"),
            ("Current Workspace Directory (/workspace/repo)", "/workspace/repo"),
            (r"Working Directory: C:\Users\dev\repo", "c:/users/dev/repo"),
            ("Current Workspace Directory (C:/Users/dev/repo/)", "c:/users/dev/repo"),
        )
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(extract_cwd_path([text]), expected)

    def test_natural_language_and_unmarked_paths_do_not_match(self):
        for text in (
            "I changed my working directory yesterday: /workspace/other",
            "The project lives at /workspace/repo",
            "Working Directory is probably /workspace/repo",
        ):
            with self.subTest(text=text):
                self.assertIsNone(extract_cwd_path([text]))

    def test_relative_empty_and_malformed_paths_are_rejected(self):
        for text in (
            "<cwd></cwd>", "<cwd>workspace/repo</cwd>",
            "Working Directory: ./repo", "Working Directory: ../repo",
            "Working Directory: /workspace/\x00repo",
        ):
            with self.subTest(text=text):
                self.assertIsNone(extract_cwd_path([text]))

    def test_first_valid_candidate_wins(self):
        self.assertEqual(extract_cwd_path([
            "Working Directory: relative/path",
            "Working Directory: /workspace/first",
            "<cwd>/workspace/second</cwd>",
        ]), "/workspace/first")

    def test_path_normalization(self):
        self.assertEqual(normalize_cwd_path("/workspace/a/../repo/./"), "/workspace/repo")
        self.assertEqual(normalize_cwd_path("C:\\\\Workspace\\\\a\\\\..\\\\Repo\\\\"), "c:/workspace/repo")
        self.assertEqual(normalize_cwd_path("/workspace/repo/"), "/workspace/repo")
        self.assertIsNone(normalize_cwd_path("repo"))

    def test_bucket_key_stability_length_and_full_path_hash(self):
        def request(path):
            return SimpleNamespace(messages=[SimpleNamespace(role="user", content=f"<cwd>{path}</cwd>")])
        first = cwd_bucket_key(request("/a/repo"), 12, 32)
        normalized_same = cwd_bucket_key(request("/a/./repo/"), 12, 32)
        other_parent = cwd_bucket_key(request("/b/repo"), 12, 32)
        self.assertEqual(first, normalized_same)
        self.assertNotEqual(first, other_parent)
        self.assertTrue(first.startswith("cwd:repo-"))
        self.assertLessEqual(len(first), 32)
        self.assertRegex(first, r"^cwd:[a-z0-9_-]+$")
        self.assertIsNone(cwd_bucket_key(request("/a"), 12, 4))


class SessionKeySanitizationTests(unittest.TestCase):
    def test_distinct_unsafe_values_do_not_collapse_to_same_key(self):
        slash = _sanitize_session_key("repo/a", max_len=64)
        space = _sanitize_session_key("repo a", max_len=64)
        already_safe = _sanitize_session_key("repo_a", max_len=64)
        self.assertEqual(len({slash, space, already_safe}), 3)

    def test_long_values_keep_digest_when_truncated(self):
        first = _sanitize_session_key("project-" + "a" * 200, max_len=32)
        second = _sanitize_session_key("project-" + "b" * 200, max_len=32)
        self.assertNotEqual(first, second)
        self.assertLessEqual(len(first), 32)
        self.assertRegex(first, r"^[A-Za-z0-9_.:-]+$")

    def test_auto_namespaces_are_reserved_from_explicit_values(self):
        explicit = _sanitize_session_key("ua:cursor", max_len=64, generated=False)
        automatic = _sanitize_session_key("ua:cursor", max_len=64, generated=True)
        self.assertNotEqual(explicit, automatic)
        self.assertEqual(automatic, "ua:cursor")

    def test_traversal_unicode_whitespace_and_empty_values_are_safe(self):
        traversal = _sanitize_session_key("../../秘密/repo", max_len=64)
        self.assertNotIn("/", traversal)
        self.assertNotIn("\\\\", traversal)
        self.assertLessEqual(len(traversal), 64)
        self.assertIsNone(_sanitize_session_key("  ", max_len=64))
        self.assertEqual(_sanitize_session_key(" stable ", max_len=64), "stable")


class UserAgentParsingTests(unittest.TestCase):
    def test_known_client_uses_token_boundaries(self):
        self.assertEqual(_client_from_ua("Cursor/1.0"), "ua:cursor")
        self.assertEqual(_client_from_ua("mycursorproxy/1.0"), "ua:mycursorproxy")
        self.assertIsNone(_client_from_ua("containscontinueword"))

    def test_unknown_product_fallback_and_invalid_values(self):
        self.assertEqual(_client_from_ua("python-httpx/0.27.0"), "ua:python-httpx")
        self.assertIsNone(_client_from_ua("browser-without-version"))
        self.assertIsNone(_client_from_ua(None))
        self.assertIsNone(_client_from_ua(123))


class SessionKeyPriorityTests(unittest.TestCase):
    def setUp(self):
        self.patcher = patch.multiple(
            config,
            SESSION_SCOPING=True,
            SESSION_SCOPING_BY_CWD=True,
            SESSION_SCOPING_BY_UA=True,
            SESSION_CWD_HASH_LEN=12,
            SESSION_KEY_MAX_LEN=64,
        )
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def test_header_then_user_then_cwd_then_ua_priority(self):
        request = SimpleNamespace(user="user-key", messages=[SimpleNamespace(role="user", content="<cwd>/workspace/repo</cwd>")])
        self.assertEqual(_session_key(request, "header-key", "Cursor/1.0"), "header-key")
        self.assertEqual(_session_key(request, None, "Cursor/1.0"), "user-key")
        request.user = None
        self.assertTrue(_session_key(request, None, "Cursor/1.0").startswith("cwd:"))

    def test_cwd_precedes_ua_and_ua_is_fallback(self):
        request = SimpleNamespace(messages=[SimpleNamespace(role="user", content="<cwd>/workspace/repo</cwd>")])
        self.assertEqual(_session_key(request, None, "Cursor/1.0"), cwd_bucket_key(request, 12, 64))
        request.messages[0].content = "hello"
        self.assertEqual(_session_key(request, None, "Cursor/1.0"), "ua:cursor")

    def test_blank_header_falls_through_and_invalid_cwd_uses_ua(self):
        request = SimpleNamespace(messages=[SimpleNamespace(role="user", content="<cwd>relative/path</cwd>")])
        self.assertEqual(_session_key(request, "   ", "Cursor/1.0"), "ua:cursor")

    def test_disabling_cwd_keeps_ua_and_explicit_keys(self):
        request = SimpleNamespace(user="user-key", messages=[SimpleNamespace(role="user", content="<cwd>/workspace/repo</cwd>")])
        with patch.object(config, "SESSION_SCOPING_BY_CWD", False):
            self.assertEqual(_session_key(request, None, "Cursor/1.0"), "user-key")
            request.user = None
            self.assertEqual(_session_key(request, None, "Cursor/1.0"), "ua:cursor")

    def test_disabling_ua_does_not_disable_header_user_or_cwd(self):
        with patch.object(config, "SESSION_SCOPING_BY_UA", False):
            self.assertEqual(_session_key(SimpleNamespace(user="user-key"), "header-key"), "header-key")
            request = SimpleNamespace(messages=[SimpleNamespace(role="user", content="<cwd>/workspace/repo</cwd>")])
            self.assertTrue(_session_key(request, None).startswith("cwd:"))

    def test_disabling_total_scoping_returns_default_bucket(self):
        request = SimpleNamespace(user="user-key", messages=[SimpleNamespace(role="user", content="<cwd>/workspace/repo</cwd>")])
        with patch.object(config, "SESSION_SCOPING", False):
            self.assertIsNone(_session_key(request, "header-key", "Cursor/1.0"))

    def test_explicit_reserved_prefix_does_not_collide_with_ua_bucket(self):
        request = SimpleNamespace(messages=[])
        explicit = _session_key(request, "ua:cursor", "Cursor/1.0")
        automatic = _session_key(request, None, "Cursor/1.0")
        self.assertNotEqual(explicit, automatic)
        self.assertEqual(automatic, "ua:cursor")

    def test_invalid_user_and_header_values_fall_through(self):
        request = SimpleNamespace(user=123, messages=[])
        self.assertEqual(_session_key(request, "  ", "Cursor/1.0"), "ua:cursor")
        with patch.object(config, "SESSION_SCOPING_BY_UA", False):
            self.assertIsNone(_session_key(request, "  ", "Cursor/1.0"))


if __name__ == "__main__":
    unittest.main()
