"""Deck -> DeckFacts: what the pitch deck actually says, quoted rather than interpreted.

This is the only place the model reads the deck. Its contract is narrow on
purpose: copy the figure as printed, name the slide it is on, quote the line it
came from, and leave the field empty when the deck does not say. Reading "$4.5M"
as 4,500,000 happens later, in `utils`, so a misread is a Python bug rather than
a prompt failure — and so that nothing the deck never said can acquire a number.
"""

from __future__ import annotations

from typing import Any

from . import config, llm
from .models import BusinessShape, DeckFacts, DeckValue

# --- What to look for -------------------------------------------------------
#
# Each entry is a field the analysis can use. The guidance becomes the schema
# property description, so this table is simultaneously the prompt, the schema
# and the documentation of what the extractor is expected to find.

FIELDS: dict[str, dict[str, str]] = {
    "company": {
        "name": "Company name exactly as written on the deck.",
        "sector": "Sector or category, e.g. 'medical device', 'B2B SaaS', 'diagnostics'.",
        "product": "One-line description of what the company sells.",
        "business_model": "How revenue is earned: device sale, subscription, licence, per-test fee.",
        "geography": "Primary market geography.",
        "stage": "Current development or commercial stage, as the deck describes it.",
        "deck_date": "Any date printed on the deck indicating when it was prepared.",
    },
    "financing": {
        "raise_amount": "Size of the round currently being raised.",
        "instrument": "Security offered: priced equity, SAFE, convertible note.",
        "pre_money": "Pre-money valuation, if stated.",
        "post_money": "Post-money valuation, if stated.",
        "safe_cap": "SAFE or note valuation cap.",
        "discount": "Conversion discount, e.g. '20%'.",
        "interest_rate": "Note interest rate.",
        "maturity": "Note maturity date or term.",
        "previously_raised": "Capital raised to date.",
        "committed": "Amount of the current round already committed or soft-circled.",
        "lead_investor": "Named lead investor for the current round.",
        "use_of_funds": "Use of proceeds, with line items if the deck breaks them out.",
        "runway": "Runway the round is said to provide.",
    },
    "commercial": {
        "asp": (
            "Price of ONE unit of the volume metric you classify in `shape` — one consumable, "
            "one test, one seat, one procedure, one subscription-year. NOT the price of the "
            "durable device or platform that the account buys once; that goes in one_time_price."
        ),
        "one_time_price": (
            "One-off price of the durable device, hardware, instrument or implementation an "
            "account pays once when it is won."
        ),
        "revenue_model": "Pricing and revenue mechanics in the deck's own words.",
        "recurring_revenue": "Recurring, subscription or service revenue per customer.",
        "consumables_revenue": "Consumable, cartridge or per-use revenue per customer.",
        "cogs": "Cost of goods sold per unit.",
        "gross_margin": "Gross margin percentage.",
        "current_customers": "Customers, accounts or sites today.",
        "pilots": "Pilot sites or evaluations underway.",
        "lois": "Letters of intent, MOUs or pre-orders.",
        "contracts": "Signed commercial contracts.",
        "users": "Users on the platform or product.",
        "patients": "Patients treated, screened or enrolled.",
        "procedures": "Procedures, tests or transactions performed.",
        "volume_per_account": "Volume per account per year: procedures, tests, seats, units.",
        "sales_cycle": "Length of the sales cycle.",
        "distribution": "Distribution model: direct, distributor, channel, OEM.",
        "sales_motion": "How the product is sold and to which buyer.",
        "sales_hiring": "Sales hiring plan: reps, territories, timing.",
        "customer_roi": "Economic value the customer gets: cost avoided, hours saved, revenue enabled.",
        "reimbursement": "Reimbursement or payor coverage status.",
    },
    "market": {
        "tam": "Total addressable market as stated, with its unit.",
        "sam": "Serviceable addressable market as stated.",
        "som": "Serviceable obtainable market as stated.",
        "tam_methodology": "How the TAM was built, if the deck explains it.",
        "addressable_customers": "Number of addressable customers or accounts.",
        "addressable_sites": "Number of addressable hospitals, clinics, facilities or sites.",
        "patient_population": "Size of the eligible patient or user population.",
        "procedure_volumes": "Annual procedure, test or transaction volume in the market.",
    },
    "development": {
        "product_milestones": "Product development milestones and their dates.",
        "regulatory_milestones": "Regulatory submissions, clearances or approvals and their dates.",
        "clinical_milestones": "Clinical study milestones and their dates.",
        "manufacturing_milestones": "Manufacturing or scale-up milestones and their dates.",
        "regulatory_pathway": "Regulatory pathway named: 510(k), De Novo, PMA, CE mark, none.",
        "launch_timing": "Stated commercial launch date or first revenue date.",
        "time_to_commercialization": "Stated time to commercialisation, e.g. '18 months'.",
    },
    "exit": {
        "exit_year": "Year of a stated or targeted exit.",
        "exit_valuation": "Stated or targeted exit valuation.",
        "exit_path": "Named acquirers or the exit route the deck describes.",
        "comparable_transactions": "Comparable acquisitions the deck cites, with values if given.",
    },
}

SYSTEM_PROMPT = (
    "You are an extraction engine for TEN Capital. You read investor pitch decks and report "
    "what they say — nothing more. You never estimate, never infer, never convert units, and "
    "never perform arithmetic. If a deck does not state something, you say so by leaving the "
    "field empty. You are judged on fidelity to the source, not on completeness."
)

_RULES = """
Rules, in order of importance:

1. `text` holds THE FIGURE ALONE, copied exactly as printed — including the currency symbol
   and the scale word — in the unit the field asks for. The surrounding sentence belongs in
   `quote`, never in `text`.
     good:  text "$4,000"                     quote "1 DEVICE MSRP $4,000; Distributor $3,200"
     good:  text "125 consumables per year"   quote "1 Device + 125 Consumables"
     bad:   text "1 DEVICE MSRP $4,000; Distributor $3,200; 1 Consumable Unit MSRP $30"
   Where a slide gives several figures, put the ONE the field asks for in `text`. If the field
   asks for a price per unit and the slide prices a device, a consumable and a bundle, pick the
   figure that matches the field, not the first one on the slide.
   Do not convert, round, normalise or re-scale the figure you pick.
2. Return one entry per field the deck actually states, and OMIT every field it does not.
   A short list is a correct answer. Never fill a gap with an estimate, an industry norm,
   or a figure implied by another slide. Do not emit an entry with empty text.
3. `field` must be one of the listed names exactly. `slide` is the 1-based slide number the
   figure appears on, taken from the "--- Slide N ---" markers. Use 0 only when you
   genuinely cannot tell.
4. `quote` is a short verbatim fragment (under 25 words) from that slide containing the figure,
   so a reader can find it. Do not paraphrase the quote.
5. Do not add up, subtract, multiply or divide anything. If the deck says a number, report that
   number; if it does not, leave the field empty.
6. For fields that describe rather than measure (revenue_model, use_of_funds, milestones), a
   faithful one-or-two-sentence summary in the deck's own vocabulary is fine.
""".strip()

_SHAPE_RULES = """
Also classify the business so the revenue model can be built bottoms-up.

The `adoption_unit` is the thing that gets adopted one at a time and that revenue scales with:
a hospital, a clinic, a physician, an enterprise account, a seat, a facility, a distributor,
an installed device, a subscriber. Choose the unit the deck's own commercial plan is organised
around. The `beneficiary` is who is ultimately served through those units — patients, users,
students, drivers. The `volume_metric` is what gets counted per unit per year — procedures,
tests, transactions, seats, units shipped.

The `volume_metric` and the `commercial.asp` figure MUST agree in denomination. If you classify
the volume metric as consumables, then `commercial.asp` is the price of ONE consumable and the
device price belongs in `commercial.one_time_price`. If you classify it as devices sold, then
`commercial.asp` is the device price and mature volume is devices per account per year. Getting
this pair out of step multiplies a device price by a consumable count and inflates the model by
two orders of magnitude, so check it before you answer.

This classification is judgement, not extraction, so base it on the deck as a whole and explain
it in `rationale`.
""".strip()


def field_names() -> list[str]:
    """Every extractable field as "group.key", the enum the response is keyed by."""
    return [f"{group}.{key}" for group, fields in FIELDS.items() for key in fields]


def build_schema() -> dict[str, Any]:
    """Build the response schema.

    Findings come back as a flat list of {field, text, slide, quote} rather than a
    nested object per group. Sixty fields each carrying their own three-key object
    compiles to a grammar the API rejects as too large; one object definition plus
    an enum of field names is a fraction of the size. It also makes absence
    explicit — a field the deck does not mention simply has no entry — which is
    exactly the contract this extractor is meant to enforce.
    """
    groups: dict[str, Any] = {
        "facts": {
            "type": "array",
            "description": "One entry per field the deck actually states. Omit the rest.",
            "items": {
                "type": "object",
                "properties": {
                    "field": {"type": "string", "enum": field_names()},
                    "text": {"type": "string", "description": "Verbatim from the deck."},
                    "slide": {
                        "type": "integer",
                        "description": "1-based slide number, 0 if unknown.",
                    },
                    "quote": {
                        "type": "string",
                        "description": "Short verbatim supporting fragment.",
                    },
                },
                "required": ["field", "text", "slide", "quote"],
                "additionalProperties": False,
            },
        }
    }

    groups["milestones"] = {
        "type": "array",
        "description": "Every dated milestone in the deck, in the order presented.",
        "items": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "date_text": {"type": "string", "description": "Date as printed: 'Q3 2026'."},
                "kind": {
                    "type": "string",
                    "enum": [
                        "product",
                        "regulatory",
                        "clinical",
                        "manufacturing",
                        "commercial",
                        "financing",
                        "other",
                    ],
                },
                "slide": {"type": "integer"},
            },
            "required": ["name", "date_text", "kind", "slide"],
            "additionalProperties": False,
        },
    }

    groups["shape"] = {
        "type": "object",
        "properties": {
            "adoption_unit": {
                "type": "string",
                "description": (
                    "Singular unit of adoption, one or two words and nothing else: "
                    "'hospital', 'firehouse', 'enterprise account', 'installed device'. "
                    "No parenthetical, no explanation — this label is printed in a table."
                ),
            },
            "beneficiary": {
                "type": "string",
                "description": (
                    "Who is ultimately served, one or two words: 'patients', 'users', "
                    "'students'. No qualifying clause."
                ),
            },
            "volume_metric": {
                "type": "string",
                "description": (
                    "What is counted per unit per year, one or two words: 'procedures', "
                    "'consumables', 'tests', 'seats'. No figures, no explanation."
                ),
            },
            "revenue_mechanics": {
                "type": "string",
                "description": "One sentence: how a single unit generates revenue.",
            },
            "has_recurring": {"type": "boolean"},
            "has_consumables": {"type": "boolean"},
            "requires_regulatory_approval": {"type": "boolean"},
            "sales_motion": {"type": "string"},
            "rationale": {"type": "string", "description": "Why this unit, in one sentence."},
        },
        "required": [
            "adoption_unit",
            "beneficiary",
            "volume_metric",
            "revenue_mechanics",
            "has_recurring",
            "has_consumables",
            "requires_regulatory_approval",
            "sales_motion",
            "rationale",
        ],
        "additionalProperties": False,
    }

    return {
        "type": "object",
        "properties": groups,
        "required": list(groups),
        "additionalProperties": False,
    }


def build_prompt(deck: dict[str, Any]) -> str:
    """Assemble the extraction turn: rules, then the slide-labelled transcript."""
    catalogue = "\n".join(
        f"{group}.{key}: {guidance}"
        for group, fields in FIELDS.items()
        for key, guidance in fields.items()
    )
    return "\n".join(
        [
            "Extract the fields below from the pitch deck transcript at the end of this message.",
            "",
            _RULES,
            "",
            "FIELDS:",
            catalogue,
            "",
            _SHAPE_RULES,
            "",
            f"=== PITCH DECK ({deck['source']}, {deck['slide_count']} slides) ===",
            deck["full_text"],
        ]
    )


def extract(deck: dict[str, Any], client: Any | None = None) -> tuple[DeckFacts, BusinessShape]:
    """Extract the deck into DeckFacts plus the business shape classification."""
    parsed = llm.complete_json(
        SYSTEM_PROMPT,
        build_prompt(deck),
        build_schema(),
        client=client,
        max_tokens=24000,
    )

    facts = DeckFacts(slide_count=deck["slide_count"], source_file=deck["source"])
    for group in FIELDS:
        target = getattr(facts, group)
        for key in FIELDS[group]:
            target[key] = DeckValue()

    valid = set(field_names())
    for raw in parsed.get("facts") or []:
        name = str(raw.get("field") or "")
        if name not in valid:
            continue
        group, _, key = name.partition(".")
        getattr(facts, group)[key] = _value(raw)

    facts.milestones = [
        {
            "name": str(item.get("name", "")).strip(),
            "date_text": str(item.get("date_text", "")).strip(),
            "kind": str(item.get("kind", "other")).strip() or "other",
            "slide": _slide(item.get("slide")),
        }
        for item in (parsed.get("milestones") or [])
        if str(item.get("name", "")).strip()
    ]

    return facts, _shape(parsed.get("shape") or {}, facts)


def _value(raw: Any) -> DeckValue:
    """Coerce one extracted field into a DeckValue, tolerating a bare string."""
    if isinstance(raw, str):
        return DeckValue(text=raw.strip())
    if not isinstance(raw, dict):
        return DeckValue()
    text = str(raw.get("text") or "").strip()
    if text.lower() in ("n/a", "none", "not stated", "not disclosed", config.MISSING.lower()):
        text = ""
    return DeckValue(text=text, slide=_slide(raw.get("slide")), quote=str(raw.get("quote") or ""))


def _label(raw: Any, default: str, words: int = 3) -> str:
    """Reduce a classification to a short printable label.

    These strings become column headings and sentence fragments on a one-page
    layout, so a qualifying clause — "installed device (unit placed with an EMS
    agency)" — has to be cut back to the noun even when the prompt asked for one.
    """
    text = str(raw or "").strip().lower()
    for separator in ("(", ",", ";", ":", " - ", " — "):
        text = text.split(separator)[0].strip()
    parts = text.split()
    return " ".join(parts[:words]) if parts else default


def _slide(raw: Any) -> int | None:
    try:
        number = int(raw)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _shape(raw: dict[str, Any], facts: DeckFacts) -> BusinessShape:
    """Build the BusinessShape, falling back to neutral labels when unclassified."""
    from . import utils

    unit = _label(raw.get("adoption_unit"), "customer")
    return BusinessShape(
        adoption_unit=unit,
        adoption_unit_plural=utils.plural(unit),
        beneficiary=_label(raw.get("beneficiary"), "users", words=2),
        volume_metric=_label(raw.get("volume_metric"), "units", words=2),
        revenue_mechanics=str(raw.get("revenue_mechanics") or "").strip(),
        has_recurring=bool(raw.get("has_recurring")),
        has_consumables=bool(raw.get("has_consumables")),
        requires_regulatory_approval=bool(raw.get("requires_regulatory_approval")),
        regulatory_pathway=facts.text("development", "regulatory_pathway"),
        sales_motion=str(raw.get("sales_motion") or "").strip()
        or facts.text("commercial", "sales_motion"),
        rationale=str(raw.get("rationale") or "").strip(),
    )
