"""Resolve every input the models need, in descending order of evidence.

Early-stage decks routinely omit penetration curves, ramp schedules and unit
economics. Their absence is not a finding — the model gets built anyway. What
matters is that the join between "the deck said this" and "the analyst assumed
this" never blurs, so each input is resolved in three passes:

    1. the deck, read verbatim by the extractor and parsed here      -> DECK
    2. a proposal from the model, grounded in the deck's own economics -> ASSUMPTION
    3. a coarse fallback from config                                   -> ASSUMPTION (LOW)

Every resolution lands in the ledger with its source, so the JSON sidecar can
show exactly which numbers management supplied and which the analysis invented.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import config, llm, utils
from .models import BusinessShape, DeckFacts, Ledger, Timeline


@dataclass
class Input:
    """One model input: what it means, how to read it from the deck, its bounds."""

    key: str
    description: str
    unit: str
    kind: str = "number"  # number | money | fraction | array | rounds
    deck_group: str = ""
    deck_field: str = ""
    low: float | None = None
    high: float | None = None
    fallback: Any = None
    required: bool = True
    # Only accept the deck's figure when its wording refers to the adoption unit.
    # A deck can state an "addressable sites" number that counts something other
    # than the unit the model adopts one at a time, and silently using it makes
    # penetration meaningless — and every penetration guard useless with it.
    requires_unit_match: bool = False


# --- The inputs -------------------------------------------------------------
#
# `{unit}` and `{volume}` are filled from the business shape, so the prompt
# speaks the deck's own vocabulary — "procedures per hospital per year" rather
# than "volume per unit".

INPUTS: tuple[Input, ...] = (
    Input(
        "asp",
        "Price of ONE {volume}. This must be in the same denomination as "
        "mature_volume_per_unit: if volume is counted in {volume}, this is the price of a "
        "single one of those, never the price of a durable device the account buys once.",
        "$ per {volume}",
        "money",
        "commercial",
        "asp",
        low=0.01,
        high=1e9,
    ),
    Input(
        "mature_volume_per_unit",
        "Annual {volume} at a fully ramped {unit}. This is the single most important "
        "adoption assumption in the model. Use 1 where the {unit} buys one unit a year.",
        "{volume} per {unit} per year",
        "number",
        "commercial",
        "volume_per_account",
        low=0.01,
        high=1e9,
    ),
    Input(
        "recurring_revenue_per_unit",
        "Annual recurring, service or subscription revenue per active {unit}, on top of "
        "{volume} revenue. Zero if there is none.",
        "$ per {unit} per year",
        "money",
        "commercial",
        "recurring_revenue",
        low=0.0,
        high=1e9,
        fallback=0.0,
        required=False,
    ),
    Input(
        "one_time_revenue_per_new_unit",
        "One-off hardware, installation or implementation revenue earned when a new {unit} "
        "is won. Zero if the model has none.",
        "$ per new {unit}",
        "money",
        "commercial",
        "one_time_price",
        low=0.0,
        high=1e9,
        fallback=0.0,
        required=False,
    ),
    Input(
        "gross_margin",
        "Gross margin at commercial scale, as a fraction.",
        "fraction",
        "fraction",
        "commercial",
        "gross_margin",
        low=0.05,
        high=0.98,
        fallback=0.65,
    ),
    Input(
        "new_units_by_commercial_year",
        "New {unit_plural} won in each of the five commercial years, as five numbers. This is "
        "the adoption ramp: it should reflect the sales motion, sales cycle and hiring plan "
        "the deck describes, and it is a success case, not a base case.",
        "{unit_plural} per year",
        "array",
        low=0.0,
        high=1e7,
    ),
    Input(
        "ramp_utilisation",
        "Fraction of mature {volume} a {unit} reaches in its first, second and third-plus "
        "year, as three fractions.",
        "fractions",
        "array",
        low=0.0,
        high=1.0,
        fallback=list(config.DEFAULT_RAMP),
    ),
    Input(
        "asp_change_annual",
        "Annual change in ASP once selling at scale, as a fraction. Negative means erosion.",
        "fraction per year",
        "fraction",
        low=-0.30,
        high=0.15,
        fallback=config.DEFAULT_ASP_EROSION,
    ),
    Input(
        "beneficiaries_per_unit_year",
        "{beneficiary} served each year through one fully ramped {unit}.",
        "{beneficiary} per {unit} per year",
        "number",
        low=0.0,
        high=1e8,
    ),
    Input(
        "addressable_units",
        "Total {unit_plural} that could realistically buy this product in the target "
        "geography — the denominator for market penetration.",
        "{unit_plural}",
        "number",
        "market",
        "addressable_sites",
        low=1.0,
        high=1e9,
        requires_unit_match=True,
    ),
    Input(
        "addressable_population",
        "Annual eligible {beneficiary} in the target geography.",
        "{beneficiary} per year",
        "number",
        "market",
        "patient_population",
        low=1.0,
        high=1e10,
        required=False,
    ),
    Input(
        "customer_value_per_unit_year",
        "Economic value one fully ramped {unit} captures each year from using the product: "
        "cost avoided, labour saved, revenue enabled. Leave at zero if the deck gives no "
        "basis to estimate it.",
        "$ per {unit} per year",
        "money",
        "commercial",
        "customer_roi",
        low=0.0,
        high=1e10,
        fallback=0.0,
        required=False,
    ),
    Input(
        "round_dilution",
        "Dilution taken by each institutional round the plan requires, as a fraction.",
        "fraction",
        "fraction",
        low=0.10,
        high=0.35,
        fallback=config.DEFAULT_ROUND_DILUTION,
    ),
    Input(
        "option_pool_increase",
        "Additional option-pool dilution across the whole financing path, as a fraction.",
        "fraction",
        "fraction",
        low=0.0,
        high=0.20,
        fallback=config.DEFAULT_OPTION_POOL_INCREASE,
    ),
)

SYSTEM_PROMPT = (
    "You are a venture analyst at TEN Capital building the success case for an early-stage "
    "company. You are given what the pitch deck actually says and asked to supply the inputs "
    "it does not. Your assumptions must be defensible from the deck's own economics, sales "
    "motion and market — not from generic startup benchmarks. This is an upside case: assume "
    "the plan works, but stay inside what the described business could physically do. "
    "You never present an assumption as though management provided it."
)


def resolve(
    facts: DeckFacts,
    shape: BusinessShape,
    timeline: Timeline,
    ledger: Ledger,
    client: Any | None = None,
) -> dict[str, Any]:
    """Return the resolved model inputs, recording every one of them in the ledger."""
    resolved: dict[str, Any] = {}
    gaps: list[Input] = []

    for spec in INPUTS:
        value = _from_deck(spec, facts, shape)
        if value is None:
            gaps.append(spec)
            continue
        entry = facts.get(spec.deck_group, spec.deck_field)
        resolved[spec.key] = value
        ledger.record(
            spec.key,
            value,
            config.DECK,
            entry.citation(),
            config.HIGH,
            notes=entry.quote or entry.text,
            unit=_fill(spec.unit, shape),
        )

    proposals = _propose(gaps, facts, shape, timeline, resolved, client) if gaps else {}

    for spec in gaps:
        proposal = proposals.get(spec.key) or {}
        value = _coerce(spec, proposal.get("value"))
        if value is None:
            value = _fallback(spec, shape, resolved, timeline)
            rationale = "Analyst fallback: the deck gives no basis and no proposal was usable."
            confidence = config.LOW
        else:
            rationale = str(proposal.get("rationale") or "").strip() or "Analyst assumption."
            # An assumption is never HIGH confidence — only the deck or a source can be.
            confidence = (
                config.MEDIUM if str(proposal.get("confidence")) in (config.HIGH, config.MEDIUM)
                else config.LOW
            )
        resolved[spec.key] = value
        ledger.record(
            spec.key, value, config.ASSUMPTION, rationale, confidence,
            unit=_fill(spec.unit, shape),
        )

    resolved["financing_rounds"] = _financing(proposals.get("financing_rounds"), timeline, ledger)
    return resolved


# --- Deck pass --------------------------------------------------------------


def _from_deck(spec: Input, facts: DeckFacts, shape: BusinessShape) -> Any:
    """Read one input from the extracted deck values, or return None."""
    if not spec.deck_group:
        return None
    text = facts.text(spec.deck_group, spec.deck_field)
    if not text:
        text = _alternate(spec, facts)
    if not text:
        return None
    if spec.requires_unit_match and not _mentions_unit(text, shape):
        # The deck states a count, but of something else. Let the model propose a
        # figure it has been told must be counted in adoption units.
        return None

    if spec.kind == "fraction":
        value = utils.parse_percent(text)
    elif spec.kind == "money":
        value = utils.parse_money(text)
    elif spec.kind == "number":
        value = utils.parse_number(text)
    else:
        return None
    return _bound(spec, value)


def _alternate(spec: Input, facts: DeckFacts) -> str:
    """Second-choice deck fields, for inputs a deck can express more than one way."""
    alternates = {
        "addressable_units": ("market", "addressable_customers"),
        "addressable_population": ("market", "procedure_volumes"),
    }
    group_field = alternates.get(spec.key)
    return facts.text(*group_field) if group_field else ""


def _mentions_unit(text: str, shape: BusinessShape) -> bool:
    """Does this deck text actually count the modelled unit of adoption?

    Matched on the stem so "firehouse" catches "firehouses" and "clinic" catches
    "clinics", and on both the singular and plural label the shape carries.
    """
    lowered = text.lower()
    labels = {shape.adoption_unit, shape.adoption_unit_plural}
    stems = {
        word[:-1] if len(word) > 4 and word.endswith("s") else word
        for label in labels
        for word in label.split()
        if len(word) > 3
    }
    return any(stem in lowered for stem in stems)


def _bound(spec: Input, value: float | None) -> float | None:
    """Reject a parse that falls outside the input's plausible range."""
    if value is None:
        return None
    if spec.low is not None and value < spec.low:
        return None
    if spec.high is not None and value > spec.high:
        return None
    return value


# --- Proposal pass ----------------------------------------------------------


def _propose(
    gaps: list[Input],
    facts: DeckFacts,
    shape: BusinessShape,
    timeline: Timeline,
    known: dict[str, Any],
    client: Any | None,
) -> dict[str, Any]:
    """Ask the model to fill the gaps, grounded in the deck it just read."""
    parsed = llm.complete_json(
        SYSTEM_PROMPT,
        _prompt(gaps, facts, shape, timeline, known),
        _schema(gaps, shape),
        client=client,
        max_tokens=12000,
    )
    return parsed


def _prompt(
    gaps: list[Input],
    facts: DeckFacts,
    shape: BusinessShape,
    timeline: Timeline,
    known: dict[str, Any],
) -> str:
    stated = [
        f"- {group}.{key}: {value.text}" + (f"  (slide {value.slide})" if value.slide else "")
        for group, fields in facts.groups().items()
        for key, value in fields.items()
        if value.present
    ]
    milestones = [
        f"- {m['name']} — {m['date_text'] or 'no date'} ({m['kind']})" for m in facts.milestones
    ]
    known_block = [
        f"- {key} = {value} (read from the deck; do not contradict it)"
        for key, value in known.items()
    ]
    needed = [
        f"- {spec.key} [{_fill(spec.unit, shape)}]: {_fill(spec.description, shape)}"
        for spec in gaps
    ]

    return "\n".join(
        [
            "Supply the missing inputs for a bottoms-up success-case model.",
            "",
            "=== BUSINESS ===",
            f"Company: {facts.text('company', 'name') or 'unnamed'}",
            f"Sector: {facts.text('company', 'sector') or 'not stated'}",
            f"Product: {facts.text('company', 'product') or 'not stated'}",
            f"Stage: {timeline.stage}",
            f"Unit of adoption: {shape.adoption_unit} (plural: {shape.adoption_unit_plural})",
            f"Volume metric: {shape.volume_metric} per {shape.adoption_unit} per year",
            f"Beneficiary: {shape.beneficiary}",
            f"Revenue mechanics: {shape.revenue_mechanics or 'not stated'}",
            f"Sales motion: {shape.sales_motion or 'not stated'}",
            "",
            "=== CLOCK (already fixed; use it, do not re-derive it) ===",
            f"Today: {timeline.today.isoformat()}",
            f"Commercial launch: {timeline.launch_date} ({timeline.launch_basis})",
            f"Months from today to launch: {timeline.months_to_launch}",
            "Commercial years: " + ", ".join(y["full_label"] for y in timeline.commercial_years),
            "",
            "=== WHAT THE DECK STATES ===",
            "\n".join(stated) or "(the deck states none of the extracted fields)",
            "",
            "=== DATED MILESTONES ===",
            "\n".join(milestones) or "(none)",
            "",
            "=== ALREADY RESOLVED FROM THE DECK ===",
            "\n".join(known_block) or "(nothing)",
            "",
            "=== INPUTS YOU MUST SUPPLY ===",
            "\n".join(needed),
            "",
            "Rules:",
            "- Ground each value in something in the deck: the price point, the sales cycle, the "
            "hiring plan, the size of an account, the described buyer. Say which, in `rationale`.",
            "- The adoption ramp must be consistent with the sales cycle and the launch date. A "
            "company with a nine-month enterprise sales cycle does not win 200 accounts in "
            "commercial year one.",
            "- Do not exceed the addressable base: cumulative new units across five years must "
            "stay well below the number of addressable units.",
            "- Report `confidence` as MEDIUM when the deck supports the value indirectly and LOW "
            "when you are reasoning from the category alone.",
            "- Also propose the financing rounds this plan requires, each tied to the milestone "
            "it funds. Do not assume the round in the deck is sufficient to reach scale.",
            "- Give plain numbers: 4500000 not '$4.5M', 0.65 not '65%'.",
        ]
    )


def _schema(gaps: list[Input], shape: BusinessShape) -> dict[str, Any]:
    """Build a response schema covering the gaps plus the financing plan."""
    properties: dict[str, Any] = {}
    for spec in gaps:
        if spec.kind == "array":
            value_schema: dict[str, Any] = {
                "type": "array",
                "items": {"type": "number"},
                "description": _fill(spec.description, shape),
            }
        else:
            value_schema = {"type": "number", "description": _fill(spec.description, shape)}
        properties[spec.key] = {
            "type": "object",
            "properties": {
                "value": value_schema,
                "rationale": {
                    "type": "string",
                    "description": "One sentence tying the value to something in the deck.",
                },
                "confidence": {"type": "string", "enum": [config.MEDIUM, config.LOW]},
            },
            "required": ["value", "rationale", "confidence"],
            "additionalProperties": False,
        }

    properties["financing_rounds"] = {
        "type": "array",
        "description": (
            "Financing events required to execute this plan, in order, starting with the round "
            "currently being raised. Amounts in plain dollars."
        ),
        "items": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "e.g. 'Current round', 'Series A'."},
                "months_from_today": {"type": "integer"},
                "amount": {"type": "number"},
                "milestone": {
                    "type": "string",
                    "description": "The milestone this round funds, in a few words.",
                },
                "from_deck": {
                    "type": "boolean",
                    "description": "True only if the deck states this round and its size.",
                },
            },
            "required": ["name", "months_from_today", "amount", "milestone", "from_deck"],
            "additionalProperties": False,
        },
    }

    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


# --- Coercion and fallbacks -------------------------------------------------


def _coerce(spec: Input, raw: Any) -> Any:
    """Validate a proposed value against the input's kind and bounds."""
    if spec.kind == "array":
        if not isinstance(raw, list) or not raw:
            return None
        values = [utils.parse_number(item) for item in raw]
        if any(value is None for value in values):
            return None
        if spec.low is not None or spec.high is not None:
            low = spec.low if spec.low is not None else -1e18
            high = spec.high if spec.high is not None else 1e18
            values = [utils.clamp(float(value), low, high) for value in values]
        return _resize(spec, values)

    value = utils.parse_number(raw)
    return _bound(spec, value)


def _resize(spec: Input, values: list[float]) -> list[float]:
    """Pad or trim an array input to the length the model expects."""
    expected = 3 if spec.key == "ramp_utilisation" else config.COMMERCIAL_YEARS
    if len(values) >= expected:
        return values[:expected]
    return values + [values[-1]] * (expected - len(values))


def _fallback(
    spec: Input, shape: BusinessShape, resolved: dict[str, Any], timeline: Timeline
) -> Any:
    """Last-resort value. Coarse by design, and always recorded as LOW confidence."""
    if spec.fallback is not None:
        return spec.fallback

    if spec.key == "new_units_by_commercial_year":
        # A geometric ramp off a single first-year account: the shape of adoption
        # matters more than the level, and the level is sensitivity-tested.
        base = 3.0
        return [round(base * (2.0**index), 1) for index in range(config.COMMERCIAL_YEARS)]
    if spec.key == "mature_volume_per_unit":
        return 1.0
    if spec.key == "beneficiaries_per_unit_year":
        return float(resolved.get("mature_volume_per_unit") or 1.0)
    if spec.key == "addressable_units":
        units = resolved.get("new_units_by_commercial_year") or []
        return max(sum(units) * 20.0, 500.0)
    if spec.key == "addressable_population":
        return None
    if spec.key == "asp":
        return 1.0
    return 0.0


def _financing(raw: Any, timeline: Timeline, ledger: Ledger) -> list[dict[str, Any]]:
    """Normalise the proposed financing plan, or fall back to a two-round path."""
    rounds: list[dict[str, Any]] = []
    for item in raw or []:
        amount = utils.parse_number(item.get("amount"))
        if amount is None or amount <= 0:
            continue
        rounds.append(
            {
                "name": str(item.get("name") or "Round").strip(),
                "months_from_today": int(utils.parse_number(item.get("months_from_today")) or 0),
                "amount": float(amount),
                "milestone": str(item.get("milestone") or "").strip(),
                "from_deck": bool(item.get("from_deck")),
            }
        )

    if not rounds:
        rounds = [
            {
                "name": "Current round",
                "months_from_today": 0,
                "amount": 0.0,
                "milestone": "as described in the deck",
                "from_deck": False,
            },
            {
                "name": "Next institutional round",
                "months_from_today": max(timeline.months_to_launch, 12),
                "amount": 0.0,
                "milestone": "commercial launch",
                "from_deck": False,
            },
        ]

    rounds.sort(key=lambda item: item["months_from_today"])
    for index, item in enumerate(rounds):
        ledger.record(
            f"financing_round_{index + 1}",
            item["amount"],
            config.DECK if item["from_deck"] else config.ASSUMPTION,
            item["milestone"] or item["name"],
            config.HIGH if item["from_deck"] else config.LOW,
            notes=f"{item['name']} at +{item['months_from_today']} months",
            unit="$",
        )
    return rounds


def _fill(text: str, shape: BusinessShape) -> str:
    """Substitute the business's own vocabulary into an input's description."""
    return (
        text.replace("{unit_plural}", shape.adoption_unit_plural)
        .replace("{unit}", shape.adoption_unit)
        .replace("{volume}", shape.volume_metric)
        .replace("{beneficiary}", shape.beneficiary)
    )
