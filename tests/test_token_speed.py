"""``chatgpt_web.token_speed`` 的纯函数测试（对照 doc/tokenspeed.md §3、§5）。

用虚拟时钟穷举，断言「算了多少秒、得出多少 tok/s」，不依赖真实耗时。
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from chatgpt_web.prompting import estimate_tokens  # noqa: E402
from chatgpt_web.token_speed import (  # noqa: E402
    LAST_FIELDS,
    SUM_FIELDS,
    TokenSpeed,
    augment_e2e,
    compute_speed,
)


class ComputeSpeedTests(unittest.TestCase):
    def test_no_first_text_returns_none(self) -> None:
        # 没有观测到首段新回复时无法给出速度：宁可没有样本，也不编。
        self.assertIsNone(
            compute_speed(
                "hi",
                "",
                submitted_at=0.0,
                first_text_at=None,
                finished_at=1.0,
                poll_interval_s=0.4,
            )
        )

    def test_basic_durations_and_rates(self) -> None:
        prompt = "p" * 300
        reply = "r" * 90
        speed = compute_speed(
            prompt,
            reply,
            submitted_at=10.0,
            first_text_at=13.0,
            finished_at=21.0,
            poll_interval_s=0.4,
        )
        assert speed is not None
        # 原始时长：TTFT = t5 - t3 = 3.0；decode = t6 - t5 = 8.0；total = 11.0
        self.assertAlmostEqual(speed.ttft_s, 3.0)
        self.assertAlmostEqual(speed.decode_s, 8.0)
        self.assertAlmostEqual(speed.total_s, 11.0)
        # token 估算走 estimate_tokens，与 prompting 同源。
        self.assertEqual(speed.prompt_tokens, estimate_tokens(prompt))
        self.assertEqual(speed.reply_tokens, estimate_tokens(reply))
        self.assertEqual(speed.prompt_chars, 300)
        self.assertEqual(speed.reply_chars, 90)
        # 未观测到生成中：不扣落定窗口。
        self.assertEqual(speed.settle_credit_s, 0.0)
        # poll_credit = 0.4 / 2 = 0.2；TTFT_adj = 3.0 - 0.2 = 2.8
        self.assertAlmostEqual(speed.poll_credit_s, 0.2)
        self.assertAlmostEqual(speed.ttft_adj_s, 2.8)
        self.assertAlmostEqual(speed.decode_adj_s, 8.0)
        self.assertAlmostEqual(speed.prefill_tps, speed.prompt_tokens / 2.8)
        self.assertAlmostEqual(speed.decode_tps, speed.reply_tokens / 8.0)

    def test_settle_credit_only_applied_when_generating_seen(self) -> None:
        common = dict(
            submitted_at=0.0,
            first_text_at=3.0,
            finished_at=11.0,
            poll_interval_s=0.4,
            settle_credit_s=2.5,
        )
        seen = compute_speed("p" * 60, "r" * 60, saw_generating=True, **common)
        unseen = compute_speed("p" * 60, "r" * 60, saw_generating=False, **common)
        assert seen is not None and unseen is not None
        # 只有「确实观测到生成中」才扣 SETTLE_S，否则没有依据。
        self.assertAlmostEqual(seen.settle_credit_s, 2.5)
        self.assertAlmostEqual(seen.decode_adj_s, 8.0 - 2.5)
        self.assertEqual(unseen.settle_credit_s, 0.0)
        self.assertAlmostEqual(unseen.decode_adj_s, 8.0)
        self.assertGreater(seen.decode_tps, unseen.decode_tps)

    def test_epsilon_floor_avoids_division_by_zero(self) -> None:
        # t5 与 t3 同刻：TTFT 修正后会落到 _EPSILON_S，不应抛 ZeroDivisionError。
        speed = compute_speed(
            "p" * 30,
            "r" * 30,
            submitted_at=5.0,
            first_text_at=5.0,
            finished_at=5.0,
            poll_interval_s=0.0,
        )
        assert speed is not None
        self.assertGreater(speed.prefill_tps, 0.0)
        self.assertGreater(speed.decode_tps, 0.0)

    def test_negative_gaps_are_clamped_to_zero(self) -> None:
        # 防御时钟乱序：first_text_at 早于 submitted_at。
        speed = compute_speed(
            "p" * 30,
            "r" * 30,
            submitted_at=10.0,
            first_text_at=9.0,
            finished_at=12.0,
            poll_interval_s=0.4,
        )
        assert speed is not None
        self.assertEqual(speed.ttft_s, 0.0)
        self.assertGreaterEqual(speed.total_s, 0.0)

    def test_empty_prompt_and_reply_yield_zero_tokens(self) -> None:
        speed = compute_speed(
            "",
            "",
            submitted_at=0.0,
            first_text_at=1.0,
            finished_at=2.0,
            poll_interval_s=0.4,
        )
        assert speed is not None
        self.assertEqual(speed.prompt_tokens, 0)
        self.assertEqual(speed.reply_tokens, 0)
        self.assertEqual(speed.prefill_tps, 0.0)
        self.assertEqual(speed.decode_tps, 0.0)

    def test_short_sample_flag_threshold(self) -> None:
        short = compute_speed(
            "p" * 30,
            "r" * 30,  # 10 tok < 50
            submitted_at=0.0,
            first_text_at=1.0,
            finished_at=2.0,
            poll_interval_s=0.4,
        )
        long = compute_speed(
            "p" * 30,
            "r" * 600,  # 200 tok
            submitted_at=0.0,
            first_text_at=1.0,
            finished_at=2.0,
            poll_interval_s=0.4,
        )
        assert short is not None and long is not None
        self.assertTrue(short.short_sample)
        self.assertFalse(long.short_sample)

    def test_none_arguments_are_tolerated(self) -> None:
        speed = compute_speed(
            None,  # type: ignore[arg-type]
            None,  # type: ignore[arg-type]
            submitted_at=0.0,
            first_text_at=1.0,
            finished_at=2.0,
            poll_interval_s=0.4,
        )
        assert speed is not None
        self.assertEqual(speed.prompt_chars, 0)
        self.assertEqual(speed.reply_chars, 0)

    def test_frozen_dataclass(self) -> None:
        speed = compute_speed(
            "p" * 30,
            "r" * 30,
            submitted_at=0.0,
            first_text_at=1.0,
            finished_at=2.0,
            poll_interval_s=0.4,
        )
        assert speed is not None
        self.assertIsInstance(speed, TokenSpeed)
        with self.assertRaises(Exception):
            speed.prefill_tps = 1.0  # type: ignore[misc]

    def test_non_streaming_has_no_e2e_fields(self) -> None:
        speed = compute_speed(
            "p" * 30,
            "r" * 90,
            submitted_at=0.0,
            first_text_at=1.0,
            finished_at=3.0,
            poll_interval_s=0.4,
        )
        assert speed is not None
        self.assertFalse(speed.streamed)
        self.assertIsNone(speed.e2e_ttft_s)
        self.assertIsNone(speed.e2e_decode_tps)
        self.assertIsNone(speed.bridge_overhead_ratio)

    def test_compute_speed_e2e_when_streaming(self) -> None:
        speed = compute_speed(
            "p" * 30,
            "r" * 300,  # 100 tok
            submitted_at=5.0,
            first_text_at=8.0,
            finished_at=16.0,
            poll_interval_s=0.4,
            streamed=True,
            request_started_at=4.5,
            first_sse_at=8.2,
            done_at=16.5,
        )
        assert speed is not None
        # e2e_ttft = first_sse - t0 = 8.2 - 4.5 = 3.7
        self.assertAlmostEqual(speed.e2e_ttft_s, 3.7)
        # e2e_decode = reply_tokens / (16.5 - 8.2)
        self.assertAlmostEqual(speed.e2e_decode_tps, estimate_tokens("r" * 300) / 8.3)
        # overhead = 1 - (ttft_adj + decode_adj) / (done - t0)
        ttft_adj = 3.0 - 0.2
        decode_adj = 8.0
        expected = 1 - (ttft_adj + decode_adj) / (16.5 - 4.5)
        self.assertAlmostEqual(speed.bridge_overhead_ratio, expected)

    def test_overhead_clamped_to_unit_interval(self) -> None:
        # 模型窗口远大于端到端总时长时，占比被夹到 [0, 1]。
        speed = compute_speed(
            "p" * 30,
            "r" * 30,
            submitted_at=0.0,
            first_text_at=1.0,
            finished_at=100.0,
            poll_interval_s=0.0,
            request_started_at=0.0,
            first_sse_at=1.0,
            done_at=1.5,
        )
        assert speed is not None
        assert speed.bridge_overhead_ratio is not None
        self.assertGreaterEqual(speed.bridge_overhead_ratio, 0.0)
        self.assertLessEqual(speed.bridge_overhead_ratio, 1.0)

    def test_augment_e2e_fills_streaming_fields(self) -> None:
        base = compute_speed(
            "p" * 30,
            "r" * 300,
            submitted_at=5.0,
            first_text_at=8.0,
            finished_at=16.0,
            poll_interval_s=0.4,
            streamed=False,
        )
        assert base is not None
        self.assertIsNone(base.e2e_ttft_s)

        augmented = augment_e2e(
            base,
            request_started_at=4.5,
            first_sse_at=8.2,
            done_at=16.5,
        )
        self.assertTrue(augmented.streamed)
        self.assertAlmostEqual(augmented.e2e_ttft_s, 3.7)
        self.assertIsNotNone(augmented.e2e_decode_tps)
        self.assertIsNotNone(augmented.bridge_overhead_ratio)
        # 基础观测速度不受影响。
        self.assertEqual(augmented.prefill_tps, base.prefill_tps)
        self.assertEqual(augmented.decode_tps, base.decode_tps)

    def test_augment_e2e_without_sse_timestamps(self) -> None:
        base = compute_speed(
            "p" * 30,
            "r" * 30,
            submitted_at=0.0,
            first_text_at=1.0,
            finished_at=2.0,
            poll_interval_s=0.4,
        )
        assert base is not None
        augmented = augment_e2e(
            base, request_started_at=0.0, first_sse_at=None, done_at=None
        )
        self.assertTrue(augmented.streamed)
        self.assertIsNone(augmented.e2e_ttft_s)
        self.assertIsNone(augmented.e2e_decode_tps)
        self.assertIsNone(augmented.bridge_overhead_ratio)

    def test_field_maps_cover_every_last_attribute(self) -> None:
        # 防漂移：LAST_FIELDS / SUM_FIELDS 的属性名必须都是 TokenSpeed 真字段。
        valid = set(TokenSpeed.__dataclass_fields__)
        for _field, attr in LAST_FIELDS:
            self.assertIn(attr, valid)
        for _field, attr in SUM_FIELDS:
            self.assertIn(attr, valid)


if __name__ == "__main__":
    unittest.main()
