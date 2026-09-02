"""Parsing and formatting helpers.

The extractor returns deck text verbatim; every reading of that text into a
number happens here, in Python. That is deliberate — it keeps arithmetic and
unit interpretation out of the model and makes a misread traceable to one
function rather than to a prompt.
"""

from __future__ import annotations

import calendar
import math
import re
from datetime import date
from typing import Any

# --- Numbers ----------------------------------------------------------------

_SCALES: dict[str, float] = {
    "trillion": 1e12,
    "billion": 1e9,
    "million": 1e6,
    "thousand": 1e3,
    "mm": 1e6,
    "bn": 1e9,
    "m": 1e6,
    "b": 1e9,
    "k": 1e3,
}

# Longest token first so "billion" wins over "b" and "mm" over "m". The trailing
# `s?\b` is what separates a scale word from an ordinary one: "Millions" scales,
# "months" does not, because "m" is not followed by a word boundary there.
_SCALE = re.compile(
    r"^\s*(trillion|billion|million|thousand|mm|bn|m|b|k)s?\b", re.IGNORECASE
)

_NUMBER_TEXT = r"[-+]?\d[\d,]*\.?\d*"
_NUMBER = re.compile(_NUMBER_TEXT)
_CURRENCY = re.compile(r"[$€£¥]")
# A figure introduced by a currency symbol, and one followed by a percent sign.
_MONEY = re.compile(r"[$€£¥]\s*(" + _NUMBER_TEXT + r")")
_PERCENT = re.compile(r"(" + _NUMBER_TEXT + r")\s*%")


def parse_number(text: Any) -> float | None:
    """Read the first number out of `text`, honouring a k/m/bn suffix or scale word.

    "$4.5M" -> 4500000.0, "1,250 hospitals" -> 1250.0, "6.4 Millions" -> 6.4e6,
    "500 procedures in 12 months" -> 500.0. Returns None when there is nothing to read.
    """
    if isinstance(text, (int, float)) and not isinstance(text, bool):
        return float(text)
    if not isinstance(text, str):
        return None
    cleaned = _CURRENCY.sub("", text)
    match = _NUMBER.search(cleaned)
    return _read(match, cleaned) if match else None


def _read(match: re.Match[str], source: str) -> float | None:
    """Turn one number match into a float, applying any scale word attached to it."""
    try:
        value = float(match.group(match.lastindex or 0).replace(",", ""))
    except (ValueError, IndexError):
        return None
    scale = _SCALE.match(source[match.end() :])
    return value * _SCALES[scale.group(1).lower()] if scale else value


def parse_money(text: Any) -> float | None:
    """Read a currency amount, preferring a figure the text actually marks as money.

    Deck cells routinely read "1 DEVICE MSRP $4,000; 1 CONSUMABLE $30". Taking the
    first number would give 1; taking the first *currency-marked* number gives the
    price the field is asking for.
    """
    if isinstance(text, (int, float)) and not isinstance(text, bool):
        return float(text)
    if not isinstance(text, str):
        return None
    if "%" in text and not _CURRENCY.search(text):
        return None

    match = _MONEY.search(text)
    if match:
        return _read(match, text)
    return parse_number(text)


def parse_percent(text: Any) -> float | None:
    """Read a percentage as a fraction, preferring a figure marked with a percent sign.

    "Device Margin 50%; Consumable Margin 87%" -> 0.50, not 0.50 by accident:
    the first %-marked figure wins over the first number in the string.
    """
    if isinstance(text, (int, float)) and not isinstance(text, bool):
        value = float(text)
        return value / 100.0 if value > 1.0 else value
    if not isinstance(text, str):
        return None

    match = _PERCENT.search(text)
    if match:
        value = _read(match, text)
        return value / 100.0 if value is not None else None

    number = parse_number(text)
    if number is None:
        return None
    return number / 100.0 if number > 1.0 else number


def parse_months(text: Any) -> int | None:
    """Read a duration in months. "18 months" -> 18, "2 years" -> 24."""
    if isinstance(text, (int, float)) and not isinstance(text, bool):
        return int(text)
    if not isinstance(text, str):
        return None
    number = parse_number(text)
    if number is None:
        return None
    lowered = text.lower()
    if "year" in lowered:
        return int(round(number * 12))
    if "quarter" in lowered:
        return int(round(number * 3))
    if "week" in lowered:
        return int(round(number / 4.345))
    return int(round(number))


_MONTHS = {name.lower(): index for index, name in enumerate(calendar.month_name) if name}
_MONTHS.update({name.lower(): index for index, name in enumerate(calendar.month_abbr) if name})

_ISO = re.compile(r"(20\d{2})-(\d{1,2})(?:-(\d{1,2}))?")
_QUARTER = re.compile(r"\bQ([1-4])[\s/'-]*(20\d{2})\b", re.I)
_YEAR_QUARTER = re.compile(r"\b(20\d{2})[\s/'-]*Q([1-4])\b", re.I)
_MONTH_YEAR = re.compile(r"\b([A-Za-z]{3,9})\.?,?\s+(20\d{2})\b")
_BARE_YEAR = re.compile(r"\b(20\d{2})\b")


def parse_date(text: Any, *, end_of_period: bool = False) -> date | None:
    """Read a date from deck-style text: "Q3 2026", "May 2025", "2027", "2026-04".

    `end_of_period` resolves a partial date to the last day of the period rather
    than the first, which is what a milestone deadline usually means.
    """
    if isinstance(text, date):
        return text
    if not isinstance(text, str) or not text.strip():
        return None

    match = _ISO.search(text)
    if match:
        year, month = int(match.group(1)), int(match.group(2))
        day = int(match.group(3)) if match.group(3) else None
        return _resolve(year, month, day, end_of_period)

    for pattern, order in ((_QUARTER, "qy"), (_YEAR_QUARTER, "yq")):
        match = pattern.search(text)
        if match:
            quarter = int(match.group(1) if order == "qy" else match.group(2))
            year = int(match.group(2) if order == "qy" else match.group(1))
            month = quarter * 3 if end_of_period else quarter * 3 - 2
            return _resolve(year, month, None, end_of_period)

    match = _MONTH_YEAR.search(text)
    if match:
        month = _MONTHS.get(match.group(1).lower())
        if month:
            return _resolve(int(match.group(2)), month, None, end_of_period)

    match = _BARE_YEAR.search(text)
    if match:
        year = int(match.group(1))
        return date(year, 12, 31) if end_of_period else date(year, 1, 1)
    return None


def _resolve(year: int, month: int, day: int | None, end_of_period: bool) -> date | None:
    month = max(1, min(12, month))
    try:
        if day:
            return date(year, month, day)
        return date(year, month, calendar.monthrange(year, month)[1] if end_of_period else 1)
    except ValueError:
        return None


def add_months(start: date, months: int) -> date:
    """Shift a date by whole months, clamping the day into the target month."""
    total = start.month - 1 + months
    year = start.year + total // 12
    month = total % 12 + 1
    day = min(start.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def months_between(start: date, end: date) -> int:
    """Whole months from `start` to `end`; negative when `end` precedes `start`."""
    months = (end.year - start.year) * 12 + (end.month - start.month)
    return months - 1 if end.day < start.day else months


def years_between(start: date, end: date) -> float:
    return (end - start).days / 365.25


# --- Safe arithmetic --------------------------------------------------------


def safe_div(numerator: float | None, denominator: float | None) -> float | None:
    """Divide, returning None rather than raising on a missing or zero divisor."""
    if numerator is None or denominator in (None, 0):
        return None
    try:
        result = float(numerator) / float(denominator)
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    return None if math.isnan(result) or math.isinf(result) else result


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def first_number(*candidates: Any) -> float | None:
    """Return the first candidate that reads as a number."""
    for candidate in candidates:
        value = parse_number(candidate)
        if value is not None:
            return value
    return None


# --- Formatting -------------------------------------------------------------


def fmt_money(value: float | None, dash: str = "—") -> str:
    """Format a currency amount at investor-deck scale: $1.2B, $47M, $850k."""
    if value is None:
        return dash
    magnitude = abs(value)
    sign = "-" if value < 0 else ""
    if magnitude >= 1e9:
        return f"{sign}${magnitude / 1e9:.1f}B"
    if magnitude >= 1e6:
        scaled = magnitude / 1e6
        return f"{sign}${scaled:.1f}M" if scaled < 100 else f"{sign}${scaled:.0f}M"
    if magnitude >= 1e3:
        return f"{sign}${magnitude / 1e3:.0f}k"
    return f"{sign}${magnitude:,.0f}"


def fmt_number(value: float | None, dash: str = "—") -> str:
    """Format a count. Large counts collapse to k/M so table cells stay narrow."""
    if value is None:
        return dash
    magnitude = abs(value)
    if magnitude >= 1e6:
        return f"{value / 1e6:.1f}M"
    if magnitude >= 10_000:
        return f"{value / 1e3:.0f}k"
    if magnitude >= 100:
        return f"{value:,.0f}"
    if magnitude >= 10:
        return f"{value:.0f}"
    return f"{value:.1f}".rstrip("0").rstrip(".")


def fmt_pct(value: float | None, places: int = 1, dash: str = "—") -> str:
    """Format a fraction as a percentage, widening precision for tiny shares."""
    if value is None:
        return dash
    percent = value * 100
    if 0 < abs(percent) < 0.1:
        return f"{percent:.2f}%"
    if 0 < abs(percent) < 1 and places < 1:
        return f"{percent:.1f}%"
    return f"{percent:.{places}f}%"


def fmt_multiple(value: float | None, dash: str = "—") -> str:
    if value is None:
        return dash
    return f"{value:.1f}×"


def fmt_money_span(low: float | None, high: float | None, dash: str = "—") -> str:
    """A currency range sharing one scale suffix: $29–40M rather than $28.6M–$39.7M.

    Table columns on a one-page layout are narrow enough that the shared suffix
    is the difference between a value fitting and wrapping mid-figure.
    """
    if low is None or high is None:
        return fmt_money(low if low is not None else high, dash)
    for threshold, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "k")):
        if abs(high) >= threshold:
            scaled_low, scaled_high = low / threshold, high / threshold
            if scaled_high < 10:
                return f"${scaled_low:.1f}–{scaled_high:.1f}{suffix}"
            return f"${scaled_low:.0f}–{scaled_high:.0f}{suffix}"
    return f"${low:,.0f}–{high:,.0f}"


def fmt_range(low: float | None, high: float | None, formatter=fmt_money) -> str:
    if low is None and high is None:
        return "—"
    if low is None or high is None:
        return formatter(low if low is not None else high)
    return f"{formatter(low)}–{formatter(high)}"


def titlecase(text: str) -> str:
    """Sentence-case a label without flattening acronyms."""
    text = (text or "").strip()
    if not text:
        return ""
    return text[0].upper() + text[1:]


def plural(word: str) -> str:
    """Naive pluralisation, good enough for adoption-unit labels."""
    word = (word or "").strip()
    if not word or word.endswith("s"):
        return word
    if word.endswith("y") and word[-2:-1].lower() not in "aeiou":
        return word[:-1] + "ies"
    if word.endswith(("ch", "sh", "x", "z")):
        return word + "es"
    return word + "s"


def truncate_words(text: str, limit: int) -> str:
    """Trim to a word budget, ending on a sentence where one is available.

    An investor page that stops mid-clause reads as a rendering failure, so a
    complete sentence inside the budget beats a longer fragment outside it. The
    ellipsis is kept only when no sentence boundary can be found.
    """
    words = (text or "").split()
    if len(words) <= limit:
        return text or ""

    clipped = " ".join(words[:limit])
    boundary = max(clipped.rfind(mark) for mark in (". ", "? ", "! "))
    if clipped.endswith((".", "?", "!")):
        return clipped
    # Only settle for a sentence if it keeps most of the budget; otherwise the
    # page would lose a paragraph to one long closing sentence.
    if boundary > 0 and boundary >= len(clipped) * 0.55:
        return clipped[: boundary + 1]
    return clipped.rstrip(",;:") + "…"
