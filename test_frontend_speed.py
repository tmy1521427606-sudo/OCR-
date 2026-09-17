from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

import demo


FRONTEND_PATH = Path(__file__).with_name("直接用图片测试.py")
SPEC = importlib.util.spec_from_file_location("ocr_frontend", FRONTEND_PATH)
assert SPEC and SPEC.loader
frontend = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(frontend)


class FrontendAndSpeedTests(unittest.TestCase):
    def test_ocr_worker_count_uses_default_and_rejects_unsafe_value(self) -> None:
        self.assertEqual(demo.ocr_worker_count({}), 3)
        self.assertEqual(demo.ocr_worker_count({"ocr_workers": 6}), 6)
        with self.assertRaises(demo.DemoError):
            demo.ocr_worker_count({"ocr_workers": 7})

    def test_frontend_command_passes_selected_products_and_ocr_concurrency(self) -> None:
        values = {
            "root": r"D:\商品OCR\jd\2026-09-02",
            "platform": "jd",
            "ocr_workers": "4",
        }

        command = frontend.build_demo_command(values, ["100001", "100002"], True, True, True)

        self.assertEqual(command[1], str(frontend.APP_DIR / "demo.py"))
        self.assertEqual(command[2:], [
            "--root", r"D:\商品OCR\jd\2026-09-02",
            "--products", "100001", "100002",
            "--platform", "jd",
            "--ocr-workers", "4",
            "--open", "--bulk-first-pass", "--new-run", "--force-ocr",
        ])

    def test_ocr_service_test_reports_current_endpoint_when_reachable(self) -> None:
        called: list[tuple[str, float]] = []

        def reachable(url: str, timeout: float) -> None:
            called.append((url, timeout))

        result = frontend.test_ocr_service_url(
            "http://192.168.1.115:8870/v1/ocr", verifier=reachable
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["url"], "http://192.168.1.115:8870/v1/ocr")
        self.assertIn("192.168.1.115:8870", result["message"])
        self.assertEqual(called, [("http://192.168.1.115:8870/v1/ocr", 3.0)])

    def test_ocr_service_test_failure_does_not_validate_changed_address(self) -> None:
        def unreachable(_url: str, _timeout: float) -> None:
            raise RuntimeError("timed out")

        result = frontend.test_ocr_service_url(
            "http://192.168.1.115:8870/v1/ocr", verifier=unreachable
        )

        self.assertFalse(result["ok"])
        self.assertIn("timed out", result["message"])
        self.assertFalse(frontend.is_current_ocr_service_verified(
            "http://192.168.1.115:8870/v1/ocr", None
        ))
        self.assertFalse(frontend.is_current_ocr_service_verified(
            "http://192.168.1.115:8871/v1/ocr", result
        ))

    def test_force_ocr_bypasses_an_existing_ocr_cache_entry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            image_path = base / "01.jpg"
            image_path.write_bytes(b"image")
            product = {"platform": "jd", "product_id": "p1"}
            image = {"name": "01.jpg", "path": str(image_path), "sha256": "image-sha"}
            config = demo.mock_config()
            config["force_ocr"] = True
            cache_key = demo.vllm_ocr_cache_key(
                image["sha256"], config["vllm_ocr_model"], config["vllm_ocr_model_version"]
            )
            store = demo.StateStore(base / "state")
            try:
                store.cache_put("ocr", cache_key, {"ok": True, "markdown": "cached text"})
                result = demo.run_ocr_one(
                    "run-1", product, image, config, store, demo.ConcurrencyMeter()
                )
            finally:
                store.close()

        self.assertFalse(result["cached"])
        self.assertEqual(config["metrics"]["ocr_api_calls"], 1)


if __name__ == "__main__":
    unittest.main()
