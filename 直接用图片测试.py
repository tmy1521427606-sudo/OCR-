from __future__ import annotations

import base64
import ctypes
import importlib.util
import ipaddress
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import urllib.parse
from ctypes import wintypes
from pathlib import Path
from typing import Any, Callable


APP_DIR = Path(__file__).resolve().parent
VENV_DIR = APP_DIR / ".venv"
VENV_PYTHON = VENV_DIR / "Scripts" / "python.exe"
CA_BUNDLE = VENV_DIR / "Lib" / "site-packages" / "pip" / "_vendor" / "certifi" / "cacert.pem"
CONFIG_PATH = APP_DIR / ".state" / "frontend-config.json"
MAX_PRODUCTS = 100
SECRET_NAMES = {"qwen_key", "pg_password"}
SAVED_NAMES = {
    "root",
    "platform",
    "paddle_ocr_api_url",
    "paddle_ocr_model_version",
    "pg_host",
    "pg_port",
    "pg_database",
    "pg_user",
    "pg_schema",
    "pg_sslmode",
    "ocr_workers",
}
CRYPTPROTECT_UI_FORBIDDEN = 0x1


def test_ocr_service_url(
    api_url: str,
    *,
    verifier: Callable[[str, float], None] | None = None,
) -> dict[str, Any]:
    """Check the configured OCR endpoint without sending an image."""
    import paddle_ocr

    url = api_url.strip()
    check = verifier or paddle_ocr.verify_paddle_available
    try:
        check(url, 3.0)
    except Exception as exc:
        return {"ok": False, "url": url, "message": str(exc)}
    endpoint = urllib.parse.urlparse(url).netloc or url
    return {"ok": True, "url": url, "message": f"OCR 服务可达：{endpoint}"}


def is_current_ocr_service_verified(api_url: str, result: dict[str, Any] | None) -> bool:
    return bool(result and result.get("ok") and result.get("url") == api_url.strip())


class DataBlob(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]


def _data_blob(data: bytes) -> tuple[DataBlob, object]:
    buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    return DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))), buffer


def _dpapi(name: str, data: bytes) -> bytes:
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    output = DataBlob()
    input_blob, input_buffer = _data_blob(data)
    if name == "protect":
        function = crypt32.CryptProtectData
        function.argtypes = [
            ctypes.POINTER(DataBlob), wintypes.LPCWSTR, ctypes.POINTER(DataBlob),
            wintypes.LPVOID, wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(DataBlob),
        ]
        arguments = (ctypes.byref(input_blob), "OCR Demo", None, None, None, CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(output))
    else:
        function = crypt32.CryptUnprotectData
        function.argtypes = [
            ctypes.POINTER(DataBlob), ctypes.POINTER(wintypes.LPWSTR), ctypes.POINTER(DataBlob),
            wintypes.LPVOID, wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(DataBlob),
        ]
        arguments = (ctypes.byref(input_blob), None, None, None, None, CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(output))
    function.restype = wintypes.BOOL
    if not function(*arguments):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(output.data, output.size)
    finally:
        kernel32.LocalFree.argtypes = [wintypes.LPVOID]
        kernel32.LocalFree.restype = wintypes.LPVOID
        kernel32.LocalFree(output.data)
        del input_buffer


def protect_secret(value: str) -> str:
    return base64.b64encode(_dpapi("protect", value.encode("utf-8"))).decode("ascii") if value else ""


def unprotect_secret(value: str) -> str:
    return _dpapi("unprotect", base64.b64decode(value, validate=True)).decode("utf-8") if value else ""


def bootstrap() -> int:
    if "--self-check" in sys.argv:
        self_check()
        return 0
    if sys.version_info[:2] != (3, 12):
        print(f"需要 Python 3.12，当前是 {sys.version.split()[0]}")
        return 1
    if Path(sys.executable).resolve() != VENV_PYTHON.resolve():
        if not VENV_PYTHON.exists():
            print("首次运行：正在创建 .venv ...")
            subprocess.check_call([sys.executable, "-m", "venv", str(VENV_DIR)])
        child_args = [arg for arg in sys.argv[1:] if arg != "--inside-venv"]
        return subprocess.call([str(VENV_PYTHON), str(Path(__file__).resolve()), "--inside-venv", *child_args])
    if any(importlib.util.find_spec(name) is None for name in ("psycopg", "openpyxl", "PIL")):
        print("首次运行：正在安装 psycopg、openpyxl 和 Pillow ...")
        subprocess.check_call(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "psycopg[binary]>=3.2,<4",
                "openpyxl>=3.1,<4",
                "Pillow>=10,<13",
            ]
        )
    run_gui()
    return 0


def load_saved() -> dict[str, object]:
    try:
        value = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            return {}
        protected = value.pop("protected_secrets", {})
        if isinstance(protected, dict):
            for name in SECRET_NAMES:
                encrypted = protected.get(name, "")
                try:
                    value[name] = unprotect_secret(encrypted) if isinstance(encrypted, str) else ""
                except (OSError, ValueError, UnicodeError):
                    value[name] = ""
        return value
    except (OSError, json.JSONDecodeError):
        return {}


def config_payload(
    values: dict[str, str],
    selected: list[str],
    rotated: bool,
    bulk_first_pass: bool = False,
    force_new: bool = False,
    force_ocr: bool = False,
) -> dict[str, object]:
    data = {name: values.get(name, "") for name in SAVED_NAMES}
    data["selected_products"] = selected
    data["keys_rotated"] = rotated
    data["bulk_first_pass"] = bulk_first_pass
    data["force_new"] = force_new
    data["force_ocr"] = force_ocr
    data["protected_secrets"] = {
        name: protect_secret(values.get(name, "")) for name in SECRET_NAMES
    }
    return data


def save_config(
    values: dict[str, str],
    selected: list[str],
    rotated: bool,
    bulk_first_pass: bool,
    force_new: bool,
    force_ocr: bool,
) -> None:
    data = config_payload(values, selected, rotated, bulk_first_pass, force_new, force_ocr)
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = CONFIG_PATH.with_name(CONFIG_PATH.name + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, CONFIG_PATH)


def build_demo_command(
    values: dict[str, str],
    selected: list[str],
    bulk_first_pass: bool,
    force_new: bool,
    force_ocr: bool,
) -> list[str]:
    command = [
        str(VENV_PYTHON),
        str(APP_DIR / "demo.py"),
        "--root",
        str(Path(values["root"]).resolve()),
        "--products",
        *selected,
        "--platform",
        values["platform"].strip(),
        "--ocr-workers",
        values.get("ocr_workers", "3").strip() or "3",
        "--open",
    ]
    if bulk_first_pass:
        command.append("--bulk-first-pass")
    if force_new:
        command.append("--new-run")
    if force_ocr:
        command.append("--force-ocr")
    return command


def validate(values: dict[str, str], selected: list[str], rotated: bool) -> str | None:
    required = {
        "root": "批次目录",
        "platform": "平台",
        "paddle_ocr_api_url": "PaddleOCR /v1/ocr 地址",
        "paddle_ocr_model_version": "PaddleOCR 模型版本",
        "pg_host": "PostgreSQL 主机",
        "pg_database": "PostgreSQL 数据库名",
        "pg_user": "PostgreSQL 用户名",
        "qwen_key": "DashScope API Key",
        "pg_password": "PostgreSQL 密码",
    }
    missing = [label for name, label in required.items() if not values.get(name, "").strip()]
    if missing:
        raise ValueError("请填写：" + "、".join(missing))
    if not Path(values["root"]).is_dir():
        raise ValueError("批次目录不存在")
    if not 1 <= len(selected) <= MAX_PRODUCTS:
        raise ValueError(f"必须选择 1 到 {MAX_PRODUCTS} 个商品，当前选择 {len(selected)} 个")
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,32}", values["platform"].strip()):
        raise ValueError("平台只能包含英文字母、数字、点、下划线或短横线")
    try:
        ocr_workers = int(values.get("ocr_workers", "3"))
    except ValueError as exc:
        raise ValueError("OCR 并发必须是 1 到 6 的整数") from exc
    if not 1 <= ocr_workers <= 6:
        raise ValueError("OCR 并发必须是 1 到 6 的整数")
    parsed = urllib.parse.urlparse(values["paddle_ocr_api_url"].strip())
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc or not parsed.path.rstrip("/").endswith("/v1/ocr"):
        raise ValueError("PaddleOCR 必须是以 /v1/ocr 结尾的完整 http(s) 地址")
    host = values["pg_host"].strip()
    if "://" in host or "/" in host or ":" in host:
        raise ValueError("PostgreSQL 主机只填域名或 IP，不要带协议、端口或数据库名")
    try:
        port = int(values["pg_port"].strip())
        if not 1 <= port <= 65535:
            raise ValueError
    except ValueError as exc:
        raise ValueError("PostgreSQL 端口必须是 1 到 65535 之间的数字") from exc
    if values.get("pg_sslmode", "verify-full") not in {"disable", "require", "verify-ca", "verify-full"}:
        raise ValueError("PostgreSQL SSL 模式只能是 disable、require、verify-ca 或 verify-full")
    if not rotated:
        raise ValueError("请先确认旧的阿里密钥已经吊销并轮换")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return None
    return "当前 PostgreSQL 主机是 IP。verify-full 通常需要填写证书对应的 DNS 主机名，否则会主机名校验失败。"


def child_environment(values: dict[str, str]) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "PADDLE_OCR_API_URL": values["paddle_ocr_api_url"].strip(),
            "PADDLE_OCR_MODEL_VERSION": values["paddle_ocr_model_version"].strip(),
            "DASHSCOPE_API_KEY": values["qwen_key"].strip(),
            "OCR_DEMO_KEYS_ROTATED": "YES",
            "POSTGRES_HOST": values["pg_host"].strip(),
            "POSTGRES_PORT": values["pg_port"].strip(),
            "POSTGRES_DATABASE": values["pg_database"].strip(),
            "POSTGRES_USER": values["pg_user"].strip(),
            "POSTGRES_PASSWORD": values["pg_password"],
            "POSTGRES_SSLMODE": values.get("pg_sslmode", "verify-full").strip(),
            "PGSSLROOTCERT": str(CA_BUNDLE) if CA_BUNDLE.is_file() else "system",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
        }
    )
    schema = values.get("pg_schema", "").strip()
    if schema:
        env["POSTGRES_SCHEMA"] = schema
    else:
        env.pop("POSTGRES_SCHEMA", None)
    return env


def self_check() -> None:
    values = {
        "root": str(APP_DIR),
        "platform": "jd",
        "paddle_ocr_api_url": "http://192.168.1.115:8870/v1/ocr",
        "paddle_ocr_model_version": "PaddleOCR-VL-1.6",
        "pg_host": "postgres.example.internal",
        "pg_port": "5432",
        "pg_database": "db",
        "pg_user": "reader",
        "pg_schema": "workdb",
        "pg_sslmode": "verify-full",
        "qwen_key": "secret-b",
        "pg_password": "secret-c",
    }
    assert validate(values, ["1"], True) is None
    assert validate(values, [str(index) for index in range(MAX_PRODUCTS)], True) is None
    try:
        validate(values, [str(index) for index in range(MAX_PRODUCTS + 1)], True)
    except ValueError:
        pass
    else:
        raise AssertionError("超过100个商品时必须拒绝运行")
    env = child_environment(values)
    assert env["PGSSLROOTCERT"] and env["POSTGRES_SSLMODE"] == "verify-full"
    try:
        payload = config_payload(values, ["1", "2", "3", "4", "5"], True, True)
    except OSError as exc:
        # Codex 的隔离账号未加载 Windows 用户配置；桌面用户启动时会执行真实 DPAPI 往返。
        print(f"GUI_SELF_CHECK_DPAPI_UNAVAILABLE={getattr(exc, 'winerror', 'unknown')}")
        return
    serialized = json.dumps(payload)
    assert payload["bulk_first_pass"] is True
    assert all(values[name] not in serialized for name in SECRET_NAMES)
    protected = payload["protected_secrets"]
    assert isinstance(protected, dict)
    assert all(unprotect_secret(protected[name]) == values[name] for name in SECRET_NAMES)
    print("GUI_SELF_CHECK_OK")


def run_gui() -> None:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    import demo

    class App:
        def __init__(self, root: tk.Tk) -> None:
            self.root = root
            self.root.title("PaddleOCR-VL 完整商品工作流 v7")
            self.root.geometry("980x900")
            self.root.minsize(820, 720)
            self.saved = load_saved()
            defaults = {
                "root": "",
                "platform": "jd",
                "paddle_ocr_api_url": "http://192.168.1.115:8870/v1/ocr",
                "paddle_ocr_model_version": "PaddleOCR-VL-1.6",
                "pg_host": "",
                "pg_port": "5432",
                "pg_database": "",
                "pg_user": "",
                "pg_schema": "workdb",
                "pg_sslmode": "verify-full",
                "ocr_workers": "1",
                "qwen_key": "",
                "pg_password": "",
            }
            self.vars = {
                name: tk.StringVar(value=str(self.saved.get(name, default)))
                for name, default in defaults.items()
            }
            self.rotated = tk.BooleanVar(value=bool(self.saved.get("keys_rotated", False)))
            self.bulk_first_pass = tk.BooleanVar(
                value=bool(self.saved.get("bulk_first_pass", False))
            )
            self.force_new = tk.BooleanVar(value=bool(self.saved.get("force_new", True)))
            self.force_ocr = tk.BooleanVar(value=bool(self.saved.get("force_ocr", False)))
            self.products: list[Path] = []
            self.events: queue.Queue[tuple[str, object]] = queue.Queue()
            self.process: subprocess.Popen[str] | None = None
            self.autosave_job: str | None = None
            self.log_window: tk.Toplevel | None = None
            self.log_popup: tk.Text | None = None
            self.running = False
            self.run_started = 0.0
            self.ocr_service_test: dict[str, Any] | None = None
            self.ocr_test_running = False
            self.output_dir: Path | None = None
            self.live_report: Path | None = None
            self.final_report: Path | None = None
            self.build()
            if self.vars["root"].get():
                self.scan_products()
            for name in self.vars:
                self.vars[name].trace_add("write", self.schedule_autosave)
            self.vars["paddle_ocr_api_url"].trace_add("write", self.invalidate_ocr_service_test)
            self.rotated.trace_add("write", self.schedule_autosave)
            self.bulk_first_pass.trace_add("write", self.schedule_autosave)
            self.force_new.trace_add("write", self.schedule_autosave)
            self.force_ocr.trace_add("write", self.schedule_autosave)
            self.product_list.bind("<<ListboxSelect>>", self.schedule_autosave)
            self.root.after(100, self.drain_events)
            self.root.after(1000, self.update_runtime)
            self.root.after_idle(lambda: self.root.state("zoomed"))
            self.root.protocol("WM_DELETE_WINDOW", self.close)

        def build(self) -> None:
            outer = ttk.Frame(self.root, padding=10)
            outer.pack(fill="both", expand=True)

            batch = ttk.LabelFrame(outer, text="1. 批次和商品", padding=8)
            batch.pack(fill="x")
            ttk.Label(batch, text="批次目录").grid(row=0, column=0, sticky="w")
            ttk.Entry(batch, textvariable=self.vars["root"]).grid(row=0, column=1, sticky="ew", padx=6)
            ttk.Button(batch, text="选择目录", command=self.choose_root).grid(row=0, column=2, padx=2)
            ttk.Button(batch, text="刷新", command=self.scan_products).grid(row=0, column=3, padx=2)
            batch.columnconfigure(1, weight=1)
            ttk.Label(batch, text=f"选择 1 到 {MAX_PRODUCTS} 个商品（Ctrl+单击可多选）").grid(
                row=1, column=0, columnspan=3, sticky="w", pady=(7, 2)
            )
            ttk.Button(batch, text="全选前100个", command=self.select_first_100).grid(
                row=1, column=3, sticky="e", pady=(7, 2)
            )
            self.product_list = tk.Listbox(batch, selectmode="extended", height=10, exportselection=False)
            self.product_list.grid(row=2, column=0, columnspan=4, sticky="ew")

            conn = ttk.LabelFrame(outer, text="2. 接口和数据库（自动保存）", padding=8)
            conn.pack(fill="x", pady=(8, 0))
            fields = [
                ("platform", "平台标识"),
                ("paddle_ocr_api_url", "PaddleOCR /v1/ocr 地址"),
                ("paddle_ocr_model_version", "PaddleOCR 模型版本"),
                ("pg_host", "PostgreSQL 主机 Endpoint"),
                ("pg_port", "PostgreSQL 端口"),
                ("pg_database", "PostgreSQL 数据库名"),
                ("pg_user", "PostgreSQL 只读用户名"),
                ("pg_schema", "Schema（默认 workdb）"),
                ("pg_sslmode", "SSL 模式（默认 verify-full）"),
            ]
            for row, (name, label) in enumerate(fields):
                ttk.Label(conn, text=label).grid(row=row, column=0, sticky="w", pady=2)
                ttk.Entry(conn, textvariable=self.vars[name]).grid(row=row, column=1, sticky="ew", padx=(8, 0), pady=2)
            self.ocr_test_button = ttk.Button(conn, text="测试 OCR 服务", command=self.test_ocr_service)
            self.ocr_test_button.grid(row=len(fields), column=0, sticky="w", pady=(7, 0))
            self.ocr_test_status = tk.StringVar(value="运行前请测试 OCR 服务")
            self.ocr_test_label = tk.Label(conn, textvariable=self.ocr_test_status, anchor="w", fg="#895b00")
            self.ocr_test_label.grid(row=len(fields), column=1, sticky="w", padx=(8, 0), pady=(7, 0))
            conn.columnconfigure(1, weight=1)

            secret = ttk.LabelFrame(outer, text="3. 凭据（Windows 加密保存，输入框隐藏）", padding=8)
            secret.pack(fill="x", pady=(8, 0))
            secret_fields = [
                ("qwen_key", "已轮换的 DashScope API Key"),
                ("pg_password", "PostgreSQL 密码"),
            ]
            for row, (name, label) in enumerate(secret_fields):
                ttk.Label(secret, text=label).grid(row=row, column=0, sticky="w", pady=2)
                ttk.Entry(secret, textvariable=self.vars[name], show="●").grid(row=row, column=1, sticky="ew", padx=(8, 0), pady=2)
            ttk.Checkbutton(
                secret,
                text="我确认截图中暴露的旧阿里密钥已经吊销，并填写了新密钥",
                variable=self.rotated,
            ).grid(row=3, column=0, columnspan=2, sticky="w", pady=(6, 0))
            secret.columnconfigure(1, weight=1)

            mode = ttk.LabelFrame(outer, text="4. 运行方式", padding=8)
            mode.pack(fill="x", pady=(8, 0))
            ttk.Checkbutton(
                mode,
                text="批量首轮（跳过联网搜索，只生成核心结果和待补全字段；约1万商品时建议勾选）",
                variable=self.bulk_first_pass,
            ).grid(row=0, column=0, columnspan=3, sticky="w")
            ttk.Checkbutton(
                mode,
                text="强制创建新运行（不恢复未完成或服务中断的运行）",
                variable=self.force_new,
            ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(4, 0))
            ttk.Checkbutton(
                mode,
                text="测速：强制重新 OCR（忽略成功缓存，会重复请求图片）",
                variable=self.force_ocr,
            ).grid(row=2, column=0, columnspan=3, sticky="w", pady=(4, 0))
            ttk.Label(mode, text="Paddle 批次并行（先测 1、2）").grid(row=3, column=0, sticky="w", pady=(6, 0))
            ttk.Combobox(
                mode,
                textvariable=self.vars["ocr_workers"],
                values=("1", "2"),
                state="readonly",
                width=8,
            ).grid(row=3, column=1, sticky="w", padx=8, pady=(6, 0))
            ttk.Label(mode, text="Paddle 每批最多 8 图；并行只提高整体吞吐").grid(
                row=3, column=2, sticky="w", pady=(6, 0)
            )
            ttk.Label(
                mode,
                text="服务中断会自动等待恢复约5分钟；超时后重新测试服务，再点重新运行即可继续未完成图片。",
                foreground="#895b00",
            ).grid(row=4, column=0, columnspan=3, sticky="w", pady=(6, 0))

            actions = ttk.Frame(outer)
            actions.pack(fill="x", pady=8)
            self.run_button = ttk.Button(actions, text="开始运行", command=self.start)
            self.run_button.pack(side="left")
            self.live_button = ttk.Button(actions, text="实时结果", command=self.open_live_report, state="disabled")
            self.live_button.pack(side="left", padx=6)
            self.report_button = ttk.Button(actions, text="正式报告", command=self.open_final_report, state="disabled")
            self.report_button.pack(side="left")
            self.folder_button = ttk.Button(actions, text="打开结果目录", command=self.open_output_dir, state="disabled")
            self.folder_button.pack(side="left", padx=6)
            ttk.Button(actions, text="查看实时日志", command=self.open_log_window).pack(side="left")
            ttk.Button(actions, text="清空日志", command=self.clear_logs).pack(side="left")
            self.status = tk.StringVar(value="填写配置后开始；失败时窗口和输入都会保留。")
            ttk.Label(actions, textvariable=self.status).pack(side="left", padx=10)

            logs = ttk.LabelFrame(outer, text="运行日志", padding=6)
            logs.pack(fill="both", expand=True)
            self.log = tk.Text(logs, height=14, wrap="word", state="normal")
            scroll = ttk.Scrollbar(logs, orient="vertical", command=self.log.yview)
            self.log.configure(yscrollcommand=scroll.set)
            self.log.pack(side="left", fill="both", expand=True)
            scroll.pack(side="right", fill="y")

        def values(self) -> dict[str, str]:
            return {name: variable.get() for name, variable in self.vars.items()}

        def invalidate_ocr_service_test(self, *_: object) -> None:
            if self.ocr_service_test is not None:
                self.ocr_service_test = None
                self.ocr_test_status.set("OCR 地址已变更，请重新测试")
                self.ocr_test_label.configure(fg="#895b00")

        def test_ocr_service(self) -> None:
            if self.ocr_test_running:
                return
            self.ocr_service_test = None
            self.ocr_test_running = True
            self.ocr_test_button.configure(state="disabled")
            self.ocr_test_status.set("正在测试 OCR 服务…")
            self.ocr_test_label.configure(fg="#1f4e78")
            url = self.vars["paddle_ocr_api_url"].get()

            def worker() -> None:
                self.events.put(("ocr_service_test", test_ocr_service_url(url)))

            threading.Thread(target=worker, daemon=True).start()

        def choose_root(self) -> None:
            chosen = filedialog.askdirectory(title="选择包含 product_id 子目录的批次目录")
            if chosen:
                self.vars["root"].set(chosen)
                self.scan_products()

        def scan_products(self) -> None:
            self.product_list.delete(0, "end")
            self.products = []
            path = Path(self.vars["root"].get().strip())
            if not path.is_dir():
                self.status.set("请选择有效的批次目录")
                return
            try:
                self.products = demo.product_candidates(path)
                for product in self.products:
                    self.product_list.insert("end", f"{product.name}  ({len(demo.find_images(product))} 张图片)")
            except OSError as exc:
                messagebox.showerror("目录读取失败", str(exc))
                return
            wanted = set(self.saved.get("selected_products", []))
            if wanted:
                for index, product in enumerate(self.products):
                    if product.name in wanted:
                        self.product_list.selection_set(index)
            elif len(self.products) == 5:
                self.product_list.selection_set(0, 4)
            self.status.set(f"找到 {len(self.products)} 个商品目录")

        def select_first_100(self) -> None:
            self.product_list.selection_clear(0, "end")
            count = min(len(self.products), MAX_PRODUCTS)
            if count:
                self.product_list.selection_set(0, count - 1)
            self.status.set(f"已选择 {count} 个商品")
            self.schedule_autosave()

        def selected_names(self) -> list[str]:
            return [self.products[index].name for index in self.product_list.curselection()]

        def schedule_autosave(self, *_: object) -> None:
            if self.autosave_job is not None:
                self.root.after_cancel(self.autosave_job)
            self.autosave_job = self.root.after(400, self.autosave)

        def autosave(self) -> None:
            self.autosave_job = None
            self.persist_config()

        def persist_config(self) -> bool:
            selected = self.selected_names()
            try:
                save_config(
                    self.values(),
                    selected,
                    self.rotated.get(),
                    self.bulk_first_pass.get(),
                    self.force_new.get(),
                    self.force_ocr.get(),
                )
                self.saved["selected_products"] = selected
                return True
            except OSError as exc:
                self.status.set(f"自动保存失败：{exc}")
                return False

        def start(self) -> None:
            values = self.values()
            selected = self.selected_names()
            if not is_current_ocr_service_verified(
                values["paddle_ocr_api_url"], self.ocr_service_test
            ):
                messagebox.showwarning(
                    "请先测试 OCR 服务",
                    "请先点击“测试 OCR 服务”。服务可达后才会启动正式任务，避免整批图片被标记待复核。",
                )
                return
            try:
                warning = validate(values, selected, self.rotated.get())
            except ValueError as exc:
                messagebox.showerror("配置不完整", str(exc))
                return
            image_count = sum(len(demo.find_images(self.products[index])) for index in self.product_list.curselection())
            notice = (
                f"将处理 {len(selected)} 个商品、{image_count} 张图片。\n\n"
                "图片会发送至本地 PaddleOCR-VL；OCR 文本会发送至阿里 Qwen；"
                "platform/product_id 会发送至 PostgreSQL。"
            )
            if self.bulk_first_pass.get():
                notice += "\n\n当前为批量首轮：不会调用 Qwen 联网搜索，缺失字段会进入待补全清单。"
            if self.force_ocr.get():
                notice += "\n\n当前为测速模式：会忽略 OCR 成功缓存，全部图片都会重新发送到本地 OCR。"
            if warning:
                notice += "\n\n警告：" + warning
            if not messagebox.askyesno("确认真实运行", notice + "\n\n确认继续吗？"):
                return
            if not self.persist_config():
                messagebox.showwarning("加密保存失败", "Windows 无法加密保存凭据；本次仍可运行，但关闭窗口后需要重新填写。")
            self.saved["selected_products"] = selected
            self.run_button.configure(state="disabled", text="运行中…")
            self.output_dir = None
            self.live_report = None
            self.final_report = None
            self.live_button.configure(state="disabled")
            self.report_button.configure(state="disabled")
            self.folder_button.configure(state="disabled")
            self.running = True
            self.run_started = time.monotonic()
            self.status.set("正在运行；下方会持续显示日志，实时结果按钮会在首个商品产出后启用。")
            self.append_log("\n========== 开始真实运行 ==========\n")
            command = build_demo_command(
                values,
                selected,
                self.bulk_first_pass.get(),
                self.force_new.get(),
                self.force_ocr.get(),
            )
            threading.Thread(
                target=self.worker,
                args=(command, child_environment(values)),
                daemon=True,
            ).start()

        def worker(self, command: list[str], env: dict[str, str]) -> None:
            try:
                self.process = subprocess.Popen(
                    command,
                    cwd=APP_DIR,
                    env=env,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                )
                assert self.process.stdin is not None and self.process.stdout is not None
                self.process.stdin.write("SEND\n")
                self.process.stdin.close()
                for line in self.process.stdout:
                    self.events.put(("log", line))
                code = self.process.wait()
                self.events.put(("done", code))
            except Exception as exc:
                self.events.put(("error", str(exc)))
            finally:
                self.process = None

        def drain_events(self) -> None:
            try:
                while True:
                    kind, value = self.events.get_nowait()
                    if kind == "log":
                        self.append_log(str(value))
                    elif kind == "ocr_service_test":
                        result = dict(value)  # type: ignore[arg-type]
                        self.ocr_test_running = False
                        self.ocr_test_button.configure(state="normal")
                        if is_current_ocr_service_verified(
                            self.vars["paddle_ocr_api_url"].get(), result
                        ):
                            self.ocr_service_test = result
                            self.ocr_test_status.set(str(result["message"]))
                            self.ocr_test_label.configure(fg="#176b3a")
                        else:
                            self.ocr_service_test = None
                            message = str(result.get("message") or "OCR 服务不可达")
                            self.ocr_test_status.set(
                                f"OCR 服务不可达：{message}；检查 VPN、路由或服务状态"
                            )
                            self.ocr_test_label.configure(fg="#9d2020")
                    elif kind == "done":
                        code = int(value)
                        self.running = False
                        self.run_button.configure(state="normal", text="重新运行")
                        self.status.set("运行完成，结果已打开。" if code == 0 else "运行失败；修改配置后可直接重新运行。")
                        if code != 0:
                            messagebox.showerror("运行失败", "输入已保留。请查看日志，修正后点击“重新运行”。")
                    elif kind == "error":
                        self.running = False
                        self.run_button.configure(state="normal", text="重新运行")
                        self.status.set("启动失败；输入已保留。")
                        messagebox.showerror("启动失败", str(value))
            except queue.Empty:
                pass
            self.root.after(100, self.drain_events)

        def update_runtime(self) -> None:
            if self.running:
                seconds = max(0, round(time.monotonic() - self.run_started))
                self.status.set(f"正在运行 {seconds // 60:02d}:{seconds % 60:02d}；下方持续显示日志。")
            self.root.after(1000, self.update_runtime)

        def open_log_window(self) -> None:
            if self.log_window is not None and self.log_window.winfo_exists():
                self.log_window.deiconify()
                self.log_window.lift()
                return
            window = tk.Toplevel(self.root)
            window.title("OCR 实时运行日志")
            window.geometry("1000x650")
            text = tk.Text(window, wrap="word")
            scroll = ttk.Scrollbar(window, orient="vertical", command=text.yview)
            text.configure(yscrollcommand=scroll.set)
            text.pack(side="left", fill="both", expand=True)
            scroll.pack(side="right", fill="y")
            text.insert("end", self.log.get("1.0", "end-1c"))
            text.see("end")
            self.log_window = window
            self.log_popup = text
            window.protocol("WM_DELETE_WINDOW", self.close_log_window)

        def close_log_window(self) -> None:
            if self.log_window is not None:
                self.log_window.destroy()
            self.log_window = None
            self.log_popup = None

        def clear_logs(self) -> None:
            self.log.delete("1.0", "end")
            if self.log_popup is not None:
                self.log_popup.delete("1.0", "end")

        def open_path(self, path: Path | None) -> None:
            if path is None:
                return
            try:
                os.startfile(path)  # type: ignore[attr-defined]
            except OSError as exc:
                messagebox.showerror("无法打开", str(exc))

        def open_live_report(self) -> None:
            self.open_path(self.live_report)

        def open_final_report(self) -> None:
            self.open_path(self.final_report)

        def open_output_dir(self) -> None:
            self.open_path(self.output_dir)

        def append_log(self, text: str) -> None:
            live_match = re.search(r"文件：(.+?实时结果\.html)", text)
            if live_match:
                self.live_report = Path(live_match.group(1).strip())
                self.output_dir = self.live_report.parent
                self.live_button.configure(state="normal")
                self.folder_button.configure(state="normal")
            output_match = re.search(r"OUTPUT_DIR=(.+)", text)
            if output_match:
                self.output_dir = Path(output_match.group(1).strip())
                self.folder_button.configure(state="normal")
            report_match = re.search(r"REPORT=(.+)", text)
            if report_match:
                self.final_report = Path(report_match.group(1).strip())
                self.report_button.configure(state="normal")
            interim_report_match = re.search(r"阶段性正式报告已更新：(.+?report\.html)", text)
            if interim_report_match:
                self.final_report = Path(interim_report_match.group(1).strip())
                self.output_dir = self.final_report.parent
                self.report_button.configure(state="normal")
                self.folder_button.configure(state="normal")
            self.log.insert("end", text)
            self.log.see("end")
            if self.log_popup is not None:
                self.log_popup.insert("end", text)
                self.log_popup.see("end")

        def close(self) -> None:
            if self.process is not None and self.process.poll() is None:
                if not messagebox.askyesno("任务仍在运行", "关闭窗口会中止当前任务；SQLite 会保留进度。确认关闭吗？"):
                    return
                self.process.terminate()
            if not self.persist_config() and not messagebox.askyesno(
                "加密保存失败", "关闭后会丢失凭据，仍要关闭吗？"
            ):
                return
            self.root.destroy()

    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    try:
        raise SystemExit(bootstrap())
    except subprocess.CalledProcessError as exc:
        print(f"环境准备失败，退出码：{exc.returncode}")
        raise SystemExit(exc.returncode)
