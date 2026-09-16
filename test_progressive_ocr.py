from __future__ import annotations

import json
import tempfile
import time
import urllib.error
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import demo


class ProgressiveOcrTests(unittest.TestCase):
    def test_fast_network_failure_retries_once_within_image_time_budget(self) -> None:
        retry_configs: list[dict[str, object]] = []

        def fake_ocr(
            run_id: str,
            product: dict[str, object],
            image: dict[str, str],
            config: dict[str, object],
            store: object,
            meter: object,
            on_first_retry: object = None,
        ) -> dict[str, object]:
            retry_configs.append(config)
            return {"ok": True, "image_name": image["name"], "markdown": "规格：60粒/瓶"}

        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            store = demo.StateStore(base / "state")
            try:
                with patch.object(demo, "run_ocr_one", fake_ocr):
                    demo.run_ocr_stage(
                        "run-1",
                        {"products": [{"platform": "jd", "product_id": "p1", "images": [
                            {"name": "01.jpg", "path": "01.jpg", "sha256": "source"}
                        ]}]},
                        {"ocr_workers": 1},
                        store,
                        demo.ConcurrencyMeter(),
                    )
            finally:
                store.close()

        self.assertEqual(retry_configs[0]["ocr_max_attempts"], 2)
        self.assertEqual(retry_configs[0]["ocr_retry_delays"], (2.0,))

    def test_outage_writes_partial_report_and_performance_summary(self) -> None:
        def fake_ocr(
            run_id: str,
            product: dict[str, object],
            image: dict[str, str],
            config: dict[str, object],
            store: object,
            meter: object,
            on_first_retry: object = None,
        ) -> dict[str, object]:
            if product["product_id"] == "p1":
                return {
                    "ok": True, "image_name": image["name"], "image_path": image["path"],
                    "sha256": image["sha256"], "markdown": "规格：60粒/瓶", "cached": False,
                    "duration_ms": 10, "attempts": 1, "raw_json_ref": None,
                    "markdown_ref": None, "log_id": "ok", "error": None,
                }
            return {
                "ok": False, "image_name": image["name"], "image_path": image["path"],
                "sha256": image["sha256"], "markdown": None, "cached": False,
                "duration_ms": 10, "attempts": 1, "raw_json_ref": None,
                "markdown_ref": None, "log_id": None,
                "error": {"code": "VLLM_NETWORK_ERROR", "message": "connection refused"},
            }

        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            selected = []
            for product_id, image_count in (("p1", 1), ("p2", 5)):
                folder = base / "batch" / product_id
                folder.mkdir(parents=True)
                for index in range(image_count):
                    (folder / f"{index + 1:02}.jpg").write_bytes(b"image")
                selected.append(folder)
            manifest = demo.build_manifest(base / "batch", selected, "jd")
            with patch.object(demo, "run_ocr_one", fake_ocr):
                with self.assertRaises(demo.DemoError) as raised:
                    demo.execute_pipeline(
                        manifest,
                        Path(demo.__file__).with_name("template-v2.json"),
                        base / "state",
                        base / "runs",
                        demo.mock_config(),
                        force_new=True,
                        skip_workbook=True,
                    )

            run_dir = next((base / "runs").iterdir())
            performance = json.loads((run_dir / "performance.json").read_text(encoding="utf-8"))
            report = (run_dir / "report.html").read_text(encoding="utf-8")

        self.assertEqual(raised.exception.code, "VLLM_NETWORK_OUTAGE")
        self.assertEqual(performance["ocr"]["planned_images"], 6)
        self.assertEqual(performance["ocr"]["completed_images"], 4)
        self.assertEqual(performance["ocr"]["unstarted_images"], 2)
        self.assertIn("p1", report)
        self.assertIn("计划 / 已完成 / 未开始", report)

    def test_network_failure_outside_fast_retry_window_is_not_retried(self) -> None:
        calls: list[int] = []

        def fake_post(*_args: object, **_kwargs: object) -> dict[str, object]:
            calls.append(1)
            raise demo.DemoError("VLLM_NETWORK_ERROR", "timed out", retryable=True)

        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = base / "01.jpg"
            demo.Image.new("RGB", (100, 100), "white").save(source)
            store = demo.StateStore(base / "state")
            config = demo.mock_config()
            config.update({
                "mock": False,
                "ocr_max_attempts": 2,
                "ocr_retry_delays": (0.0,),
                "ocr_fast_network_failure_seconds": 0.0,
            })
            try:
                with patch.object(demo, "post_json_without_proxy", fake_post):
                    result = demo.run_ocr_one(
                        "run-1",
                        {"platform": "jd", "product_id": "p1"},
                        {"name": "01.jpg", "path": str(source), "sha256": "source"},
                        config,
                        store,
                        demo.ConcurrencyMeter(),
                    )
            finally:
                store.close()

        self.assertFalse(result["ok"])
        self.assertEqual(len(calls), 1)

    def test_non_network_ocr_error_is_not_retried(self) -> None:
        calls: list[int] = []
        response = {"choices": [{"message": {"content": '{"blocks": []}'}}]}

        def fake_post(*_args: object, **_kwargs: object) -> dict[str, object]:
            calls.append(1)
            return response

        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = base / "01.jpg"
            demo.Image.new("RGB", (100, 100), "white").save(source)
            store = demo.StateStore(base / "state")
            config = demo.mock_config()
            config.update({"mock": False, "ocr_max_attempts": 2, "ocr_retry_delays": (0.0,)})
            try:
                with patch.object(demo, "post_json_without_proxy", fake_post):
                    result = demo.run_ocr_one(
                        "run-1",
                        {"platform": "jd", "product_id": "p1"},
                        {"name": "01.jpg", "path": str(source), "sha256": "source"},
                        config,
                        store,
                        demo.ConcurrencyMeter(),
                    )
            finally:
                store.close()

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "EMPTY_VLLM_OCR_BLOCKS")
        self.assertEqual(len(calls), 1)

    def test_timeout_is_marked_for_review_once_without_background_retry(self) -> None:
        manifest = {
            "products": [
                {
                    "platform": "jd",
                    "product_id": "p1",
                    "images": [
                        {"name": "01.jpg", "path": "01.jpg", "sha256": "fast"},
                        {"name": "02.jpg", "path": "02.jpg", "sha256": "slow"},
                    ],
                }
            ]
        }
        attempted: list[str] = []
        timeouts: list[object] = []

        def fake_ocr(
            run_id: str,
            product: dict[str, object],
            image: dict[str, str],
            config: dict[str, object],
            store: object,
            meter: object,
            on_first_retry: object = None,
        ) -> dict[str, object]:
            attempted.append(image["name"])
            timeouts.append(config.get("ocr_request_timeout"))
            if image["name"] == "02.jpg":
                return {
                    "ok": False,
                    "image_name": "02.jpg",
                    "error": {"code": "VLLM_NETWORK_ERROR", "message": "timed out"},
                }
            return {"ok": True, "image_name": "01.jpg", "markdown": "已识别文字"}

        with tempfile.TemporaryDirectory() as temporary:
            store = demo.StateStore(Path(temporary) / "state")
            try:
                with patch.object(demo, "run_ocr_one", fake_ocr):
                    output = demo.run_ocr_stage(
                        "run-1", manifest, {"ocr_workers": 1}, store, demo.ConcurrencyMeter()
                    )
            finally:
                store.close()

        result = output[demo.identity_key("jd", "p1")]
        self.assertEqual(attempted, ["01.jpg", "02.jpg"])
        self.assertEqual(timeouts, [30, 30])
        self.assertEqual(result[1]["error"]["code"], "OCR_TIMEOUT_REVIEW")

    def test_assemble_product_marks_conflicting_daily_dosage_and_competitor_note_for_review(self) -> None:
        product = {"platform": "jd", "product_id": "p1", "images": []}
        record = next(iter(demo.mock_database_results({"products": [product]}).values()))
        document = demo.assemble_product(
            product,
            record,
            "mock-db.json",
            [],
            {
                "ok": True,
                "data": {
                    "guige": "60粒/瓶",
                    "guige_zong_liang": 60,
                    "ri_fu_liang": "每日1粒、每日3粒",
                    "min_ri_fu_liang": 1,
                    "max_ri_fu_liang": 3,
                    "notes": "图片04内容属竞品，请勿与本商品混淆",
                },
                "raw_json_ref": "mock-extract.json",
            },
            demo.skipped_search("TEST", "test", []),
            demo.load_json(Path(demo.__file__).with_name("template-v2.json")),
        )

        codes = {item["code"] for item in document["validation_issues"]}
        self.assertEqual(document["status"], "review")
        self.assertTrue({"DOSAGE_CONFLICT", "IMAGE_PRODUCT_MISMATCH"}.issubset(codes))

    def test_vllm_preflight_reports_unavailable_service_without_starting_ocr(self) -> None:
        opener = MagicMock()
        opener.open.side_effect = urllib.error.URLError("timed out")

        with patch.object(demo.urllib.request, "build_opener", return_value=opener):
            with self.assertRaises(demo.DemoError) as raised:
                demo.verify_vllm_available("http://192.168.1.115:8801/v1", "EMPTY")

        self.assertEqual(raised.exception.code, "VLLM_UNAVAILABLE")

    def test_ocr_stage_stops_after_three_consecutive_network_failures(self) -> None:
        manifest = {
            "products": [
                {
                    "platform": "jd",
                    "product_id": "p1",
                    "images": [
                        {"name": f"{index:02d}.jpg", "path": f"{index:02d}.jpg", "sha256": str(index)}
                        for index in range(1, 6)
                    ],
                }
            ]
        }
        attempted: list[str] = []

        def fake_ocr(
            run_id: str,
            product: dict[str, object],
            image: dict[str, str],
            config: dict[str, object],
            store: object,
            meter: object,
            on_first_retry: object = None,
        ) -> dict[str, object]:
            attempted.append(image["name"])
            return {
                "ok": False,
                "image_name": image["name"],
                "error": {"code": "VLLM_NETWORK_ERROR", "message": "timeout"},
            }

        with tempfile.TemporaryDirectory() as temporary:
            store = demo.StateStore(Path(temporary) / "state")
            try:
                with patch.object(demo, "run_ocr_one", fake_ocr):
                    with self.assertRaises(demo.DemoError) as raised:
                        demo.run_ocr_stage(
                            "run-1", manifest, {}, store, demo.ConcurrencyMeter()
                        )
            finally:
                store.close()

        self.assertEqual(raised.exception.code, "VLLM_NETWORK_OUTAGE")
        self.assertEqual(len(attempted), 3)

    def test_completed_product_is_exported_while_another_product_is_still_in_ocr(self) -> None:
        live_report_seen_while_second_product_runs: list[bool] = []
        completed_product_live_reports: list[str] = []

        def fake_ocr(
            run_id: str,
            product: dict[str, object],
            image: dict[str, str],
            config: dict[str, object],
            store: object,
            meter: object,
            on_first_retry: object = None,
        ) -> dict[str, object]:
            if product["product_id"] == "p2":
                deadline = time.monotonic() + 1.0
                while time.monotonic() < deadline:
                    reports = list(Path(config["output_probe"]).rglob("实时结果.html"))
                    if reports and "p1" in reports[0].read_text(encoding="utf-8"):
                        live_report_seen_while_second_product_runs.append(True)
                        completed_product_live_reports.append(
                            reports[0].read_text(encoding="utf-8")
                        )
                        break
                    time.sleep(0.01)
                else:
                    live_report_seen_while_second_product_runs.append(False)
            return {
                "ok": True,
                "image_name": image["name"],
                "image_path": image["path"],
                "sha256": image["sha256"],
                "markdown": "规格：60粒/瓶",
                "cached": False,
                "duration_ms": 1,
                "attempts": 1,
                "raw_json_ref": None,
                "markdown_ref": None,
                "log_id": "test-success",
                "error": None,
            }

        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            products = []
            for product_id in ("p1", "p2"):
                product_dir = base / "batch" / product_id
                product_dir.mkdir(parents=True)
                (product_dir / "01.jpg").write_bytes(product_id.encode())
                products.append(product_dir)
            manifest = demo.build_manifest(base / "batch", products, "jd")
            config = demo.mock_config()
            config["output_probe"] = str(base / "runs")
            with patch.object(demo, "run_ocr_one", fake_ocr):
                demo.execute_pipeline(
                    manifest,
                    Path(demo.__file__).with_name("template-v2.json"),
                    base / "state",
                    base / "runs",
                    config,
                    force_new=True,
                    skip_workbook=True,
                )

        self.assertEqual(live_report_seen_while_second_product_runs, [True])
        self.assertNotIn("OCR_RETRYING", completed_product_live_reports[0])

    def test_pipeline_writes_timeout_review_to_live_report(self) -> None:

        def fake_ocr(
            run_id: str,
            product: dict[str, object],
            image: dict[str, str],
            config: dict[str, object],
            store: object,
            meter: object,
            on_first_retry: object = None,
        ) -> dict[str, object]:
            if image["name"] == "01.jpg":
                return {
                    "ok": True,
                    "image_name": "01.jpg",
                    "image_path": "01.jpg",
                    "sha256": "first",
                    "markdown": "规格：60粒/瓶",
                    "cached": False,
                    "duration_ms": 1,
                    "attempts": 1,
                    "raw_json_ref": None,
                    "markdown_ref": None,
                    "log_id": "test-1",
                    "error": None,
                }
            return {
                "ok": False,
                "image_name": "02.jpg",
                "image_path": "02.jpg",
                "sha256": "second",
                "markdown": None,
                "cached": False,
                "duration_ms": 1,
                "attempts": 1,
                "raw_json_ref": None,
                "markdown_ref": None,
                "log_id": None,
                "error": {"code": "VLLM_NETWORK_ERROR", "message": "timed out"},
            }

        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            product_dir = base / "batch" / "p1"
            product_dir.mkdir(parents=True)
            (product_dir / "01.jpg").write_bytes(b"first")
            (product_dir / "02.jpg").write_bytes(b"second")
            manifest = demo.build_manifest(base / "batch", [product_dir], "jd")
            config = demo.mock_config()
            config["output_probe"] = str(base / "runs")
            with patch.object(demo, "run_ocr_one", fake_ocr):
                result = demo.execute_pipeline(
                    manifest,
                    Path(demo.__file__).with_name("template-v2.json"),
                    base / "state",
                    base / "runs",
                    config,
                    force_new=True,
                    skip_workbook=True,
                )

            live_report = Path(result["output_dir"]) / "实时结果.html"
            self.assertIn("OCR_TIMEOUT_REVIEW", live_report.read_text(encoding="utf-8"))
            self.assertIn("p1", live_report.read_text(encoding="utf-8"))

if __name__ == "__main__":
    unittest.main()
