"""Steps 8 and 9 — the valuation band, implied enterprise values, and the exit test.

The multiple range is built only from transactions where a source stated both the
price and the trailing revenue. When research returns nothing usable, the band
falls back to a stated range that is labelled an assumption on the page — the
analysis does not quietly borrow authority it has not earned.
"""

from __future__ import annotations

import statistics

from . import config, utils
from .models import (
    DeckFacts,
    ExitClaimTest,
    Ledger,
    RevenueModel,
    Timeline,
    Transaction,
    ValuationBand,
    ValuationPoint,
)

# Premium a strategic acquirer pays over the financial band, applied to the high end.
STRATEGIC_PREMIUM = 0.35

# Verified transactions needed before the data may set the range on its own.
_RANGE_MINIMUM = 3

# How far management's exit claim may sit from the modelled midpoint before the
# analysis stops calling it reasonable.
_REASONABLE_LOW, _REASONABLE_HIGH, _AGGRESSIVE_HIGH = 0.70, 1.40, 2.50


def build_band(transactions: list[Transaction], ledger: Ledger) -> ValuationBand:
    """Derive the revenue-multiple range from verified transactions, or fall back."""
    multiples = sorted(t.multiple for t in transactions if t.usable)

    fallback_low, fallback_high = config.FALLBACK_MULTIPLE_RANGE

    if len(multiples) >= _RANGE_MINIMUM:
        quantiles = statistics.quantiles(multiples, n=4)
        low, high = quantiles[0], quantiles[2]
        basis = (
            f"interquartile range of {len(multiples)} verified transactions "
            f"({utils.fmt_multiple(min(multiples))}–{utils.fmt_multiple(max(multiples))} full range)"
        )
        source_type = config.RESEARCH
    elif multiples:
        # One or two transactions cannot carry a range: an outlier taken at face
        # value here propagates straight into the exit value and the MOIC. The
        # observed level is pulled halfway toward the analyst default and given
        # that default's relative width, and the band is recorded as an
        # assumption rather than as transaction evidence.
        observed = statistics.median(multiples)
        fallback_mid = statistics.fmean([fallback_low, fallback_high])
        centre = statistics.fmean([observed, fallback_mid])
        spread = (fallback_high - fallback_low) / (2 * fallback_mid)
        low, high = centre * (1 - spread), centre * (1 + spread)
        observed_text = ", ".join(utils.fmt_multiple(value) for value in multiples)
        basis = (
            f"{len(multiples)} verified transaction"
            f"{'s' if len(multiples) > 1 else ''} at {observed_text}, blended halfway toward "
            f"the {utils.fmt_multiple(fallback_low)}–{utils.fmt_multiple(fallback_high)} "
            "analyst default because one or two transactions cannot carry a range"
        )
        source_type = config.ASSUMPTION
    else:
        low, high = fallback_low, fallback_high
        basis = (
            "no transaction with both a disclosed price and disclosed revenue was found; "
            "this is an analyst range, not evidence"
        )
        source_type = config.ASSUMPTION

    band = ValuationBand(
        low=round(low, 2),
        high=round(high, 2),
        midpoint=round(statistics.fmean([low, high]), 2),
        basis=basis,
        source_type=source_type,
        transaction_count=len(multiples),
        strategic_premium=STRATEGIC_PREMIUM,
    )
    ledger.record(
        "revenue_multiple_range",
        [band.low, band.high],
        band.source_type,
        basis,
        config.MEDIUM if len(multiples) >= _RANGE_MINIMUM else config.LOW,
        unit="× revenue",
    )
    return band


def build(
    revenue: RevenueModel, band: ValuationBand, ledger: Ledger
) -> list[ValuationPoint]:
    """Apply the band to each commercial year's revenue."""
    points: list[ValuationPoint] = []
    for year in revenue.years:
        point = ValuationPoint(
            index=year.index,
            label=year.label,
            revenue=year.revenue,
            low=year.revenue * band.low,
            high=year.revenue * band.high,
            midpoint=year.revenue * band.midpoint,
            strategic=year.revenue * band.high * (1.0 + band.strategic_premium),
        )
        points.append(point)
        ledger.record(
            f"enterprise_value_cy{year.index}",
            round(point.midpoint, 2),
            config.CALCULATED,
            f"CY{year.index} revenue × {utils.fmt_multiple(band.midpoint)} (band midpoint)",
            config.MEDIUM,
            notes=f"range {utils.fmt_money(point.low)}–{utils.fmt_money(point.high)}",
            unit="$",
        )
    return points


def test_exit_claim(
    facts: DeckFacts,
    timeline: Timeline,
    points: list[ValuationPoint],
    ledger: Ledger,
) -> ExitClaimTest:
    """Step 9: measure management's stated exit against the independent model."""
    stated_value = utils.parse_money(facts.text("exit", "exit_valuation"))
    stated_year = timeline.stated_exit_year
    test = ExitClaimTest(
        stated=bool(stated_value or stated_year),
        stated_value=stated_value,
        stated_year=stated_year,
    )
    if not test.stated:
        test.verdict = "Not stated"
        test.explanation = (
            "The deck states no exit valuation or exit year, so there is no management claim "
            "to test. The valuation range in this analysis is the model's own."
        )
        return test

    matched = _year_at(timeline, points, stated_year)
    if matched:
        test.modelled_low, test.modelled_high = matched.low, matched.high
        test.modelled_year = stated_year

    if stated_year and timeline.launch_date and stated_year < timeline.launch_date.year:
        test.verdict = "Internally inconsistent"
        test.explanation = (
            f"The deck targets an exit in {stated_year}, before modelled commercial launch in "
            f"{timeline.launch_date.year}. On the deck's own development timeline the company "
            "would be sold before it has sold anything."
        )
    elif matched is None:
        supporting = _first_supporting(points, stated_value) if stated_value else None
        target = supporting or (points[-1] if points else None)
        if target:
            test.modelled_low, test.modelled_high = target.low, target.high
            test.modelled_year = _calendar_year(timeline, target.index)
        if stated_year is None:
            test.verdict = "Undated"
            test.explanation = _undated_text(test, target)
        else:
            test.verdict = "Mistimed"
            test.explanation = _mistimed_text(test, timeline, target, supporting)
    else:
        test.verdict, test.explanation = _compare(test, matched, timeline)

    ledger.record(
        "management_exit_claim",
        stated_value,
        config.DECK if stated_value else config.ASSUMPTION,
        facts.get("exit", "exit_valuation").citation(),
        config.HIGH if stated_value else config.LOW,
        notes=f"{test.verdict}: {test.explanation}",
        unit="$",
    )
    return test


# --- Comparison helpers -----------------------------------------------------


def _calendar_year(timeline: Timeline, index: int) -> int | None:
    window = timeline.commercial_year(index)
    if not window:
        return None
    return int(str(window["start"])[:4])


def _year_at(
    timeline: Timeline, points: list[ValuationPoint], stated_year: int | None
) -> ValuationPoint | None:
    """The modelled commercial year whose window contains the stated exit year."""
    if not stated_year:
        return None
    for point in points:
        window = timeline.commercial_year(point.index)
        if window and int(str(window["start"])[:4]) <= stated_year <= int(str(window["end"])[:4]):
            return point
    return None


def _first_supporting(
    points: list[ValuationPoint], stated_value: float | None
) -> ValuationPoint | None:
    """The earliest modelled year whose high end reaches the stated exit value."""
    if not stated_value:
        return None
    return next((point for point in points if point.high >= stated_value), None)


def _compare(
    test: ExitClaimTest, matched: ValuationPoint, timeline: Timeline
) -> tuple[str, str]:
    """Classify a stated value against the modelled range for the same year."""
    if test.stated_value is None:
        return (
            "Mistimed",
            f"The deck names an exit in {test.stated_year} but no valuation. At modelled "
            f"{matched.label} revenue of {utils.fmt_money(matched.revenue)} the band implies "
            f"{utils.fmt_money(matched.low)}–{utils.fmt_money(matched.high)}.",
        )

    ratio = utils.safe_div(test.stated_value, matched.midpoint)
    difference = (
        f"Deck exit: {utils.fmt_money(test.stated_value)} in {test.stated_year}. "
        f"Model-supported range: {utils.fmt_money(matched.low)}–{utils.fmt_money(matched.high)} "
        f"at {matched.label}, on modelled revenue of {utils.fmt_money(matched.revenue)}."
    )
    if ratio is None:
        return "Overstated", difference + " Modelled revenue in that year is nil."
    if ratio < _REASONABLE_LOW:
        return (
            "Understated",
            difference + f" The claim sits {1 / ratio:.1f}× below the modelled midpoint — the "
            "deck is underselling what its own plan would be worth.",
        )
    if ratio <= _REASONABLE_HIGH:
        return "Reasonable", difference + " The claim sits inside the modelled range."
    if ratio <= _AGGRESSIVE_HIGH:
        return (
            "Overstated",
            difference + f" The claim is {ratio:.1f}× the modelled midpoint: moderately "
            "aggressive, but within reach if adoption runs ahead of plan.",
        )
    return (
        "Overstated",
        difference + f" The claim is {ratio:.1f}× the modelled midpoint and is not supported "
        "by the revenue the plan produces in that year.",
    )


def _undated_text(test: ExitClaimTest, target: ValuationPoint | None) -> str:
    """Explain an exit value the deck never attaches to a year."""
    stated = f"Deck exit: {utils.fmt_money(test.stated_value)}, with no year stated."
    if target is None:
        return stated + " The model produces no revenue against which to test it."
    return (
        f"{stated} The model first supports that value at {target.label}, on revenue of "
        f"{utils.fmt_money(target.revenue)} "
        f"({utils.fmt_money(target.low)}–{utils.fmt_money(target.high)}). Without a stated "
        "year the claim cannot be tested for timing, only for level."
    )


def _mistimed_text(
    test: ExitClaimTest,
    timeline: Timeline,
    target: ValuationPoint | None,
    supporting: ValuationPoint | None,
) -> str:
    """Explain an exit dated outside the modelled commercial window."""
    stated = (
        f"Deck exit: {utils.fmt_money(test.stated_value)} in {test.stated_year}"
        if test.stated_value
        else f"Deck targets an exit in {test.stated_year}"
    )
    if supporting and target:
        year = _calendar_year(timeline, target.index)
        gap = (year - test.stated_year) if (year and test.stated_year) else None
        timing = (
            f" roughly {gap} year{'s' if gap and abs(gap) != 1 else ''} after the deck assumes it"
            if gap and gap > 0
            else ""
        )
        return (
            f"{stated}. The model first supports that value at {target.label} "
            f"({utils.fmt_money(target.low)}–{utils.fmt_money(target.high)}){timing}. The issue "
            "is timing rather than the number itself."
        )
    if target:
        return (
            f"{stated}. The stated year falls outside the five modelled commercial years. At "
            f"{target.label}, the model's furthest point, the band implies "
            f"{utils.fmt_money(target.low)}–{utils.fmt_money(target.high)}."
        )
    return f"{stated}. The model produces no revenue in that year against which to test it."
