from __future__ import annotations

import argparse
import html
import json
import os
import socket
from pathlib import Path
from typing import Any, Callable

import demo
from batch_jobs import BatchJobStore, dump_json
from retry_review import collect_review_images, load_product_documents, merge_missing_fields, retry_manifest


BatchExecutor = Callable[[dict[str, Any], Path], dict[str, Any]]


def write_job_index(output_root: Path, summary: dict[str, Any]) -> None:
    dump_json(output_root / "任务总览.json", summary)
    batch_text = " / ".join(f"{html.escape(key)}: {value}" for key, value in summary["batches"].items())
    image_text = " / ".join(f"{html.escape(key)}: {value}" for key, value in summary["images"].items())
    (output_root / "任务总览.html").write_text(
        "<meta charset='utf-8'><title>OCR v6 任务总览</title>"
        f"<h1>任务 {html.escape(summary['job_id'])}</h1>"
        f"<p>状态：{html.escape(summary['status'])}</p>"
        f"<p>批次：{batch_text or '无'}</p><p>图片：{image_text or '无'}</p>",
        encoding="utf-8",
    )


def write_review_list(manifest: dict[str, Any], output_dir: Path, result: dict[str, Any]) -> list[dict[str, str]]:
    pipeline = result.get("result", {})
    products_dir = Path(str(pipeline.get("output_dir") or "")) / "products"
    documents: list[dict[str, Any]] = []
    if products_dir.is_dir():
        for path in products_dir.glob("*.json"):
            documents.append(json.loads(path.read_text(encoding="utf-8")))
    queue = collect_review_images(manifest, documents)
    dump_json(output_dir / "待复核图片.json", queue)
    return queue


def run_job(job_id: str, store: BatchJobStore, runs_root: Path, executor: BatchExecutor) -> dict[str, Any]:
    job_root = runs_root / job_id
    review_queue: list[dict[str, str]] = []
    while (batch := store.next_runnable_batch(job_id)) is not None:
        store.mark_batch_running(job_id, batch.batch_no, f"{socket.gethostname()}-{os.getpid()}")
        output_dir = job_root / "batches" / f"{batch.batch_no:04d}"
        output_dir.mkdir(parents=True, exist_ok=True)
        result = executor(store.batch_manifest(job_id, batch.batch_no), output_dir)
        if result.get("status") == "paused":
            store.mark_batch_paused(job_id, batch.batch_no, str(result.get("pause_reason") or "服务暂停"))
            write_job_index(job_root, store.summary(job_id))
            return {"status": "paused", "batch_no": batch.batch_no}
        review_queue.extend(write_review_list(store.batch_manifest(job_id, batch.batch_no), output_dir, result))
        store.mark_batch_exported(job_id, batch.batch_no, str(output_dir))
        write_job_index(job_root, store.summary(job_id))
    summary = store.summary(job_id)
    retry_root = job_root / "retry-review"
    dump_json(retry_root / "待重试图片.json", review_queue)
    write_job_index(job_root, summary)
    return {"status": "completed", "summary": summary, "review_images": len(review_queue)}


def real_executor(config: dict[str, Any], state_dir: Path) -> BatchExecutor:
    template = Path(config["template_path"])

    def execute(manifest: dict[str, Any], output_dir: Path) -> dict[str, Any]:
        result = demo.execute_pipeline(
            manifest,
            demo.load_json(template),
            state_dir / "pipeline",
            output_dir,
            config,
            force_new=True,
        )
        return {"status": "complete", "result": result}

    return execute


def run_retry_review(job_id: str, store: BatchJobStore, state_dir: Path, config: dict[str, Any]) -> None:
    job_root = state_dir.parent / "runs" / job_id
    queue_path = job_root / "retry-review" / "待重试图片.json"
    if not queue_path.is_file():
        return
    queue = json.loads(queue_path.read_text(encoding="utf-8"))
    if not queue:
        dump_json(job_root / "最终合并结果" / "汇总.json", [])
        return
    manifests = [store.batch_manifest(job_id, number) for number in range(1, len(store.batch_sizes(job_id)) + 1)]
    manifest = retry_manifest(queue, manifests)
    retry_root = job_root / "retry-review"
    result = real_executor(config, state_dir)(manifest, retry_root)
    originals = load_product_documents(job_root / "batches")
    retries = load_product_documents(Path(result["result"]["output_dir"]))
    final_root = job_root / "最终合并结果"
    for key, original in originals.items():
        merged = merge_missing_fields(original, retries[key]) if key in retries else original
        dump_json(final_root / f"{key[1]}.json", merged)
    dump_json(final_root / "汇总.json", list(load_product_documents(final_root).values()))


def main() -> int:
    parser = argparse.ArgumentParser(description="OCR v6 后台批处理 worker")
    parser.add_argument("--job", required=True)
    parser.add_argument("--state", required=True)
    args = parser.parse_args()
    state_dir = Path(args.state).resolve()
    store = BatchJobStore(state_dir)
    try:
        config = store.config(args.job)
        first_batch = store.next_runnable_batch(args.job)
        if first_batch is None:
            raise RuntimeError("任务没有可运行批次")
        if not config.get("vllm_ocr_api_base"):
            # Secrets stay in the worker process environment (provided by the GUI),
            # never in the job SQLite file.
            config = demo.real_config(store.batch_manifest(args.job, first_batch.batch_no))
        config.setdefault("template_path", str(Path(__file__).with_name("template-v2.json")))
        result = run_job(args.job, store, state_dir.parent / "runs", real_executor(config, state_dir))
        if result["status"] == "completed":
            run_retry_review(args.job, store, state_dir, config)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
