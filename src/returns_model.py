"""Step 12 — investor proceeds, MOIC and IRR under two exit outcomes.

The two scenarios exist to make the trade-off visible: an earlier acquisition
takes less dilution but arrives before the revenue that supports a strategic
price, while holding to commercial scale raises the valuation and costs
ownership. Both are computed from the same band and the same ownership path.
"""

from __future__ import annotations

from datetime import date

from . import config, utils
from .models import Ledger, OwnershipPath, ReturnOutcome, Timeline, ValuationPoint

EARLY_YEAR = 3
SCALE_YEAR = 5


def build(
    valuations: list[ValuationPoint],
    ownership: OwnershipPath,
    timeline: Timeline,
    ledger: Ledger,
) -> list[ReturnOutcome]:
    """Return the earlier-exit and scale-exit outcomes."""
    outcomes: list[ReturnOutcome] = []
    scenarios = (
        ("earlier", "Earlier exit", EARLY_YEAR, ownership.early_exit_ownership),
        ("scale", "Scale exit", SCALE_YEAR, ownership.scale_exit_ownership),
    )

    for name, label, index, stake in scenarios:
        point = next((v for v in valuations if v.index == index), None)
        if point is None:
            continue
        outcome = _outcome(name, label, point, stake, ownership.investment, timeline)
        outcomes.append(outcome)
        ledger.record(
            f"investor_moic_{name}",
            round(outcome.moic, 2),
            config.CALCULATED,
            f"({utils.fmt_money(point.midpoint)} enterprise value × {stake:.2%} ownership) / "
            f"{utils.fmt_money(ownership.investment)} invested",
            config.MEDIUM,
            notes=f"{outcome.year_label}, {outcome.years_held:.1f} years held",
            unit="×",
        )
    return outcomes


def _outcome(
    name: str,
    label: str,
    point: ValuationPoint,
    stake: float,
    investment: float,
    timeline: Timeline,
) -> ReturnOutcome:
    """Carry one exit year through to proceeds, MOIC and IRR."""
    proceeds = point.midpoint * stake
    years = _years_held(timeline, point.index)
    outcome = ReturnOutcome(
        name=name,
        label=label,
        year_index=point.index,
        year_label=point.label,
        years_held=years,
        revenue=point.revenue,
        exit_value=point.midpoint,
        ownership=stake,
        proceeds=proceeds,
        moic=utils.safe_div(proceeds, investment) or 0.0,
    )
    outcome.irr = _irr(investment, proceeds, years)
    return outcome


def _years_held(timeline: Timeline, index: int) -> float:
    """Years from today to the end of the exit commercial year."""
    window = timeline.commercial_year(index)
    if not window:
        return float(index)
    end = date.fromisoformat(str(window["end"]))
    return max(0.5, utils.years_between(timeline.today, end))


def _irr(investment: float, proceeds: float, years: float) -> float | None:
    """Gross IRR for a single outflow and a single inflow.

    Uses numpy-financial where available so the calculation matches the rest of
    the industry's tooling, and falls back to the closed form — exact for two
    cash flows — when it is not installed.
    """
    if investment <= 0 or proceeds <= 0 or years <= 0:
        return None

    periods = max(1, int(round(years)))
    try:
        import numpy_financial as npf

        flows = [-investment] + [0.0] * (periods - 1) + [proceeds]
        rate = float(npf.irr(flows))
        if rate == rate and abs(rate) < 100:  # reject NaN and non-convergence
            return rate
    except (ImportError, ValueError, FloatingPointError):
        pass

    return (proceeds / investment) ** (1.0 / years) - 1.0
