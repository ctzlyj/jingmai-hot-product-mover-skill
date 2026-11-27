"""Verified acceptance and reuse of one preview from its original listing package."""

import os
import hashlib
import json
from pathlib import Path
import re
import shutil
import tempfile

from photo_contract import media_entries, package_path
from photo_generation import atomic_json, load, sha


def scope_hash(manifest):
    facts = {key: manifest.get(key) for key in ("packageId", "store", "facts")}
    return hashlib.sha256(
        json.dumps(facts, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def validate_reuse(manifest, role, sku_key, relative, digest):
    record = manifest.get("acceptedSamples", {}).get(relative) if relative else None
    if record and (
        record.get("role") != role or record.get("skuKey") != sku_key or record.get("sha256") != digest
    ):
        raise ValueError("已接受样张与本图位、规格或文件哈希不一致")
    if record and record.get("scopeSha256") and record["scopeSha256"] != scope_hash(manifest):
        raise ValueError("样张验收后的商品、店铺或规格事实已改变")
    return record


def verified_preview(root, image_id, read_manifest):
    root = Path(root)
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,50}", image_id):
        raise ValueError("请选择原样张编号")
    parent = read_manifest(root)
    sample = package_path(root, ".samples/" + image_id)
    manifest = read_manifest(sample)
    if manifest.get("sampleOf") != {"packageId": parent["packageId"], "imageId": image_id} or (
        manifest["store"] != parent["store"] or manifest["facts"] != parent["facts"]
    ):
        raise ValueError("样张所属商品、店铺或规格已改变")
    plan = load(sample / "plan.json", [])
    if (
        len(plan) != 1
        or plan[0].get("id") != image_id
        or manifest.get("samplePlanSha256") != sha(sample / "plan.json")
    ):
        raise ValueError("原样张设计与回执不一致")
    media = list(media_entries(manifest))
    if len(media) != 1 or not media[0].get("previewOnly") or media[0].get("id") != image_id:
        raise ValueError("缺少逐图预览回执")
    original = package_path(sample, media[0]["localPath"])
    digest = sha(original)
    if digest != media[0]["sha256"]:
        raise ValueError("原样张像素与回执不一致")
    relative = f"已接受样张/{image_id}-{digest}.jpg"
    return parent, plan[0], original, digest, relative


def accepted_context(root, image_id, read_manifest):
    parent, task, _, digest, relative = verified_preview(root, image_id, read_manifest)
    target = package_path(root, relative)
    if relative not in parent.get("sourcePhotos", []) or not target.is_file():
        raise ValueError("没有本任务已接受的样张")
    record = validate_reuse(parent, task["role"], task.get("skuKey"), relative, sha(target))
    if not record or record.get("sha256") != digest or not record.get("evidence"):
        raise ValueError("缺少对应的样张接受依据")
    return {
        "imageId": image_id,
        "role": task["role"],
        "skuKey": task.get("skuKey"),
        "sha256": digest,
        "reusePhoto": relative,
        "imagePath": str(target),
        "evidence": record["evidence"],
        "targetAtAcceptance": record.get("targetAtAcceptance"),
    }


def accept_sample(root, image_id, evidence, read_manifest, target_sku=None):
    """Make the accepted pixels available as a free reuse input to the full plan."""
    root = Path(root)
    if not isinstance(evidence, str) or not evidence.strip():
        raise ValueError("需要原样张编号和接受样张的答复依据")
    parent, task, original, digest, relative = verified_preview(root, image_id, read_manifest)
    target = package_path(root, relative)
    target.parent.mkdir(exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as out:
        tmp = Path(out.name)
    try:
        shutil.copyfile(original, tmp)
        if sha(tmp) != digest:
            raise ValueError("源样张在复制时改变，请重新核对")
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)
    if relative not in parent["sourcePhotos"]:
        parent["sourcePhotos"].append(relative)
    accepted = parent.setdefault("acceptedSamples", {})
    previous = accepted.get(relative, {})
    accepted[relative] = {
        "role": task["role"],
        "skuKey": task.get("skuKey"),
        "evidence": evidence,
        "firstImageReference": task.get("firstImageReference"),
        "sourceSkuId": task.get("sourceSkuId"),
        "sha256": digest,
        "imageId": image_id,
        "scopeSha256": scope_hash(parent),
    }
    target_at_acceptance = previous.get("targetAtAcceptance") or (
        target_sku(root, parent, task.get("skuKey")) if target_sku else {}
    )
    if target_at_acceptance.get("targetStatus") == "verified":
        accepted[relative]["targetAtAcceptance"] = target_at_acceptance
    atomic_json(root / "manifest.json", parent)
    return {"reusePhoto": relative, "role": task["role"], "skuKey": task.get("skuKey")}
