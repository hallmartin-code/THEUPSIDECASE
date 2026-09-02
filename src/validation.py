"""Validation run before the PDF is generated.

Two kinds of check live here. Identity checks re-derive a number a second way and
confirm the model agrees with itself — revenue against volume × price, enterprise
value against revenue × multiple. Coherence checks ask whether the result is
possible at all: penetration above 100%, accounts beyond the addressable base, an
exit dated before the revenue that would justify it.

A failure is reported. Nothing here edits an assumption to make a check pass.
"""

from __future__ import annotations

from typing import Any

from . import utils
from .models import (
    CompanyAnalysis,
    ValidationIssue,
)

ERROR, WARNING, NOTE = "ERROR", "WARNING", "NOTE"

# Identity checks compare two floating-point paths to the same number.
_TOLERANCE = 0.005


def run(analysis: CompanyAnalysis, inputs: dict[str, Any]) -> list[ValidationIssue]:
    """Run every check and return the findings, most severe first."""
    issues: list[ValidationIssue] = []

    issues += _timeline(analysis)
    issues += _revenue_identity(analysis, inputs)
    issues += _market(analysis)
    issues += _valuation_identity(analysis)
    issues += _ownership(analysis)
    issues += _financing(analysis)
    issues += _exit(analysis)

    order = {ERROR: 0, WARNING: 1, NOTE: 2}
    return sorted(issues, key=lambda issue: order.get(issue.severity, 3))


# --- Timeline ---------------------------------------------------------------


def _timeline(analysis: CompanyAnalysis) -> list[ValidationIssue]:
    """Surface the contradictions the timeline engine found, plus launch coherence."""
    issues = [
        ValidationIssue(WARNING, "timeline.contradiction", text)
        for text in analysis.timeline.contradictions
    ]
    if analysis.timeline.launch_date is None:
        issues.append(
            ValidationIssue(
                ERROR, "timeline.launch", "No commercial launch date could be resolved."
            )
        )
    return issues


# --- Revenue ----------------------------------------------------------------


def _revenue_identity(analysis: CompanyAnalysis, inputs: dict[str, Any]) -> list[ValidationIssue]:
    """revenue = volume × price, recomputed from the inputs rather than the model."""
    issues: list[ValidationIssue] = []
    volume_per_unit = float(inputs.get("mature_volume_per_unit") or 0.0)

    for year in analysis.revenue.years:
        expected_volume = year.effective_units * volume_per_unit
        if not _close(year.volume, expected_volume):
            issues.append(
                ValidationIssue(
                    ERROR,
                    "revenue.volume",
                    f"{year.label}: modelled volume ({year.volume:,.0f}) does not equal "
                    f"effective accounts × volume per account ({expected_volume:,.0f}).",
                )
            )
        if not _close(year.product_revenue, year.volume * year.asp):
            issues.append(
                ValidationIssue(
                    ERROR,
                    "revenue.price",
                    f"{year.label}: product revenue does not equal volume × ASP.",
                )
            )
        if year.active_units and year.effective_units > year.active_units + _TOLERANCE:
            issues.append(
                ValidationIssue(
                    ERROR,
                    "revenue.cohort",
                    f"{year.label}: cohort-weighted accounts exceed accounts won to date.",
                )
            )

    if analysis.revenue.years and all(y.revenue <= 0 for y in analysis.revenue.years):
        issues.append(
            ValidationIssue(
                ERROR,
                "revenue.zero",
                "The model produces no revenue in any commercial year; the deck supplied no "
                "price or volume basis and no assumption could be grounded.",
            )
        )
    return issues


# --- Market -----------------------------------------------------------------


def _market(analysis: CompanyAnalysis) -> list[ValidationIssue]:
    """Penetration ceilings, the addressable base, and reach against population."""
    issues: list[ValidationIssue] = []
    reach = analysis.reach

    for index, penetration in reach.penetration.items():
        if penetration > 1.0:
            issues.append(
                ValidationIssue(
                    ERROR,
                    "market.penetration",
                    f"CY{index}: modelled penetration is {penetration:.0%} of the addressable "
                    "base, which is not attainable.",
                )
            )
        elif penetration > 0.35:
            issues.append(
                ValidationIssue(
                    WARNING,
                    "market.penetration",
                    f"CY{index}: the success case reaches {penetration:.0%} of the addressable "
                    "base, an unusually high share for a five-year ramp.",
                )
            )

    if reach.addressable_units:
        final = analysis.revenue.years[-1] if analysis.revenue.years else None
        if final and final.active_units > reach.addressable_units:
            issues.append(
                ValidationIssue(
                    ERROR,
                    "market.accounts",
                    f"Active accounts at {final.label} ({final.active_units:,.0f}) exceed the "
                    f"addressable base ({reach.addressable_units:,.0f}).",
                )
            )

    for index, share in reach.population_reach.items():
        if share > 1.0:
            issues.append(
                ValidationIssue(
                    WARNING,
                    "market.population",
                    f"CY{index}: modelled reach is {share:.0%} of the eligible population. This "
                    "is only coherent if the same individual is served more than once a year.",
                )
            )

    issues += _revenue_against_market(analysis)

    if reach.tam_reconciles is False:
        # Worth a flag rather than a note: the usual cause of a large divergence
        # is that price and volume are being counted in different denominations,
        # which inflates every figure downstream of revenue.
        issues.append(ValidationIssue(WARNING, "market.tam", reach.tam_note))

    issues += _pricing(analysis)
    return issues


def _pricing(analysis: CompanyAnalysis) -> list[ValidationIssue]:
    """Would a customer actually pay what the model charges them?

    An account that captures less value than it spends will not buy, so a
    value-to-spend ratio below 1 is either a pricing error or a unit mismatch
    between price and volume — and it is the cheapest available check on both.
    """
    value = analysis.customer_value
    if not value.computed or value.ratio is None:
        return []
    if value.ratio >= 1.0:
        return []
    return [
        ValidationIssue(
            WARNING,
            "pricing.value",
            f"Each account is modelled to spend "
            f"{_money(value.annual_spend)} a year against {_money(value.annual_value_created)} "
            f"of value created ({value.ratio:.2f}× value-to-spend). Below 1× the account has no "
            "economic reason to buy, so either the price or the volume per account is wrong.",
        )
    ]


def _money(value: float | None) -> str:
    return utils.fmt_money(value)


def _revenue_against_market(analysis: CompanyAnalysis) -> list[ValidationIssue]:
    """Does the modelled ramp outgrow the market it is selling into?

    Penetration catches this when the addressable base is counted in adoption
    units. This catches the same error from the revenue side, which is the one
    that reaches the exit value and the MOIC.
    """
    final = analysis.revenue.years[-1] if analysis.revenue.years else None
    if final is None or final.revenue <= 0:
        return []

    issues: list[ValidationIssue] = []
    market = analysis.reach.bottom_up_market
    if market:
        share = final.revenue / market
        if share > 1.0:
            issues.append(
                ValidationIssue(
                    ERROR,
                    "market.revenue",
                    f"{final.label} revenue is {share:.0%} of the entire bottoms-up addressable "
                    "market, which the company cannot capture.",
                )
            )
        elif share > 0.5:
            issues.append(
                ValidationIssue(
                    WARNING,
                    "market.revenue",
                    f"{final.label} revenue is {share:.0%} of the bottoms-up addressable market "
                    "— an implausible share for a five-year ramp.",
                )
            )

    tam = analysis.reach.management_tam
    if tam and final.revenue > tam:
        issues.append(
            ValidationIssue(
                WARNING,
                "market.tam_exceeded",
                f"{final.label} revenue exceeds the TAM the deck itself states; either the ramp "
                "or the stated market is wrong.",
            )
        )
    return issues


# --- Valuation --------------------------------------------------------------


def _valuation_identity(analysis: CompanyAnalysis) -> list[ValidationIssue]:
    """valuation = revenue × selected multiple, recomputed against the band."""
    issues: list[ValidationIssue] = []
    band = analysis.band
    for point in analysis.valuations:
        if not _close(point.midpoint, point.revenue * band.midpoint):
            issues.append(
                ValidationIssue(
                    ERROR,
                    "valuation.identity",
                    f"{point.label}: implied value does not equal revenue × the band midpoint.",
                )
            )
    if band.source_type != "RESEARCH":
        found = band.transaction_count
        issues.append(
            ValidationIssue(
                WARNING,
                "valuation.evidence",
                (
                    "No transaction with both a disclosed price and disclosed revenue was found, "
                    "so the multiple range is an analyst assumption rather than transaction "
                    "evidence."
                    if found == 0
                    else f"Only {found} transaction(s) disclosed both a price and revenue, which "
                    "is too thin to set a range; the band is blended with the analyst default "
                    "and is an assumption rather than transaction evidence."
                ),
            )
        )
    return issues


# --- Ownership and financing ------------------------------------------------


def _ownership(analysis: CompanyAnalysis) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    path = analysis.ownership

    for step in path.steps:
        if not 0.0 <= step.ownership <= 1.0:
            issues.append(
                ValidationIssue(
                    ERROR,
                    "ownership.bounds",
                    f"Ownership after '{step.event}' is {step.ownership:.1%}, outside 0–100%.",
                )
            )
    if path.initial_ownership and path.scale_exit_ownership > path.initial_ownership:
        issues.append(
            ValidationIssue(
                ERROR,
                "ownership.dilution",
                "Ownership at exit exceeds ownership at entry; the path is not diluting.",
            )
        )
    if path.initial_ownership == 0:
        issues.append(
            ValidationIssue(
                WARNING,
                "ownership.terms",
                "Ownership could not be computed: the deck states no valuation, cap or "
                "conversion terms.",
            )
        )
    return issues


def _financing(analysis: CompanyAnalysis) -> list[ValidationIssue]:
    """Check the financing path actually carries the plan to commercial scale."""
    issues: list[ValidationIssue] = []
    rounds = analysis.financing
    months_to_launch = analysis.timeline.months_to_launch

    if len(rounds) <= 1 and months_to_launch > 12:
        issues.append(
            ValidationIssue(
                WARNING,
                "financing.sufficiency",
                f"Commercial launch is {months_to_launch} months away and only the round on "
                "offer is modelled; the success case requires capital the deck does not describe.",
            )
        )

    if rounds:
        last = max(rounds, key=lambda item: item.year or 0)
        launch_years = months_to_launch / 12.0
        if (last.year or 0) < launch_years:
            issues.append(
                ValidationIssue(
                    WARNING,
                    "financing.runway",
                    f"The last modelled financing event ({last.name}, year {last.year}) falls "
                    "before commercial launch, so no capital is modelled into the commercial "
                    "ramp itself.",
                )
            )
    return issues


def _exit(analysis: CompanyAnalysis) -> list[ValidationIssue]:
    """Flag an exit dated before the commercialisation that would support it."""
    test = analysis.exit_claim
    if test.verdict in ("Mistimed", "Internally inconsistent", "Overstated"):
        return [ValidationIssue(WARNING, "exit.claim", f"{test.verdict}. {test.explanation}")]
    return []


def _close(left: float, right: float) -> bool:
    """Compare two floats with a relative tolerance, treating a shared zero as equal."""
    scale = max(abs(left), abs(right), 1.0)
    return abs(left - right) / scale <= _TOLERANCE
