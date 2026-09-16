from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

import demo
from batch_jobs import BatchJobStore


def manifest_with_products(count: int) -> dict[str, object]:
    return {
        "products": [
            {
                "platform": "jd",
                "product_id": f"p{index:05d}",
                "images": [
                    {
                        "name": "01.jpg",
                        "path": f"D:/images/{index:05d}/01.jpg",
                        "sha256": f"sha-{index:05d}",
                    }
                ],
            }
            for index in range(1, count + 1)
        ]
    }


class BatchJobTests(unittest.TestCase):
    def test_v6_app_version_and_state_root_are_independent(self) -> None:
        self.assertEqual(demo.APP_VERSION, "0.6.0")
        self.assertEqual(Path(demo.__file__).resolve().parent, Path(r"D:\OCR_v6"))

    def test_201_products_create_strict_batches_and_only_first_is_runnable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = BatchJobStore(Path(temporary) / "state")
            try:
                job_id = store.create_job(manifest_with_products(201))
                self.assertEqual(store.batch_sizes(job_id), [100, 100, 1])
                self.assertEqual(store.next_runnable_batch(job_id).batch_no, 1)
                store.mark_batch_exported(job_id, 1)
                self.assertEqual(store.next_runnable_batch(job_id).batch_no, 2)
            finally:
                store.close()

    def test_resume_keeps_exported_batch_and_releases_interrupted_batch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = BatchJobStore(Path(temporary) / "state")
            try:
                job_id = store.create_job(manifest_with_products(201))
                store.mark_batch_exported(job_id, 1)
                store.mark_batch_running(job_id, 2, worker_id="old-worker")
                store.resume_job(job_id)
                self.assertEqual(store.batch_statuses(job_id), ["exported", "pending", "pending"])
                self.assertEqual(store.next_runnable_batch(job_id).batch_no, 2)
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
