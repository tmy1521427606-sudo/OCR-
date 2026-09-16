from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


BATCH_SIZE = 100


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def dump_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def manifest_hash(manifest: dict[str, Any]) -> str:
    payload = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class BatchRecord:
    job_id: str
    batch_no: int
    product_count: int
    status: str


class BatchJobStore:
    """Durable job/batch state for local v6 workers."""

    def __init__(self, root: Path):
        self.root = root.resolve()
        self.jobs_root = self.root / "jobs"
        self.root.mkdir(parents=True, exist_ok=True)
        self.jobs_root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.root / "batch-jobs.sqlite3")
        self.db.row_factory = sqlite3.Row
        with self.db:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    manifest_hash TEXT NOT NULL,
                    status TEXT NOT NULL,
                    config_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            self.db.execute(
                """
                CREATE TABLE IF NOT EXISTS batches (
                    job_id TEXT NOT NULL,
                    batch_no INTEGER NOT NULL,
                    product_count INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    worker_id TEXT,
                    pause_reason TEXT,
                    output_dir TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (job_id, batch_no)
                )
                """
            )
            self.db.execute(
                """
                CREATE TABLE IF NOT EXISTS job_products (
                    job_id TEXT NOT NULL,
                    batch_no INTEGER NOT NULL,
                    position_in_batch INTEGER NOT NULL,
                    platform TEXT NOT NULL,
                    product_id TEXT NOT NULL,
                    product_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    PRIMARY KEY (job_id, platform, product_id)
                )
                """
            )
            self.db.execute(
                """
                CREATE TABLE IF NOT EXISTS job_images (
                    job_id TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    product_id TEXT NOT NULL,
                    image_name TEXT NOT NULL,
                    image_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    error_code TEXT,
                    PRIMARY KEY (job_id, platform, product_id, image_name)
                )
                """
            )
            self.db.execute(
                """
                CREATE TABLE IF NOT EXISTS job_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL,
                    batch_no INTEGER,
                    event TEXT NOT NULL,
                    message TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )

    def close(self) -> None:
        self.db.close()

    def create_job(self, manifest: dict[str, Any], config: dict[str, Any] | None = None) -> str:
        products = manifest.get("products")
        if not isinstance(products, list) or not products:
            raise ValueError("任务清单至少需要一个商品")
        job_id = f"job-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
        created = now()
        with self.db:
            self.db.execute(
                "INSERT INTO jobs VALUES(?,?,?,?,?,?)",
                (job_id, manifest_hash(manifest), "created", json.dumps(config or {}, ensure_ascii=False), created, created),
            )
            for index, product in enumerate(products):
                if not isinstance(product, dict):
                    raise ValueError("商品清单项必须是对象")
                platform = str(product.get("platform") or "").strip()
                product_id = str(product.get("product_id") or "").strip()
                if not platform or not product_id:
                    raise ValueError("商品缺少 platform 或 product_id")
                batch_no = index // BATCH_SIZE + 1
                position = index % BATCH_SIZE + 1
                if position == 1:
                    count = min(BATCH_SIZE, len(products) - index)
                    self.db.execute(
                        "INSERT INTO batches VALUES(?,?,?,?,?,?,?,?,?)",
                        (job_id, batch_no, count, "pending", None, None, None, created, created),
                    )
                self.db.execute(
                    "INSERT INTO job_products VALUES(?,?,?,?,?,?,?)",
                    (job_id, batch_no, position, platform, product_id, json.dumps(product, ensure_ascii=False), "pending"),
                )
                for image in product.get("images", []):
                    image_name = str(image.get("name") or "").strip()
                    if not image_name:
                        raise ValueError(f"商品 {product_id} 存在缺少名称的图片")
                    self.db.execute(
                        "INSERT INTO job_images VALUES(?,?,?,?,?,?,?)",
                        (job_id, platform, product_id, image_name, json.dumps(image, ensure_ascii=False), "pending", None),
                    )
            self._event(job_id, None, "job_created", f"商品数={len(products)}")
        dump_json(self.job_dir(job_id) / "manifest.json", manifest)
        return job_id

    def job_dir(self, job_id: str) -> Path:
        return self.jobs_root / job_id

    def batch_sizes(self, job_id: str) -> list[int]:
        rows = self.db.execute(
            "SELECT product_count FROM batches WHERE job_id=? ORDER BY batch_no", (job_id,)
        ).fetchall()
        return [int(row[0]) for row in rows]

    def batch_statuses(self, job_id: str) -> list[str]:
        rows = self.db.execute(
            "SELECT status FROM batches WHERE job_id=? ORDER BY batch_no", (job_id,)
        ).fetchall()
        return [str(row[0]) for row in rows]

    def next_runnable_batch(self, job_id: str) -> BatchRecord | None:
        row = self.db.execute(
            """
            SELECT b.job_id, b.batch_no, b.product_count, b.status
            FROM batches b
            WHERE b.job_id=? AND b.status='pending'
              AND NOT EXISTS (
                  SELECT 1 FROM batches earlier
                  WHERE earlier.job_id=b.job_id
                    AND earlier.batch_no < b.batch_no
                    AND earlier.status != 'exported'
              )
            ORDER BY b.batch_no
            LIMIT 1
            """,
            (job_id,),
        ).fetchone()
        return BatchRecord(**dict(row)) if row else None

    def mark_batch_running(self, job_id: str, batch_no: int, worker_id: str) -> None:
        self._set_batch(job_id, batch_no, "running", worker_id=worker_id)

    def mark_batch_exported(self, job_id: str, batch_no: int, output_dir: str | None = None) -> None:
        self._set_batch(job_id, batch_no, "exported", output_dir=output_dir)
        with self.db:
            self.db.execute(
                "UPDATE job_products SET status='exported' WHERE job_id=? AND batch_no=?",
                (job_id, batch_no),
            )
            self._event(job_id, batch_no, "batch_exported", output_dir or "")
            remaining = self.db.execute(
                "SELECT COUNT(*) FROM batches WHERE job_id=? AND status!='exported'", (job_id,)
            ).fetchone()[0]
            if not remaining:
                self.db.execute("UPDATE jobs SET status='completed', updated_at=? WHERE job_id=?", (now(), job_id))

    def mark_batch_paused(self, job_id: str, batch_no: int, reason: str) -> None:
        self._set_batch(job_id, batch_no, "paused", pause_reason=reason)
        with self.db:
            self.db.execute("UPDATE jobs SET status='paused', updated_at=? WHERE job_id=?", (now(), job_id))

    def resume_job(self, job_id: str) -> None:
        with self.db:
            self.db.execute(
                "UPDATE batches SET status='pending', worker_id=NULL, pause_reason=NULL, updated_at=? WHERE job_id=? AND status IN ('running','paused')",
                (now(), job_id),
            )
            self.db.execute("UPDATE jobs SET status='created', updated_at=? WHERE job_id=?", (now(), job_id))
            self._event(job_id, None, "job_resumed", "")

    def batch_manifest(self, job_id: str, batch_no: int) -> dict[str, Any]:
        rows = self.db.execute(
            "SELECT product_json FROM job_products WHERE job_id=? AND batch_no=? ORDER BY position_in_batch",
            (job_id, batch_no),
        ).fetchall()
        return {"products": [json.loads(row[0]) for row in rows]}

    def config(self, job_id: str) -> dict[str, Any]:
        row = self.db.execute("SELECT config_json FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        if not row:
            raise KeyError(job_id)
        return json.loads(row[0])

    def summary(self, job_id: str) -> dict[str, Any]:
        row = self.db.execute("SELECT status FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        if not row:
            raise KeyError(job_id)
        batches = self.db.execute(
            "SELECT status, COUNT(*) AS count FROM batches WHERE job_id=? GROUP BY status", (job_id,)
        ).fetchall()
        images = self.db.execute(
            "SELECT status, COUNT(*) AS count FROM job_images WHERE job_id=? GROUP BY status", (job_id,)
        ).fetchall()
        return {
            "job_id": job_id,
            "status": row[0],
            "batches": {str(item["status"]): int(item["count"]) for item in batches},
            "images": {str(item["status"]): int(item["count"]) for item in images},
        }

    def _set_batch(self, job_id: str, batch_no: int, status: str, **values: str | None) -> None:
        assignments = ["status=?", "updated_at=?"]
        params: list[Any] = [status, now()]
        for name, value in values.items():
            assignments.append(f"{name}=?")
            params.append(value)
        params.extend([job_id, batch_no])
        with self.db:
            cursor = self.db.execute(
                f"UPDATE batches SET {', '.join(assignments)} WHERE job_id=? AND batch_no=?", params
            )
            if cursor.rowcount != 1:
                raise KeyError(f"未找到批次 {job_id}/{batch_no}")
            self._event(job_id, batch_no, f"batch_{status}", "")

    def _event(self, job_id: str, batch_no: int | None, event: str, message: str) -> None:
        self.db.execute(
            "INSERT INTO job_events(job_id,batch_no,event,message,created_at) VALUES(?,?,?,?,?)",
            (job_id, batch_no, event, message, now()),
        )
