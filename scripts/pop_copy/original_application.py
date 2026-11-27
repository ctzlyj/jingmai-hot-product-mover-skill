"""Read a rejection only for a matched original official product application."""
from __future__ import annotations
from typing import Any
import jdo_listing_mvp as jingmai


def read_rejection(product_id: int, *, apply_id=None, detail=None) -> dict[str, Any] | None:
    try:
        if detail is None:
            detail = jingmai.get(
                "jdo-product-update",
                "/rest/sp-product/v0/product-apply/{applyId}",
                {"applyId": str(apply_id or product_id), "scene": "pop"},
            ).get("data") or {}
    except jingmai.JdoError:
        return None
    apply_info = detail.get("applyInfo") or {}
    returned_pid = (detail.get("productInfo") or {}).get("productId")
    if (str(returned_pid) != str(product_id)
            or (apply_id and apply_info.get("applyId") is not None and str(apply_info["applyId"]) != str(apply_id))):
        return None
    draft_status = int(apply_info.get("draftComputeStatus") or 0)
    if draft_status != 3:
        return None
    bound_id = apply_info.get("applyId") or apply_id
    rejection = {
        "draftComputeStatus": draft_status,
        "bossAuditStatus": int(apply_info.get("bossAuditStatus") or 0),
    }
    if bound_id is not None:
        rejection["applyId"] = str(bound_id)
    reason_fields = ("auditReason", "keeperReason", "pushReason")
    reasons = {field: apply_info[field] for field in reason_fields if apply_info.get(field)}
    if bound_id is not None:
        rejection.update(reasons)
    elif reasons:
        rejection["latestProductReview"] = {**reasons, "productId": str(product_id), "originalApplicationVerified": False}
    fields = ["productId", "productName", "keeperReason", "pushReason",
              "draftStatus", "draftComputeStatus", "keeperStatus"]
    args = {"page": 1, "pageSize": 10, "sortBy": "created", "sortType": "desc", "productIdList": [product_id]}
    endpoint = "/rest/sp-product/v0/product-applies"
    # Older CLI schemas can reject optional fields before returning a JSON envelope.
    for scope in (["applyId", "auditReason", *fields], ["auditReason", *fields], fields):
        try:
            response = jingmai.get("jdo-product-update", endpoint, {**args, "scopeSet": scope})
        except jingmai.JdoError:
            continue
        if response.get("success") is not False and isinstance(response.get("data"), list):
            break
    else:
        return rejection
    rows = [item for item in response["data"] if isinstance(item, dict) and str(item.get("productId")) == str(product_id)]
    matches = [item for item in rows if bound_id is not None and str(item.get("applyId")) == str(bound_id)]
    row = matches[0] if len(matches) == 1 else None
    if row:
        rejection['reasonScope']='original-application'
        for key in ("productName","status","keeperStatus","pushStatus","auditReason","keeperReason","pushReason"):
            if row.get(key) not in (None,""):
                rejection.setdefault(key,row[key])
    if rows and rows[0] is not row:
        latest={k:rows[0][k] for k in ("applyId","productName","auditReason","keeperReason","pushReason")
                if rows[0].get(k) not in (None,"")}
        if any(latest.get(field) for field in reason_fields):
            rejection["latestProductReview"]={**latest,"productId":str(product_id),"originalApplicationVerified":False}
    return rejection
