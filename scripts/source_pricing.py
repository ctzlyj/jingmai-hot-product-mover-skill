from decimal import Decimal, InvalidOperation
from typing import Any

from component_rounded_pricing import (
    PricePlan, calculate_final_price, create_price_plan, round_money,
)


def component_mode(pricing: dict[str, Any]) -> bool:
    mode = pricing.get("roundingMode", "combined")
    if mode not in {"combined", "component"}:
        raise ValueError("未知的定价取整口径")
    return mode == "component"


def finite_decimal(value: Any, label: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValueError(f"{label}必须是有限数字") from error
    if not result.is_finite():
        raise ValueError(f"{label}必须是有限数字")
    return result


def component_source_plan(source_price: Any, pricing: dict[str, Any]) -> PricePlan:
    if not component_mode(pricing):
        raise ValueError("必须显式选择逐项取整口径")
    source = finite_decimal(source_price, "来源价")
    multiplier = finite_decimal(pricing.get("targetArrivalMultiplier", "1"), "到手价倍率")
    below = finite_decimal(pricing.get("belowCompetitor", "0.10"), "目标差额")
    coupon = finite_decimal(pricing.get("coupon", "0"), "优惠券")
    direct = finite_decimal(pricing.get("activityDiscountRate", "0.15"), "活动比例")
    subsidy = finite_decimal(pricing.get("nationalSubsidyRate", "0.15"), "国补比例")
    if source <= 0 or multiplier <= 0 or below < 0:
        raise ValueError("来源价及倍率必须大于0，目标差额不得为负数")
    if coupon != 0:
        raise ValueError("逐项取整当前仅支持零优惠券，不能自动切换公式")
    if not 0 <= direct < 1 or not 0 <= subsidy < 1:
        raise ValueError("折扣比例必须大于等于0且小于1")
    target = round_money(source * multiplier - below)
    return create_price_plan(target, direct, subsidy, target_gap=Decimal("0"))


def component_source_audit(source_price: Any, jd_price: Any, pricing: dict[str, Any]) -> dict[str, Any]:
    plan = component_source_plan(source_price, pricing)
    actual_jd = finite_decimal(jd_price, "京东价")
    if actual_jd <= 0 or actual_jd != round_money(actual_jd):
        raise ValueError("京东价必须是大于0的分位金额")
    calculated = calculate_final_price(
        actual_jd, Decimal(str(pricing.get("activityDiscountRate", "0.15"))),
        Decimal(str(pricing.get("nationalSubsidyRate", "0.15"))),
    )
    return {
        "sourcePrice": str(source_price), "jdPrice": str(actual_jd),
        "expectedJdPrice": str(plan.jd_price), "arrivalPrice": str(calculated.final_price),
        "expectedArrivalPrice": str(plan.target_final_price),
        "directReduction": str(calculated.direct_reduction),
        "governmentSubsidy": str(calculated.government_subsidy),
        "roundingMode": "component",
        "ok": actual_jd == plan.jd_price and calculated.final_price == plan.target_final_price,
    }
