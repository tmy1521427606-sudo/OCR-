from __future__ import annotations

import time
import unittest

import demo


class PerformanceTests(unittest.TestCase):
    def test_comparison_marks_faster_total_time_and_higher_ocr_throughput(self) -> None:
        comparator = getattr(demo, "compare_performance", None)
        self.assertTrue(callable(comparator), "缺少 compare_performance")

        comparison = comparator(
            {"total_ms": 90_000, "ocr": {"success_images_per_minute": 6.0}},
            {"total_ms": 120_000, "ocr": {"success_images_per_minute": 4.0}},
        )

        self.assertEqual(comparison["total_ms_delta"], -30_000)
        self.assertEqual(comparison["total_ms_change_percent"], -25.0)
        self.assertEqual(comparison["ocr_success_images_per_minute_delta"], 2.0)

    def test_parallel_stages_finish_in_the_time_of_one_stage(self) -> None:
        runner = getattr(demo, "run_parallel_stages", None)
        self.assertTrue(callable(runner), "缺少 run_parallel_stages")

        def database() -> str:
            time.sleep(0.15)
            return "database-ready"

        def ocr() -> str:
            time.sleep(0.15)
            return "ocr-ready"

        database_result, ocr_result, timings = runner(database, ocr)

        self.assertEqual(database_result, "database-ready")
        self.assertEqual(ocr_result, "ocr-ready")
        self.assertLess(timings["parallel_wall_ms"], 230)
        self.assertGreaterEqual(timings["redshift_ms"], 140)
        self.assertGreaterEqual(timings["ocr_ms"], 140)

    def test_performance_summary_reports_latency_throughput_and_cache_hits(self) -> None:
        summary_builder = getattr(demo, "build_performance_summary", None)
        self.assertTrue(callable(summary_builder), "缺少 build_performance_summary")

        summary = summary_builder(
            {"redshift_ms": 120_000, "ocr_ms": 60_000, "parallel_wall_ms": 120_000, "qwen_ms": 30_000},
            [
                {"ok": True, "cached": False, "duration_ms": 100},
                {"ok": True, "cached": True, "duration_ms": 0},
                {"ok": True, "cached": False, "duration_ms": 600},
                {"ok": False, "cached": False, "duration_ms": 200},
            ],
            {"ocr_api_calls": 3},
            {"ocr": 3},
            total_ms=150_000,
        )

        self.assertEqual(summary["ocr"]["total_images"], 4)
        self.assertEqual(summary["ocr"]["success_images"], 3)
        self.assertEqual(summary["ocr"]["failed_images"], 1)
        self.assertEqual(summary["ocr"]["cache_hits"], 1)
        self.assertEqual(summary["ocr"]["p50_ms"], 200)
        self.assertEqual(summary["ocr"]["p95_ms"], 600)
        self.assertEqual(summary["ocr"]["success_p50_ms"], 100)
        self.assertEqual(summary["ocr"]["success_p95_ms"], 600)
        self.assertEqual(summary["ocr"]["success_images_per_minute"], 3.0)
        self.assertEqual(summary["stages_ms"]["parallel_wall_ms"], 120_000)


if __name__ == "__main__":
    unittest.main()
