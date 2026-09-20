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
============================  ====================================================

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

    lines = [
        f"批次：{batch_label}",
        f"运行 ID：{result.get('run_id') or '-'}",
        f"状态：{status}",
        f"商品：成功 {success_count} 个，待复核 {review_count} 个",
        f"输出目录：{result.get('output_dir') or '-'}",
    ]
    if result.get("workbook"):
        lines.append(f"结果文件：{Path(str(result['workbook'])).name}")
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

    if status == "complete":
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


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="OCR v7 邮件报警自检")
    parser.add_argument("--self-test", action="store_true", help="发送一封测试邮件")
    parser.add_argument("--dry-run", action="store_true", help="只打印配置，不发送")
    args = parser.parse_args(argv)
    if not args.self_test:
        parser.print_help()
        return 0
    return self_test(dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
