"""邮件报警模块。

无人值守（Linux 服务器后台）运行时，把每个批次的运行结果通过 SMTP 发到指定邮箱。
默认收件人是 13306032298@163.com。

设计约束：

* 只依赖 Python 标准库（``smtplib`` / ``email``），不引入任何第三方依赖，
  这样 systemd 里的服务不会因为多装一个包就起不来。
* **本模块的任何函数都不会向上抛异常。** 报警发不出去只记日志并返回 False，
  绝不阻断 OCR 主管线——报警是旁路，不是主流程的一部分。
* 全部配置来自环境变量（见 :func:`mail_config`），不落盘、不写进命令行，
  避免密钥出现在进程列表和日志里。

常用环境变量：

============================  ====================================================
变量                          说明
============================  ====================================================
``ALERT_MAIL_ENABLED``        是否启用邮件报警，默认 ``1``
``ALERT_SMTP_HOST``           SMTP 服务器，默认 ``smtp.163.com``
``ALERT_SMTP_PORT``           SMTP 端口，默认 ``465``
``ALERT_SMTP_SSL``            ``1``（默认）用 SMTP_SSL；``0`` 用 STARTTLS
``ALERT_SMTP_USER``           登录账号，163 场景填完整邮箱地址
``ALERT_SMTP_PASSWORD``       163 的**授权码**（不是邮箱登录密码）
``ALERT_MAIL_FROM``           发件人，默认等于 ``ALERT_SMTP_USER``
``ALERT_MAIL_TO``             收件人，多个用逗号分隔，默认 ``13306032298@163.com``
``ALERT_MAIL_SUBJECT_PREFIX`` 主题前缀，默认 ``[OCR-V7]``
``ALERT_ON_SUCCESS``          运行成功时是否也发邮件，默认 ``1``
``ALERT_ATTACH_ARTIFACTS``    是否附带 performance.json / 待复核图片.json，默认 ``1``
``ALERT_SMTP_TIMEOUT``        网络超时秒数，默认 ``30``
``ALERT_EMPTY_PRODUCT_MIN_FIELDS``
                              一个商品至少要有几个核心字段非空才算「识别出来了」，
                              默认 ``1``（即核心字段全空才报警）
============================  ====================================================

报警触发点一览（都在 :mod:`ocr_daemon` 里调用）：

============================  ==================================  ==========
触发条件                      调用                                 级别
============================  ==================================  ==========
批次跑完（成功 / 待复核 / 失败）  :func:`notify_pipeline_result`       success / warning / error
批次抛出异常                    :func:`notify_pipeline_failure`      error
CSV 没生成 / 一行数据都没有       :func:`notify_artifacts_missing`     error
商品核心字段全空                 :func:`notify_products_empty`        warning
守护进程被中断 / 非正常退出        :func:`notify_daemon_stopped`        warning / error
批次长时间没有进度                :func:`notify_stuck`                 error
内网 OCR 服务探活失败 / 恢复       :func:`notify_ocr_probe`             error / info
启动前配置不全                   :func:`notify`                       critical
============================  ==================================  ==========

自检（会真的发一封测试邮件）：::

    python email_alert.py --self-test
    python email_alert.py --self-test --dry-run   # 只看配置，不发信
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import smtplib
import socket
import ssl
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formataddr, formatdate
from pathlib import Path
from typing import Any, Iterable, Sequence

LOGGER = logging.getLogger("ocr.alert")

DEFAULT_MAIL_TO = "13306032298@163.com"
DEFAULT_SMTP_HOST = "smtp.163.com"
DEFAULT_SMTP_PORT = 465
DEFAULT_SUBJECT_PREFIX = "[OCR-V7]"
DEFAULT_TIMEOUT = 30

#: 单个附件上限；超过就不附，避免把收件箱撑爆或被服务端拒收。
MAX_ATTACHMENT_BYTES = 2 * 1024 * 1024

#: 判定为「假」的环境变量取值。
_FALSE_VALUES = {"0", "false", "no", "off", "n", "disable", "disabled", "否", "不"}

#: 严重级别 → 邮件主题里的中文标签。
SEVERITY_LABELS = {
    "critical": "【严重故障】",
    "error": "【运行失败】",
    "warning": "【待复核】",
    "success": "【运行成功】",
    "info": "【运行信息】",
}

CST = timezone(timedelta(hours=8))


def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() not in _FALSE_VALUES


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        LOGGER.warning("环境变量 %s=%r 不是整数，回退为 %s", name, raw, default)
        return default


def _split_addresses(raw: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in raw.replace(";", ",").split(",") if item.strip())


@dataclass(frozen=True)
class MailConfig:
    """从环境变量解析出来的邮件配置（不可变）。"""

    enabled: bool
    host: str
    port: int
    use_ssl: bool
    username: str
    password: str
    sender: str
    recipients: tuple[str, ...]
    subject_prefix: str
    alert_on_success: bool
    attach_artifacts: bool
    timeout: int

    @property
    def ready(self) -> bool:
        """是否具备真正发信的条件。"""
        return bool(self.enabled and self.host and self.recipients)

    def summary(self) -> str:
        """给日志用的配置摘要，绝不包含密码。"""
        return (
            f"enabled={self.enabled} host={self.host}:{self.port} "
            f"ssl={self.use_ssl} user={self.username or '-'} "
            f"from={self.sender or '-'} to={','.join(self.recipients) or '-'} "
            f"alert_on_success={self.alert_on_success} "
            f"attach={self.attach_artifacts} password={'已设置' if self.password else '未设置'}"
        )


def mail_config() -> MailConfig:
    """读取环境变量得到邮件配置。本函数不会抛异常。"""
    username = os.environ.get("ALERT_SMTP_USER", "").strip()
    password = os.environ.get("ALERT_SMTP_PASSWORD", "").strip()
    sender = os.environ.get("ALERT_MAIL_FROM", "").strip() or username
    explicit_to = os.environ.get("ALERT_MAIL_TO", "").strip()
    recipients = _split_addresses(explicit_to) if explicit_to else (DEFAULT_MAIL_TO,)
    default_enabled = bool(username and password)
    return MailConfig(
        enabled=_env_flag("ALERT_MAIL_ENABLED", default_enabled),
        host=os.environ.get("ALERT_SMTP_HOST", DEFAULT_SMTP_HOST).strip() or DEFAULT_SMTP_HOST,
        port=_env_int("ALERT_SMTP_PORT", DEFAULT_SMTP_PORT),
        use_ssl=_env_flag("ALERT_SMTP_SSL", True),
        username=username,
        password=password,
        sender=sender,
        recipients=recipients,
        subject_prefix=os.environ.get("ALERT_MAIL_SUBJECT_PREFIX", DEFAULT_SUBJECT_PREFIX).strip()
        or DEFAULT_SUBJECT_PREFIX,
        alert_on_success=_env_flag("ALERT_ON_SUCCESS", True),
        attach_artifacts=_env_flag("ALERT_ATTACH_ARTIFACTS", True),
        timeout=_env_int("ALERT_SMTP_TIMEOUT", DEFAULT_TIMEOUT),
    )


def empty_product_min_fields() -> int:
    """一个商品至少要几个核心字段非空，才算「识别出来了」。默认 1。"""
    value = _env_int("ALERT_EMPTY_PRODUCT_MIN_FIELDS", 1)
    return max(0, value)


def _attach_file(message: EmailMessage, path: Path) -> bool:
    """把单个文件挂到邮件上；失败或超限时只记日志。"""
    try:
        if not path.is_file():
            return False
        size = path.stat().st_size
        if size > MAX_ATTACHMENT_BYTES:
            LOGGER.warning("附件 %s 有 %.1f KB，超过上限，跳过", path.name, size / 1024)
            return False
        message.add_attachment(
            path.read_bytes(),
            maintype="application",
            subtype="octet-stream",
            filename=path.name,
        )
        return True
    except OSError as exc:
        LOGGER.warning("读取附件 %s 失败：%s", path, exc)
        return False


def send_mail(
    subject: str,
    body: str,
    *,
    attachments: Sequence[Path] | None = None,
    config: MailConfig | None = None,
) -> bool:
    """发送一封纯文本邮件。**永不抛异常**，成功返回 True。

    :param subject: 不含前缀的主题，函数会补 ``ALERT_MAIL_SUBJECT_PREFIX``。
    :param body: 纯文本正文。
    :param attachments: 需要附带的小文件（超过上限会被静默跳过）。
    :param config: 显式传入的配置；默认从环境变量读取。
    """
    resolved = config or mail_config()
    full_subject = f"{resolved.subject_prefix}{subject}"
    if not resolved.enabled:
        LOGGER.info("邮件报警已关闭，跳过发送：%s", full_subject)
        return False
    if not resolved.recipients:
        LOGGER.warning("没有配置收件人（ALERT_MAIL_TO），跳过发送：%s", full_subject)
        return False

    message = EmailMessage()
    message["Subject"] = full_subject
    message["From"] = (
        formataddr(("OCR 报警", resolved.sender))
        if resolved.sender
        else formataddr(("OCR 报警", resolved.username or "ocr-alert@localhost"))
    )
    message["To"] = ", ".join(resolved.recipients)
    message["Date"] = formatdate(localtime=True)
    message["X-OCR-Alert"] = "ocr-v7-daemon"
    message.set_content(body, charset="utf-8")

    attached: list[str] = []
    if resolved.attach_artifacts and attachments:
        for path in attachments:
            if _attach_file(message, Path(path)):
                attached.append(Path(path).name)

    try:
        if resolved.use_ssl:
            context = ssl.create_default_context()
            with smtplib.SMTP_SSL(
                resolved.host, resolved.port, timeout=resolved.timeout, context=context
            ) as client:
                if resolved.username:
                    client.login(resolved.username, resolved.password)
                client.send_message(message)
        else:
            with smtplib.SMTP(resolved.host, resolved.port, timeout=resolved.timeout) as client:
                client.ehlo()
                try:
                    client.starttls(context=ssl.create_default_context())
                    client.ehlo()
                except smtplib.SMTPException:
                    LOGGER.warning("SMTP 服务器不支持 STARTTLS，继续以明文发送")
                if resolved.username:
                    client.login(resolved.username, resolved.password)
                client.send_message(message)
    except (smtplib.SMTPException, ssl.SSLError, OSError, socket.timeout) as exc:
        LOGGER.error(
            "邮件发送失败（%s:%s -> %s）：%s",
            resolved.host,
            resolved.port,
            ",".join(resolved.recipients),
            exc,
        )
        return False
    LOGGER.info(
        "邮件已发送：%s -> %s%s",
        full_subject,
        ",".join(resolved.recipients),
        f"（附件 {', '.join(attached)}）" if attached else "",
    )
    return True


def notify(
    title: str,
    lines: Iterable[str],
    *,
    severity: str = "info",
    attachments: Sequence[Path] | None = None,
    config: MailConfig | None = None,
) -> bool:
    """按「标题 + 多行正文」的形式发一封报警邮件。"""
    resolved = config or mail_config()
    label = SEVERITY_LABELS.get(severity, SEVERITY_LABELS["info"])
    now = datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")
    body_lines = [
        f"时间：{now}（北京时间）",
        f"主机：{socket.gethostname()}",
        f"级别：{severity}",
        "",
        *lines,
        "",
        "-- ",
        "本邮件由 OCR v7 后台服务自动发送，请勿直接回复。",
    ]
    return send_mail(
        f"{label}{title}",
        "\n".join(str(item) for item in body_lines),
        attachments=attachments,
        config=resolved,
    )


def _format_duration(milliseconds: Any) -> str:
    try:
        seconds = float(milliseconds) / 1000
    except (TypeError, ValueError):
        return "未知"
    if seconds < 60:
        return f"{seconds:.1f} 秒"
    minutes, remainder = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes} 分 {remainder} 秒"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} 小时 {minutes} 分"


def _load_review_images(output_dir: Path | None) -> list[dict[str, Any]]:
    if not output_dir:
        return []
    queue_path = Path(output_dir) / "待复核图片.json"
    try:
        value = json.loads(queue_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return value if isinstance(value, list) else []


def _artifact_paths(output_dir: Path | None) -> list[Path]:
    if not output_dir:
        return []
    base = Path(output_dir)
    candidates = [base / "performance.json", base / "待复核图片.json"]
    return [path for path in candidates if path.is_file()]


def notify_pipeline_result(
    result: dict[str, Any],
    *,
    batch_label: str,
    config: MailConfig | None = None,
) -> bool:
    """把 :func:`demo.execute_pipeline` 的返回值汇总成一封邮件。

    成功（``complete``）时是否发送由 ``ALERT_ON_SUCCESS`` 控制；
    ``review`` 和失败一定发送。
    """
    resolved = config or mail_config()
    status = str(result.get("status") or "unknown")
    success_count = int(result.get("success_count") or 0)
    review_count = int(result.get("review_count") or 0)
    output_dir = Path(str(result.get("output_dir") or "")) if result.get("output_dir") else None
    performance = result.get("performance") or {}
    ocr = performance.get("ocr") or {}

    # 产物审计信息由 ocr_daemon.run_one_batch 汇总后塞进 result 里。
    product_count = int(result.get("product_count") or 0)
    csv_paths = [str(item) for item in (result.get("csv_files") or []) if item]
    csv_missing = [str(item) for item in (result.get("csv_missing") or []) if item]
    csv_rows = int(result.get("csv_rows") or 0)
    empty_products = [item for item in (result.get("empty_products") or []) if isinstance(item, dict)]
    min_fields = empty_product_min_fields()

    lines = [
        f"批次：{batch_label}",
        f"运行 ID：{result.get('run_id') or '-'}",
        f"状态：{status}",
        f"商品：成功 {success_count} 个，待复核 {review_count} 个，合计 {product_count} 个",
        f"输出目录：{result.get('output_dir') or '-'}",
    ]
    lines.append(
        f"Excel：{Path(str(result['workbook'])).name}" if result.get("workbook") else "Excel：未生成"
    )

    # ---- 产物检查：CSV 有没有生成、有没有数据 ----
    lines.append("")
    lines.append("—— 产物检查 ——")
    lines.append(f"CSV：{len(csv_paths)} 个文件，合计 {csv_rows} 行数据")
    for item in csv_paths[:10]:
        lines.append(f"  {item}")
    if len(csv_paths) > 10:
        lines.append(f"  ...其余 {len(csv_paths) - 10} 个见输出目录")
    if csv_missing:
        lines.append("")
        lines.append(f"!! {len(csv_missing)} 个批次块没有生成 CSV（完整列表见单独告警邮件）：")
        for item in csv_missing[:3]:
            lines.append(f"  {item}")
        if len(csv_missing) > 3:
            lines.append(f"  ...其余 {len(csv_missing) - 3} 条见单独告警邮件")
    if product_count and csv_rows < product_count:
        lines.append("")
        lines.append(f"!! CSV 只有 {csv_rows} 行，少于商品数 {product_count} 行，有商品没写进结果文件。")

    lines.append("")
    lines.append("—— 性能摘要 ——")
    lines.append(f"总耗时：{_format_duration(performance.get('total_ms'))}")
    lines.append(
        "OCR 图片："
        f"计划 {ocr.get('planned_images', '-')}，"
        f"成功 {ocr.get('success_images', '-')}，"
        f"失败 {ocr.get('failed_images', '-')}，"
        f"缓存命中 {ocr.get('cache_hits', '-')}"
    )
    lines.append(
        f"OCR 耗时 P50 {_format_duration(ocr.get('success_p50_ms'))} / "
        f"P95 {_format_duration(ocr.get('success_p95_ms'))}"
    )

    review_images = _load_review_images(output_dir)
    if review_images:
        lines.append("")
        lines.append(f"—— 待复核图片 {len(review_images)} 张（最多列 20 条）——")
        for item in review_images[:20]:
            error = item.get("error") or {}
            lines.append(
                f"  {item.get('product_id', '-')}/{item.get('image_name', '-')}："
                f"{error.get('code', '-')} {error.get('message', '')}".rstrip()
            )
        if len(review_images) > 20:
            lines.append(f"  ...其余 {len(review_images) - 20} 条见待复核图片.json")

    if empty_products:
        lines.append("")
        lines.append(
            f"—— 完全没识别出来的商品 {len(empty_products)} 个"
            f"（完整列表见单独告警邮件，这里只列前 5 条）——"
        )
        lines.append(f"（判定：核心字段里非空值少于 {min_fields} 个）")
        for item in empty_products[:5]:
            note = item.get("note") or "核心字段全部为空"
            best = item.get("best_run_id") or "-"
            lines.append(f"  {item.get('product_id', '-')}：{note}（run {best}）")
        if len(empty_products) > 5:
            lines.append(f"  ...其余 {len(empty_products) - 5} 个见单独告警邮件")

    # ---- 定级：产物缺失 > 商品全空 > 管线自身状态 ----
    artifact_error = bool(csv_missing) or (product_count > 0 and csv_rows == 0)
    if artifact_error:
        severity = "error"
        title = f"批次没有产出结果文件：{batch_label}"
    elif empty_products:
        severity = "warning"
        title = f"批次有 {len(empty_products)} 个商品完全没识别出来：{batch_label}"
    elif status == "complete":
        severity = "success"
        if not resolved.alert_on_success:
            LOGGER.info("批次 %s 运行成功，ALERT_ON_SUCCESS=0，跳过邮件", batch_label)
            return False
        title = f"批次运行成功：{batch_label}（{success_count} 个商品）"
    elif status == "review":
        severity = "warning"
        title = f"批次需要人工复核：{batch_label}（待复核 {review_count} 个商品）"
    else:
        severity = "error"
        title = f"批次运行状态异常：{batch_label}（{status}）"

    return notify(
        title,
        lines,
        severity=severity,
        attachments=_artifact_paths(output_dir),
        config=resolved,
    )


def notify_pipeline_failure(
    *,
    batch_label: str,
    error_code: str,
    message: str,
    output_dir: Path | None = None,
    retry_in_seconds: float | None = None,
    config: MailConfig | None = None,
) -> bool:
    """把一次运行异常汇总成告警邮件。"""
    resolved = config or mail_config()
    lines = [
        f"批次：{batch_label}",
        f"错误码：{error_code}",
        f"说明：{message}",
        f"输出目录：{output_dir or '-'}",
    ]
    if retry_in_seconds is not None:
        lines.append(f"服务将在 {int(retry_in_seconds)} 秒后自动重试该批次。")

    review_images = _load_review_images(output_dir)
    if review_images:
        lines.append("")
        lines.append(f"已有 {len(review_images)} 张图片待复核，成功产物均已落盘，可断点续跑。")

    lines.append("")
    lines.append("—— 排查建议 ——")
    lines.append("1. 确认 PaddleOCR 内网服务（PADDLE_OCR_API_URL）是否可达；")
    lines.append("2. 确认 DashScope / PostgreSQL 凭据是否过期；")
    lines.append("3. 查看输出目录下的 report.html 与 ocr服务中断-*.json。")

    return notify(
        f"批次运行失败：{batch_label}（{error_code}）",
        lines,
        severity="error",
        attachments=_artifact_paths(output_dir),
        config=resolved,
    )


def notify_products_empty(
    *,
    batch_label: str,
    empty_products: Sequence[dict[str, Any]],
    total_products: int,
    min_fields: int | None = None,
    output_dir: Path | None = None,
    config: MailConfig | None = None,
) -> bool:
    """有商品「什么都没识别出来」时单独告警（不等批次汇总）。"""
    resolved = config or mail_config()
    threshold = empty_product_min_fields() if min_fields is None else max(0, min_fields)
    lines = [
        f"批次：{batch_label}",
        f"商品总数：{total_products} 个，其中 {len(empty_products)} 个核心字段全空",
        f"判定标准：核心字段里非空值少于 {threshold} 个",
        f"输出目录：{output_dir or '-'}",
        "",
        "—— 明细（最多 30 条）——",
    ]
    for item in list(empty_products)[:30]:
        lines.append(f"  {item.get('product_id', '-')}：{item.get('note') or '核心字段全部为空'}")
    if len(empty_products) > 30:
        lines.append(f"  ...其余 {len(empty_products) - 30} 个见输出目录下 products/ 的商品 JSON")
    lines.append("")
    lines.append("—— 排查建议 ——")
    lines.append("1. 打开该商品目录，确认图片是否真的能看清文字（纯色条 / 切片图会识别不出）；")
    lines.append("2. 查 OCR 服务日志里对应图片的 log_id，看是不是 422 / 超时；")
    lines.append("3. 确认该 product_id 在主数据表里存在（否则数据库侧全空，只剩 OCR 一条路）。")

    return notify(
        f"有商品完全没识别出来：{batch_label}（{len(empty_products)} 个）",
        lines,
        severity="warning",
        config=resolved,
    )


def notify_artifacts_missing(
    *,
    batch_label: str,
    missing: Sequence[str],
    expected: int,
    output_dir: Path | None = None,
    config: MailConfig | None = None,
) -> bool:
    """结果文件（CSV）没生成、或生成了一行数据都没有时告警。"""
    resolved = config or mail_config()
    lines = [
        f"批次：{batch_label}",
        f"应该有 {expected} 个结果文件，实际缺失 {len(missing)} 个",
        f"输出目录：{output_dir or '-'}",
        "",
        "—— 缺失明细（最多 20 条）——",
    ]
    for item in list(missing)[:20]:
        lines.append(f"  {item}")
    if len(missing) > 20:
        lines.append(f"  ...其余 {len(missing) - 20} 条")
    lines.append("")
    lines.append("—— 排查建议 ——")
    lines.append("1. 直接看上面的目录里有没有 result.csv / result.xlsx；")
    lines.append("2. 看同目录的 report.html 与 performance.json，确认管线走到哪一步断的；")
    lines.append("3. 看日志里最后一次 [pipeline] 输出，定位卡在 OCR、数据库还是导出环节。")

    return notify(
        f"批次没有产出结果文件：{batch_label}",
        lines,
        severity="error",
        attachments=_artifact_paths(output_dir),
        config=resolved,
    )


def notify_daemon_stopped(
    *,
    reason: str,
    severity: str = "warning",
    batch_label: str = "",
    batch_status: str = "",
    output_dir: Path | None = None,
    config: MailConfig | None = None,
) -> bool:
    """守护进程被中断 / 非正常退出时告警。

    「优雅停止」（收到一次 SIGTERM 且当前批次跑完）也会通知，但级别是 ``info``，
    便于区分「人主动停的」和「进程被弄死了」。
    """
    resolved = config or mail_config()
    lines = [
        f"原因：{reason}",
    ]
    if batch_label:
        lines.append(f"批次：{batch_label}")
    if batch_status:
        lines.append(f"批次状态：{batch_status}")
    if output_dir:
        lines.append(f"输出目录：{output_dir}")
        lines.append("已完成的产物都在上面的目录里，重启服务会自动断点续跑。")
    lines.append("")
    lines.append("—— 排查建议 ——")
    if severity == "info":
        lines.append("1. 这是收到一次停止信号后的正常收尾退出，确认是本人操作即可忽略；")
        lines.append("2. 重启：sudo systemctl start ocr-v7-daemon。")
    else:
        lines.append("1. systemctl status ocr-v7-daemon 看是不是被 OOM / 超时杀掉；")
        lines.append("2. journalctl -u ocr-v7-daemon -n 200 看退出前最后几行；")
        lines.append("3. 进程若已被 kill -9，本机不会再发邮件，靠这条「重启后发现上次未收尾」记录判断。")

    return notify(
        f"后台服务已退出：{reason}",
        lines,
        severity=severity,
        attachments=_artifact_paths(output_dir),
        config=resolved,
    )


def notify_stuck(
    *,
    batch_label: str,
    stuck_minutes: float,
    last_progress: str = "",
    output_dir: Path | None = None,
    config: MailConfig | None = None,
) -> bool:
    """批次长时间没有进度时告警（看门狗触发）。"""
    resolved = config or mail_config()
    lines = [
        f"批次：{batch_label}",
        f"已连续 {stuck_minutes:.0f} 分钟没有任何进度输出，疑似卡住。",
        f"最后一次进度：{last_progress or '（进程启动后一直没有进度）'}",
        f"输出目录：{output_dir or '-'}",
        "",
        "—— 说明 ——",
        "服务仍在运行，不会自杀。后续一旦有新进度会恢复正常，本条只发一次。",
        "",
        "—— 排查建议 ——",
        "1. 内网 OCR 服务是否被限流 / 挂起（看 OCR 侧日志）；",
        "2. 单张图片是否特别大导致 OCR 侧长时间无响应；",
        "3. 是否卡在数据库查询（PostgreSQL 侧看 pg_stat_activity）；",
        "4. 确认没有人工在等交互输入 —— 服务模式下任何 input() 都会卡死。",
    ]

    return notify(
        f"批次疑似卡住：{batch_label}（{stuck_minutes:.0f} 分钟无进度）",
        lines,
        severity="error",
        attachments=_artifact_paths(output_dir),
        config=resolved,
    )


def notify_ocr_probe(
    *,
    url: str,
    host: str,
    port: int,
    down: bool,
    consecutive: int = 1,
    detail: str = "",
    config: MailConfig | None = None,
) -> bool:
    """内网 OCR 服务探活失败 / 恢复时告警。"""
    resolved = config or mail_config()
    if down:
        lines = [
            f"OCR 地址：{url}",
            f"探测目标：{host}:{port}",
            f"连续失败：{consecutive} 次",
            f"错误：{detail or '连接失败'}",
            "",
            "—— 说明 ——",
            "只影响新批次的 OCR 阶段；已在跑的批次会自动退避重试，不会丢数据。",
            "如果这台 OCR 服务本来就有定时停机窗口，把它写进",
            "ALERT_OCR_MAINTENANCE_WINDOWS（如 14:00-18:00）就不会在窗口内报警。",
        ]
        title = f"内网 OCR 服务不可达（连续 {consecutive} 次）"
        severity = "error"
    else:
        lines = [
            f"OCR 地址：{url}",
            f"探测目标：{host}:{port}",
            "",
            "服务已恢复响应，新的批次可以正常跑。",
        ]
        title = "内网 OCR 服务已恢复"
        severity = "info"

    return notify(title, lines, severity=severity, config=resolved)


def self_test(*, dry_run: bool = False) -> int:
    """命令行自检：打印配置（不含密码），可选真的发一封测试邮件。"""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config = mail_config()
    print("邮件配置：" + config.summary())
    if not config.ready:
        print(
            "结论：邮件报警未就绪。请至少设置 ALERT_SMTP_USER、ALERT_SMTP_PASSWORD；"
            "收件人默认是 13306032298@163.com。",
            file=sys.stderr,
        )
        return 1
    if dry_run:
        print("结论：配置完整（dry-run，未发信）。")
        return 0
    ok = notify(
        "邮件报警自检",
        ["这是一封来自 OCR v7 后台服务的自检邮件，收到即表示报警链路可用。"],
        severity="info",
        config=config,
    )
    print("结论：" + ("测试邮件已发送。" if ok else "测试邮件发送失败，请检查上面的错误日志。"))
    return 0 if ok else 1


def preview_all() -> int:
    """把每一种报警邮件的主题和正文打印出来，**不发信**。

    用途：OCR 服务不在线时也能核对报警文案；改完模板后肉眼验收。
    """
    captured: list[tuple[str, str]] = []
    original = globals()["send_mail"]

    def _fake_send(subject: str, body: str, **_kwargs: Any) -> bool:
        captured.append((subject, body))
        return True

    globals()["send_mail"] = _fake_send  # type: ignore[assignment]
    try:
        config = mail_config()
        sample_perf = {
            "total_ms": 1_842_000,
            "ocr": {
                "planned_images": 812,
                "success_images": 800,
                "failed_images": 12,
                "cache_hits": 640,
                "success_p50_ms": 2100,
                "success_p95_ms": 5400,
            },
        }
        notify_pipeline_result(
            {
                "status": "complete",
                "run_id": "20260920-180500-abc123",
                "output_dir": "/var/lib/ocr-v7/runs/2026-09-20/20260920-180500-abc123",
                "workbook": "/var/lib/ocr-v7/runs/2026-09-20/.../result.xlsx",
                "success_count": 500,
                "review_count": 0,
                "product_count": 500,
                "csv_files": ["/var/lib/ocr-v7/runs/2026-09-20/.../result.csv"],
                "csv_rows": 500,
                "performance": sample_perf,
            },
            batch_label="2026-09-20",
            config=config,
        )
        notify_pipeline_result(
            {
                "status": "review",
                "run_id": "20260920-180500-abc123",
                "output_dir": "/var/lib/ocr-v7/runs/2026-09-20/20260920-180500-abc123",
                "workbook": "/var/lib/ocr-v7/runs/2026-09-20/.../result.xlsx",
                "success_count": 492,
                "review_count": 8,
                "product_count": 500,
                "csv_files": ["/var/lib/ocr-v7/runs/2026-09-20/.../result.csv"],
                "csv_rows": 495,
                "empty_products": [
                    {"product_id": "100005996353", "note": "核心字段全部为空", "best_run_id": "r1"},
                    {"product_id": "100011526893", "note": "仅识别出商品名称", "best_run_id": "r1"},
                ],
                "performance": sample_perf,
            },
            batch_label="2026-09-20",
            config=config,
        )
        notify_artifacts_missing(
            batch_label="2026-09-20",
            missing=["第 2/5 块（run 20260920-183000-def456）没有生成 result.csv"],
            expected=5,
            output_dir=Path("/var/lib/ocr-v7/runs/2026-09-20/20260920-180500-abc123"),
            config=config,
        )
        notify_products_empty(
            batch_label="2026-09-20",
            empty_products=[
                {"product_id": "100005996353", "note": "核心字段全部为空"},
                {"product_id": "100011526893", "note": "核心字段全部为空"},
            ],
            total_products=500,
            output_dir=Path("/var/lib/ocr-v7/runs/2026-09-20/20260920-180500-abc123"),
            config=config,
        )
        notify_pipeline_failure(
            batch_label="2026-09-20",
            error_code="PADDLE_OCR_INTERRUPTED",
            message="内网 OCR 服务连续 3 次请求失败，已暂停本批次",
            output_dir=Path("/var/lib/ocr-v7/runs/2026-09-20/20260920-180500-abc123"),
            retry_in_seconds=300,
            config=config,
        )
        notify_daemon_stopped(
            reason="收到 SIGTERM：当前批次跑完后退出（systemctl stop）",
            severity="info",
            batch_label="2026-09-20",
            batch_status="complete",
            output_dir=Path("/var/lib/ocr-v7/runs/2026-09-20/20260920-180500-abc123"),
            config=config,
        )
        notify_daemon_stopped(
            reason="再次收到 SIGINT：立即退出（批次 2026-09-20 未收尾）",
            severity="error",
            batch_label="2026-09-20",
            batch_status="interrupted",
            output_dir=Path("/var/lib/ocr-v7/runs/2026-09-20/20260920-180500-abc123"),
            config=config,
        )
        notify_stuck(
            batch_label="2026-09-20",
            stuck_minutes=30,
            last_progress="[pipeline] 100/500 OCR 完成（第 100 张已返回）",
            output_dir=Path("/var/lib/ocr-v7/runs/2026-09-20/20260920-180500-abc123"),
            config=config,
        )
        notify_ocr_probe(
            url="http://192.168.1.115:8870/v1/ocr",
            host="192.168.1.115",
            port=8870,
            down=True,
            consecutive=3,
            detail="Connection refused",
            config=config,
        )
        notify_ocr_probe(
            url="http://192.168.1.115:8870/v1/ocr",
            host="192.168.1.115",
            port=8870,
            down=False,
            config=config,
        )
    finally:
        globals()["send_mail"] = original  # type: ignore[assignment]

    print(f"共 {len(captured)} 封报警邮件预览（未发送）：")
    for index, (subject, body) in enumerate(captured, start=1):
        print("")
        print("=" * 72)
        print(f"[{index}] 主题：{subject}")
        print("-" * 72)
        print(body)
    print("=" * 72)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="OCR v7 邮件报警自检")
    parser.add_argument("--self-test", action="store_true", help="发送一封测试邮件")
    parser.add_argument("--dry-run", action="store_true", help="只打印配置，不发送")
    parser.add_argument(
        "--preview", action="store_true", help="打印全部报警邮件正文（不发信），用于核对文案"
    )
    args = parser.parse_args(argv)
    if args.preview:
        return preview_all()
    if not args.self_test:
        parser.print_help()
        return 0
    return self_test(dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
