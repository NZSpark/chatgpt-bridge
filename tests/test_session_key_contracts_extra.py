"""Additional default-bucket and User-Agent fallback contract tests."""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from chatgpt_web import config
from chatgpt_web.server import _session_key


class SessionKeyFallbackContractTests(unittest.TestCase):
    def setUp(self):
        self.patcher = patch.multiple(
            config,
            SESSION_SCOPING=True,
            SESSION_SCOPING_BY_CWD=False,
            SESSION_SCOPING_BY_UA=True,
            SESSION_KEY_MAX_LEN=64,
        )
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def test_unknown_versioned_user_agent_gets_stable_fallback_bucket(self):
        request = SimpleNamespace(user=None, messages=[])
        first = _session_key(request, None, "custom-agent/2.4.1")
        second = _session_key(request, None, "custom-agent/2.4.1")
        self.assertEqual(first, "ua:custom-agent")
        self.assertEqual(first, second)

    def test_unrecognized_user_agent_without_product_version_uses_default(self):
        request = SimpleNamespace(user=None, messages=[])
        self.assertIsNone(_session_key(request, None, "Mozilla compatible browser"))

    def test_missing_identifiers_use_default_bucket(self):
        request = SimpleNamespace(messages=[])
        self.assertIsNone(_session_key(request, None, None))
        self.assertIsNone(_session_key(request, None, ""))

    def test_non_string_user_agent_is_ignored(self):
        request = SimpleNamespace(user=None, messages=[])
        self.assertIsNone(_session_key(request, None, 123))

    def test_trimmed_header_and_user_values_remain_stable(self):
        request = SimpleNamespace(user="  user-bucket  ", messages=[])
        self.assertEqual(_session_key(request, "  header-bucket  "), "header-bucket")
        self.assertEqual(_session_key(request, None), "user-bucket")

    def test_disabling_ua_fallback_returns_default_without_explicit_key(self):
        request = SimpleNamespace(user=None, messages=[])
        with patch.object(config, "SESSION_SCOPING_BY_UA", False):
            self.assertIsNone(_session_key(request, None, "Cursor/1.0"))


if __name__ == "__main__":
    unittest.main()
