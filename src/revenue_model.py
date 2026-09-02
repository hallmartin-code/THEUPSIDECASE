"""Steps 3 and 4 — the bottoms-up cohort revenue build and its sensitivity test.

No CAGR is applied to anyone's TAM here. Revenue is assembled from the unit of
adoption upward: accounts are won in cohorts, each cohort ramps toward mature
utilisation on its own clock, and revenue is what those accounts actually consume
at the modelled price.

All arithmetic in this module is deterministic Python. The model chose the inputs;
it does not get to do the sums.
"""

from __future__ import annotations

from typing import Any

from . import config, utils
from .models import BusinessShape, Ledger, RevenueModel, RevenueYear, Timeline


def build(
    inputs: dict[str, Any],
    shape: BusinessShape,
    timeline: Timeline,
    ledger: Ledger,
) -> RevenueModel:
    """Build the upside-case cohort model and the lower-utilisation twin."""
    ramp = tuple(inputs.get("ramp_utilisation") or config.DEFAULT_RAMP)
    new_units = [float(value) for value in inputs.get("new_units_by_commercial_year") or []]
    new_units = (new_units + [0.0] * config.COMMERCIAL_YEARS)[: config.COMMERCIAL_YEARS]

    model = RevenueModel(
        unit_label=shape.adoption_unit_plural,
        volume_label=shape.volume_metric,
        ramp=ramp,
        mature_volume_per_unit=float(inputs.get("mature_volume_per_unit") or 0.0),
        asp=float(inputs.get("asp") or 0.0),
        gross_margin=float(inputs.get("gross_margin") or 0.0),
    )

    model.years = _project(new_units, ramp, inputs, timeline, model.mature_volume_per_unit)

    driver, description, sensitivity_years = _sensitivity(
        model, new_units, ramp, inputs, timeline, shape
    )
    model.sensitivity_driver = driver
    model.sensitivity_description = description
    model.sensitivity_years = sensitivity_years
    model.sensitivity_volume_per_unit = (
        model.mature_volume_per_unit * config.SENSITIVITY_UTILISATION
    )

    _record(model, ledger, shape)
    return model


# --- The cohort build -------------------------------------------------------


def _project(
    new_units: list[float],
    ramp: tuple[float, ...],
    inputs: dict[str, Any],
    timeline: Timeline,
    mature_volume: float,
    recurring_scale: float = 1.0,
) -> list[RevenueYear]:
    """Roll cohorts forward five commercial years.

    `effective_units` is the cohort-weighted account count — three accounts in
    their first year at 30% utilisation count as 0.9 mature-equivalent accounts.
    Revenue scales off that, which is the whole point of modelling cohorts rather
    than applying a growth rate to a total.
    """
    asp = float(inputs.get("asp") or 0.0)
    erosion = float(inputs.get("asp_change_annual") or 0.0)
    recurring = float(inputs.get("recurring_revenue_per_unit") or 0.0) * recurring_scale
    one_time = float(inputs.get("one_time_revenue_per_new_unit") or 0.0)
    margin = float(inputs.get("gross_margin") or 0.0)
    per_unit_beneficiaries = float(inputs.get("beneficiaries_per_unit_year") or 0.0)
    mature_age = len(ramp)

    years: list[RevenueYear] = []
    cumulative_beneficiaries = 0.0

    for index in range(1, config.COMMERCIAL_YEARS + 1):
        window = timeline.commercial_year(index) or {}
        year = RevenueYear(
            index=index,
            label=window.get("full_label", f"CY{index}"),
            new_units=new_units[index - 1],
        )

        for cohort in range(1, index + 1):
            age = index - cohort + 1
            utilisation = ramp[min(age, mature_age) - 1]
            units = new_units[cohort - 1]
            year.active_units += units
            year.effective_units += units * utilisation
            if age >= mature_age:
                year.mature_units += units

        year.asp = asp * ((1.0 + erosion) ** (index - 1))
        year.volume = year.effective_units * mature_volume
        year.product_revenue = year.volume * year.asp
        year.recurring_revenue = year.effective_units * recurring
        year.revenue = year.product_revenue + year.recurring_revenue + year.new_units * one_time
        year.gross_profit = year.revenue * margin
        year.beneficiaries = year.effective_units * per_unit_beneficiaries
        cumulative_beneficiaries += year.beneficiaries
        year.cumulative_beneficiaries = cumulative_beneficiaries

        years.append(year)
    return years


# --- Sensitivity ------------------------------------------------------------


def _sensitivity(
    model: RevenueModel,
    new_units: list[float],
    ramp: tuple[float, ...],
    inputs: dict[str, Any],
    timeline: Timeline,
    shape: BusinessShape,
) -> tuple[str, str, list[RevenueYear]]:
    """Flex the variable that actually drives this model's revenue.

    Mature volume per unit is the default and usually the right one. But a model
    whose revenue is mostly one-off hardware turns on how many accounts get won,
    and a pure subscription model turns on price per account — flexing utilisation
    in either case would test nothing.
    """
    fraction = config.SENSITIVITY_UTILISATION
    final = model.years[-1] if model.years else None
    total = final.revenue if final else 0.0

    product_share = utils.safe_div(final.product_revenue, total) if final else None
    recurring_share = utils.safe_div(final.recurring_revenue, total) if final else None

    if product_share is not None and product_share >= 0.5:
        driver = f"mature {shape.volume_metric} per {shape.adoption_unit}"
        description = (
            f"mature {shape.adoption_unit} utilisation = "
            f"{utils.fmt_number(model.mature_volume_per_unit)} "
            f"{shape.volume_metric}/year"
        )
        years = _project(
            new_units, ramp, inputs, timeline, model.mature_volume_per_unit * fraction
        )
    elif recurring_share is not None and recurring_share >= 0.5:
        driver = f"recurring revenue per {shape.adoption_unit}"
        description = (
            f"recurring revenue = "
            f"{utils.fmt_money(inputs.get('recurring_revenue_per_unit'))} per "
            f"{shape.adoption_unit}/year"
        )
        years = _project(
            new_units, ramp, inputs, timeline, model.mature_volume_per_unit,
            recurring_scale=fraction,
        )
    else:
        driver = f"new {shape.adoption_unit_plural} won per year"
        description = (
            f"{utils.fmt_number(sum(new_units))} {shape.adoption_unit_plural} won across "
            "five commercial years"
        )
        years = _project(
            [units * fraction for units in new_units],
            ramp,
            inputs,
            timeline,
            model.mature_volume_per_unit,
        )
    return driver, description, years


# --- Ledger -----------------------------------------------------------------


def _record(model: RevenueModel, ledger: Ledger, shape: BusinessShape) -> None:
    """Record the headline outputs, each with the formula that produced it."""
    formula = (
        f"effective {shape.adoption_unit_plural} (cohort-weighted) × mature "
        f"{shape.volume_metric} per {shape.adoption_unit} × ASP, plus recurring and one-off revenue"
    )
    for year in model.years:
        ledger.record(
            f"revenue_cy{year.index}", round(year.revenue, 2), config.CALCULATED, formula,
            config.MEDIUM, notes=year.label, unit="$",
        )
        ledger.record(
            f"active_{shape.adoption_unit}_cy{year.index}", round(year.active_units, 1),
            config.CALCULATED, "sum of cohorts won to date", config.MEDIUM, notes=year.label,
        )
        ledger.record(
            f"{shape.beneficiary}_cy{year.index}", round(year.beneficiaries, 0),
            config.CALCULATED,
            f"effective {shape.adoption_unit_plural} × {shape.beneficiary} per "
            f"{shape.adoption_unit} per year",
            config.MEDIUM, notes=year.label,
        )

    for year in model.sensitivity_years:
        ledger.record(
            f"revenue_cy{year.index}_sensitivity", round(year.revenue, 2), config.CALCULATED,
            f"same build at {config.SENSITIVITY_UTILISATION:.0%} of {model.sensitivity_driver}",
            config.MEDIUM, notes=year.label, unit="$",
        )
