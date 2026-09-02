"""Step 1 — fix the clock.

Everything downstream is anchored here. The calendar years printed on a deck's
timeline slide are frequently stale by the time the deck is read: this module
works out when commercial launch actually falls relative to *today*, indexes five
commercial years off that, and reports where the 3- and 5-year marks land.

Contradictions are recorded, never repaired. An exit that precedes launch, or a
clearance that lands after the launch it gates, is a finding about the deck.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from . import config, utils
from .models import BusinessShape, DeckFacts, Ledger, Timeline

# Months from today to commercial launch, by stage, when the deck gives no date.
# Deliberately coarse: these are last-resort assumptions and are labelled as such.
_STAGE_LAG: tuple[tuple[tuple[str, ...], int, str], ...] = (
    (("revenue", "commercial", "selling", "launched", "shipping", "in market"), 0, "commercial"),
    (("cleared", "approved", "ce mark", "510(k) clearance", "authorised"), 6, "approved"),
    (("submitted", "filing", "under review", "pma", "de novo", "510(k)"), 18, "in review"),
    (("pivotal", "phase 3", "phase iii", "clinical trial", "first-in-human"), 30, "clinical"),
    (("pilot", "beta", "prototype", "validation", "feasibility"), 15, "pilot"),
    (("preclinical", "pre-clinical", "discovery", "research", "concept"), 42, "preclinical"),
    (("seed", "pre-seed", "development", "mvp"), 24, "development"),
)

_DEFAULT_LAG = 24


def build(
    deck: dict[str, Any],
    facts: DeckFacts,
    shape: BusinessShape,
    ledger: Ledger,
    today: date | None = None,
) -> Timeline:
    """Resolve the commercialisation clock and return a populated Timeline."""
    today = today or date.today()
    timeline = Timeline(today=today)

    timeline.deck_date = _deck_date(deck, facts)
    timeline.deck_date_basis = deck.get("deck_date_basis", "") if timeline.deck_date else "unknown"
    timeline.stage = facts.text("company", "stage") or "not stated"

    launch, basis, source_type, confidence = _launch_date(facts, timeline, today)
    timeline.launch_date = launch
    timeline.launch_basis = basis
    timeline.months_to_launch = max(0, utils.months_between(today, launch))

    ledger.record(
        "commercial_launch",
        launch.isoformat(),
        source_type,
        basis,
        confidence,
        notes=f"{timeline.months_to_launch} months from {today.isoformat()}",
    )

    timeline.commercial_years = _commercial_years(launch)
    _place_horizons(timeline, today)
    timeline.stated_exit_year = _stated_exit_year(facts, timeline)
    timeline.contradictions = _contradictions(facts, timeline, shape, today)
    return timeline


# --- Anchors ----------------------------------------------------------------


def _deck_date(deck: dict[str, Any], facts: DeckFacts) -> date | None:
    """The deck's own as-of date: printed on the slides, then file metadata."""
    stated = utils.parse_date(facts.text("company", "deck_date"))
    if stated:
        return stated
    parsed = deck.get("deck_date")
    return parsed if isinstance(parsed, date) else None


def _launch_date(
    facts: DeckFacts, timeline: Timeline, today: date
) -> tuple[date, str, str, str]:
    """Resolve commercial launch, in descending order of evidence.

    Returns (date, basis, source_type, confidence). A stated launch date that has
    already passed is still used as evidence of intent but is not treated as
    achieved — `_contradictions` reports it and the model re-anchors to today.
    """
    stated = utils.parse_date(facts.text("development", "launch_timing"), end_of_period=True)
    if stated and stated > today:
        return stated, "launch date stated in the deck", config.DECK, config.HIGH

    months = utils.parse_months(facts.text("development", "time_to_commercialization"))
    if months is not None and months >= 0:
        anchor = timeline.deck_date or today
        projected = utils.add_months(anchor, months)
        if projected > today:
            return (
                projected,
                f"{months} months to commercialisation stated in the deck, from "
                f"{'the deck date' if timeline.deck_date else 'today'}",
                config.DECK,
                config.MEDIUM,
            )

    regulatory = _next_regulatory_milestone(facts, today)
    if regulatory:
        when, name = regulatory
        return (
            utils.add_months(when, 3),
            f"three months after {name}, the gating milestone in the deck",
            config.CALCULATED,
            config.MEDIUM,
        )

    if stated:
        # Stated but already past: anchor to today, and flag it separately.
        return (
            utils.add_months(today, 6),
            f"deck's stated launch ({stated.isoformat()}) has passed; re-anchored to today",
            config.ASSUMPTION,
            config.LOW,
        )

    lag, label = _stage_lag(facts.text("company", "stage"))
    return (
        utils.add_months(today, lag),
        f"stage assumption: {lag} months from today for a company described as {label}",
        config.ASSUMPTION,
        config.LOW,
    )


def _stage_lag(stage_text: str) -> tuple[int, str]:
    """Map the deck's stage language onto months-to-launch."""
    lowered = (stage_text or "").lower()
    for keywords, months, label in _STAGE_LAG:
        if any(keyword in lowered for keyword in keywords):
            return months, label
    return _DEFAULT_LAG, "early stage"


def _next_regulatory_milestone(facts: DeckFacts, today: date) -> tuple[date, str] | None:
    """The earliest future approval-type milestone, which usually gates launch."""
    candidates: list[tuple[date, str]] = []
    for milestone in facts.milestones:
        if milestone["kind"] not in ("regulatory", "clinical"):
            continue
        when = utils.parse_date(milestone["date_text"], end_of_period=True)
        if when and when > today:
            candidates.append((when, milestone["name"]))

    for key in ("regulatory_milestones", "clinical_milestones"):
        when = utils.parse_date(facts.text("development", key), end_of_period=True)
        if when and when > today:
            candidates.append((when, facts.text("development", key)[:60]))

    return min(candidates, key=lambda item: item[0]) if candidates else None


# --- Commercial years -------------------------------------------------------


def _commercial_years(launch: date) -> list[dict[str, Any]]:
    """Five twelve-month windows starting at launch, each labelled for a reader."""
    years: list[dict[str, Any]] = []
    for index in range(1, config.COMMERCIAL_YEARS + 1):
        start = utils.add_months(launch, 12 * (index - 1))
        end = utils.add_months(launch, 12 * index)
        span = str(start.year) if start.year == end.year else f"{start.year}–{end.year}"
        years.append(
            {
                "index": index,
                "start": start.isoformat(),
                "end": end.isoformat(),
                "label": f"CY{index}",
                "calendar": span,
                "full_label": f"CY{index} ({span})",
            }
        )
    return years


def _place_horizons(timeline: Timeline, today: date) -> None:
    """Work out which commercial year the 3- and 5-year marks fall in.

    The spec is explicit that the one-pager's Year 3 and Year 5 columns mean
    three and five years from *today*, not from the deck's calendar.
    """
    for horizon, attribute in ((3, "year3"), (5, "year5")):
        mark = utils.add_months(today, 12 * horizon)
        index = _commercial_index(timeline, mark)
        setattr(timeline, f"{attribute}_commercial_index", index)
        if index == 0:
            label = f"{mark.year} · pre-launch"
        elif index > config.COMMERCIAL_YEARS:
            label = f"{mark.year} · beyond CY{config.COMMERCIAL_YEARS}"
        else:
            label = f"{mark.year} · CY{index}"
        setattr(timeline, f"{attribute}_label", label)


def _commercial_index(timeline: Timeline, mark: date) -> int:
    """0 if the mark falls before launch, otherwise the commercial year it lands in."""
    if timeline.launch_date is None or mark < timeline.launch_date:
        return 0
    months = utils.months_between(timeline.launch_date, mark)
    return months // 12 + 1


# "M&A Year 3 of Sales", "exit in year 4 of commercialisation" — a relative exit,
# expressed in commercial years rather than on the calendar.
_RELATIVE_EXIT = re.compile(
    r"year\s*(\d)\b.{0,24}?(sales|revenue|commercial|launch|operation)", re.IGNORECASE
)


def _stated_exit_year(facts: DeckFacts, timeline: Timeline) -> int | None:
    """The calendar year of the stated exit, resolving relative phrasing.

    Decks often date an exit against their own commercial clock rather than the
    calendar. Reading "Year 3 of Sales" as commercial year three is what lets the
    claim be tested at all — the alternative is discarding it as undated.
    """
    text = facts.text("exit", "exit_year")
    exit_date = utils.parse_date(text)
    if exit_date:
        return exit_date.year

    match = _RELATIVE_EXIT.search(text)
    if match:
        window = timeline.commercial_year(int(match.group(1)))
        if window:
            return int(str(window["start"])[:4])
    return None


# --- Contradictions ---------------------------------------------------------


def _contradictions(
    facts: DeckFacts, timeline: Timeline, shape: BusinessShape, today: date
) -> list[str]:
    """Report timeline conflicts in the deck. Reporting only — nothing is repaired."""
    found: list[str] = []
    launch = timeline.launch_date

    stated_launch = utils.parse_date(facts.text("development", "launch_timing"), end_of_period=True)
    if stated_launch and stated_launch <= today:
        found.append(
            f"Deck places commercial launch at {facts.text('development', 'launch_timing')}, "
            f"which has already passed; the model re-anchors launch to "
            f"{launch.isoformat() if launch else 'today'}."
        )

    if timeline.deck_date and utils.months_between(timeline.deck_date, today) >= 12:
        months = utils.months_between(timeline.deck_date, today)
        found.append(
            f"Deck is {months} months old ({timeline.deck_date.isoformat()}); all calendar "
            "years on its timeline slides are read relative to today, not as printed."
        )

    gate = _next_regulatory_milestone(facts, today)
    if gate and stated_launch and gate[0] > stated_launch:
        found.append(
            f"Regulatory milestone ({gate[1][:50]}) is dated after the stated commercial "
            "launch, so revenue would begin before the approval that permits it."
        )

    if shape.requires_regulatory_approval and not facts.get("development", "regulatory_pathway").present:
        found.append(
            "Product requires regulatory approval but the deck names no pathway, so launch "
            "timing rests on a stage assumption rather than a filing schedule."
        )

    if timeline.stated_exit_year and launch and timeline.stated_exit_year < launch.year:
        found.append(
            f"Stated exit ({timeline.stated_exit_year}) precedes modelled commercial launch "
            f"({launch.year}): the deck assumes an exit before the business sells anything."
        )

    runway = utils.parse_months(facts.text("financing", "runway"))
    if runway is not None and runway < timeline.months_to_launch:
        found.append(
            f"Round is described as {runway} months of runway against {timeline.months_to_launch} "
            "months to launch, so the current round does not fund the timeline it describes."
        )

    return found
