"""Steps 5 and 6 — market reach and customer economic value.

Penetration is measured against a base the analysis can defend, and an
independent bottoms-up addressable market is built from the same unit economics
that drive revenue. Where that disagrees with management's TAM, the discrepancy
is stated rather than reconciled away.
"""

from __future__ import annotations

from typing import Any

from . import config, utils
from .models import (
    BusinessShape,
    CustomerValue,
    DeckFacts,
    Ledger,
    ReachModel,
    RevenueModel,
)

# How far management's TAM may sit from the bottoms-up build before the analysis
# calls it unreconciled. Market sizing is genuinely imprecise; a 3x band is wide
# enough to allow for scope differences and narrow enough to catch a headline.
_TAM_TOLERANCE = 3.0


def build(
    inputs: dict[str, Any],
    facts: DeckFacts,
    shape: BusinessShape,
    revenue: RevenueModel,
    ledger: Ledger,
) -> tuple[ReachModel, CustomerValue]:
    """Return the reach model and the customer value-to-spend test."""
    reach = ReachModel()

    reach.addressable_units = inputs.get("addressable_units")
    units_entry = ledger.get("addressable_units")
    reach.addressable_units_basis = units_entry.source if units_entry else ""

    reach.addressable_population = inputs.get("addressable_population")
    population_entry = ledger.get("addressable_population")
    reach.addressable_population_basis = population_entry.source if population_entry else ""

    mature_revenue_per_unit = _mature_revenue_per_unit(inputs)
    reach.bottom_up_market = (
        reach.addressable_units * mature_revenue_per_unit if reach.addressable_units else None
    )
    reach.management_tam = utils.parse_money(facts.text("market", "tam"))
    _reconcile(reach, facts)

    for year in revenue.years:
        penetration = utils.safe_div(year.active_units, reach.addressable_units)
        if penetration is not None:
            reach.penetration[year.index] = penetration
            ledger.record(
                f"penetration_cy{year.index}",
                round(penetration, 6),
                config.CALCULATED,
                f"active {shape.adoption_unit_plural} / addressable {shape.adoption_unit_plural}",
                config.MEDIUM,
                notes=year.label,
                unit="fraction",
            )
        population = utils.safe_div(year.beneficiaries, reach.addressable_population)
        if population is not None:
            reach.population_reach[year.index] = population

    if reach.bottom_up_market:
        ledger.record(
            "bottom_up_addressable_market",
            round(reach.bottom_up_market, 2),
            config.CALCULATED,
            f"addressable {shape.adoption_unit_plural} × mature revenue per "
            f"{shape.adoption_unit} ({utils.fmt_money(mature_revenue_per_unit)}/year)",
            config.MEDIUM,
            unit="$",
        )

    return reach, _customer_value(inputs, mature_revenue_per_unit, shape, ledger)


def _mature_revenue_per_unit(inputs: dict[str, Any]) -> float:
    """Annual revenue from one fully ramped account, one-off revenue excluded.

    One-off hardware is deliberately left out: this figure is the recurring claim
    on a customer's budget, which is what both the market build and the
    value-to-spend ratio need.
    """
    volume = float(inputs.get("mature_volume_per_unit") or 0.0)
    asp = float(inputs.get("asp") or 0.0)
    recurring = float(inputs.get("recurring_revenue_per_unit") or 0.0)
    return volume * asp + recurring


def _reconcile(reach: ReachModel, facts: DeckFacts) -> None:
    """Compare management's TAM with the bottoms-up build and state the outcome."""
    if reach.management_tam is None:
        reach.tam_note = (
            "The deck states no TAM, so market penetration is measured against the "
            "bottoms-up addressable base built from the deck's own unit economics."
        )
        return
    if not reach.bottom_up_market:
        reach.tam_note = (
            f"Management TAM of {utils.fmt_money(reach.management_tam)} could not be tested: "
            "the deck gives no addressable account count to build against."
        )
        return

    ratio = utils.safe_div(reach.management_tam, reach.bottom_up_market)
    if ratio is None:
        return
    reach.tam_reconciles = (1 / _TAM_TOLERANCE) <= ratio <= _TAM_TOLERANCE
    if reach.tam_reconciles:
        reach.tam_note = (
            f"Management TAM ({utils.fmt_money(reach.management_tam)}) is within range of the "
            f"bottoms-up build ({utils.fmt_money(reach.bottom_up_market)}); penetration is "
            "measured against the bottoms-up base."
        )
    else:
        direction = "above" if ratio > 1 else "below"
        reach.tam_note = (
            f"Management TAM ({utils.fmt_money(reach.management_tam)}) is {ratio:.1f}× "
            f"{direction} the bottoms-up market implied by the deck's own account count and "
            f"pricing ({utils.fmt_money(reach.bottom_up_market)}). Penetration in this analysis "
            "is measured against the bottoms-up base, not the stated TAM."
        )


def _customer_value(
    inputs: dict[str, Any],
    mature_revenue_per_unit: float,
    shape: BusinessShape,
    ledger: Ledger,
) -> CustomerValue:
    """Step 6: annual value created for one account against annual spend captured."""
    value_created = inputs.get("customer_value_per_unit_year")
    customer = CustomerValue(
        annual_value_created=value_created or None,
        annual_spend=mature_revenue_per_unit or None,
        basis=(ledger.get("customer_value_per_unit_year").source
               if ledger.get("customer_value_per_unit_year") else ""),
    )
    if not value_created or not mature_revenue_per_unit:
        customer.basis = customer.basis or (
            "The deck gives no basis for quantifying customer value, so the value-to-spend "
            "ratio is not computed."
        )
        return customer

    customer.ratio = utils.safe_div(value_created, mature_revenue_per_unit)
    customer.computed = customer.ratio is not None
    if customer.computed:
        ledger.record(
            "customer_value_to_spend",
            round(customer.ratio, 2),
            config.CALCULATED,
            f"annual value created per {shape.adoption_unit} / annual spend captured per "
            f"{shape.adoption_unit}",
            config.MEDIUM,
            unit="×",
        )
    return customer
