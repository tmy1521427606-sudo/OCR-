# Local Qwen 10,000-product batching Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Create an independent v6 local Qwen-VL application that can checkpoint and resume a 10,000-product job, processing exactly 100 products per strictly serial batch and producing an isolated result for every batch.

**Architecture:** v6 keeps v5's OCR, analysis, export, and image preprocessing code as the single-product/batch execution engine. A new SQLite-backed job store records immutable product assignment, picture outcome, batch state, and worker ownership; a detached background worker calls the existing pipeline for one batch at a time and advances only after that batch is exported. A final retry phase reads prior product documents and merges only missing/review fields.

**Tech Stack:** Python 3.12, standard-library `sqlite3`/`subprocess`/`threading`, Pillow, existing psycopg/openpyxl, Windows Tkinter front end.

**Spec:** `D:\OCR_v5\docs\superpowers\specs\2026-09-04-local-qwen-10000-products-design.md`

## Global Constraints

- OCR engine remains the existing local Qwen-VL `/v1` endpoint; do not add PaddleOCR or a remote queue.
- A batch contains exactly 100 products except the final remainder batch.
- Batch N+1 must not begin before batch N has an exported report and state `exported`.
- A per-picture OCR request has a 30-second budget; terminal OCR failures are review items, never an unhandled batch exception.
- v5 is read-only after the initial source copy; all v6 state lives under `D:\OCR_v6`.
- The worker may outlive the GUI but cannot run while Windows/the computer is shut down; restarting resumes from SQLite state.
- Secrets remain only in DPAPI-protected frontend configuration; never write or log raw credentials.
- `D:\OCR_v6` is not a Git repository. Record exact test commands/results in `测试记录.md` instead of commits.

---

### Task 1: Create the independent v6 baseline

**Files:**
- Create: `D:\OCR_v6\demo.py`, `D:\OCR_v6\直接用图片测试.py`, `D:\OCR_v6\run_demo.ps1`, `D:\OCR_v6\启动OCR_Demo.cmd`, `D:\OCR_v6\template-v2.json`, `D:\OCR_v6\export_workbook.mjs`, `D:\OCR_v6\README.md`
- Create: `D:\OCR_v6\test_performance.py`, `D:\OCR_v6\test_progressive_ocr.py`, `D:\OCR_v6\test_vllm_adapter.py`, `D:\OCR_v6\test_frontend_speed.py`
- Create: `D:\OCR_v6\测试记录.md`
- Modify: `D:\OCR_v6\demo.py`, `D:\OCR_v6\直接用图片测试.py`

**Interfaces:**
- Consumes: v5 source files named above; no `.state`, `.venv`, `runs`, cache, or frontend secrets are copied.
- Produces: an executable v6 baseline with `APP_VERSION = "0.6.0"` and an empty v6 state directory created only on first launch.

- [ ] **Step 1: Copy only source, templates, launcher, documentation, and tests from v5 into v6**

Run the following PowerShell command; it intentionally excludes runtime state, virtual environments, results, and credentials:

```powershell
$source = 'D:\OCR_v5'
$target = 'D:\OCR_v6'
$files = @('demo.py','直接用图片测试.py','run_demo.ps1','启动OCR_Demo.cmd','template-v2.json','export_workbook.mjs','README.md','test_performance.py','test_progressive_ocr.py','test_vllm_adapter.py','test_frontend_speed.py')
foreach ($file in $files) { Copy-Item -LiteralPath (Join-Path $source $file) -Destination (Join-Path $target $file) -Force }
```

- [ ] **Step 2: Bootstrap the independent v6 virtual environment**

Run: `D:\OCR_v6\run_demo.ps1 -SelfTest`

Expected: v6 creates `D:\OCR_v6\.venv` and its own dependencies without reading `D:\OCR_v5\.venv`; the copied baseline self-test may otherwise pass.

- [ ] **Step 3: Write the failing version-isolation test**

Add this test to `test_batch_jobs.py`:

```python
def test_v6_app_version_and_state_root_are_independent() -> None:
    assert demo.APP_VERSION == "0.6.0"
    assert Path(demo.__file__).resolve().parent == Path(r"D:\OCR_v6")
```

- [ ] **Step 4: Run the test and verify it fails against the copied v5 baseline**

Run: `D:\OCR_v6\.venv\Scripts\python.exe -m unittest test_batch_jobs.BatchJobTests.test_v6_app_version_and_state_root_are_independent -v`

Expected: FAIL because `APP_VERSION` is still `0.5.0`.

- [ ] **Step 5: Make the minimal v6-only changes**

Set `APP_VERSION = "0.6.0"` in `demo.py`; update visible application labels from `v5` to `v6`; keep `APP_DIR = Path(__file__).resolve().parent` so state/config remains under v6. Do not copy `D:\OCR_v5\.state`.

- [ ] **Step 6: Verify the v6 baseline**

Run:

```powershell
D:\OCR_v6\run_demo.ps1 -SelfTest
D:\OCR_v6\.venv\Scripts\python.exe -m unittest test_performance test_progressive_ocr test_vllm_adapter test_frontend_speed test_batch_jobs -q
```

Expected: all existing tests and the new isolation test pass.

- [ ] **Step 7: Record the baseline verification**

Append the executed commands, timestamps, and pass/fail status to `D:\OCR_v6\测试记录.md`; do not copy any credential values.

### Task 2: Add a durable 100-product job store

**Files:**
- Create: `D:\OCR_v6\batch_jobs.py`
- Create: `D:\OCR_v6\test_batch_jobs.py`

**Interfaces:**
- Consumes: a manifest shaped as `{"products": [{"platform", "product_id", "images"}]}`.
- Produces: `BatchJobStore(state_dir: Path)`, `create_job(manifest, batch_size=100) -> str`, `next_runnable_batch(job_id) -> BatchRecord | None`, `mark_batch_exported(job_id, batch_no) -> None`, `resume_job(job_id) -> None`.
- Persistent tables: `jobs`, `batches`, `job_products`, `job_images`, `job_events`. Primary keys include `job_id`; product assignment is immutable after creation.

- [ ] **Step 1: Write the failing strict-batching test**

Add this test:

```python
def test_201_products_create_100_100_1_and_only_first_batch_is_runnable(tmp_path: Path) -> None:
    store = BatchJobStore(tmp_path / "state")
    job_id = store.create_job(manifest_with_products(201), batch_size=100)
    assert store.batch_sizes(job_id) == [100, 100, 1]
    assert store.next_runnable_batch(job_id).batch_no == 1
    store.mark_batch_exported(job_id, 1)
    assert store.next_runnable_batch(job_id).batch_no == 2
```

`manifest_with_products` must build literal product ids `p00001` through `p00201`, each with one test image record.

- [ ] **Step 2: Run the test and verify it fails because `BatchJobStore` does not exist**

Run: `D:\OCR_v6\.venv\Scripts\python.exe -m unittest test_batch_jobs.BatchJobTests.test_201_products_create_100_100_1_and_only_first_batch_is_runnable -v`

Expected: FAIL with an import or name error for `BatchJobStore`.

- [ ] **Step 3: Implement schema creation and immutable batch assignment**

Implement these exact rules in `batch_jobs.py`:

```python
def create_job(self, manifest: dict[str, Any], batch_size: int = 100) -> str:
    if batch_size != 100:
        raise ValueError("v6 批大小固定为 100 个商品")
    # Insert jobs, batches, job_products and job_images in one transaction.
    # batch_no = product_index // 100 + 1; position_in_batch = product_index % 100 + 1.

def next_runnable_batch(self, job_id: str) -> BatchRecord | None:
    # Return the lowest pending batch only if no smaller batch is non-exported.
```

Persist a manifest JSON artifact at `jobs/<job_id>/manifest.json`; its SHA-256 becomes the job fingerprint.

- [ ] **Step 4: Run the strict-batching test and all job-store tests**

Run: `D:\OCR_v6\.venv\Scripts\python.exe -m unittest test_batch_jobs -v`

Expected: PASS; the test proves batch 2 remains unavailable until batch 1 is exported.

- [ ] **Step 5: Write and run the resume test**

Add and run:

```python
def test_resume_keeps_exported_batch_and_returns_interrupted_batch(tmp_path: Path) -> None:
    store = BatchJobStore(tmp_path / "state")
    job_id = store.create_job(manifest_with_products(201))
    store.mark_batch_exported(job_id, 1)
    store.mark_batch_running(job_id, 2, worker_id="old-worker")
    store.resume_job(job_id)
    assert store.batch_statuses(job_id) == ["exported", "pending", "pending"]
    assert store.next_runnable_batch(job_id).batch_no == 2
```

Expected: PASS; no product in exported batch 1 returns to pending.

### Task 3: Make the existing pipeline return review output instead of aborting

**Files:**
- Modify: `D:\OCR_v6\demo.py: OcrStageError, run_ocr_stage, execute_pipeline`
- Modify: `D:\OCR_v6\test_progressive_ocr.py`

**Interfaces:**
- Consumes: existing `run_ocr_one` result dictionaries and an optional `pipeline_mode` string, either `"interactive"` or `"batch"`.
- Produces: `run_ocr_stage(..., tolerate_service_outage=True) -> OcrStageResult`, where all started images have a result, remaining images are returned as `pending_images`, and no `OcrStageError` is raised merely for the consecutive-network-failure threshold.

- [ ] **Step 1: Write the failing service-outage preservation test**

Replace the old expectation that three network failures raise an exception with:

```python
def test_batch_ocr_service_outage_returns_completed_failures_and_pending_images() -> None:
    result = demo.run_ocr_stage(
        "run-1", manifest_with_five_images(), {"ocr_workers": 1}, store,
        demo.ConcurrencyMeter(), tolerate_service_outage=True,
    )
    assert [item["image_name"] for item in result.completed_results[key]] == ["01.jpg", "02.jpg", "03.jpg"]
    assert result.pending_images[key] == ["04.jpg", "05.jpg"]
    assert all(item["error"]["code"] == "VLLM_NETWORK_ERROR" for item in result.completed_results[key])
```

Use a fake `run_ocr_one` which always returns a complete `VLLM_NETWORK_ERROR` result.

- [ ] **Step 2: Run the test and verify it fails against the exception-based implementation**

Run: `D:\OCR_v6\.venv\Scripts\python.exe -m unittest test_progressive_ocr.ProgressiveOcrTests.test_batch_ocr_service_outage_returns_completed_failures_and_pending_images -v`

Expected: FAIL with `VLLM_NETWORK_OUTAGE`.

- [ ] **Step 3: Implement `OcrStageResult` and tolerant service-outage behavior**

Create a small dataclass:

```python
@dataclass
class OcrStageResult:
    completed_results: dict[str, list[dict[str, Any]]]
    pending_images: dict[str, list[str]]
    paused_for_service: bool
    duration_ms: int
```

Keep interactive behavior unchanged by default. In batch mode, after three consecutive network failures, stop scheduling further OCR calls, return the completed failures and names of unscheduled images, and let the caller write a pause report. Individual timeout, invalid response, and non-network OCR errors remain terminal review results and processing continues.

- [ ] **Step 4: Write the failing database-isolation regression test**

Add a test whose fake Redshift stage raises `DemoError("REDSHIFT_ERROR", "test")` while fake OCR returns one success and one timeout. Assert the batch result writes `performance.json`, a product JSON, and an OCR result; assert the product validation contains `DATABASE_RECORD_MISSING`.

- [ ] **Step 5: Run the database-isolation test and verify it fails against the current future ordering**

Run: `D:\OCR_v6\.venv\Scripts\python.exe -m unittest test_progressive_ocr.ProgressiveOcrTests.test_database_failure_keeps_ocr_output_and_performance -v`

Expected: FAIL because `database_future.result()` masks OCR output or no performance file is emitted.

- [ ] **Step 6: Implement independent future collection in `execute_pipeline`**

Collect the OCR and Redshift futures separately. Convert Redshift failure to an empty database mapping plus a structured database issue; always await and preserve OCR output before entering export. In batch mode, create product documents and report even when the result is `paused_for_service`; mark batch status `paused` rather than throwing.

- [ ] **Step 7: Run pipeline regression tests**

Run: `D:\OCR_v6\.venv\Scripts\python.exe -m unittest test_progressive_ocr test_performance test_vllm_adapter -q`

Expected: PASS, including the existing real-time partial-output tests.

### Task 4: Implement sequential batch worker and isolated outputs

**Files:**
- Create: `D:\OCR_v6\batch_worker.py`
- Create: `D:\OCR_v6\test_batch_worker.py`
- Modify: `D:\OCR_v6\demo.py`

**Interfaces:**
- Consumes: `python batch_worker.py --job <job_id> --state <state_dir>` and the job configuration stored at creation time.
- Produces: `run_job(job_id, store, config) -> JobRunSummary`; each exported batch uses `runs/<job_id>/batches/<batch_no:04d>/` and writes `report.html`, `performance.json`, `products/`, `待复核图片.json`, plus XLSX when enabled.

- [ ] **Step 1: Write the failing sequencing integration test**

Use 201 products so the real fixed batch size produces three batches. Fake `execute_pipeline` writes a marker and returns. Assert:

```python
assert calls == [1, 2, 3]
assert (job_root / "batches" / "0001" / "report.html").exists()
assert (job_root / "batches" / "0002" / "report.html").exists()
assert (job_root / "batches" / "0003" / "report.html").exists()
assert store.batch_statuses(job_id) == ["exported", "exported", "exported"]
```

The fake records that batch 1 was `exported` before batch 2 was called.

- [ ] **Step 2: Run the integration test and verify it fails because no worker exists**

Run: `D:\OCR_v6\.venv\Scripts\python.exe -m unittest test_batch_worker.BatchWorkerTests.test_worker_exports_batch_before_starting_next_batch -v`

Expected: FAIL with an import error for `batch_worker`.

- [ ] **Step 3: Implement the worker loop**

Implement this control flow:

```python
while batch := store.next_runnable_batch(job_id):
    store.mark_batch_running(job_id, batch.batch_no, worker_id)
    result = execute_pipeline(batch_manifest, template, state_dir, batch_output_dir, config,
                              force_new=True, batch_context={"job_id": job_id, "batch_no": batch.batch_no})
    if result["status"] == "paused":
        store.mark_batch_paused(job_id, batch.batch_no, result["pause_reason"])
        return JobRunSummary(job_id, "paused", batch.batch_no)
    write_review_list(batch_output_dir, result["documents"])
    store.mark_batch_exported(job_id, batch.batch_no, result["summary"])
    write_job_index(job_root, store.job_summary(job_id))
```

`write_job_index` must atomically write `runs/<job_id>/任务总览.html` and `任务总览.json` after every exported or paused batch.

- [ ] **Step 4: Run sequencing and report-isolation tests**

Add a test with 201 products and fake one-image OCR success per product. Assert the three output directories are `0001`, `0002`, and `0003`, each report lists only that batch's products, and total index lists 201 products. Run:

```powershell
D:\OCR_v6\.venv\Scripts\python.exe -m unittest test_batch_worker -v
```

Expected: PASS.

- [ ] **Step 5: Write and run interrupted-worker resume test**

Simulate `KeyboardInterrupt` after batch 1 export and before batch 2 export. Start a second worker with the same job id; assert it calls only batch 2 and does not rewrite `batches/0001/report.html`.

Expected: PASS.

### Task 5: Add the final review-image retry and non-destructive merge

**Files:**
- Create: `D:\OCR_v6\retry_review.py`
- Create: `D:\OCR_v6\test_retry_review.py`
- Modify: `D:\OCR_v6\batch_worker.py`

**Interfaces:**
- Consumes: exported product JSON files and every `待复核图片.json` after all standard batches are exported.
- Produces: `run_retry_review(job_id, ...) -> RetrySummary`, `runs/<job_id>/retry-review/`, and `runs/<job_id>/最终合并结果/`.
- Merge contract: `merge_missing_fields(original, retry)` returns a copy of `original`; it may fill only an empty/`None` field or a field whose source status is `review`, and must not replace a non-empty confirmed value.

- [ ] **Step 1: Write the failing non-overwrite merge test**

Add:

```python
def test_retry_merge_fills_blank_review_field_without_overwriting_confirmed_field() -> None:
    original = {"fields": {"规格": "60粒", "产地": None}, "field_status": {"规格": "confirmed", "产地": "review"}}
    retry = {"fields": {"规格": "90粒", "产地": "日本"}}
    merged = merge_missing_fields(original, retry)
    assert merged["fields"] == {"规格": "60粒", "产地": "日本"}
    assert merged["field_status"]["产地"] == "retry_filled"
```

- [ ] **Step 2: Run the test and verify it fails because the merge function does not exist**

Run: `D:\OCR_v6\.venv\Scripts\python.exe -m unittest test_retry_review.RetryReviewTests.test_retry_merge_fills_blank_review_field_without_overwriting_confirmed_field -v`

Expected: FAIL with an import or name error for `merge_missing_fields`.

- [ ] **Step 3: Implement retry candidate collection and merge**

Read only image names with terminal review statuses from exported batch artifacts. For each product, run OCR only for those images, call Qwen analysis with its existing product document as context, and write a retry product document separate from the original. Use `merge_missing_fields` for the final document; never mutate a batch's original `products/<product_id>.json`.

- [ ] **Step 4: Run the merge test and a retry-isolation integration test**

Add an integration test with one exported product containing one successful image and one `review_timeout` image. Fake retry OCR succeeds; assert the retry call receives only the failed filename, `最终合并结果/<product_id>.json` fills one blank field, and the original batch JSON bytes remain unchanged. Run:

```powershell
D:\OCR_v6\.venv\Scripts\python.exe -m unittest test_retry_review -v
```

Expected: PASS.

### Task 6: Extend the GUI into a job monitor and detached launcher

**Files:**
- Modify: `D:\OCR_v6\直接用图片测试.py`
- Create: `D:\OCR_v6\test_frontend_jobs.py`
- Modify: `D:\OCR_v6\README.md`

**Interfaces:**
- Consumes: current validated connection settings, selected root/platform, `BatchJobStore` summaries, and `batch_worker.py --job`.
- Produces: buttons labelled `创建 10,000 商品任务`, `后台开始`, `暂停`, `继续未完成任务`, and a non-editable progress area that shows job id, current batch, exported batches, product/image success/review counts, worker state, and links to latest batch report/task index.

- [ ] **Step 1: Write the failing detached-command test**

Add:

```python
def test_background_worker_command_has_job_and_state_without_secrets() -> None:
    command = gui.build_batch_worker_command("job-123", Path(r"D:\OCR_v6\.state"))
    assert command[-4:] == ["--job", "job-123", "--state", r"D:\OCR_v6\.state"]
    assert not any("key" in item.casefold() or "password" in item.casefold() for item in command)
```

- [ ] **Step 2: Run the test and verify it fails because the command builder does not exist**

Run: `D:\OCR_v6\.venv\Scripts\python.exe -m unittest test_frontend_jobs.FrontendJobTests.test_background_worker_command_has_job_and_state_without_secrets -v`

Expected: FAIL with an import or name error for `build_batch_worker_command`.

- [ ] **Step 3: Implement job creation, detached worker launch, and polling**

`create_batch_job` stores a DPAPI-protected configuration snapshot in the job store; the child command receives only job id and state path. Launch it with `subprocess.Popen(..., creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP, close_fds=True)`. The GUI polls SQLite every 1 second using `root.after(1000, refresh_job_status)`; do not read worker stdout to decide state.

- [ ] **Step 4: Implement pause and resume semantics**

`暂停` writes `pause_requested=1`; worker checks it between completed images and before a new batch, writes a pause report, and exits with status `paused`. `继续未完成任务` clears the request and starts a new detached worker only when no live worker PID is recorded. Do not kill a worker directly from the GUI.

- [ ] **Step 5: Run GUI command and existing frontend tests**

Run:

```powershell
D:\OCR_v6\.venv\Scripts\python.exe -m unittest test_frontend_jobs test_frontend_speed -v
D:\OCR_v6\.venv\Scripts\python.exe D:\OCR_v6\直接用图片测试.py --self-check
```

Expected: PASS; no secret is present in command arguments or status display.

### Task 7: Verify end-to-end behavior and prepare the 100-product pilot

**Files:**
- Modify: `D:\OCR_v6\README.md`
- Modify: `D:\OCR_v6\测试记录.md`

**Interfaces:**
- Consumes: all v6 tests, launcher, and a user-selected product root.
- Produces: documented v6 launch/continue procedure and an evidence record for the first 100-product pilot.

- [ ] **Step 1: Run the complete automated suite**

Run:

```powershell
D:\OCR_v6\.venv\Scripts\python.exe -m unittest test_batch_jobs test_batch_worker test_retry_review test_frontend_jobs test_performance test_progressive_ocr test_vllm_adapter test_frontend_speed -q
D:\OCR_v6\run_demo.ps1 -SelfTest
```

Expected: all tests pass and `SELF_TEST_OK` is printed.

- [ ] **Step 2: Perform a local mocked 201-product end-to-end run**

Run the batch worker with deterministic mocked OCR/Qwen configuration. Verify three strict batch directories, a task overview, a retry-review directory, and a final merged directory exist. Verify batch 2 starts only after batch 1 report exists by reading `job_events` ordering.

- [ ] **Step 3: Document the user pilot procedure**

In `README.md`, give these exact user-facing steps: start `D:\OCR_v6\启动OCR_Demo.cmd`; choose a root containing at least 100 products; create the batch job; start it in background; close/reopen the window to inspect status; use continue after PC restart; open `runs/<job_id>/任务总览.html`; after every standard batch finishes, inspect `retry-review` and `最终合并结果`.

- [ ] **Step 4: Record verification and pilot expectations**

Append test results plus the explicit performance caveat to `测试记录.md`: Qwen-VL remains the bottleneck; v6 improves completion, isolation, and recoverability rather than guaranteeing a 5-second per-image latency.

## Plan self-review

- Spec coverage: Tasks 2 and 4 cover immutable 100-product strict batching, checkpoints, and isolated outputs; Task 3 covers OCR/Redshift fault isolation; Task 5 covers all-job-complete retry and non-destructive matching; Task 6 covers background local execution and UI monitoring; Task 7 covers acceptance verification.
- Placeholder scan: no open implementation placeholders are present.
- Interface consistency: `BatchJobStore` owns job/batch state, `batch_worker.run_job` owns sequencing, and `retry_review.merge_missing_fields` owns final-field merge. Later tasks use these exact names.
