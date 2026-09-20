from __future__ import annotations

import base64
import json
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


PADDLE_BATCH_SIZE = 8


class PaddleOcrError(RuntimeError):
    pass


@dataclass(frozen=True)
class PaddleImage:
    request_id: str
    path: Path


def verify_paddle_available(api_url: str, timeout: float = 3.0) -> None:
    parsed = urllib.parse.urlparse(api_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise PaddleOcrError("Paddle OCR 地址无效")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        with socket.create_connection((parsed.hostname, port), timeout=timeout):
            pass
    except OSError as exc:
        raise PaddleOcrError(f"Paddle OCR unavailable: {exc}") from exc


def build_payload(images: list[PaddleImage]) -> dict[str, list[dict[str, str]]]:
    if not 1 <= len(images) <= PADDLE_BATCH_SIZE:
        raise PaddleOcrError(f"Paddle batch must contain 1 to {PADDLE_BATCH_SIZE} images")
    ids = [image.request_id for image in images]
    if len(set(ids)) != len(ids):
        # 服务端遇到重复 id 会整批返回 422，在本地就拦下来，报错信息才有指向性。
        raise PaddleOcrError("Paddle batch contains duplicate request ids")
    return {"images": [
        {"id": image.request_id, "image_base64": base64.b64encode(image.path.read_bytes()).decode("ascii")}
        for image in images
    ]}


def validate_results(expected_ids: list[str], results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    actual_ids = [str(item.get("id", "")) for item in results]
    if len(actual_ids) != len(expected_ids):
        raise PaddleOcrError("Paddle OCR returned an incomplete batch")
    if len(set(actual_ids)) != len(actual_ids) or set(actual_ids) != set(expected_ids):
        raise PaddleOcrError("Paddle OCR result IDs do not match request IDs")
    return [next(item for item in results if str(item["id"]) == image_id) for image_id in expected_ids]


def post_batch(api_url: str, images: list[PaddleImage], timeout: int = 30) -> list[dict[str, Any]]:
    request = urllib.request.Request(
        api_url.rstrip("/"),
        data=json.dumps(build_payload(images)).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise PaddleOcrError(f"Paddle OCR HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise PaddleOcrError(f"Paddle OCR unavailable: {exc}") from exc
    if not isinstance(body, dict) or not isinstance(body.get("results"), list):
        raise PaddleOcrError("Paddle OCR returned invalid JSON")
    return validate_results([image.request_id for image in images], body["results"])
