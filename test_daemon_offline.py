"""后台守护进程的离线自测。

不连内网、不连数据库、不发邮件，用来验证：

* 批次发现与分块；
* 台账的去重 / 重试判定；
* demo.py 无人值守模式（``OCR_ASSUME_YES``）下不会卡在 input()；
* 邮件配置解析与默认收件人；
* 多批次块之间 config 的运行期状态会被清理。

运行方式::

    .venv/bin/python test_daemon_offline.py
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

import demo
import email_alert
import ocr_daemon


def write_image(path: Path, color: tuple[int, int, int] = (200, 200, 200)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (8, 8), color).save(path, format="JPEG")
    return path


def make_batch(root: Path, batch_name: str, products: dict[str, int]) -> Path:
    """在 root 下造一个批次目录：{product_id: 图片数量}。"""
    batch = root / batch_name
    for index, (product_id, image_count) in enumerate(products.items()):
        for number in range(1, image_count + 1):
            # 每张图内容不同，避免 sha256 相同（那正是线上踩过的坑）。
            write_image(batch / product_id / f"{number:02d}.jpg", (10 + index, number, 30))
    return batch


def disabled_mail() -> email_alert.MailConfig:
    """测试里永远不许真的连 SMTP。"""
    return email_alert.MailConfig(
        enabled=False,
        host="",
        port=465,
        use_ssl=True,
        username="",
        password="",
        sender="",
        recipients=(),
        subject_prefix="",
        alert_on_success=True,
        attach_artifacts=False,
        timeout=1,
    )


ENV_BASE = {
    "ALERT_MAIL_ENABLED": "0",
    "OCR_DEMO_KEYS_ROTATED": "YES",
    "DASHSCOPE_API_KEY": "test-key",
    "POSTGRES_HOST": "localhost",
    "POSTGRES_DATABASE": "db",
    "POSTGRES_USER": "user",
    "POSTGRES_PASSWORD": "pw",
}


class PlanChunksTest(unittest.TestCase):
    def test_chunks_respect_limit(self) -> None:
        products = [Path(f"p{index}") for index in range(205)]
        chunks = ocr_daemon.plan_chunks(products, 100)
        self.assertEqual([len(chunk) for chunk in chunks], [100, 100, 5])
        self.assertEqual(chunks[0][0], products[0])
        self.assertEqual(chunks[-1][-1], products[-1])

    def test_chunk_size_over_limit_is_clamped(self) -> None:
        products = [Path(f"p{index}") for index in range(150)]
        chunks = ocr_daemon.plan_chunks(products, 9999)
        self.assertTrue(all(len(chunk) <= demo.MAX_PRODUCTS for chunk in chunks))


class DiscoverBatchesTest(unittest.TestCase):
    def test_only_directories_with_products_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_batch(root, "0918", {"1001": 1})
            make_batch(root, "0919", {"1002": 2})
            (root / "空目录").mkdir()
            (root / "只有一个说明.txt").write_text("x", encoding="utf-8")
            batches = [path.name for path in ocr_daemon.discover_batches(root)]
            self.assertEqual(batches, ["0918", "0919"])

    def test_missing_root_raises_demo_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(demo.DemoError) as ctx:
                ocr_daemon.discover_batches(Path(tmp) / "不存在")
            self.assertEqual(ctx.exception.code, "INPUT_ROOT_NOT_FOUND")


class LedgerTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.ledger = ocr_daemon.BatchLedger(Path(self._tmp.name) / "ledger.json")
        self.path = Path(self._tmp.name) / "0918"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _record(self, status: str, *, attempts: int = 1, manifest_hash: str = "h1"):
        record = self.ledger.get("0918", self.path)
        record.status = status
        record.attempts = attempts
        record.manifest_hash = manifest_hash
        return record

    def test_complete_and_unchanged_is_skipped(self) -> None:
        record = self._record("complete", manifest_hash="h1")
        should_run, reason = self.ledger.should_run(
            record, "h1", retry_review=True, max_attempts=3
        )
        self.assertFalse(should_run)
        self.assertIn("未变化", reason)

    def test_complete_but_changed_reruns(self) -> None:
        record = self._record("complete", manifest_hash="h1")
        should_run, reason = self.ledger.should_run(
            record, "h2", retry_review=True, max_attempts=3
        )
        self.assertTrue(should_run)
        self.assertIn("变化", reason)

    def test_review_retry_respects_max_attempts(self) -> None:
        record = self._record("review", attempts=3)
        should_run, reason = self.ledger.should_run(
            record, "h1", retry_review=True, max_attempts=3
        )
        self.assertFalse(should_run)
        self.assertIn("上限", reason)

    def test_review_retry_can_be_disabled(self) -> None:
        record = self._record("review", attempts=1)
        should_run, reason = self.ledger.should_run(
            record, "h1", retry_review=False, max_attempts=3
        )
        self.assertFalse(should_run)
        self.assertIn("关闭", reason)

    def test_failed_retries_until_limit(self) -> None:
        record = self._record("failed", attempts=1)
        should_run, _ = self.ledger.should_run(record, "h1", retry_review=True, max_attempts=3)
        self.assertTrue(should_run)
        record.attempts = 3
        should_run, _ = self.ledger.should_run(record, "h1", retry_review=True, max_attempts=3)
        self.assertFalse(should_run)

    def test_round_trip_persistence(self) -> None:
        record = self._record("review", attempts=2)
        record.success = 7
        record.review = 3
        record.last_error = "PADDLE_OCR_INTERRUPTED: 服务未恢复"
        self.ledger.save()
        reloaded = ocr_daemon.BatchLedger(self.ledger.path)
        again = reloaded.records["0918"]
        self.assertEqual(again.status, "review")
        self.assertEqual(again.attempts, 2)
        self.assertEqual(again.success, 7)
        self.assertIn("PADDLE_OCR_INTERRUPTED", again.last_error)


class UnattendedModeTest(unittest.TestCase):
    """服务进程没有 tty，缺配置必须立刻报错，绝不能停在 input() 上。"""

    def test_assume_yes_reads_env(self) -> None:
        with mock.patch.dict(os.environ, {"OCR_ASSUME_YES": "1"}, clear=False):
            self.assertTrue(demo.assume_yes())
        with mock.patch.dict(os.environ, {"OCR_ASSUME_YES": "0"}, clear=False):
            self.assertFalse(demo.assume_yes())
        env = {key: value for key, value in os.environ.items() if key != "OCR_ASSUME_YES"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertFalse(demo.assume_yes())

    def test_required_text_fails_fast_instead_of_prompting(self) -> None:
        with mock.patch.dict(os.environ, {"OCR_ASSUME_YES": "1"}, clear=False):
            os.environ.pop("OCR_DAEMON_TEST_MISSING", None)
            with mock.patch("builtins.input", side_effect=AssertionError("不应该提示输入")):
                with mock.patch("getpass.getpass", side_effect=AssertionError("不应该提示输入")):
                    with self.assertRaises(demo.DemoError) as ctx:
                        demo.required_text("OCR_DAEMON_TEST_MISSING", "测试密钥", secret=True)
        self.assertEqual(ctx.exception.code, "CONFIG_REQUIRED")
        self.assertIn("OCR_DAEMON_TEST_MISSING", str(ctx.exception))

    def test_real_config_is_non_interactive(self) -> None:
        env = {
            **ENV_BASE,
            "OCR_ASSUME_YES": "1",
            "PADDLE_OCR_API_URL": "http://192.168.1.115:8870/v1/ocr",
        }
        manifest = {"root": "/tmp", "platform": "jd", "products": [], "manifest_hash": "h"}
        with mock.patch.dict(os.environ, env, clear=True):
            with mock.patch("builtins.input", side_effect=AssertionError("不应该提示输入")):
                config = demo.real_config(manifest)
        self.assertFalse(config["mock"])
        self.assertEqual(config["ocr_provider"], "paddleocr-vl")
        self.assertEqual(config["redshift_connection"]["password"], "pw")

    def test_real_config_requires_explicit_rotation_opt_in(self) -> None:
        env = {**ENV_BASE, "OCR_ASSUME_YES": "1"}
        env.pop("OCR_DEMO_KEYS_ROTATED")
        manifest = {"root": "/tmp", "platform": "jd", "products": [], "manifest_hash": "h"}
        with mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaises(demo.DemoError) as ctx:
                demo.real_config(manifest)
        self.assertEqual(ctx.exception.code, "KEY_ROTATION_REQUIRED")


class PreflightTest(unittest.TestCase):
    def test_reports_missing_credentials_and_rotation(self) -> None:
        env = {"ALERT_MAIL_ENABLED": "0"}
        with mock.patch.dict(os.environ, env, clear=True):
            mail = email_alert.mail_config()
            problems = ocr_daemon.preflight_env(mail)
        self.assertIn("DASHSCOPE_API_KEY", problems)
        self.assertIn("POSTGRES_PASSWORD", problems)
        self.assertIn("OCR_DEMO_KEYS_ROTATED", problems)

    def test_passes_when_environment_is_complete(self) -> None:
        with mock.patch.dict(os.environ, ENV_BASE, clear=True):
            mail = email_alert.mail_config()
            self.assertEqual(ocr_daemon.preflight_env(mail), [])


class MailConfigTest(unittest.TestCase):
    def test_default_recipient_is_the_ops_mailbox(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            config = email_alert.mail_config()
        self.assertEqual(config.recipients, ("13306032298@163.com",))
        self.assertFalse(config.ready, "没配账号密码时不应该认为已就绪")

    def test_multiple_recipients_and_password_masking(self) -> None:
        env = {
            "ALERT_SMTP_USER": "sender@163.com",
            "ALERT_SMTP_PASSWORD": "super-secret",
            "ALERT_MAIL_TO": "a@x.com, b@y.com;c@z.com",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            config = email_alert.mail_config()
        self.assertEqual(config.recipients, ("a@x.com", "b@y.com", "c@z.com"))
        self.assertTrue(config.ready)
        self.assertTrue(config.enabled)
        self.assertEqual(config.port, 465)
        self.assertNotIn("super-secret", config.summary())
        self.assertIn("已设置", config.summary())

    def test_broken_int_falls_back_to_default(self) -> None:
        env = {"ALERT_SMTP_PORT": "not-a-number"}
        with mock.patch.dict(os.environ, env, clear=True):
            config = email_alert.mail_config()
        self.assertEqual(config.port, email_alert.DEFAULT_SMTP_PORT)

    def test_disabled_mail_is_a_no_op_and_never_raises(self) -> None:
        with mock.patch.dict(os.environ, {"ALERT_MAIL_ENABLED": "0"}, clear=True):
            self.assertFalse(email_alert.send_mail("主题", "正文"))
            self.assertFalse(
                email_alert.notify_pipeline_failure(
                    batch_label="0918", error_code="X", message="boom"
                )
            )

    def test_smtp_failure_is_swallowed(self) -> None:
        env = {
            "ALERT_MAIL_ENABLED": "1",
            "ALERT_SMTP_USER": "sender@163.com",
            "ALERT_SMTP_PASSWORD": "pw",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            with mock.patch(
                "smtplib.SMTP_SSL", side_effect=OSError("network down")
            ):
                self.assertFalse(email_alert.send_mail("主题", "正文"))


class RunOneBatchTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.input_root = self.root / "inbox"
        self.batch = make_batch(self.input_root, "0918", {"1001": 2, "1002": 2, "1003": 2})
        self.options = ocr_daemon.RunOptions(
            input_root=self.input_root,
            platform="jd",
            template_path=self.root / "template-v2.json",
            state_dir=self.root / "state",
            output_root=self.root / "runs",
            ocr_workers=1,
            bulk_first_pass=False,
            chunk_size=2,
            dry_run=False,
            retryable_backoff=0,
            max_attempts=3,
            retry_review=True,
            mail=disabled_mail(),
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_dry_run_reports_plan_without_calling_pipeline(self) -> None:
        options = ocr_daemon.RunOptions(**{**self.options.__dict__, "dry_run": True})
        with mock.patch.object(demo, "execute_pipeline") as pipeline:
            result, _ = ocr_daemon.run_one_batch(self.batch, options, config=None)
        pipeline.assert_not_called()
        self.assertEqual(result["status"], "dry-run")
        self.assertEqual(result["product_count"], 3)
        self.assertEqual(result["chunk_count"], 2)

    def test_chunks_are_aggregated_and_config_metrics_reset(self) -> None:
        calls: list[dict] = []

        def fake_execute(manifest, template_path, state_dir, output_root, config, **kwargs):
            config.setdefault("metrics", {})
            config["metrics"]["ocr_api_calls"] = config["metrics"].get("ocr_api_calls", 0) + 1
            config["run_output_dir"] = str(Path(output_root) / f"run-{len(calls)}")
            calls.append({"count": len(manifest["products"]), "metrics": dict(config["metrics"])})
            return {
                "status": "complete",
                "run_id": f"run-{len(calls) - 1}",
                "output_dir": config["run_output_dir"],
                "success_count": len(manifest["products"]),
                "review_count": 0,
                "workbook": None,
                "performance": {
                    "total_ms": 1000,
                    "ocr": {
                        "planned_images": 4,
                        "success_images": 4,
                        "failed_images": 0,
                        "cache_hits": 0,
                    },
                },
            }

        with mock.patch.object(demo, "real_config", return_value={"metrics": {}}):
            with mock.patch.object(demo, "execute_pipeline", side_effect=fake_execute):
                result, config = ocr_daemon.run_one_batch(self.batch, self.options, config=None)

        self.assertEqual([call["count"] for call in calls], [2, 1])
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["success_count"], 3)
        self.assertEqual(result["performance"]["ocr"]["planned_images"], 8)
        self.assertEqual(len(result["chunks"]), 2)
        # 关键：第二块拿到的是干净的 metrics，而不是第一块的累加结果。
        self.assertEqual(calls[1]["metrics"], {"ocr_api_calls": 1})
        self.assertIn("metrics", config)

    def test_reset_per_run_state_clears_metrics_and_output_dir(self) -> None:
        config = {"metrics": {"ocr_api_calls": 7}, "run_output_dir": "/tmp/old", "mock": False}
        ocr_daemon._reset_per_run_state(config)
        self.assertEqual(config["metrics"], {})
        self.assertNotIn("run_output_dir", config)
        self.assertFalse(config["mock"], "不应误删其它配置")

    def test_batch_with_no_products_raises(self) -> None:
        empty = self.input_root / "空批次"
        empty.mkdir(parents=True)
        with self.assertRaises(demo.DemoError) as ctx:
            ocr_daemon.run_one_batch(empty, self.options, config=None)
        self.assertEqual(ctx.exception.code, "NO_PRODUCTS")


class ProcessCycleTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.input_root = self.root / "inbox"
        make_batch(self.input_root, "0918", {"1001": 2})
        self.options = ocr_daemon.RunOptions(
            input_root=self.input_root,
            platform="jd",
            template_path=self.root / "template-v2.json",
            state_dir=self.root / "state",
            output_root=self.root / "runs",
            ocr_workers=1,
            bulk_first_pass=True,
            chunk_size=100,
            dry_run=False,
            retryable_backoff=0,
            max_attempts=3,
            retry_review=True,
            mail=disabled_mail(),
        )
        self.ledger = ocr_daemon.BatchLedger(self.root / "state" / "daemon" / "ledger.json")
        self.stopper = ocr_daemon.StopController()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_second_cycle_skips_the_completed_batch(self) -> None:
        fake_result = {
            "status": "complete",
            "batch_key": "0918",
            "run_id": "run-1",
            "output_dir": str(self.root / "runs" / "0918" / "run-1"),
            "success_count": 1,
            "review_count": 0,
            "workbook": None,
            "performance": {"total_ms": 10, "ocr": {}},
            "chunks": [],
        }
        with mock.patch.dict(os.environ, {"ALERT_MAIL_ENABLED": "0"}, clear=False):
            with mock.patch.object(ocr_daemon, "run_one_batch", return_value=(fake_result, {})) as runner:
                first = ocr_daemon.process_cycle(
                    self.options, self.ledger, self.stopper, config_holder={}
                )
                second = ocr_daemon.process_cycle(
                    self.options, self.ledger, self.stopper, config_holder={}
                )
        self.assertEqual((first["ran"], first["skipped"]), (1, 0))
        self.assertEqual((second["ran"], second["skipped"]), (0, 1))
        self.assertEqual(runner.call_count, 1)
        record = self.ledger.records["0918"]
        self.assertEqual(record.status, "complete")
        self.assertEqual(record.attempts, 1)

    def test_retryable_failure_is_recorded_and_keeps_batch_retryable(self) -> None:
        error = demo.DemoError("PADDLE_OCR_INTERRUPTED", "服务未在自动恢复窗口内恢复")
        with mock.patch.dict(os.environ, {"ALERT_MAIL_ENABLED": "0"}, clear=False):
            with mock.patch.object(ocr_daemon, "run_one_batch", side_effect=error):
                stats = ocr_daemon.process_cycle(
                    self.options, self.ledger, self.stopper, config_holder={}
                )
        self.assertEqual(stats["failed"], 1)
        self.assertTrue(stats["paused"], "可重试错误应该让本轮进入退避")
        record = self.ledger.records["0918"]
        self.assertEqual(record.status, "interrupted")
        self.assertIn("PADDLE_OCR_INTERRUPTED", record.last_error)
        should_run, _ = self.ledger.should_run(
            record, record.manifest_hash, retry_review=True, max_attempts=3
        )
        self.assertTrue(should_run, "可重试错误不应该让批次变成永久失败")

    def test_unhandled_exception_marks_batch_failed_without_killing_daemon(self) -> None:
        with mock.patch.dict(os.environ, {"ALERT_MAIL_ENABLED": "0"}, clear=False):
            with mock.patch.object(
                ocr_daemon, "run_one_batch", side_effect=RuntimeError("boom")
            ):
                stats = ocr_daemon.process_cycle(
                    self.options, self.ledger, self.stopper, config_holder={}
                )
        self.assertEqual(stats["failed"], 1)
        self.assertEqual(self.ledger.records["0918"].status, "failed")


class PidFileTest(unittest.TestCase):
    def test_acquire_and_release(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pid_file = Path(tmp) / "daemon" / "ocr-daemon.pid"
            ocr_daemon.acquire_pid_file(pid_file)
            self.assertEqual(pid_file.read_text(encoding="utf-8").strip(), str(os.getpid()))
            ocr_daemon.release_pid_file(pid_file)
            self.assertFalse(pid_file.exists())

    def test_live_pid_blocks_duplicate_start(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pid_file = Path(tmp) / "ocr-daemon.pid"
            pid_file.write_text("4242", encoding="utf-8")
            # 直接打桩进程存活判断，避免依赖具体 PID 是否恰好存在。
            with mock.patch.object(ocr_daemon, "_process_alive", return_value=True):
                with self.assertRaises(ocr_daemon.AlreadyRunning):
                    ocr_daemon.acquire_pid_file(pid_file)
            self.assertEqual(pid_file.read_text(encoding="utf-8").strip(), "4242")

    def test_stale_pid_file_is_reclaimed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pid_file = Path(tmp) / "ocr-daemon.pid"
            pid_file.write_text("999999999", encoding="utf-8")
            ocr_daemon.acquire_pid_file(pid_file)
            self.assertEqual(pid_file.read_text(encoding="utf-8").strip(), str(os.getpid()))


class StopControllerTest(unittest.TestCase):
    def test_sleep_returns_early_after_stop_request(self) -> None:
        stopper = ocr_daemon.StopController()
        stopper.requested = True
        self.assertFalse(stopper.sleep(5))

    def test_sleep_completes_when_not_interrupted(self) -> None:
        stopper = ocr_daemon.StopController()
        self.assertTrue(stopper.sleep(0.05))


class PipelineResultMailTest(unittest.TestCase):
    def test_review_result_builds_body_with_review_images(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            (output_dir / "待复核图片.json").write_text(
                json.dumps(
                    [
                        {
                            "platform": "jd",
                            "product_id": "100005996353",
                            "image_name": "21.jpg",
                            "error": {"code": "PADDLE_OCR_REVIEW", "message": "Paddle OCR HTTP 422"},
                        }
                    ],
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            captured: dict[str, object] = {}

            def fake_notify(title, lines, *, severity="info", attachments=None, config=None):
                captured["title"] = title
                captured["severity"] = severity
                captured["body"] = "\n".join(lines)
                return True

            with mock.patch.dict(os.environ, {"ALERT_MAIL_ENABLED": "0"}, clear=False):
                with mock.patch.object(email_alert, "notify", side_effect=fake_notify):
                    email_alert.notify_pipeline_result(
                        {
                            "status": "review",
                            "run_id": "run-1",
                            "success_count": 97,
                            "review_count": 3,
                            "output_dir": str(output_dir),
                            "performance": {
                                "total_ms": 3_827_332,
                                "ocr": {
                                    "planned_images": 2044,
                                    "success_images": 2031,
                                    "failed_images": 13,
                                    "cache_hits": 499,
                                    "success_p50_ms": 6411,
                                },
                            },
                        },
                        batch_label="0918",
                    )
            self.assertEqual(captured["severity"], "warning")
            self.assertIn("0918", str(captured["title"]))
            body = str(captured["body"])
            self.assertIn("100005996353/21.jpg", body)
            self.assertIn("Paddle OCR HTTP 422", body)
            self.assertIn("1 小时 3 分", body)
            self.assertIn("缓存命中 499", body)

    def test_success_result_is_silent_when_alert_on_success_disabled(self) -> None:
        with mock.patch.dict(
            os.environ, {"ALERT_MAIL_ENABLED": "0", "ALERT_ON_SUCCESS": "0"}, clear=False
        ):
            with mock.patch.object(email_alert, "notify") as patched:
                sent = email_alert.notify_pipeline_result(
                    {"status": "complete", "success_count": 10, "review_count": 0},
                    batch_label="0918",
                )
        self.assertFalse(sent)
        patched.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
