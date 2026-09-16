from __future__ import annotations

import base64
import tempfile
import unittest
from pathlib import Path

from paddle_ocr import PaddleImage, PaddleOcrError, build_payload, validate_results


class PaddleAdapterTests(unittest.TestCase):
    def test_payload_uses_unique_id_and_base64_image_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            image = Path(temporary) / "01.jpg"
            image.write_bytes(b"image")
            payload = build_payload([PaddleImage("jd/497394/01.jpg", image)])
        self.assertEqual(payload["images"][0]["id"], "jd/497394/01.jpg")
        self.assertEqual(payload["images"][0]["image_base64"], base64.b64encode(b"image").decode())

    def test_response_rejects_incomplete_or_duplicate_ids(self) -> None:
        with self.assertRaisesRegex(PaddleOcrError, "incomplete"):
            validate_results(["a", "b"], [{"id": "a", "text": "ok", "blocks": []}])
        with self.assertRaisesRegex(PaddleOcrError, "do not match"):
            validate_results(["a", "b"], [{"id": "a", "text": "ok", "blocks": []}, {"id": "a", "text": "ok", "blocks": []}])


if __name__ == "__main__":
    unittest.main()
