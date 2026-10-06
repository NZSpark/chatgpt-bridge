"""结束判定状态机与 `_complete_text` 的快速路径（doc/tasks.md T4.2）。

`tests/test_end_detection.py` 用假 page 端到端验证「真实踩过的坑」；这里补的是
**穷举式**的纯函数验证：不跑浏览器，直接把各种观测喂给状态机，断言它给的动作。
两边互补——前者证明集成行为，后者证明每个分支都被明确定义（而不是碰巧）。
"""

import asyncio
import unittest

from chatgpt_web.driver import ChatGPTWebDriver
from chatgpt_web.end_detection import EndLimits, EndState, evaluate_poll

LIMITS = EndLimits(quiet_polls=3, stable_polls=2, stall_limit=4, extend_step_s=180.0)


def _state(**kw) -> EndState:
    return EndState(**kw)


class FinishDecisionTests(unittest.TestCase):
    def test_generating_never_finishes(self) -> None:
        verdict = evaluate_poll(
            _state(saw_generating=True, quiet_count=99),
            limits=LIMITS,
            reply_seen=True,
            generating=True,
            normalized="正在输出",
            last_normalized="正在输出",
            last_len=4,
        )
        self.assertEqual(verdict.action, "continue")

    def test_stop_button_gone_but_content_still_changing_keeps_waiting(self) -> None:
        """停止按钮消失 ≠ 结束：内容还在变时必须继续等（分段输出的前半段）。"""
        state = _state(saw_generating=True, quiet_count=2)
        verdict = evaluate_poll(
            state,
            limits=LIMITS,
            reply_seen=True,
            generating=False,
            normalized="正文…",
            last_normalized="正文",
            last_len=2,
        )
        self.assertEqual(verdict.action, "continue")
        self.assertEqual(verdict.state.quiet_count, 0, "内容变化必须清零静默计数")

    def test_segment_resume_resets_quiet_window(self) -> None:
        """分段输出恢复（内容重新增长）时，即使已经静默好几轮也不能收尾。"""
        state = _state(saw_generating=True, quiet_count=2)
        verdict = evaluate_poll(
            state,
            limits=LIMITS,
            reply_seen=True,
            generating=False,
            normalized="正文 + 新段落",
            last_normalized="正文",
            last_len=2,
        )
        self.assertEqual(verdict.action, "continue")
        self.assertEqual(verdict.state.quiet_count, 0)

    def test_quiet_window_satisfied_finishes(self) -> None:
        state = _state(saw_generating=True, quiet_count=2)
        verdict = evaluate_poll(
            state,
            limits=LIMITS,
            reply_seen=True,
            generating=False,
            normalized="最终答复",
            last_normalized="最终答复",
            last_len=4,
        )
        self.assertEqual(verdict.action, "finish")
        self.assertEqual(verdict.state.quiet_count, 3)

    def test_pending_tokens_block_finish(self) -> None:
        state = _state(saw_generating=True, quiet_count=99)
        verdict = evaluate_poll(
            state,
            limits=LIMITS,
            reply_seen=True,
            generating=False,
            pending=True,
            normalized="半截 TOOL_CALL",
            last_normalized="半截 TOOL_CALL",
            last_len=9,
        )
        self.assertEqual(verdict.action, "continue")

    def test_never_saw_generating_falls_back_to_stable_text(self) -> None:
        state = _state(saw_generating=False, stable_count=2)
        verdict = evaluate_poll(
            state,
            limits=LIMITS,
            reply_seen=True,
            generating=False,
            normalized="固定文本",
            last_normalized="固定文本",
            last_len=4,
        )
        threshold = max(LIMITS.stable_polls, LIMITS.quiet_polls)
        self.assertEqual(verdict.action, "finish")
        self.assertEqual(verdict.state.stable_count, threshold)

    def test_stable_fallback_requires_quiet_window_too(self) -> None:
        """兜底路径同样要等 max(STABLE_POLLS, RESUME_QUIET_POLLS)，比旧逻辑保守。"""
        state = _state(saw_generating=False, stable_count=0)
        verdict = evaluate_poll(
            state,
            limits=LIMITS,
            reply_seen=True,
            generating=False,
            normalized="固定文本",
            last_normalized="固定文本",
            last_len=4,
        )
        self.assertEqual(verdict.action, "continue")
        self.assertEqual(verdict.state.stable_count, 1)


class FailureDecisionTests(unittest.TestCase):
    def test_stall_fails_fast(self) -> None:
        state = _state(stalled=3)
        verdict = evaluate_poll(
            state,
            limits=LIMITS,
            reply_seen=False,
            generating=False,
            normalized="",
        )
        self.assertEqual(verdict.action, "fail_stalled")
        self.assertEqual(verdict.state.stalled, 4)

    def test_generating_resets_stall_counter(self) -> None:
        state = _state(stalled=3)
        verdict = evaluate_poll(
            state,
            limits=LIMITS,
            reply_seen=False,
            generating=True,
            normalized="",
        )
        self.assertEqual(verdict.action, "continue")
        self.assertEqual(verdict.state.stalled, 0)

    def test_deadline_extends_while_generating(self) -> None:
        verdict = evaluate_poll(
            _state(saw_generating=True),
            limits=LIMITS,
            reply_seen=False,
            generating=True,
            past_deadline=True,
            extend_budget=300.0,
        )
        self.assertEqual(verdict.action, "extend")
        self.assertEqual(verdict.extend_seconds, 180.0)

    def test_deadline_extension_uses_leftover_budget(self) -> None:
        verdict = evaluate_poll(
            _state(stalled=0),
            limits=LIMITS,
            reply_seen=False,
            generating=True,
            past_deadline=True,
            extend_budget=30.0,
        )
        self.assertEqual(verdict.action, "extend")
        self.assertEqual(verdict.extend_seconds, 30.0)

    def test_deadline_fails_when_budget_exhausted(self) -> None:
        verdict = evaluate_poll(
            _state(stalled=0),
            limits=LIMITS,
            reply_seen=False,
            generating=True,
            past_deadline=True,
            extend_budget=0.0,
        )
        self.assertEqual(verdict.action, "fail_timeout")

    def test_deadline_returns_partial_content_instead_of_resending(self) -> None:
        verdict = evaluate_poll(
            _state(stalled=0, saw_generating=True),
            limits=LIMITS,
            reply_seen=False,
            generating=True,
            past_deadline=True,
            extend_budget=300.0,
            has_partial_text=True,
        )
        self.assertEqual(verdict.action, "finish")
        self.assertIn("不重发", verdict.note)

    def test_deadline_not_reached_keeps_waiting(self) -> None:
        verdict = evaluate_poll(
            _state(stalled=0),
            limits=LIMITS,
            reply_seen=False,
            generating=False,
            past_deadline=False,
        )
        self.assertEqual(verdict.action, "continue")

    def test_state_is_not_mutated_in_place(self) -> None:
        state = _state(stalled=0, quiet_count=0)
        evaluate_poll(state, limits=LIMITS, reply_seen=False, generating=False)
        self.assertEqual(state.stalled, 0, "状态机不应就地修改入参（回写由调用方负责）")


class CompleteTextFastPathTests(unittest.TestCase):
    class _Node:
        def __init__(self, text: str, animated: bool) -> None:
            self.text = text
            self.animated = animated
            self.scripts: list = []
            self.inner_text_calls = 0

        async def evaluate(self, script):
            self.scripts.append(script)
            if "revealing" in script:          # 动画探测
                return self.animated
            if "querySelector('.pending" in script:   # pending 探测
                return self.animated
            return self.text                   # 克隆路径的 JS

        async def inner_text(self):
            self.inner_text_calls += 1
            return self.text

        async def text_content(self):
            return self.text

    def setUp(self) -> None:
        self.driver = ChatGPTWebDriver(user_data_dir="/tmp/chatgpt-test-noprofile")

    def test_no_clone_when_no_animation(self) -> None:
        node = self._Node("完整文本", animated=False)
        text = asyncio.run(self.driver._complete_text(node))
        self.assertEqual(text, "完整文本")
        self.assertEqual(node.inner_text_calls, 1, "无动画时应直接读 inner_text")
        self.assertFalse(
            any("cloneNode" in s for s in node.scripts),
            "无动画时不应触发 DOM 克隆",
        )

    def test_clone_used_when_animation_present(self) -> None:
        node = self._Node("逐步显现的完整文本", animated=True)
        text = asyncio.run(self.driver._complete_text(node))
        self.assertEqual(text, "逐步显现的完整文本")
        self.assertTrue(
            any("cloneNode" in s for s in node.scripts),
            "存在动画 token 时必须走克隆路径（否则会读到半截文本）",
        )

    def test_probe_failure_falls_back_to_clone(self) -> None:
        class _Broken(self._Node):
            async def evaluate(self, script):
                self.scripts.append(script)
                if "revealing" in script:
                    raise RuntimeError("探测失败")
                if "querySelector('.pending" in script:
                    raise RuntimeError("探测失败")
                return self.text

        node = _Broken("文本", animated=True)
        text = asyncio.run(self.driver._complete_text(node))
        self.assertEqual(text, "文本")
        self.assertTrue(any("cloneNode" in s for s in node.scripts))

    def test_none_node_returns_empty(self) -> None:
        self.assertEqual(asyncio.run(self.driver._complete_text(None)), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
