from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP


CENT = Decimal("0.01")


TARGET_GAP = Decimal("0.10")


class PriceInversionError(ValueError):
    pass


@dataclass(frozen=True)
class FinalPriceCalculation:
    jd_price: Decimal
    direct_reduction: Decimal
    government_subsidy: Decimal
    final_price: Decimal


@dataclass(frozen=True)
class PricePlan:
    competitor_price: Decimal
    target_gap: Decimal
    target_final_price: Decimal
    jd_price: Decimal
    direct_reduction: Decimal
    government_subsidy: Decimal
    final_price: Decimal
    final_difference: Decimal


def round_money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def parse_rate(value: str | Decimal) -> Decimal:
    rate = Decimal(str(value))
    if rate > 1:
        rate /= 100
    if rate < 0 or rate >= 1:
        raise ValueError("Rates must be at least 0 and less than 100 percent.")
    return rate


def parse_target_gap(value: str | Decimal) -> Decimal:
    try:
        target_gap = round_money(Decimal(str(value)))
    except (InvalidOperation, ValueError) as error:
        raise ValueError("Target gap must be a non-negative money amount.") from error
    if target_gap < 0:
        raise ValueError("Target gap must be a non-negative money amount.")
    return target_gap


def calculate_final_price(jd_price: Decimal, direct_rate: Decimal, subsidy_rate: Decimal) -> FinalPriceCalculation:
    jd_price = round_money(Decimal(jd_price))
    direct_rate = parse_rate(direct_rate)
    subsidy_rate = parse_rate(subsidy_rate)
    direct_reduction = round_money(jd_price * direct_rate)
    government_subsidy = round_money(jd_price * (Decimal("1") - direct_rate) * subsidy_rate)
    final_price = round_money(jd_price - direct_reduction - government_subsidy)
    return FinalPriceCalculation(jd_price, direct_reduction, government_subsidy, final_price)


def create_price_plan(
    competitor_price: Decimal,
    direct_rate: Decimal,
    subsidy_rate: Decimal,
    target_gap: Decimal = TARGET_GAP,
) -> PricePlan:
    competitor_price = round_money(Decimal(competitor_price))
    direct_rate = parse_rate(direct_rate)
    subsidy_rate = parse_rate(subsidy_rate)
    target_gap = parse_target_gap(target_gap)
    target = round_money(competitor_price - target_gap)
    if target <= 0:
        raise PriceInversionError("Target gap leaves no positive target final price.")
    net_rate = Decimal("1") - direct_rate - ((Decimal("1") - direct_rate) * subsidy_rate)
    if net_rate <= 0:
        raise PriceInversionError("Combined discount rates leave no positive payable rate.")

    center_cents = int((target / net_rate * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    solutions: list[FinalPriceCalculation] = []
    for cents in range(max(1, center_cents - 1000), center_cents + 1001):
        calculation = calculate_final_price(Decimal(cents) / 100, direct_rate, subsidy_rate)
        if calculation.final_price == target:
            solutions.append(calculation)
    if not solutions:
        raise PriceInversionError(f"No two-decimal JD price produces target final price {target}.")

    best = max(solutions, key=lambda item: item.jd_price)
    return PricePlan(
        competitor_price=competitor_price,
        target_gap=target_gap,
        target_final_price=target,
        jd_price=best.jd_price,
        direct_reduction=best.direct_reduction,
        government_subsidy=best.government_subsidy,
        final_price=best.final_price,
        final_difference=round_money(best.final_price - competitor_price),
    )
