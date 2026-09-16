from __future__ import annotations

import unittest

from single_image_ocr_benchmark import format_result
from single_image_ocr_ui import default_connection_values


class SingleImageOcrTests(unittest.TestCase):
    def test_ui_uses_saved_connection_values_with_empty_safe_defaults(self) -> None:
        values = default_connection_values({"vllm_ocr_api_base": "http://127.0.0.1:8801/v1", "vllm_ocr_model": "qwen-vl", "vllm_ocr_model_version": "qwen-vl-int8"})
        self.assertEqual(values["vllm_ocr_api_base"], "http://127.0.0.1:8801/v1")
        self.assertEqual(values["vllm_ocr_model"], "qwen-vl")
        self.assertEqual(values["vllm_ocr_model_version"], "qwen-vl-int8")
        self.assertEqual(values["vllm_ocr_api_key"], "")

    def test_format_result_exposes_elapsed_bytes_and_success(self) -> None:
        value = format_result(
            {
                "ok": True,
                "duration_ms": 1234,
                "markdown": "包装文字",
                "input_image": {"source_bytes": 4_000_000, "sent_bytes": 400_000},
            }
        )
        self.assertEqual(value["耗时秒"], 1.234)
        self.assertEqual(value["原图字节"], 4_000_000)
        self.assertEqual(value["发送字节"], 400_000)
        self.assertEqual(value["状态"], "成功")


if __name__ == "__main__":
    unittest.main()
