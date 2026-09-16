from __future__ import annotations

import importlib.util
import os
import time
from pathlib import Path
from tkinter import Tk, filedialog
from typing import Any

import demo


APP_DIR = Path(__file__).resolve().parent


def format_result(result: dict[str, Any]) -> dict[str, Any]:
    image = result.get("input_image") or {}
    source_bytes = int(image.get("source_bytes", image.get("original_bytes", 0)) or 0)
    sent_bytes = int(image.get("sent_bytes", image.get("bytes", 0)) or 0)
    return {
        "状态": "成功" if result.get("ok") else "失败",
        "耗时秒": round(float(result.get("duration_ms", 0)) / 1000, 3),
        "原图字节": source_bytes,
        "发送字节": sent_bytes,
        "OCR文本": result.get("markdown") or "",
        "错误": (result.get("error") or {}).get("message", ""),
    }


def load_frontend() -> Any:
    spec = importlib.util.spec_from_file_location("v6_frontend", APP_DIR / "直接用图片测试.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("无法读取前端配置")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def choose_image() -> Path | None:
    root = Tk()
    root.withdraw()
    try:
        selected = filedialog.askopenfilename(
            title="选择一张要测速的商品图片",
            filetypes=[("图片", "*.jpg *.jpeg *.png *.webp *.bmp *.tif *.tiff")],
        )
        return Path(selected) if selected else None
    finally:
        root.destroy()


def main() -> int:
    image_path = choose_image()
    if not image_path:
        print("已取消。")
        return 0
    frontend = load_frontend()
    values = frontend.load_saved()
    env = frontend.child_environment({key: str(values.get(key) or "") for key in frontend.SAVED_NAMES | frontend.SECRET_NAMES})
    os.environ.update(env)
    product = {"platform": str(values.get("platform") or "jd"), "product_id": "single-image", "images": []}
    config = demo.real_config({"products": [product]})
    image = {"name": image_path.name, "path": str(image_path.resolve()), "sha256": demo.file_sha256(image_path)}
    store = demo.StateStore(APP_DIR / ".state")
    try:
        started = time.perf_counter()
        result = demo.run_ocr_one("single-image-benchmark", product, image, config, store, demo.ConcurrencyMeter())
        result["duration_ms"] = round((time.perf_counter() - started) * 1000)
    finally:
        store.close()
    report = format_result(result)
    print("\n========== 单图纯 OCR 测速 ==========")
    for key, value in report.items():
        print(f"{key}: {value}")
    print("====================================")
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
