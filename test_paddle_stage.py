from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import demo
from paddle_ocr import PaddleImage, PaddleOcrError, build_payload


class PaddleStageTests(unittest.TestCase):
    def test_duplicate_file_names_have_distinct_paddle_request_ids(self) -> None:
        first = {"platform": "jd", "product_id": "a"}
        second = {"platform": "jd", "product_id": "b"}
        image = {"name": "01.jpg", "sha256": "same-file-name"}

        self.assertNotEqual(
            demo.paddle_request_id(first, image),
            demo.paddle_request_id(second, image),
        )

    def test_byte_identical_images_get_distinct_request_ids(self) -> None:
        """线上真实踩过的坑：同一商品下 20.jpg 与 26.jpg 字节完全一致。

        只用 sha256 做 request_id 会撞车——服务端 422 拒收整批，本地按
        request_id 建索引还会把前一张的图片名覆盖掉。
        """
        product = {"platform": "jd", "product_id": "100005996353"}
        same_sha = "061e4abb19f2e0357e75eef085bdb2f0"
        first = {"name": "20.jpg", "sha256": same_sha}
        second = {"name": "26.jpg", "sha256": same_sha}

        self.assertNotEqual(
            demo.paddle_request_id(product, first),
            demo.paddle_request_id(product, second),
        )
        self.assertIn("20.jpg", demo.paddle_request_id(product, first))
        self.assertIn("26.jpg", demo.paddle_request_id(product, second))

    def test_duplicate_request_ids_are_rejected_before_http(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            image = Path(temporary) / "20.jpg"
            image.write_bytes(b"image")
            duplicated = [
                PaddleImage("jd/1/20.jpg", image),
                PaddleImage("jd/1/20.jpg", image),
            ]
            with self.assertRaises(PaddleOcrError) as caught:
                build_payload(duplicated)
        self.assertIn("duplicate request ids", str(caught.exception))

    def test_failed_batch_falls_back_to_per_image_requests(self) -> None:
        """整批被拒时，同批其它正常图片不应该被一起判死。"""
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            first = base / "01.jpg"
            second = base / "02.jpg"
            first.write_bytes(b"image-one")
            second.write_bytes(b"image-two")
            store = demo.StateStore(base / "state")
            manifest = {"products": [{
                "platform": "jd", "product_id": "p1",
                "images": [
                    {"name": "01.jpg", "path": str(first), "sha256": "one"},
                    {"name": "02.jpg", "path": str(second), "sha256": "two"},
                ],
            }]}
            calls: list[int] = []

            def fake_post(_url: str, images: list[PaddleImage], timeout: int):
                calls.append(len(images))
                if len(images) > 1:
                    raise PaddleOcrError("Paddle OCR HTTP 422")
                return [{"id": images[0].request_id, "text": "识别成功"}]

            try:
                with patch.object(demo, "verify_paddle_available", return_value=None), \
                        patch.object(demo, "post_batch", side_effect=fake_post):
                    output = demo.run_paddle_ocr_stage(
                        "run-1",
                        manifest,
                        {
                            "paddle_ocr_api_url": "http://127.0.0.1:8870/v1/ocr",
                            "paddle_workers": 1,
                        },
                        store,
                        demo.ConcurrencyMeter(),
                    )
            finally:
                store.close()

        items = output[demo.identity_key("jd", "p1")]
        self.assertEqual(len(items), 2)
        self.assertTrue(all(item["ok"] for item in items), "逐图重试后两张都该成功")
        self.assertEqual([item["image_name"] for item in items], ["01.jpg", "02.jpg"])
        self.assertEqual(calls, [2, 2, 1, 1], "整批失败两次后逐张重试")

    def test_duplicate_image_names_are_reported_instead_of_silently_dropped(self) -> None:
        """两张同内容图片都要出现在结果里，不能有一张被改名顶掉。"""
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            one = base / "20.jpg"
            two = base / "26.jpg"
            payload = b"identical-bytes"
            one.write_bytes(payload)
            two.write_bytes(payload)
            store = demo.StateStore(base / "state")
            manifest = {"products": [{
                "platform": "jd", "product_id": "p1",
                "images": [
                    {"name": "20.jpg", "path": str(one), "sha256": "same"},
                    {"name": "26.jpg", "path": str(two), "sha256": "same"},
                ],
            }]}

            def fake_post(_url: str, images: list[PaddleImage], timeout: int):
                return [{"id": image.request_id, "text": f"文本-{image.request_id}"} for image in images]

            try:
                with patch.object(demo, "verify_paddle_available", return_value=None), \
                        patch.object(demo, "post_batch", side_effect=fake_post):
                    output = demo.run_paddle_ocr_stage(
                        "run-1",
                        manifest,
                        {
                            "paddle_ocr_api_url": "http://127.0.0.1:8870/v1/ocr",
                            "paddle_workers": 1,
                        },
                        store,
                        demo.ConcurrencyMeter(),
                    )
            finally:
                store.close()

        names = [item["image_name"] for item in output[demo.identity_key("jd", "p1")]]
        self.assertEqual(names, ["20.jpg", "26.jpg"], "两张图都必须保留原名")
        self.assertEqual(len({item["sha256"] for item in output[demo.identity_key("jd", "p1")]}), 1)

    def test_blank_paddle_text_becomes_review_failure(self) -> None:
        result = demo.paddle_result_to_ocr_result(
            {"name": "01.jpg", "path": "01.jpg", "sha256": "x"},
            {"id": "jd/a/x", "text": " ", "blocks": []},
            duration_ms=1,
            attempts=1,
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "PADDLE_OCR_REVIEW")

    def test_unreachable_paddle_keeps_the_run_for_manual_resume(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            image = base / "01.jpg"
            image.write_bytes(b"image")
            store = demo.StateStore(base / "state")
            manifest = {"products": [{
                "platform": "jd", "product_id": "p1",
                "images": [{"name": "01.jpg", "path": str(image), "sha256": "one"}],
            }]}
            try:
                with patch.object(
                    demo, "verify_paddle_available",
                    side_effect=demo.PaddleOcrError("Paddle OCR unavailable: timed out"),
                ), patch.object(demo, "post_batch") as post:
                    with self.assertRaises(demo.DemoError) as caught:
                        demo.run_paddle_ocr_stage(
                            "run-1",
                            manifest,
                            {
                                "paddle_ocr_api_url": "http://127.0.0.1:8870/v1/ocr",
                                "paddle_workers": 1,
                                "paddle_recovery_attempts": 1,
                            },
                            store,
                            demo.ConcurrencyMeter(),
                        )
                    events = store.events("run-1")
            finally:
                store.close()

        post.assert_not_called()
        self.assertEqual(caught.exception.code, "PADDLE_OCR_INTERRUPTED")
        self.assertTrue(any(item["status"] == "interrupted" for item in events))

    def test_paddle_recovers_and_retries_the_current_batch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            image = base / "01.jpg"
            image.write_bytes(b"image")
            store = demo.StateStore(base / "state")
            manifest = {"products": [{
                "platform": "jd", "product_id": "p1",
                "images": [{"name": "01.jpg", "path": str(image), "sha256": "one"}],
            }]}
            try:
                with patch.object(
                    demo,
                    "verify_paddle_available",
                    side_effect=[demo.PaddleOcrError("Paddle OCR unavailable: timed out"), None],
                ), patch.object(
                    demo,
                    "post_batch",
                    side_effect=lambda _url, images, timeout: [{"id": images[0].request_id, "text": "识别成功"}],
                ), patch.object(demo.time, "sleep"):
                    output = demo.run_paddle_ocr_stage(
                        "run-1",
                        manifest,
                        {
                            "paddle_ocr_api_url": "http://127.0.0.1:8870/v1/ocr",
                            "paddle_workers": 1,
                            "paddle_recovery_attempts": 2,
                            "paddle_recovery_delay_seconds": 0,
                            "run_output_dir": str(base / "output"),
                        },
                        store,
                        demo.ConcurrencyMeter(),
                    )
                    events = store.events("run-1")
                    record_count = len(list((base / "output").glob("ocr服务中断-*.json")))
            finally:
                store.close()

        self.assertTrue(output[demo.identity_key("jd", "p1")][0]["ok"])
        self.assertEqual(
            [item["status"] for item in events if item["stage"] == "ocr_service"],
            ["interrupted", "recovered"],
        )
        self.assertEqual(record_count, 1)


if __name__ == "__main__":
    unittest.main()
