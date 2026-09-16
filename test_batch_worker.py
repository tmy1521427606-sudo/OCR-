from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from batch_jobs import BatchJobStore
from batch_worker import run_job
from test_batch_jobs import manifest_with_products


class BatchWorkerTests(unittest.TestCase):
    def test_worker_exports_batch_before_starting_next_batch(self) -> None:
        calls: list[int] = []
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = BatchJobStore(root / "state")
            try:
                job_id = store.create_job(manifest_with_products(201))

                def executor(manifest: dict[str, object], output_dir: Path) -> dict[str, object]:
                    batch_no = int(output_dir.name)
                    if batch_no > 1:
                        self.assertEqual(store.batch_statuses(job_id)[batch_no - 2], "exported")
                    calls.append(batch_no)
                    (output_dir / "report.html").parent.mkdir(parents=True, exist_ok=True)
                    (output_dir / "report.html").write_text(f"batch-{batch_no}", encoding="utf-8")
                    return {"status": "complete"}

                result = run_job(job_id, store, root / "runs", executor)
                self.assertEqual(result["status"], "completed")
                self.assertEqual(calls, [1, 2, 3])
                self.assertEqual(store.batch_statuses(job_id), ["exported", "exported", "exported"])
                self.assertTrue((root / "runs" / job_id / "batches" / "0001" / "report.html").exists())
                self.assertTrue((root / "runs" / job_id / "任务总览.html").exists())
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
