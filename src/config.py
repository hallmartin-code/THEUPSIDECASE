"""Configuration: environment, firm identity, modelling defaults and visual style.

Everything a run can be tuned by lives here. The analytical modules import
constants from this file rather than reading the environment themselves, so a
single import is enough to see what a run was configured with.
"""

from __future__ import annotations

import os
from pathlib import Path

# --- Environment ------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Load .env here rather than in app.py: the path is anchored to the project root,
# so `python path\to\app.py` started from anywhere still finds the key, and it
# runs before the os.getenv calls below (app.py imports this module first).
try:
    from dotenv import load_dotenv

    load_dotenv(PROJECT_ROOT / ".env")
except ModuleNotFoundError:  # pragma: no cover - optional convenience
    pass


def _flag(name: str, default: bool) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    return raw not in ("0", "false", "no", "off")


# --- Firm identity ----------------------------------------------------------

FIRM_NAME = "TEN CAPITAL GROUP"
FIRM_SHORT = "TEN Capital"
CONFIDENTIALITY = "Confidential – for recipients only."
DOC_SUBTITLE = "What the company could look like if the plan works"

# --- Model ------------------------------------------------------------------

LLM_MODEL = os.getenv("UPSIDE_MODEL", "claude-opus-5")
LLM_EFFORT = os.getenv("UPSIDE_EFFORT", "high")

# The web-search tool variant supported by the Opus 5 / Sonnet 5 generation.
WEB_SEARCH_TOOL = "web_search_20260209"

# --- Paths ------------------------------------------------------------------

ASSETS_DIR = PROJECT_ROOT / "assets"
OUTPUT_DIR = PROJECT_ROOT / "output"
FIRM_LOGO = ASSETS_DIR / "TEN_Capital_logo_footer.png"

RESEARCH_ENABLED = _flag("UPSIDE_RESEARCH", True)
RESEARCH_CACHE = PROJECT_ROOT / os.getenv("UPSIDE_RESEARCH_CACHE", ".cache/research.json")

# --- Source classification --------------------------------------------------

DECK = "DECK"
RESEARCH = "RESEARCH"
ASSUMPTION = "ASSUMPTION"
CALCULATED = "CALCULATED"
SOURCE_TYPES = (DECK, RESEARCH, ASSUMPTION, CALCULATED)

HIGH, MEDIUM, LOW = "HIGH", "MEDIUM", "LOW"
CONFIDENCE_LEVELS = (HIGH, MEDIUM, LOW)

# --- Modelling defaults -----------------------------------------------------
#
# These are the fallbacks the assumption engine reaches for when neither the
# deck nor the model proposes something better. Every one of them enters the
# ledger as an ASSUMPTION with LOW confidence, never as a deck figure.

# Fraction of mature utilisation an account reaches in its 1st, 2nd, 3rd+ year.
DEFAULT_RAMP = (0.30, 0.65, 1.00)

# The sensitivity case in step 4: mature volume per unit at this fraction.
SENSITIVITY_UTILISATION = 0.65

# Annual ASP change once selling at scale (negative = erosion).
DEFAULT_ASP_EROSION = -0.03

# Horizon modelled past commercial launch.
COMMERCIAL_YEARS = 5

# Financing: dilution taken by each modelled round, and option-pool top-ups.
DEFAULT_ROUND_DILUTION = 0.20
DEFAULT_OPTION_POOL_INCREASE = 0.05

# Convertible instruments, when the deck does not state terms.
DEFAULT_SAFE_DISCOUNT = 0.20
DEFAULT_NOTE_RATE = 0.08

# Valuation multiple band used only when research returns no usable transactions.
FALLBACK_MULTIPLE_RANGE = (3.0, 6.0)

# Probability band language for the success case (analytical, not statistical).
SUCCESS_CASE_BANDS = ((0.60, "20–30%"), (0.45, "15–20%"), (0.0, "10–15%"))

MISSING = "[NOT IN DECK]"

# --- Visual style -----------------------------------------------------------

NAVY = "#0A2342"
INK = "#1B1F27"
MUTED = "#5A6472"
GHOST = "#8A8F98"
RULE = "#C9D2DE"
BAND = "#F2F4F7"
ACCENT = "#1F6FB2"
FLAG = "#A5402A"

BASE_FONT = "Helvetica"
BOLD_FONT = "Helvetica-Bold"
ITALIC_FONT = "Helvetica-Oblique"

# Geometry, in inches. The page is landscape letter: seven sections and three
# tables read far better across three columns than down one.
PAGE_MARGIN = 0.42
HEADER_HEIGHT = 0.86
FOOTER_BAND_HEIGHT = 0.92
COLUMN_GAP = 0.20
BAND_GAP = 0.14
