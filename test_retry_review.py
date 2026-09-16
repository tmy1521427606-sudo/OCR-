from __future__ import annotations

import unittest

from retry_review import collect_review_images, merge_missing_fields


class RetryReviewTests(unittest.TestCase):
    def test_retry_merge_fills_blank_review_field_without_overwriting_confirmed_field(self) -> None:
        original = {
            "fields": {"规格": "60粒", "产地": None},
            "field_status": {"规格": "confirmed", "产地": "review"},
        }
        retry = {"fields": {"规格": "90粒", "产地": "日本"}}
        merged = merge_missing_fields(original, retry)
        self.assertEqual(merged["fields"], {"规格": "60粒", "产地": "日本"})
        self.assertEqual(merged["field_status"]["产地"], "retry_filled")

    def test_review_queue_uses_manifest_images_missing_from_success_artifacts(self) -> None:
        manifest = {"products": [{"platform": "jd", "product_id": "p1", "images": [{"name": "01.jpg"}, {"name": "02.jpg"}]}]}
        document = {"identity": {"platform": "jd", "product_id": "p1"}, "artifacts": {"ocr": [{"image": "01.jpg"}]}}
        self.assertEqual(collect_review_images(manifest, [document]), [{"platform": "jd", "product_id": "p1", "image_name": "02.jpg"}])


if __name__ == "__main__":
    unittest.main()
