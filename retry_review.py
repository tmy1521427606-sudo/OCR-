from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any


def collect_review_images(manifest: dict[str, Any], documents: list[dict[str, Any]]) -> list[dict[str, str]]:
    successful: dict[tuple[str, str], set[str]] = {}
    for document in documents:
        identity = document.get("identity", {})
        key = (str(identity.get("platform") or ""), str(identity.get("product_id") or ""))
        successful[key] = {
            str(item.get("image") or "") for item in document.get("artifacts", {}).get("ocr", [])
        }
    queue: list[dict[str, str]] = []
    for product in manifest.get("products", []):
        key = (str(product.get("platform") or ""), str(product.get("product_id") or ""))
        for image in product.get("images", []):
            name = str(image.get("name") or "")
            if name and name not in successful.get(key, set()):
                queue.append({"platform": key[0], "product_id": key[1], "image_name": name})
    return queue


def merge_missing_fields(original: dict[str, Any], retry: dict[str, Any]) -> dict[str, Any]:
    """Keep confirmed values; use retry output only for empty/review fields."""
    merged = copy.deepcopy(original)
    fields = merged.setdefault("fields", {})
    statuses = merged.setdefault("field_status", {})
    for name, value in retry.get("fields", {}).items():
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        current = fields.get(name)
        state = statuses.get(name)
        if current is None or (isinstance(current, str) and not current.strip()) or state == "review":
            fields[name] = value
            statuses[name] = "retry_filled"
    return merged


def retry_manifest(queue: list[dict[str, str]], manifests: list[dict[str, Any]]) -> dict[str, Any]:
    wanted = {(item["platform"], item["product_id"], item["image_name"]) for item in queue}
    products: list[dict[str, Any]] = []
    for manifest in manifests:
        for product in manifest.get("products", []):
            images = [
                image for image in product.get("images", [])
                if (str(product.get("platform")), str(product.get("product_id")), str(image.get("name"))) in wanted
            ]
            if images:
                products.append({**product, "images": images})
    return {"products": products}


def load_product_documents(root: Path) -> dict[tuple[str, str], dict[str, Any]]:
    documents: dict[tuple[str, str], dict[str, Any]] = {}
    for path in root.rglob("products/*.json"):
        value = json.loads(path.read_text(encoding="utf-8"))
        identity = value.get("identity", {})
        key = (str(identity.get("platform") or ""), str(identity.get("product_id") or ""))
        if all(key):
            documents[key] = value
    return documents
