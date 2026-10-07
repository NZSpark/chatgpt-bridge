"""PI-018 session state invariants."""

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from chatgpt_web import config
from chatgpt_web.driver import ChatGPTWebDriver


class SessionStateInvariantTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bridge-invariants-"))
        self.state_file = self.tmp / ".chatgpt_state"
        self.patch = mock.patch.object(config, "SESSION_FILE", self.state_file)
        self.patch.start()
        self.driver = ChatGPTWebDriver(user_data_dir=str(self.tmp / "profile"))

    def tearDown(self):
        self.patch.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_different_sessions_do_not_share_history_or_caps(self):
        a = self.driver._state("a")
        b = self.driver._state("b")
        a.has_history = True
        a.turns = 4
        a.est_tokens = 100
        a.cap_hit = True
        self.assertFalse(b.has_history)
        self.assertEqual(b.turns, 0)
        self.assertEqual(b.est_tokens, 0)
        self.assertFalse(b.cap_hit)

    def test_same_session_state_survives_repeated_lookup(self):
        state = self.driver._state("same")
        state.has_history = True
        state.turns = 7
        state.est_tokens = 321
        self.assertIs(self.driver._state("same"), state)
        self.assertTrue(self.driver._state("same").has_history)
        self.assertEqual(self.driver._state("same").turns, 7)
        self.assertEqual(self.driver._state("same").est_tokens, 321)

    def test_reset_marks_rotation_without_removing_session_target(self):
        state = self.driver._state("rotate")
        state.has_history = True
        state.turns = 9
        state.cap_hit = True
        self.driver.reset_session("rotate")
        rotated = self.driver._state("rotate")
        self.assertIn("rotate", self.driver.session_keys())
        self.assertTrue(rotated.pending_rotation)
        self.assertFalse(rotated.has_history)
        self.assertFalse(rotated.cap_hit)

    def test_cap_hit_and_budget_boundaries_are_deterministic(self):
        with mock.patch.object(config, "SESSION_MAX_TURNS", 3), mock.patch.object(
            config, "SESSION_MAX_TOKENS", 100
        ):
            state = self.driver._state("budget")
            state.turns = 2
            state.est_tokens = 99
            self.assertFalse(self.driver._session_over_budget("budget"))
            state.turns = 3
            self.assertTrue(self.driver._session_over_budget("budget"))
            state.turns = 0
            state.est_tokens = 100
            self.assertTrue(self.driver._session_over_budget("budget"))


if __name__ == "__main__":
    unittest.main()
