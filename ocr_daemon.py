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
* **邮件报警**：见 :mod:`email_alert`。会发邮件的情况：批次正常结束（汇总）、
  批次抛错、**没产出 result.csv**、**有商品核心字段全空**、**批次长时间没进度**、
  **内网 OCR 探活失败/恢复**、**服务被中断或上次非正常退出**。
  内网 OCR 有固定停机窗口时用 ``ALERT_OCR_MAINTENANCE_WINDOWS`` 声明，
  窗口内探活失败不算故障。
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import signal
import socket
import sys
import threading
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Sequence
from urllib.parse import urlsplit

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

#: 服务端单个批次块允许的商品数上限。
#: ``demo.MAX_PRODUCTS``(100) 是 demo.py 交互式选商品的限制，服务端没人交互，
#: 这里放宽到 500；相应地 ``build_manifest`` 必须传 ``allow_many=True``。
MAX_CHUNK_SIZE = 500
DEFAULT_CHUNK_SIZE = 500

#: 判定「这个商品到底有没有识别出东西」只看这些核心字段（用户拍板的清单）。
#: 全部为空才算「完全没识别出来」。
CORE_METRIC_FIELDS = (
    "规格",
    "包装",
    "规格总量",
    "最小单位价格",
    "是否多规格",
    "日服量",
    "最小日服量",
    "最大日服量",
    "最小日服成本",
    "最大日服成本",
    "成分",
    "人群",
    "功能",
    "品牌",
    "剂型",
    "蓝帽标识",
    "代工厂",
)

#: CSV 里这些取值等同于「空」。导出时 list/dict 会被 json.dumps 成 "[]" / "{}"。
_BLANK_TOKENS = {"", "[]", "{}", "null", "none"}

#: 批次指纹算法。
#: ``metadata`` = 「相对路径 + 文件大小 + mtime_ns」，只 stat，不读文件内容；
#: ``content``  = 逐字节 sha256，最严格，但要把整个批次目录完整读一遍。
FINGERPRINT_METADATA = "metadata"
FINGERPRINT_CONTENT = "content"


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        LOGGER.warning("环境变量 %s=%r 不是数字，回退为 %s", name, raw, default)
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        LOGGER.warning("环境变量 %s=%r 不是整数，回退为 %s", name, raw, default)
        return default


# --------------------------------------------------------------------------- #
# 日志
# --------------------------------------------------------------------------- #
def configure_logging(
    log_file: Path | None,
    *,
    verbose: bool = False,
    watchdog: ProgressWatchdog | None = None,
) -> None:
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

    # demo.py 用 print 打进度，这里把它的输出也标注一下来源，方便 grep；
    # 同时把进度喂给看门狗作为「还活着」的心跳。
    def _progress(message: str) -> None:
        LOGGER.info("[pipeline] %s", message)
        if watchdog is not None:
            watchdog.beat(str(message))

    demo.progress = _progress  # type: ignore[assignment]


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
# 停机窗口
# --------------------------------------------------------------------------- #
def _parse_hhmm(text: str) -> int | None:
    parts = text.strip().split(":")
    if len(parts) != 2:
        return None
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour * 60 + minute


def parse_maintenance_windows(raw: str) -> tuple[tuple[int, int], ...]:
    """解析 ``14:00-18:00,02:00-02:30`` 形式的 OCR 停机窗口（支持跨零点）。

    内网 OCR 服务每天固定时段关停时，把窗口写进 ``ALERT_OCR_MAINTENANCE_WINDOWS``，
    窗口内探活失败就不发告警，避免每天收到两封无意义的「服务不可达」。
    """
    windows: list[tuple[int, int]] = []
    for chunk in str(raw or "").replace(";", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        left, separator, right = chunk.partition("-")
        start = _parse_hhmm(left) if separator else None
        end = _parse_hhmm(right) if separator else None
        if start is None or end is None:
            LOGGER.warning("停机窗口 %r 无法解析（应为 14:00-18:00），已忽略", chunk)
            continue
        windows.append((start, end))
    return tuple(windows)


def in_maintenance_window(
    windows: Sequence[tuple[int, int]], when: datetime | None = None
) -> bool:
    now = when or datetime.now(CST)
    minute = now.hour * 60 + now.minute
    for start, end in windows:
        if start <= end:
            if start <= minute < end:
                return True
        elif minute >= start or minute < end:  # 跨零点，如 23:30-01:00
            return True
    return False


def _describe_windows(windows: Sequence[tuple[int, int]]) -> str:
    return "、".join(f"{start // 60:02d}:{start % 60:02d}-{end // 60:02d}:{end % 60:02d}" for start, end in windows)


# --------------------------------------------------------------------------- #
# 卡住看门狗
# --------------------------------------------------------------------------- #
class ProgressWatchdog:
    """后台线程盯着「多久没有进度输出」，超时发一次告警。

    只看 ``demo.progress`` 推来的进度；跑完一批就撤防，所以空闲等待新批次时
    不会误报。发出告警后若又来了新进度，会记一条「已恢复」并重新武装，
    下次再卡住还能再报一次。
    """

    def __init__(
        self,
        timeout_seconds: float,
        *,
        interval: float = 30.0,
        notify_fn: Callable[[str, float, str, Path | None], bool] | None = None,
        mail: email_alert.MailConfig | None = None,
    ) -> None:
        self.timeout = float(timeout_seconds)
        self.interval = float(interval)
        self._notify_fn = notify_fn
        self._mail = mail
        self._lock = threading.Lock()
        self._batch = ""
        self._output_dir: Path | None = None
        self._last_progress = 0.0
        self._last_wall = ""
        self._last_message = ""
        self._alerted = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="ocr-watchdog", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=5)

    def begin_batch(self, batch_label: str, output_dir: Path | None = None) -> None:
        with self._lock:
            self._batch = batch_label
            self._output_dir = output_dir
            self._last_progress = time.monotonic()
            self._last_wall = datetime.now(CST).strftime("%H:%M:%S")
            self._last_message = ""
            self._alerted = False

    def end_batch(self) -> None:
        with self._lock:
            self._batch = ""

    def beat(self, message: str = "") -> None:
        with self._lock:
            self._last_progress = time.monotonic()
            self._last_wall = datetime.now(CST).strftime("%H:%M:%S")
            if message:
                self._last_message = str(message)
            if self._alerted:
                self._alerted = False
                LOGGER.info("看门狗：批次恢复有进度输出，重新开始计时")

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            payload: tuple[str, float, str, Path | None] | None = None
            with self._lock:
                if not self._batch or self._alerted:
                    continue
                idle = time.monotonic() - self._last_progress
                if idle < self.timeout:
                    continue
                self._alerted = True
                payload = (
                    self._batch,
                    idle / 60.0,
                    f"{self._last_wall} {self._last_message}".strip(),
                    self._output_dir,
                )
            if payload is None:
                continue
            batch_label, minutes, last_message, output_dir = payload
            LOGGER.error("看门狗：批次 %s 已 %.0f 分钟无进度输出", batch_label, minutes)
            if self._notify_fn is not None:
                self._notify_fn(batch_label, minutes, last_message, output_dir)


# --------------------------------------------------------------------------- #
# 内网 OCR 探活
# --------------------------------------------------------------------------- #
class OcrProbe:
    """定时探测内网 OCR 服务端口，连续失败 / 恢复各发一次告警。"""

    def __init__(
        self,
        url: str,
        *,
        interval: float,
        failures_to_alert: int = 2,
        repeat_after: float = 3600.0,
        maintenance: Sequence[tuple[int, int]] = (),
        mail: email_alert.MailConfig | None = None,
        timeout: float = 5.0,
    ) -> None:
        self.url = url
        self.interval = float(interval)
        self.failures_to_alert = max(1, int(failures_to_alert))
        self.repeat_after = max(0.0, float(repeat_after))
        self.maintenance = tuple(maintenance)
        self.mail = mail
        self.timeout = timeout
        self.host, self.port = self._endpoint(url)
        self._last_probe = 0.0
        self._consecutive_failures = 0
        self._last_alert_at = 0.0
        self._down = False

    @staticmethod
    def _endpoint(url: str) -> tuple[str, int]:
        parts = urlsplit(url if "//" in url else f"//{url}")
        host = parts.hostname or ""
        if parts.port:
            port = parts.port
        else:
            port = 443 if parts.scheme == "https" else 80
        return host, port

    @property
    def enabled(self) -> bool:
        return bool(self.host and self.port)

    def probe_once(self) -> tuple[bool, str]:
        if not self.enabled:
            return False, f"无法从 {self.url!r} 解析出主机端口"
        try:
            with socket.create_connection((self.host, self.port), timeout=self.timeout):
                return True, ""
        except (OSError, socket.timeout) as exc:
            return False, f"{type(exc).__name__}: {exc}"

    def maybe_probe(self, now: float | None = None) -> None:
        """到了探测间隔就探一次；本函数不会抛异常。"""
        stamp = time.monotonic() if now is None else now
        if stamp - self._last_probe < self.interval:
            return
        self._last_probe = stamp
        ok, detail = self.probe_once()

        if ok:
            if self._down:
                LOGGER.info("内网 OCR 服务已恢复：%s", self.url)
                email_alert.notify_ocr_probe(
                    url=self.url,
                    host=self.host,
                    port=self.port,
                    down=False,
                    config=self.mail,
                )
            self._down = False
            self._consecutive_failures = 0
            self._last_alert_at = 0.0
            return

        self._consecutive_failures += 1
        in_window = in_maintenance_window(self.maintenance)
        LOGGER.warning(
            "内网 OCR 探活失败（连续 %d 次，%s）%s",
            self._consecutive_failures,
            detail,
            "；当前在停机窗口内，不发告警" if in_window else "",
        )
        if in_window:
            # 停机窗口内不算故障，并且清掉已告警标记，
            # 这样窗口结束后若还没恢复会立刻再报一次。
            self._down = False
            self._last_alert_at = 0.0
            return
        if self._consecutive_failures < self.failures_to_alert:
            return
        if self._last_alert_at and stamp - self._last_alert_at < self.repeat_after:
            return
        self._last_alert_at = stamp
        self._down = True
        email_alert.notify_ocr_probe(
            url=self.url,
            host=self.host,
            port=self.port,
            down=True,
            consecutive=self._consecutive_failures,
            detail=detail,
            config=self.mail,
        )


# --------------------------------------------------------------------------- #
# 产物审计
# --------------------------------------------------------------------------- #
def _is_blank(value: Any) -> bool:
    text = "" if value is None else str(value).strip()
    return text.lower() in _BLANK_TOKENS


def audit_chunk_output(
    chunk_result: dict[str, Any], *, core_fields: Sequence[str] = CORE_METRIC_FIELDS, min_fields: int = 1
) -> dict[str, Any]:
    """读一个批次块产出的 ``result.csv``，统计行数与「核心字段全空」的商品。

    直接读交付物本身（而不是读内存里的 document），这样「写了但没落盘」
    这类问题也能被发现。本函数不会抛异常。
    """
    csv_path = Path(str(chunk_result.get("csv") or ""))
    report: dict[str, Any] = {
        "run_id": str(chunk_result.get("run_id") or ""),
        "output_dir": str(chunk_result.get("output_dir") or ""),
        "csv": str(csv_path) if str(chunk_result.get("csv") or "") else "",
        "csv_ok": False,
        "rows": 0,
        "empties": [],
    }
    if not report["csv"] or not csv_path.is_file():
        if report["csv"]:
            LOGGER.error("批次块产物缺失：%s 不存在", report["csv"])
        else:
            LOGGER.error("批次块 %s 的结果里没有 csv 字段", report["run_id"] or "?")
        return report

    try:
        with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            headers = [name for name in (reader.fieldnames or []) if name]
            present = [name for name in core_fields if name in headers]
            missing_columns = [name for name in core_fields if name not in headers]
            if missing_columns:
                LOGGER.warning(
                    "CSV %s 缺少核心列 %s，这些列不参与空值判定",
                    csv_path.name,
                    "、".join(missing_columns),
                )
            for row in reader:
                report["rows"] += 1
                product_id = str(row.get("product_id") or "").strip() or f"第{report['rows']}行"
                filled = [name for name in present if not _is_blank(row.get(name))]
                if len(filled) >= max(0, min_fields):
                    continue
                if filled:
                    note = f"只识别出 {len(filled)} 个核心字段（{'、'.join(filled)}）"
                else:
                    note = "核心字段全部为空"
                report["empties"].append({"product_id": product_id, "note": note})
    except (OSError, csv.Error, UnicodeDecodeError) as exc:
        LOGGER.error("读取 %s 失败：%s", csv_path, exc)
        return report

    report["csv_ok"] = True
    return report


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
    size = max(1, min(chunk_size, MAX_CHUNK_SIZE))
    return [products[index:index + size] for index in range(0, len(products), size)]


def batch_fingerprint(
    products: Sequence[Path], mode: str = FINGERPRINT_METADATA
) -> str:
    """算出一个批次的清单指纹，用来判断「同一批次要不要重跑」。

    **默认的 metadata 模式只 stat，不读文件内容。** 这点在大批量下是决定性的：
    一万个商品、几个 G 的图片，如果用 content 模式，每轮扫描（默认 60 秒一次）
    都要把这几 G 完整读一遍，磁盘直接跑满。metadata 模式只做 200k 次 stat，
    通常 1~3 秒就能跑完，代价是「路径/大小/mtime 完全相同的不同文件」
    会被当成没变化 —— 对本场景（上传一次就不再改）足够。
    """
    payload: list[dict[str, Any]] = []
    if mode == FINGERPRINT_CONTENT:
        for path in products:
            payload.append(
                {
                    "product_id": demo.canonical_id(path.name),
                    "images": [
                        (image.relative_to(path).as_posix(), demo.file_sha256(image))
                        for image in demo.find_images(path)
                    ],
                }
            )
        return demo.stable_hash(payload)

    for path in products:
        entries: list[tuple[str, int, int]] = []
        for image in demo.find_images(path):
            try:
                stat = image.stat()
            except OSError:
                continue
            entries.append(
                (image.relative_to(path).as_posix(), stat.st_size, stat.st_mtime_ns)
            )
        payload.append({"product_id": demo.canonical_id(path.name), "images": entries})
    return demo.stable_hash(payload)


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
    fingerprint: str = FINGERPRINT_METADATA


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
    products: list[Path] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """跑完一个批次目录（内部按 chunk 分批调用管线）。

    :param products: 已经扫描好的商品目录列表。``process_cycle`` 扫过一次了，
        这里直接复用，避免一万个商品的大批次被重复遍历整棵树。

    返回 ``(汇总结果, 可复用的管线配置)``。
    """
    batch_key = batch_dir.name
    batch_output_root = options.output_root / batch_key
    if products is None:
        products = demo.product_candidates(batch_dir)
    if not products:
        raise demo.DemoError("NO_PRODUCTS", f"批次 {batch_key} 下没有商品目录")

    chunks = plan_chunks(products, options.chunk_size)
    LOGGER.info(
        "批次 %s：%d 个商品，分成 %d 个批次块（每块最多 %d 个）",
        batch_key,
        len(products),
        len(chunks),
        min(options.chunk_size, MAX_CHUNK_SIZE),
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
        manifest = demo.build_manifest(batch_dir, chunk, options.platform, allow_many=True)
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

    # ---- 产物审计：CSV 有没有落盘、有没有数据、有没有商品全空 ----
    min_fields = email_alert.empty_product_min_fields()
    audits = [audit_chunk_output(item, min_fields=min_fields) for item in documents]
    csv_files = [audit["csv"] for audit in audits if audit["csv_ok"]]
    csv_missing = [
        f"第 {index}/{len(chunks)} 块（run {audit['run_id'] or '?'}）："
        f"{audit['csv'] or '结果里没有 csv 路径'}（输出目录 {audit['output_dir'] or '-'}）"
        for index, audit in enumerate(audits, start=1)
        if not audit["csv_ok"]
    ]
    csv_rows = sum(int(audit["rows"]) for audit in audits)
    empty_products = [
        {
            "product_id": item["product_id"],
            "note": item["note"],
            "best_run_id": audit["run_id"],
        }
        for audit in audits
        for item in audit["empties"]
    ]
    if csv_missing:
        LOGGER.error("批次 %s 有 %d 个批次块没产出 CSV", batch_key, len(csv_missing))
    if empty_products:
        LOGGER.warning("批次 %s 有 %d 个商品核心字段全空", batch_key, len(empty_products))

    return (
        {
            "status": overall,
            "batch_key": batch_key,
            "run_id": ",".join(item for item in run_ids if item),
            "output_dir": str(last_output_dir),
            "success_count": success,
            "review_count": review,
            "product_count": len(products),
            "csv_files": csv_files,
            "csv_missing": csv_missing,
            "csv_rows": csv_rows,
            "empty_products": empty_products,
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
    watchdog: ProgressWatchdog | None = None,
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
            scan_started = time.perf_counter()
            products = demo.product_candidates(batch_dir)
            manifest_hash = batch_fingerprint(products, options.fingerprint)
            scan_seconds = time.perf_counter() - scan_started
            if scan_seconds > 5:
                LOGGER.warning(
                    "批次 %s 扫描耗时 %.1f 秒（%d 个商品，指纹模式=%s）",
                    batch_key,
                    scan_seconds,
                    len(products),
                    options.fingerprint,
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
        if watchdog is not None:
            watchdog.begin_batch(batch_key, options.output_root / batch_key)
        try:
            result, config_holder["config"] = run_one_batch(
                batch_dir, options, config=config_holder.get("config"), products=products
            )
        except KeyboardInterrupt:
            if watchdog is not None:
                watchdog.end_batch()
            record.status = "interrupted"
            record.last_error = "收到中断信号"
            ledger.save()
            LOGGER.warning("批次 %s 被中断，产物已落盘，可断点续跑", batch_key)
            email_alert.notify_daemon_stopped(
                reason=f"批次 {batch_key} 运行中被中断，服务即将退出",
                severity="error",
                batch_label=batch_key,
                batch_status="interrupted",
                output_dir=Path(
                    str(
                        (config_holder.get("config") or {}).get("run_output_dir")
                        or (options.output_root / batch_key)
                    )
                ),
                config=options.mail,
            )
            raise
        except demo.DemoError as exc:
            if watchdog is not None:
                watchdog.end_batch()
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
            if watchdog is not None:
                watchdog.end_batch()
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
        if watchdog is not None:
            watchdog.end_batch()
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
        # 用户点名的两类报警：结果文件没生成、商品什么都没识别出来。
        # 这两类各发一封独立邮件（汇总邮件正文里只留摘要，避免重复）。
        if result.get("csv_missing"):
            email_alert.notify_artifacts_missing(
                batch_label=batch_key,
                missing=result["csv_missing"],
                expected=len(result.get("chunks") or []),
                output_dir=Path(str(result.get("output_dir") or record.path)),
                config=options.mail,
            )
        if result.get("empty_products"):
            email_alert.notify_products_empty(
                batch_label=batch_key,
                empty_products=result["empty_products"],
                total_products=int(result.get("product_count") or 0),
                output_dir=Path(str(result.get("output_dir") or record.path)),
                config=options.mail,
            )
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
        "--chunk-size",
        type=int,
        default=_env_int("OCR_CHUNK_SIZE", DEFAULT_CHUNK_SIZE),
        help=f"每个批次块最多几个商品（上限 {MAX_CHUNK_SIZE}），默认 {DEFAULT_CHUNK_SIZE}",
    )
    parser.add_argument(
        "--fingerprint",
        choices=(FINGERPRINT_METADATA, FINGERPRINT_CONTENT),
        default=os.environ.get("OCR_FINGERPRINT", FINGERPRINT_METADATA).strip().lower()
        or FINGERPRINT_METADATA,
        help=(
            "批次变更判定方式：metadata（默认，只 stat，适合大批量）"
            "或 content（逐字节 sha256，慢但最严格）"
        ),
    )
    parser.add_argument(
        "--stuck-minutes",
        type=float,
        default=_env_float("OCR_STUCK_MINUTES", 30.0),
        help="批次连续多少分钟没有进度就发一次「疑似卡住」告警，默认 30",
    )
    parser.add_argument("--no-stuck-alert", action="store_true", help="关闭「卡住」看门狗")
    parser.add_argument(
        "--ocr-probe-url",
        default=os.environ.get("PADDLE_OCR_API_URL", ""),
        help="内网 OCR 服务地址（探活用），默认取 PADDLE_OCR_API_URL",
    )
    parser.add_argument(
        "--ocr-probe-interval",
        type=float,
        default=_env_float("OCR_PROBE_INTERVAL", 600.0),
        help="OCR 探活间隔秒数，默认 600",
    )
    parser.add_argument(
        "--ocr-probe-failures",
        type=int,
        default=_env_int("OCR_PROBE_FAILURES", 2),
        help="连续失败几次才告警，默认 2",
    )
    parser.add_argument(
        "--ocr-probe-repeat",
        type=float,
        default=_env_float("OCR_PROBE_REPEAT", 3600.0),
        help="服务持续不可达时多少秒后重复提醒一次，默认 3600",
    )
    parser.add_argument("--no-ocr-probe", action="store_true", help="关闭内网 OCR 探活")
    parser.add_argument(
        "--maintenance-window",
        default=os.environ.get("ALERT_OCR_MAINTENANCE_WINDOWS", ""),
        help="OCR 停机窗口，如 14:00-18:00（多个用逗号分隔）；窗口内探活失败不告警",
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

    mail = email_alert.mail_config()
    maintenance = parse_maintenance_windows(args.maintenance_window)

    watchdog: ProgressWatchdog | None = None
    if not args.no_stuck_alert and args.stuck_minutes > 0:
        # 下限 60 秒：再短会把正常的 OCR 慢请求误判成卡住。
        stuck_seconds = max(60.0, args.stuck_minutes * 60)
        watchdog = ProgressWatchdog(
            stuck_seconds,
            # 检查频率跟着超时走，但夹在 10s ~ 60s 之间，别把 CPU 空转掉。
            interval=max(10.0, min(60.0, stuck_seconds / 10)),
            notify_fn=lambda label, minutes, last, out: email_alert.notify_stuck(
                batch_label=label,
                stuck_minutes=minutes,
                last_progress=last,
                output_dir=out,
                config=mail,
            ),
            mail=mail,
        )

    configure_logging(log_file, verbose=args.verbose, watchdog=watchdog)

    if not args.platform.strip():
        LOGGER.critical("必须通过 --platform 或 OCR_DEMO_PLATFORM 指定数据库平台标识")
        return 2
    template_path = Path(args.template).resolve()
    if not template_path.is_file():
        LOGGER.critical("模板文件不存在：%s", template_path)
        return 2
    input_root = Path(args.input_root).resolve()

    LOGGER.info("邮件报警配置：%s", mail.summary())
    if maintenance:
        LOGGER.info("OCR 停机窗口：%s（窗口内探活失败不告警）", _describe_windows(maintenance))
    else:
        LOGGER.info("OCR 停机窗口：未配置（探活失败就会告警）")
    if watchdog is not None:
        LOGGER.info("卡住看门狗：连续 %.0f 分钟没有进度输出则告警一次", args.stuck_minutes)
    LOGGER.info(
        "批次变更判定：%s（%s）",
        args.fingerprint,
        "只比对路径/大小/mtime，不读文件内容"
        if args.fingerprint == FINGERPRINT_METADATA
        else "逐字节 sha256，慢但最严格",
    )
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
        fingerprint=args.fingerprint,
    )

    # 非交互确认：服务账号没有 tty，任何 input() 都会卡死服务。
    # OCR_DEMO_KEYS_ROTATED 由 preflight_env 强制要求，这里不代填。
    os.environ["OCR_ASSUME_YES"] = "1"
    os.environ["OCR_DEMO_PLATFORM"] = options.platform

    stopper = StopController()
    stopper.install()
    ledger = BatchLedger(daemon_dir / "ledger.json")
    config_holder: dict[str, Any] = {}

    # 上次进程被 kill -9 / 机器断电时，台账里的批次会停在 running。
    # 死掉的进程发不出邮件，所以只能在这一轮启动时补报一次。
    unfinished = [record for record in ledger.records.values() if record.status == "running"]

    probe: OcrProbe | None = None
    # 下限 10 秒：探活本身只是一次 TCP 连接，太频繁没意义还容易误判。
    probe_interval = max(10.0, args.ocr_probe_interval)
    if args.no_ocr_probe:
        LOGGER.info("OCR 探活：已通过 --no-ocr-probe 关闭")
    elif not args.ocr_probe_url.strip():
        LOGGER.info("OCR 探活：未配置 PADDLE_OCR_API_URL，跳过")
    else:
        probe = OcrProbe(
            args.ocr_probe_url.strip(),
            interval=probe_interval,
            failures_to_alert=args.ocr_probe_failures,
            repeat_after=args.ocr_probe_repeat,
            maintenance=maintenance,
            mail=mail,
        )
        if probe.enabled:
            LOGGER.info(
                "OCR 探活：%s:%d 每 %s 秒一次，连续失败 %d 次告警",
                probe.host,
                probe.port,
                int(probe_interval),
                args.ocr_probe_failures,
            )
        else:
            LOGGER.warning("无法从 %r 解析出主机端口，OCR 探活已关闭", args.ocr_probe_url)
            probe = None

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
        if unfinished:
            email_alert.notify(
                "发现上次运行没有正常收尾",
                [
                    f"有 {len(unfinished)} 个批次停在 running 状态，说明上次的进程是被"
                    "强杀（kill -9 / OOM / 断电）而不是正常退出的。",
                    "这些批次会自动重跑；已完成的图片有缓存，不会重复付费。",
                    "",
                    "—— 明细（最多 10 条）——",
                    *[
                        f"  {record.batch_key}：第 {record.attempts} 次尝试，"
                        f"run={record.run_id or '-'}，目录 {record.path}"
                        for record in unfinished[:10]
                    ],
                ],
                severity="warning",
                config=mail,
            )
            # 报过一次就改成 interrupted，避免 systemd 反复重启时每轮都刷一封。
            for record in unfinished:
                record.status = "interrupted"
                record.last_error = record.last_error or "上次进程未正常退出"
            ledger.save()
        email_alert.notify(
            "后台服务已启动",
            [
                f"收件目录：{input_root}",
                f"平台标识：{options.platform}",
                f"输出目录：{output_root}",
                f"轮询模式：{'单轮' if args.once else f'每 {args.interval} 秒'}",
                f"单块商品数上限：{min(options.chunk_size, MAX_CHUNK_SIZE)}",
                f"批次变更判定：{args.fingerprint}",
                f"卡住告警：{'关闭' if watchdog is None else f'{args.stuck_minutes:.0f} 分钟无进度'}",
                f"OCR 探活：{'关闭' if probe is None else f'{probe.host}:{probe.port} 每 {int(probe_interval)} 秒'}",
                f"OCR 停机窗口：{_describe_windows(maintenance) or '未配置'}",
            ],
            severity="info",
            config=mail,
        )

    if watchdog is not None:
        watchdog.start()

    exit_code = 0
    stop_reason = ""
    try:
        while True:
            if probe is not None:
                probe.maybe_probe()
            try:
                stats = process_cycle(
                    options, ledger, stopper, config_holder=config_holder, watchdog=watchdog
                )
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
                stop_reason = "收到停止信号，当前批次已跑完，服务正常退出"
                break
            wait = options.retryable_backoff if stats.get("paused") else args.interval
            LOGGER.info("本轮结束 %s，%s 秒后进入下一轮", stats, int(wait))
            if not stopper.sleep(wait):
                stop_reason = "收到停止信号，服务在轮询间隙退出"
                break
    except KeyboardInterrupt:
        LOGGER.warning("守护进程被中断退出")
        exit_code = 130
        stop_reason = "收到第二次中断信号（SIGINT/SIGTERM），立即退出，可能有批次未收尾"
    finally:
        if watchdog is not None:
            watchdog.stop()
        release_pid_file(pid_file)
        ledger.save()
        LOGGER.info("守护进程已退出：pid=%s", os.getpid())
        # 单轮模式不报「服务已退出」，那只是跑一遍就结束，不是异常。
        if stop_reason and not args.once and not args.dry_run:
            email_alert.notify_daemon_stopped(
                reason=stop_reason,
                severity="info" if exit_code == 0 else "error",
                config=mail,
            )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
