from __future__ import annotations

import argparse
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import demo
from batch_jobs import BatchJobStore


APP_DIR = Path(__file__).resolve().parent


def frontend_module():
    spec = importlib.util.spec_from_file_location("v6_frontend", APP_DIR / "直接用图片测试.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("无法加载前端配置")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> int:
    parser = argparse.ArgumentParser(description="创建并后台启动 OCR v6 10,000 商品任务")
    parser.add_argument("--root", help="商品目录；省略时使用前端已保存的目录")
    parser.add_argument("--platform", help="平台；省略时使用前端已保存的平台")
    args = parser.parse_args()
    frontend = frontend_module()
    values = frontend.load_saved()
    root = Path(args.root or str(values.get("root") or "")).resolve()
    platform = args.platform or str(values.get("platform") or "")
    if not root.is_dir() or not platform:
        raise SystemExit("请先启动 直接用图片测试.py 保存完整配置和批次目录，或传入 --root 与 --platform。")
    frontend.validate({key: str(values.get(key) or "") for key in frontend.SAVED_NAMES | frontend.SECRET_NAMES}, ["placeholder"], True)
    products = demo.product_candidates(root)
    manifest = demo.build_manifest(root, products, platform, allow_many=True)
    store = BatchJobStore(APP_DIR / ".state")
    try:
        job_id = store.create_job(manifest, {"template_path": str(APP_DIR / "template-v2.json")})
    finally:
        store.close()
    env = frontend.child_environment({key: str(values.get(key) or "") for key in frontend.SAVED_NAMES | frontend.SECRET_NAMES})
    process = subprocess.Popen(
        [str(APP_DIR / ".venv" / "Scripts" / "python.exe"), str(APP_DIR / "batch_worker.py"), "--job", job_id, "--state", str(APP_DIR / ".state")],
        cwd=APP_DIR, env=env, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
    )
    assert process.stdin is not None
    process.stdin.write(b"SEND\n")
    process.stdin.close()
    print(f"任务已后台启动：{job_id}")
    print(f"任务总览：{APP_DIR / 'runs' / job_id / '任务总览.html'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
