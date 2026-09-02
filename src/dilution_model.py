"""Steps 10 and 11 — the capital the plan requires, and what it does to ownership.

Two assumptions are refused here. The rounds in the deck are not assumed to be
sufficient: the financing path is built from the milestones the success case
actually requires. And the investor is not assumed to hold their opening
percentage: every round on that path dilutes, and so does the option pool.
"""

from __future__ import annotations

from typing import Any

from . import config, utils
from .models import (
    DeckFacts,
    FinancingRound,
    Ledger,
    OwnershipPath,
    OwnershipStep,
    Timeline,
)

PRICED, SAFE, NOTE = "priced equity", "SAFE", "convertible note"

_INSTRUMENT_KEYWORDS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("convertible note", "conv note", "promissory", "kiss"), NOTE),
    (("safe", "simple agreement"), SAFE),
    (("priced", "series a", "series seed", "preferred", "equity round", "common"), PRICED),
)


def build(
    facts: DeckFacts,
    inputs: dict[str, Any],
    timeline: Timeline,
    investment: float,
    ledger: Ledger,
) -> tuple[list[FinancingRound], OwnershipPath]:
    """Return the modelled financing path and the investor's ownership through it."""
    rounds = _rounds(facts, inputs, timeline, ledger)
    path = _ownership(facts, inputs, rounds, timeline, investment, ledger)
    return rounds, path


# --- Capital requirements ---------------------------------------------------


def _rounds(
    facts: DeckFacts, inputs: dict[str, Any], timeline: Timeline, ledger: Ledger
) -> list[FinancingRound]:
    """Turn the proposed financing plan into rounds, tied to the milestones they fund."""
    dilution = float(inputs.get("round_dilution") or config.DEFAULT_ROUND_DILUTION)
    pool = float(inputs.get("option_pool_increase") or config.DEFAULT_OPTION_POOL_INCREASE)
    deck_raise = utils.parse_money(facts.text("financing", "raise_amount"))

    rounds: list[FinancingRound] = []
    for position, item in enumerate(inputs.get("financing_rounds") or []):
        amount = float(item.get("amount") or 0.0)
        from_deck = bool(item.get("from_deck"))
        if position == 0 and deck_raise:
            # The round being raised now is stated in the deck; prefer that figure.
            amount, from_deck = deck_raise, True
        if amount <= 0:
            continue
        rounds.append(
            FinancingRound(
                name=item.get("name") or f"Round {position + 1}",
                year=int(round(float(item.get("months_from_today") or 0) / 12.0)),
                amount=amount,
                milestone=item.get("milestone") or "",
                source_type=config.DECK if from_deck else config.ASSUMPTION,
                # The round the investor joins does not dilute that investor.
                dilution=0.0 if position == 0 else dilution,
                option_pool=0.0 if position == 0 else pool / max(1, _later_count(inputs)),
                note=item.get("milestone") or "",
            )
        )

    if not rounds and deck_raise:
        rounds = [
            FinancingRound(
                name="Current round",
                year=0,
                amount=deck_raise,
                milestone=facts.text("financing", "use_of_funds")[:80],
                source_type=config.DECK,
            )
        ]

    total = sum(item.amount for item in rounds)
    ledger.record(
        "capital_required_total",
        round(total, 2),
        config.CALCULATED,
        "sum of the financing events the success case requires: "
        + ", ".join(f"{item.name} {utils.fmt_money(item.amount)}" for item in rounds),
        config.LOW if any(r.source_type == config.ASSUMPTION for r in rounds) else config.MEDIUM,
        unit="$",
    )
    return rounds


def _later_count(inputs: dict[str, Any]) -> int:
    """How many rounds follow the current one — the pool top-up is spread across them."""
    return max(1, len(inputs.get("financing_rounds") or []) - 1)


# --- Ownership --------------------------------------------------------------


def _ownership(
    facts: DeckFacts,
    inputs: dict[str, Any],
    rounds: list[FinancingRound],
    timeline: Timeline,
    investment: float,
    ledger: Ledger,
) -> OwnershipPath:
    """Convert today's cheque to a percentage, then dilute it round by round."""
    instrument = _instrument(facts)
    path = OwnershipPath(instrument=instrument, investment=investment)

    valuation, basis, effective, notes = _conversion(facts, inputs, rounds, timeline, investment)
    path.conversion_valuation = valuation
    path.conversion_basis = basis
    path.notes = notes

    ownership = utils.safe_div(effective, valuation)
    if ownership is None:
        path.notes.append(
            "The deck states neither a valuation nor a cap, so ownership cannot be computed "
            "from the terms offered."
        )
        return path

    path.initial_ownership = utils.clamp(ownership, 0.0, 1.0)
    path.steps.append(
        OwnershipStep(
            event=f"On conversion ({instrument})",
            ownership=path.initial_ownership,
            note=basis,
        )
    )

    current = path.initial_ownership
    later = [item for item in rounds[1:] if item.dilution > 0]
    for index, item in enumerate(later):
        current *= 1.0 - item.dilution
        if item.option_pool:
            current *= 1.0 - item.option_pool
        path.steps.append(
            OwnershipStep(
                event=f"After {item.name}",
                ownership=current,
                note=f"{item.dilution:.0%} round dilution"
                + (f" plus {item.option_pool:.0%} option pool" if item.option_pool else ""),
            )
        )
        if index == 0:
            path.early_exit_ownership = current

    path.scale_exit_ownership = current
    if not path.early_exit_ownership:
        path.early_exit_ownership = path.initial_ownership

    ledger.record(
        "investor_ownership_initial", round(path.initial_ownership, 6), config.CALCULATED,
        f"{utils.fmt_money(investment)} / {basis}", config.MEDIUM, unit="fraction",
    )
    ledger.record(
        "investor_ownership_at_scale", round(path.scale_exit_ownership, 6), config.CALCULATED,
        "initial ownership diluted through "
        + (", ".join(item.name for item in later) or "no further rounds"),
        config.MEDIUM, unit="fraction",
    )
    return path


def _instrument(facts: DeckFacts) -> str:
    """Classify the security on offer from the deck's own language."""
    text = (facts.text("financing", "instrument") or "").lower()
    for keywords, instrument in _INSTRUMENT_KEYWORDS:
        if any(keyword in text for keyword in keywords):
            return instrument
    if facts.get("financing", "safe_cap").present:
        return SAFE
    if facts.get("financing", "post_money").present or facts.get("financing", "pre_money").present:
        return PRICED
    return SAFE  # the default instrument at this stage, recorded as an assumption


def _conversion(
    facts: DeckFacts,
    inputs: dict[str, Any],
    rounds: list[FinancingRound],
    timeline: Timeline,
    investment: float,
) -> tuple[float | None, str, float, list[str]]:
    """Work out the valuation the investment converts at, and at what effective size.

    Returns (valuation, basis, effective investment, notes). A note accrues
    interest; a SAFE or note converts at the better of its cap and the discounted
    price of the next priced round.
    """
    notes: list[str] = []
    instrument = _instrument(facts)
    post = utils.parse_money(facts.text("financing", "post_money"))
    pre = utils.parse_money(facts.text("financing", "pre_money"))
    raise_amount = utils.parse_money(facts.text("financing", "raise_amount"))
    cap = utils.parse_money(facts.text("financing", "safe_cap"))
    discount = utils.parse_percent(facts.text("financing", "discount"))

    if instrument == PRICED:
        valuation = post or (pre + raise_amount if pre and raise_amount else pre)
        if valuation:
            basis = (
                f"post-money valuation of {utils.fmt_money(valuation)}"
                if post
                else f"pre-money {utils.fmt_money(pre)} plus the {utils.fmt_money(raise_amount)} round"
            )
            return valuation, basis, investment, notes
        return None, "no valuation stated", investment, notes

    # Convertible: the conversion price is the better of the cap and the discount.
    if discount is None:
        discount = config.DEFAULT_SAFE_DISCOUNT
        notes.append(
            f"No conversion discount stated; {discount:.0%} assumed, the market standard at "
            "this stage."
        )
    next_pre = _next_round_pre_money(inputs, rounds)
    discounted = next_pre * (1.0 - discount) if next_pre else None

    candidates = [value for value in (cap or post, discounted) if value]
    if not candidates:
        return None, "no cap, valuation or next-round basis available", investment, notes

    valuation = min(candidates)
    if discounted and valuation == discounted and (cap or post):
        basis = (
            f"the {discount:.0%} discount to a modelled next-round pre-money of "
            f"{utils.fmt_money(next_pre)}, which prices below the "
            f"{utils.fmt_money(cap or post)} cap"
        )
    elif cap or post:
        basis = f"the {utils.fmt_money(cap or post)} cap"
    else:
        basis = (
            f"the {discount:.0%} discount to a modelled next-round pre-money of "
            f"{utils.fmt_money(next_pre)}"
        )

    effective = investment
    if instrument == NOTE:
        rate = utils.parse_percent(facts.text("financing", "interest_rate"))
        if rate is None:
            rate = config.DEFAULT_NOTE_RATE
            notes.append(f"No note rate stated; {rate:.0%} simple interest assumed.")
        years = _years_to_conversion(rounds, timeline)
        effective = investment * (1.0 + rate * years)
        notes.append(
            f"Note accrues {rate:.0%} simple interest over {years:.1f} years to conversion, "
            f"converting {utils.fmt_money(effective)} of principal and interest."
        )

    notes.append(
        "Ownership is calculated on a post-money basis; a pre-money SAFE or a larger option "
        "pool at conversion would produce a smaller stake."
    )
    return valuation, basis, effective, notes


def _next_round_pre_money(inputs: dict[str, Any], rounds: list[FinancingRound]) -> float | None:
    """Infer the next priced round's pre-money from its size and the dilution it takes.

    A round raising A for d of the company implies a post-money of A/d and a
    pre-money of A(1-d)/d. That is arithmetic on two modelled inputs, so it is
    calculated here rather than asked for.
    """
    later = rounds[1:]
    if not later:
        return None
    dilution = float(inputs.get("round_dilution") or config.DEFAULT_ROUND_DILUTION)
    amount = later[0].amount
    if amount <= 0 or dilution <= 0:
        return None
    return amount * (1.0 - dilution) / dilution


def _years_to_conversion(rounds: list[FinancingRound], timeline: Timeline) -> float:
    """Years until the next priced round, which is when a note converts."""
    later = [item for item in rounds[1:] if item.year is not None]
    if later:
        return max(0.5, float(later[0].year))
    return max(1.0, timeline.months_to_launch / 12.0)
