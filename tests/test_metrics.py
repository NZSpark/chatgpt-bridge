import time
import unittest

from chatgpt_web.metrics import COUNTERS, LATENCIES, MetricsRegistry


class MetricsRegistryTests(unittest.TestCase):
    def test_snapshot_contains_declared_metrics(self):
        registry = MetricsRegistry()
        snapshot = registry.snapshot()

        self.assertEqual(set(snapshot["counters"]), set(COUNTERS))
        self.assertEqual(set(snapshot["latency"]), set(LATENCIES))
        self.assertEqual(snapshot["counters"]["request_total"], 0)
        self.assertEqual(snapshot["latency"]["request_latency"]["count"], 0)

    def test_counter_increment_and_latency_observation(self):
        registry = MetricsRegistry()
        self.assertEqual(registry.inc("request_total"), 1)
        self.assertEqual(registry.inc("request_total", 2), 3)
        registry.observe("request_latency", 0.25)
        registry.observe("request_latency", 0.75)

        latency = registry.snapshot()["latency"]["request_latency"]
        self.assertEqual(registry.snapshot()["counters"]["request_total"], 3)
        self.assertEqual(latency["count"], 2)
        self.assertAlmostEqual(latency["total_seconds"], 1.0)
        self.assertAlmostEqual(latency["avg_seconds"], 0.5)

    def test_timer_records_elapsed_time(self):
        registry = MetricsRegistry()
        with registry.timer("tool_execution_latency"):
            time.sleep(0.001)

        sample = registry.snapshot()["latency"]["tool_execution_latency"]
        self.assertEqual(sample["count"], 1)
        self.assertGreaterEqual(sample["total_seconds"], 0.0)

    def test_negative_observation_is_clamped(self):
        registry = MetricsRegistry()
        registry.observe("request_latency", -1)
        sample = registry.snapshot()["latency"]["request_latency"]
        self.assertEqual(sample["total_seconds"], 0.0)
        self.assertEqual(sample["avg_seconds"], 0.0)

    def test_reset_clears_samples(self):
        registry = MetricsRegistry()
        registry.inc("tool_call_total", 4)
        registry.observe("tool_execution_latency", 0.2)
        registry.reset()
        snapshot = registry.snapshot()

        self.assertEqual(snapshot["counters"]["tool_call_total"], 0)
        self.assertEqual(snapshot["latency"]["tool_execution_latency"]["count"], 0)
        self.assertEqual(snapshot["latency"]["tool_execution_latency"]["total_seconds"], 0.0)

    def test_unknown_metric_names_are_rejected(self):
        registry = MetricsRegistry()
        with self.assertRaises(ValueError):
            registry.inc("not_a_counter")
        with self.assertRaises(ValueError):
            registry.observe("not_a_latency", 1.0)
        with self.assertRaises(ValueError):
            registry.timer("not_a_latency")
