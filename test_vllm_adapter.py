from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from PIL import Image

import demo


class VllmAdapterTests(unittest.TestCase):
    def test_schema_requires_only_blocks_without_duplicate_recognized_text(self) -> None:
        schema = demo.VLLM_OCR_SCHEMA

        self.assertNotIn("recognized_text", schema["properties"])
        self.assertEqual(schema["required"], ["blocks"])

    def test_prepare_vllm_image_downscales_large_image_before_upload(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "large.png"
            Image.new("RGB", (3200, 2400), "white").save(source)

            prepared = demo.prepare_vllm_image(source)

        self.assertEqual((prepared["source_width"], prepared["source_height"]), (3200, 2400))
        self.assertLessEqual(prepared["sent_width"], demo.OCR_IMAGE_MAX_LONG_EDGE)
        self.assertLessEqual(prepared["sent_width"] * prepared["sent_height"], demo.OCR_IMAGE_MAX_PIXELS)
        self.assertEqual(prepared["mime_type"], "image/jpeg")

    def test_prepare_vllm_image_does_not_upscale_small_image(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "small.png"
            Image.new("RGB", (800, 600), "white").save(source)

            prepared = demo.prepare_vllm_image(source)

        self.assertEqual((prepared["sent_width"], prepared["sent_height"]), (800, 600))

    def test_blocks_rebuild_full_text_when_top_level_text_is_truncated(self) -> None:
        extractor = getattr(demo, "vllm_markdown", None)
        self.assertTrue(callable(extractor), "缺少 vllm_markdown")
        response = {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "recognized_text": "纽曼思®DHA",
                                "blocks": [
                                    {"order": 2, "text": "净含量：22.68克", "legibility": "clear"},
                                    {"order": 1, "text": "纽曼思®DHA", "legibility": "clear"},
                                    {"order": 3, "text": "净含量：22.68克", "legibility": "clear"},
                                ],
                            },
                            ensure_ascii=False,
                        )
                    }
                }
            ]
        }

        self.assertEqual(
            extractor(response),
            "纽曼思®DHA\n净含量：22.68克\n净含量：22.68克",
        )

    def test_cache_key_changes_when_vllm_model_version_changes(self) -> None:
        cache_key = getattr(demo, "vllm_ocr_cache_key", None)
        self.assertTrue(callable(cache_key), "缺少 vllm_ocr_cache_key")
        base = cache_key("image-sha", "Qwen/Qwen3.8-27B", "build-a")
        self.assertNotEqual(base, cache_key("image-sha", "Qwen/Qwen3.8-27B", "build-b"))


if __name__ == "__main__":
    unittest.main()
