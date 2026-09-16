from __future__ import annotations

import importlib.util
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any

import demo
from single_image_ocr_benchmark import format_result


APP_DIR = Path(__file__).resolve().parent


def default_connection_values(saved: dict[str, object]) -> dict[str, str]:
    return {
        "vllm_ocr_api_base": str(saved.get("vllm_ocr_api_base") or ""),
        "vllm_ocr_model": str(saved.get("vllm_ocr_model") or ""),
        "vllm_ocr_model_version": str(saved.get("vllm_ocr_model_version") or ""),
        "vllm_ocr_api_key": str(saved.get("vllm_ocr_api_key") or ""),
        "platform": str(saved.get("platform") or "jd"),
    }


def load_saved() -> dict[str, object]:
    spec = importlib.util.spec_from_file_location("v6_frontend", APP_DIR / "直接用图片测试.py")
    if spec is None or spec.loader is None:
        return {}
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.load_saved()


class BenchmarkWindow:
    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title("本地 Qwen 单图 OCR 测速")
        self.root.geometry("860x650")
        self.values = {name: tk.StringVar(value=value) for name, value in default_connection_values(load_saved()).items()}
        self.image_path = tk.StringVar()
        self.status = tk.StringVar(value="选择图片后开始；测速只调用本地 OCR。")
        self._build()

    def _build(self) -> None:
        frame = ttk.Frame(self.root, padding=14)
        frame.pack(fill="both", expand=True)
        fields = [("图片", self.image_path, True), ("本地 Qwen-VL /v1", self.values["vllm_ocr_api_base"], False), ("模型名", self.values["vllm_ocr_model"], False), ("模型版本", self.values["vllm_ocr_model_version"], False), ("API Key（可空）", self.values["vllm_ocr_api_key"], False), ("平台", self.values["platform"], False)]
        for row, (label, variable, browse) in enumerate(fields):
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", pady=5)
            ttk.Entry(frame, textvariable=variable, width=88, show="•" if label.startswith("API") else "").grid(row=row, column=1, sticky="ew", pady=5)
            if browse:
                ttk.Button(frame, text="选择图片", command=self.choose_image).grid(row=row, column=2, padx=(8, 0))
        frame.columnconfigure(1, weight=1)
        self.start = ttk.Button(frame, text="开始纯 OCR 测速", command=self.start_benchmark)
        self.start.grid(row=6, column=1, sticky="w", pady=(12, 8))
        ttk.Label(frame, textvariable=self.status).grid(row=7, column=0, columnspan=3, sticky="w", pady=(0, 8))
        self.output = tk.Text(frame, wrap="word", height=24)
        self.output.grid(row=8, column=0, columnspan=3, sticky="nsew")
        frame.rowconfigure(8, weight=1)

    def choose_image(self) -> None:
        selected = filedialog.askopenfilename(title="选择一张商品图片", filetypes=[("图片", "*.jpg *.jpeg *.png *.webp *.bmp *.tif *.tiff")])
        if selected:
            self.image_path.set(selected)

    def start_benchmark(self) -> None:
        path = Path(self.image_path.get())
        if not path.is_file() or not self.values["vllm_ocr_api_base"].get().strip() or not self.values["vllm_ocr_model"].get().strip():
            messagebox.showerror("信息不完整", "请选择图片，并填写本地 Qwen-VL /v1 地址和模型名。")
            return
        self.start.configure(state="disabled")
        self.status.set("正在请求本地 OCR；不会查询 Redshift 或调用后续 Qwen…")
        self.output.delete("1.0", "end")
        threading.Thread(target=self._run, args=(path,), daemon=True).start()

    def _run(self, path: Path) -> None:
        config = {
            "mock": False, "vllm_ocr_api_base": demo.ensure_vllm_api_base(self.values["vllm_ocr_api_base"].get().strip()),
            "vllm_ocr_api_key": self.values["vllm_ocr_api_key"].get().strip() or "EMPTY",
            "vllm_ocr_model": self.values["vllm_ocr_model"].get().strip(),
            "vllm_ocr_model_version": self.values["vllm_ocr_model_version"].get().strip() or "single-image-benchmark",
            "force_ocr": True,
            "ocr_max_attempts": 1, "ocr_request_timeout": 30,
        }
        product = {"platform": self.values["platform"].get().strip() or "jd", "product_id": "single-image", "images": []}
        image = {"name": path.name, "path": str(path.resolve()), "sha256": demo.file_sha256(path)}
        store = demo.StateStore(APP_DIR / ".state")
        try:
            started = time.perf_counter()
            result = demo.run_ocr_one("single-image-benchmark", product, image, config, store, demo.ConcurrencyMeter())
            result["duration_ms"] = round((time.perf_counter() - started) * 1000)
        except Exception as exc:
            result = {"ok": False, "duration_ms": 0, "error": {"message": str(exc)}}
        finally:
            store.close()
        report = format_result(result)
        text = "\n".join(f"{key}: {value}" for key, value in report.items())
        self.root.after(0, lambda: self._done(text, bool(result.get("ok"))))

    def _done(self, text: str, ok: bool) -> None:
        self.output.insert("1.0", text)
        self.status.set("测速完成" if ok else "测速失败；请查看错误信息。")
        self.start.configure(state="normal")

    def run(self) -> None:
        self.root.mainloop()


if __name__ == "__main__":
    BenchmarkWindow().run()
