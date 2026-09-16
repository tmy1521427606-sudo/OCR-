from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import demo


class PaddleStageTests(unittest.TestCase):
    def test_duplicate_file_names_have_distinct_paddle_request_ids(self) -> None:
        first = {"platform": "jd", "product_id": "a"}
        second = {"platform": "jd", "product_id": "b"}
        image = {"name": "01.jpg", "sha256": "same-file-name"}

        self.assertNotEqual(
            demo.paddle_request_id(first, image),
            demo.paddle_request_id(second, image),
        )

    def test_blank_paddle_text_becomes_review_failure(self) -> None:
        result = demo.paddle_result_to_ocr_result(
            {"name": "01.jpg", "path": "01.jpg", "sha256": "x"},
            {"id": "jd/a/x", "text": " ", "blocks": []},
            duration_ms=1,
            attempts=1,
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "PADDLE_OCR_REVIEW")

    def test_unreachable_paddle_is_marked_once_without_sending_batches(self) -> None:
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
                    output = demo.run_paddle_ocr_stage(
                        "run-1",
                        manifest,
                        {"paddle_ocr_api_url": "http://127.0.0.1:8870/v1/ocr", "paddle_workers": 1},
                        store,
                        demo.ConcurrencyMeter(),
                    )
            finally:
                store.close()

        item = output[demo.identity_key("jd", "p1")][0]
        self.assertFalse(item["ok"])
        self.assertEqual(item["error"]["code"], "PADDLE_OCR_UNREACHABLE")
        post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
