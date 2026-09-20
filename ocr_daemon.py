"""OCR v7 Linux 服务器后台运行入口。

把一个批次目录（其下是 ``product_id`` 子目录）交给 :func:`demo.execute_pipeline`
跑完，把结果写到 ``runs/<批次名>/<时间戳-运行ID>/``，并在每次运行结束后按结果
发邮件报警（成功汇总 / 待复核 / 失败）。

两种运行方式::

    # 常驻：每 60 秒扫描一次收件目录，有新批次就跑
    python ocr_daemon.py --input-root /data/ocr/inbox --platform jd

    # 只跑一轮就退出（适合挂 cron 或 systemd timer）
    python ocr_daemon.py --input-root /data/ocr/inbox --platform jd --once

进度、断点续跑、缓存等能力全部复用 demo.py 里的 SQLite 状态库，
所以服务被 kill 掉之后再启动，已完成的图片不会重复付费。

设计要点（相对直接跑 ``demo.py`` 的差别）：

* **完全非交互**：不读 stdin，所有确认走环境变量（``OCR_ASSUME_YES``）。
  服务账号没有 tty，任何 ``input()`` 都会直接卡死。
* **批次级台账**：``.state/daemon/ledger.json`` 记录每个批次的状态和清单指纹，
  同一批次不会重复跑；批次目录里加了新图片（指纹变化）会自动重跑。
* **优雅退出**：收到 SIGTERM/SIGINT 后等当前批次跑完再退出，不会留下半截状态；
  再收到一次信号则立即退出。
* **失败退避**：OCR 服务中断 / 网络中断会在退避时间后自动重试，直到成功或
  达到 ``--max-attempts`` 上限。
* **邮件报警**：见 :mod:`email_alert`。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

import demo
import email_alert

APP_DIR = Path(__file__).resolve().parent
LOGGER = logging.getLogger("ocr.daemon")
CST = timezone(timedelta(hours=8))

#: 守护进程运行前必须存在的环境变量（值不能为空）。
REQUIRED_ENV = (
    "DASHSCOPE_API_KEY",
    "POSTGRES_HOST",
    "POSTGRES_DATABASE",
    "POSTGRES_USER",
    "POSTGRES_PASSWORD",
)

#: 必须由运维在环境文件里显式写死的一次性安全确认。
#: 这里刻意不给默认值——如果守护进程自动填 YES，等于把 demo.py 里那道
#: 「确认泄露的阿里 Key 已轮换」的人工闸门悄悄拆掉。
REQUIRED_CONFIRMATIONS = {
    "OCR_DEMO_KEYS_ROTATED": "确认此前截图 / KNIME 脚本里泄露的阿里 DashScope Key 已吊销并轮换",
}

#: 这些错误码说明外部服务暂时不可用，应该退避后重试而不是判定批次失败。
#: PADDLE_OCR_INTERRUPTED 来自 demo.raise_paddle_manual_resume_required；
#: VLLM_NETWORK_OUTAGE 来自 demo.run_ocr_stage 的连续网络失败保护。
RETRYABLE_ERROR_CODES = {
    "PADDLE_OCR_INTERRUPTED",
    "VLLM_NETWORK_OUTAGE",
}

LEDGER_VERSION = 1


# --------------------------------------------------------------------------- #
# 日志
# --------------------------------------------------------------------------- #
def configure_logging(log_file: Path | None, *, verbose: bool = False) -> None:
    """同时输出到 stdout（systemd 收进 journal）和日志文件。"""
    level = logging.DEBUG if verbose else logging.INFO
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_file:
        try:
            log_file.parent.mkdir(parents=True, exist_ok=True)
            handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
        except OSError as exc:
            print(f"警告：无法写入日志文件 {log_file}：{exc}", file=sys.stderr)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s [%(name)s] %(message)s",
        handlers=handlers,
        force=True,
    )
    # demo.py 用 print 打进度，这里把它的输出也标注一下来源，方便 grep。
    demo.progress = lambda message: LOGGER.info("[pipeline] %s", message)  # type: ignore[assignment]


# --------------------------------------------------------------------------- #
# PID 文件
# --------------------------------------------------------------------------- #
class AlreadyRunning(RuntimeError):
    pass


def acquire_pid_file(path: Path) -> None:
    """写 PID 文件；若已有存活进程占用则抛 :class:`AlreadyRunning`。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        try:
            existing = int(path.read_text(encoding="utf-8").strip() or 0)
        except (OSError, ValueError):
            existing = 0
        if existing > 0 and existing != os.getpid() and _process_alive(existing):
            raise AlreadyRunning(f"已有守护进程在运行（PID {existing}），拒绝重复启动")
        LOGGER.warning("发现残留 PID 文件（PID %s 已不存在），继续启动", existing or "?")
    path.write_text(str(os.getpid()), encoding="utf-8")


def release_pid_file(path: Path) -> None:
    try:
        if path.exists() and path.read_text(encoding="utf-8").strip() == str(os.getpid()):
            path.unlink()
    except OSError:
        pass


def _process_alive(pid: int) -> bool:
    """判断 PID 是否存活；Linux 上用 ``os.kill(pid, 0)``。"""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # 进程存在但不是当前用户
    except OSError:
        return False
    return True


# --------------------------------------------------------------------------- #
# 优雅退出
# --------------------------------------------------------------------------- #
class StopController:
    """收集 SIGTERM / SIGINT。

    第一次收到信号：请求停止（等当前批次跑完）。
    第二次收到信号：立即抛出 :class:`KeyboardInterrupt` 强行退出。
    """

    def __init__(self) -> None:
        self.requested = False
        self._count = 0

    def install(self) -> None:
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(sig, self._handle)
            except (ValueError, OSError):  # 非主线程 / 平台不支持
                pass

    def _handle(self, signum: int, _frame: Any) -> None:
        self._count += 1
        name = signal.Signals(signum).name
        if self._count == 1:
            self.requested = True
            LOGGER.warning("收到 %s：当前批次跑完后将退出（再发一次信号可立即退出）", name)
        else:
            LOGGER.error("再次收到 %s：立即退出", name)
            raise KeyboardInterrupt(name)

    def sleep(self, seconds: float) -> bool:
        """可被打断的 sleep；返回 False 表示应当退出。"""
        deadline = time.monotonic() + max(0.0, seconds)
        while time.monotonic() < deadline:
            if self.requested:
                return False
            time.sleep(min(1.0, max(0.0, deadline - time.monotonic())))
        return not self.requested


# --------------------------------------------------------------------------- #
# 批次台账
# --------------------------------------------------------------------------- #
@dataclass
class BatchRecord:
    batch_key: str
    path: str
    manifest_hash: str = ""
    status: str = "pending"
    attempts: int = 0
    run_id: str = ""
    finished_at: str = ""
    success: int = 0
    review: int = 0
    last_error: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "manifest_hash": self.manifest_hash,
            "status": self.status,
            "attempts": self.attempts,
            "run_id": self.run_id,
            "finished_at": self.finished_at,
            "success": self.success,
            "review": self.review,
            "last_error": self.last_error,
        }


class BatchLedger:
    """记录每个批次的处理结果，保证「同一批次不重复跑」。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.records: dict[str, BatchRecord] = {}
        self.load()

    def load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(raw, dict):
            return
        for key, value in (raw.get("batches") or {}).items():
            if not isinstance(value, dict):
                continue
            self.records[str(key)] = BatchRecord(
                batch_key=str(key),
                path=str(value.get("path") or ""),
                manifest_hash=str(value.get("manifest_hash") or ""),
                status=str(value.get("status") or "pending"),
                attempts=int(value.get("attempts") or 0),
                run_id=str(value.get("run_id") or ""),
                finished_at=str(value.get("finished_at") or ""),
                success=int(value.get("success") or 0),
                review=int(value.get("review") or 0),
                last_error=str(value.get("last_error") or ""),
            )

    def save(self) -> None:
        payload = {
            "version": LEDGER_VERSION,
            "updated_at": datetime.now(CST).isoformat(),
            "batches": {key: record.to_json() for key, record in sorted(self.records.items())},
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self.path)
        except OSError as exc:
            LOGGER.error("台账写入失败 %s：%s", self.path, exc)

    def get(self, batch_key: str, path: Path) -> BatchRecord:
        record = self.records.get(batch_key)
        if record is None:
            record = BatchRecord(batch_key=batch_key, path=str(path))
            self.records[batch_key] = record
        return record

    def should_run(self, record: BatchRecord, manifest_hash: str, *, retry_review: bool, max_attempts: int) -> tuple[bool, str]:
        """判断批次是否需要跑，返回 (是否跑, 原因)。"""
        if record.status == "complete" and record.manifest_hash == manifest_hash:
            return False, "已成功处理且图片清单未变化"
        if record.status == "complete" and record.manifest_hash != manifest_hash:
            return True, "批次目录内容已变化"
        if record.status in {"pending", "failed", "interrupted"}:
            if record.attempts >= max_attempts:
                return False, f"已尝试 {record.attempts} 次，达到上限 {max_attempts}"
            return True, "首次处理或上次失败"
        if record.status == "review":
            if not retry_review:
                return False, "需人工复核且已关闭自动重试"
            if record.attempts >= max_attempts:
                return False, f"待复核批次已重试 {record.attempts} 次，达到上限 {max_attempts}"
            return True, "上次有待复核图片，尝试补齐"
        return True, f"未知状态 {record.status}"


# --------------------------------------------------------------------------- #
# 批次发现
# --------------------------------------------------------------------------- #
def discover_batches(input_root: Path) -> list[Path]:
    """收件目录下「包含至少一个商品子目录」的一级目录，就是一个批次。"""
    if not input_root.is_dir():
        raise demo.DemoError("INPUT_ROOT_NOT_FOUND", f"收件目录不存在: {input_root}")
    batches: list[Path] = []
    for child in sorted(input_root.iterdir(), key=lambda item: demo.natural_key(item.name)):
        if not child.is_dir():
            continue
        try:
            if demo.product_candidates(child):
                batches.append(child)
        except OSError as exc:
            LOGGER.warning("扫描 %s 失败：%s", child, exc)
    return batches


def plan_chunks(products: list[Path], chunk_size: int) -> list[list[Path]]:
    size = max(1, min(chunk_size, demo.MAX_PRODUCTS))
    return [products[index:index + size] for index in range(0, len(products), size)]


# --------------------------------------------------------------------------- #
# 单个批次执行
# --------------------------------------------------------------------------- #
@dataclass
class RunOptions:
    input_root: Path
    platform: str
    template_path: Path
    state_dir: Path
    output_root: Path
    ocr_workers: int
    bulk_first_pass: bool
    chunk_size: int
    dry_run: bool
    retryable_backoff: float
    max_attempts: int
    retry_review: bool
    mail: email_alert.MailConfig = field(default_factory=email_alert.mail_config)


def preflight_env(mail: email_alert.MailConfig) -> list[str]:
    """检查必需环境变量与安全确认项，返回有问题的项列表。"""
    missing = [name for name in REQUIRED_ENV if not os.environ.get(name, "").strip()]
    unconfirmed = [
        name
        for name in REQUIRED_CONFIRMATIONS
        if os.environ.get(name, "").strip().upper() not in {"YES", "ROTATED"}
    ]
    if not missing and not unconfirmed:
        return []
    lines: list[str] = []
    if missing:
        lines.append("缺失的环境变量：" + "、".join(missing))
    if unconfirmed:
        lines.append("尚未确认的安全项：")
        for name in unconfirmed:
            lines.append(f"  {name} —— {REQUIRED_CONFIRMATIONS[name]}")
        lines.append("确认后在环境文件里写入 OCR_DEMO_KEYS_ROTATED=YES")
    lines.append("")
    lines.append("请补齐 /etc/ocr-v7/ocr-daemon.env 后重启服务。")
    LOGGER.error("启动前检查未通过：%s", "；".join(lines))
    email_alert.notify("后台服务启动失败：配置不完整", lines, severity="critical", config=mail)
    return missing + unconfirmed


def build_pipeline_config(manifest: dict[str, Any], options: RunOptions) -> dict[str, Any]:
    """构造一次性的管线配置（非交互）。"""
    config = demo.real_config(manifest)
    config["ocr_workers"] = options.ocr_workers
    config["paddle_workers"] = min(2, options.ocr_workers)
    config["force_ocr"] = False
    config["bulk_first_pass"] = options.bulk_first_pass
    config["include_hot_topics"] = not options.bulk_first_pass
    return config


def _reset_per_run_state(config: dict[str, Any]) -> None:
    """``execute_pipeline`` 会往 config 里写运行期数据；跑下一批前必须清掉。

    特别是 ``metrics``：它是个累加字典，复用 config 会把上一批的 API 调用次数
    带到下一批的 performance.json 里。
    """
    config["metrics"] = {}
    config.pop("run_output_dir", None)


def run_one_batch(
    batch_dir: Path,
    options: RunOptions,
    *,
    config: dict[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """跑完一个批次目录（内部按 chunk 分批调用管线）。

    返回 ``(汇总结果, 可复用的管线配置)``。
    """
    batch_key = batch_dir.name
    batch_output_root = options.output_root / batch_key
    products = demo.product_candidates(batch_dir)
    if not products:
        raise demo.DemoError("NO_PRODUCTS", f"批次 {batch_key} 下没有商品目录")

    chunks = plan_chunks(products, options.chunk_size)
    LOGGER.info(
        "批次 %s：%d 个商品，分成 %d 个批次块（每块最多 %d 个）",
        batch_key,
        len(products),
        len(chunks),
        min(options.chunk_size, demo.MAX_PRODUCTS),
    )
    if options.dry_run:
        return (
            {
                "status": "dry-run",
                "batch_key": batch_key,
                "product_count": len(products),
                "chunk_count": len(chunks),
                "output_dir": str(batch_output_root),
            },
            config or {},
        )

    documents: list[dict[str, Any]] = []
    run_ids: list[str] = []
    total_ms = 0
    ocr_totals = {"planned_images": 0, "success_images": 0, "failed_images": 0, "cache_hits": 0}
    last_output_dir = batch_output_root
    for index, chunk in enumerate(chunks, start=1):
        manifest = demo.build_manifest(batch_dir, chunk, options.platform)
        if config is None:
            # 只构造一次，后续批次块复用（凭据、地址、schema 都与商品无关）。
            config = build_pipeline_config(manifest, options)
        _reset_per_run_state(config)
        LOGGER.info(
            "批次 %s 第 %d/%d 块：%d 个商品，清单指纹 %s",
            batch_key,
            index,
            len(chunks),
            len(chunk),
            manifest["manifest_hash"][:12],
        )
        result = demo.execute_pipeline(
            manifest,
            options.template_path,
            options.state_dir / "pipeline",
            batch_output_root,
            config,
            force_new=True,
        )
        run_ids.append(str(result.get("run_id") or ""))
        last_output_dir = Path(str(result.get("output_dir") or batch_output_root))
        total_ms += int((result.get("performance") or {}).get("total_ms") or 0)
        ocr = (result.get("performance") or {}).get("ocr") or {}
        for key in ocr_totals:
            ocr_totals[key] += int(ocr.get(key) or 0)
        documents.append(result)

    success = sum(int(item.get("success_count") or 0) for item in documents)
    review = sum(int(item.get("review_count") or 0) for item in documents)
    statuses = {str(item.get("status") or "unknown") for item in documents}
    if "failed" in statuses:
        overall = "failed"
    elif statuses == {"complete"}:
        overall = "complete"
    else:
        overall = "review"
    return (
        {
            "status": overall,
            "batch_key": batch_key,
            "run_id": ",".join(item for item in run_ids if item),
            "output_dir": str(last_output_dir),
            "success_count": success,
            "review_count": review,
            "workbook": next(
                (item.get("workbook") for item in reversed(documents) if item.get("workbook")), None
            ),
            "performance": {
                "total_ms": total_ms,
                "ocr": ocr_totals,
            },
            "chunks": [
                {
                    "run_id": item.get("run_id"),
                    "status": item.get("status"),
                    "success_count": item.get("success_count"),
                    "review_count": item.get("review_count"),
                    "output_dir": item.get("output_dir"),
                }
                for item in documents
            ],
        },
        config or {},
    )


# --------------------------------------------------------------------------- #
# 循环
# --------------------------------------------------------------------------- #
def process_cycle(
    options: RunOptions,
    ledger: BatchLedger,
    stopper: StopController,
    *,
    config_holder: dict[str, Any],
) -> dict[str, int]:
    """扫描一轮收件目录并处理需要跑的批次。"""
    stats = {"scanned": 0, "ran": 0, "skipped": 0, "failed": 0, "paused": False}
    batches = discover_batches(options.input_root)
    stats["scanned"] = len(batches)
    if not batches:
        LOGGER.info("收件目录 %s 暂无批次", options.input_root)
        return stats

    for batch_dir in batches:
        if stopper.requested:
            LOGGER.info("已请求停止，剩余批次留待下次处理")
            break
        batch_key = batch_dir.name
        record = ledger.get(batch_key, batch_dir)
        try:
            products = demo.product_candidates(batch_dir)
            manifest_hash = demo.stable_hash(
                [
                    {
                        "product_id": demo.canonical_id(path.name),
                        "images": [
                            (image.relative_to(path).as_posix(), demo.file_sha256(image))
                            for image in demo.find_images(path)
                        ],
                    }
                    for path in products
                ]
            )
        except (OSError, demo.DemoError) as exc:
            LOGGER.warning("批次 %s 扫描失败：%s", batch_key, exc)
            record.status = "failed"
            record.last_error = f"扫描失败: {exc}"
            ledger.save()
            stats["failed"] += 1
            continue

        should_run, reason = ledger.should_run(
            record,
            manifest_hash,
            retry_review=options.retry_review,
            max_attempts=options.max_attempts,
        )
        if not should_run:
            LOGGER.info("跳过批次 %s：%s", batch_key, reason)
            stats["skipped"] += 1
            continue

        LOGGER.info("处理批次 %s（%s）", batch_key, reason)
        record.attempts += 1
        record.manifest_hash = manifest_hash
        record.status = "running"
        ledger.save()
        started = time.time()
        try:
            result, config_holder["config"] = run_one_batch(
                batch_dir, options, config=config_holder.get("config")
            )
        except KeyboardInterrupt:
            record.status = "interrupted"
            record.last_error = "收到中断信号"
            ledger.save()
            raise
        except demo.DemoError as exc:
            code = str(getattr(exc, "code", "DEMO_ERROR"))
            message = demo.safe_message(exc)
            record.status = "interrupted" if code in RETRYABLE_ERROR_CODES else "failed"
            record.last_error = f"{code}: {message}"
            ledger.save()
            stats["failed"] += 1
            LOGGER.error("批次 %s 失败 [%s]：%s", batch_key, code, message)
            # execute_pipeline 在开跑前就把 run_output_dir 写进了 config，
            # 所以即使抛异常也能定位到这次的产物目录。
            failed_output = Path(
                str((config_holder.get("config") or {}).get("run_output_dir") or record.path)
            )
            retryable = code in RETRYABLE_ERROR_CODES and record.attempts < options.max_attempts
            email_alert.notify_pipeline_failure(
                batch_label=batch_key,
                error_code=code,
                message=message,
                output_dir=failed_output,
                retry_in_seconds=options.retryable_backoff if retryable else None,
                config=options.mail,
            )
            if retryable:
                stats["paused"] = True
                if not stopper.sleep(options.retryable_backoff):
                    break
                continue
            continue
        except Exception as exc:  # noqa: BLE001 - 守护进程必须活下来
            record.status = "failed"
            record.last_error = f"未处理异常: {exc}"
            ledger.save()
            stats["failed"] += 1
            LOGGER.error("批次 %s 出现未处理异常：\n%s", batch_key, traceback.format_exc())
            email_alert.notify_pipeline_failure(
                batch_label=batch_key,
                error_code="UNHANDLED_EXCEPTION",
                message=f"{type(exc).__name__}: {exc}",
                output_dir=Path(record.path),
                config=options.mail,
            )
            continue

        elapsed = time.time() - started
        record.status = str(result.get("status") or "failed")
        record.run_id = str(result.get("run_id") or "")
        record.success = int(result.get("success_count") or 0)
        record.review = int(result.get("review_count") or 0)
        record.finished_at = datetime.now(CST).isoformat()
        record.last_error = ""
        ledger.save()
        stats["ran"] += 1
        LOGGER.info(
            "批次 %s 完成：status=%s 成功=%d 待复核=%d 耗时=%.1f 分钟",
            batch_key,
            record.status,
            record.success,
            record.review,
            elapsed / 60,
        )
        if options.dry_run:
            LOGGER.info("dry-run 模式：不发邮件")
            continue
        email_alert.notify_pipeline_result(result, batch_label=batch_key, config=options.mail)
    return stats


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="OCR v7 后台运行入口（Linux 服务器 / systemd）"
    )
    parser.add_argument(
        "--input-root",
        default=os.environ.get("OCR_INPUT_ROOT", str(APP_DIR / "inbox")),
        help="收件根目录：其下每个子目录算一个批次，批次下是 product_id 目录",
    )
    parser.add_argument(
        "--platform",
        default=os.environ.get("OCR_DEMO_PLATFORM", ""),
        help="数据库平台标识（jd / tmall ...），必填",
    )
    parser.add_argument("--template", default=str(APP_DIR / "template-v2.json"))
    parser.add_argument("--state-dir", default=str(APP_DIR / ".state"))
    parser.add_argument("--output-root", default=str(APP_DIR / "runs"))
    parser.add_argument("--once", action="store_true", help="只扫描一轮就退出")
    parser.add_argument("--interval", type=float, default=60.0, help="轮询间隔秒数，默认 60")
    parser.add_argument(
        "--error-backoff", type=float, default=300.0, help="外部服务不可用时的退避秒数，默认 300"
    )
    parser.add_argument("--max-attempts", type=int, default=3, help="单个批次最多尝试次数，默认 3")
    parser.add_argument(
        "--no-retry-review", action="store_true", help="待复核批次不自动重试"
    )
    parser.add_argument(
        "--ocr-workers",
        type=int,
        choices=range(1, demo.OCR_MAX_WORKERS + 1),
        default=int(os.environ.get("OCR_WORKERS", "1")),
    )
    parser.add_argument("--bulk-first-pass", action="store_true", help="跳过联网搜索，只跑 OCR + 数据库")
    parser.add_argument(
        "--chunk-size", type=int, default=demo.MAX_PRODUCTS, help=f"每个批次块最多几个商品（上限 {demo.MAX_PRODUCTS}）"
    )
    parser.add_argument("--pid-file", default="", help="PID 文件路径，默认 <state-dir>/daemon/ocr-daemon.pid")
    parser.add_argument("--log-file", default="", help="日志文件路径，默认 <state-dir>/daemon/ocr-daemon.log")
    parser.add_argument("--dry-run", action="store_true", help="只列出待处理批次，不真正运行")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    state_dir = Path(args.state_dir).resolve()
    output_root = Path(args.output_root).resolve()
    daemon_dir = state_dir / "daemon"
    pid_file = Path(args.pid_file).resolve() if args.pid_file else daemon_dir / "ocr-daemon.pid"
    log_file = Path(args.log_file).resolve() if args.log_file else daemon_dir / "ocr-daemon.log"

    configure_logging(log_file, verbose=args.verbose)

    if not args.platform.strip():
        LOGGER.critical("必须通过 --platform 或 OCR_DEMO_PLATFORM 指定数据库平台标识")
        return 2
    template_path = Path(args.template).resolve()
    if not template_path.is_file():
        LOGGER.critical("模板文件不存在：%s", template_path)
        return 2
    input_root = Path(args.input_root).resolve()

    mail = email_alert.mail_config()
    LOGGER.info("邮件报警配置：%s", mail.summary())
    if args.dry_run:
        # dry-run 不碰任何外部服务，也不该因为缺密钥就报错，更不该发告警邮件。
        LOGGER.info("dry-run 模式：跳过凭据与服务预检")
    elif preflight_env(mail):
        return 2

    options = RunOptions(
        input_root=input_root,
        platform=args.platform.strip(),
        template_path=template_path,
        state_dir=state_dir,
        output_root=output_root,
        ocr_workers=args.ocr_workers,
        bulk_first_pass=args.bulk_first_pass,
        chunk_size=args.chunk_size,
        dry_run=args.dry_run,
        retryable_backoff=args.error_backoff,
        max_attempts=args.max_attempts,
        retry_review=not args.no_retry_review,
        mail=mail,
    )

    # 非交互确认：服务账号没有 tty，任何 input() 都会卡死服务。
    # OCR_DEMO_KEYS_ROTATED 由 preflight_env 强制要求，这里不代填。
    os.environ["OCR_ASSUME_YES"] = "1"
    os.environ["OCR_DEMO_PLATFORM"] = options.platform

    stopper = StopController()
    stopper.install()
    ledger = BatchLedger(daemon_dir / "ledger.json")
    config_holder: dict[str, Any] = {}

    try:
        acquire_pid_file(pid_file)
    except AlreadyRunning as exc:
        LOGGER.critical("%s", exc)
        return 3

    LOGGER.info(
        "守护进程启动：pid=%s 收件目录=%s 平台=%s 模式=%s",
        os.getpid(),
        input_root,
        options.platform,
        "dry-run" if args.dry_run else ("单轮" if args.once else f"常驻 {args.interval}s"),
    )
    if not args.dry_run:
        email_alert.notify(
            "后台服务已启动",
            [
                f"收件目录：{input_root}",
                f"平台标识：{options.platform}",
                f"输出目录：{output_root}",
                f"轮询模式：{'单轮' if args.once else f'每 {args.interval} 秒'}",
            ],
            severity="info",
            config=mail,
        )

    exit_code = 0
    try:
        while True:
            try:
                stats = process_cycle(options, ledger, stopper, config_holder=config_holder)
            except KeyboardInterrupt:
                LOGGER.warning("收到中断，退出")
                raise
            except demo.DemoError as exc:
                LOGGER.error("本轮扫描失败 [%s]：%s", exc.code, demo.safe_message(exc))
                stats = {"failed": 1, "paused": False}
            if args.once:
                LOGGER.info("单轮模式结束：%s", stats)
                exit_code = 1 if stats.get("failed") else 0
                break
            if stopper.requested:
                break
            wait = options.retryable_backoff if stats.get("paused") else args.interval
            LOGGER.info("本轮结束 %s，%s 秒后进入下一轮", stats, int(wait))
            if not stopper.sleep(wait):
                break
    except KeyboardInterrupt:
        LOGGER.warning("守护进程被中断退出")
        exit_code = 130
    finally:
        release_pid_file(pid_file)
        ledger.save()
        LOGGER.info("守护进程已退出：pid=%s", os.getpid())
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
