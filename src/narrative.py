"""Steps 2, 13 and 14 — the written layer, generated after the arithmetic is settled.

The model writes here; it does not calculate. Every figure it is allowed to use
has already been computed and is handed to it in the prompt, and the probability
band it describes is chosen in Python from how much of the model rests on the
deck rather than on assumption.

The bar for the year-by-year narrative is causal specificity. "The company gains
significant traction" says nothing; "following clearance, three pilot hospitals
convert to commercial accounts and five regional reps open 15 more systems" is a
claim a reader can disagree with.
"""

from __future__ import annotations

import json
from typing import Any

from . import config, llm, utils
from .models import CompanyAnalysis, Condition, Narrative

SYSTEM_PROMPT = (
    "You are a venture analyst at TEN Capital writing the success case for an early-stage "
    "company. The financial model has already been built and validated; every number you need "
    "is given to you. You never introduce a figure that is not in the data you were given, and "
    "you never recompute one. You write causally and specifically — what has to happen, in what "
    "order, for the modelled outcome to occur — and you never present the success case as the "
    "expected outcome."
)


def build(analysis: CompanyAnalysis, client: Any | None = None) -> Narrative:
    """Generate the narrative sections and the 'what has to be true' conditions."""
    probability = _probability_band(analysis)
    parsed = llm.complete_json(
        SYSTEM_PROMPT,
        _prompt(analysis, probability),
        _schema(),
        client=client,
        max_tokens=12000,
    )

    narrative = Narrative(
        success_case=utils.truncate_words(str(parsed.get("success_case") or "").strip(), 150),
        year_by_year=[
            {
                "year": str(item.get("year") or "").strip(),
                "text": str(item.get("text") or "").strip(),
            }
            for item in (parsed.get("year_by_year") or [])
            if str(item.get("text") or "").strip()
        ],
        benchmark_verdict=str(parsed.get("benchmark_verdict") or "").strip(),
        benchmark_explanation=str(parsed.get("benchmark_explanation") or "").strip(),
        success_probability=probability,
        success_statement=str(parsed.get("success_statement") or "").strip(),
        base_case=str(parsed.get("base_case") or "").strip(),
        downside=str(parsed.get("downside") or "").strip(),
        headline_assumption=str(parsed.get("headline_assumption") or "").strip(),
    )
    analysis.conditions = _conditions(parsed.get("conditions") or [])
    return narrative


# --- Probability ------------------------------------------------------------


def _probability_band(analysis: CompanyAnalysis) -> str:
    """Choose the success-case probability band from how well evidenced the model is.

    This is an analytical band, not a statistic. It moves with the share of model
    inputs the deck actually supplied, whether the valuation rests on real
    transactions, and how many validation findings the analysis raised.
    """
    from .assumption_engine import INPUTS

    keys = {spec.key for spec in INPUTS}
    resolved = [entry for entry in analysis.ledger.entries if entry.metric in keys]
    deck_share = utils.safe_div(
        sum(1 for entry in resolved if entry.from_deck), len(resolved)
    ) or 0.0

    errors = sum(1 for issue in analysis.issues if issue.severity == "ERROR")
    warnings = sum(1 for issue in analysis.issues if issue.severity == "WARNING")

    score = utils.clamp(
        deck_share
        + (0.10 if analysis.band.transaction_count else 0.0)
        + (0.05 if analysis.comparables else 0.0)
        - 0.05 * errors
        - 0.02 * warnings,
        0.0,
        1.0,
    )
    return next(band for threshold, band in config.SUCCESS_CASE_BANDS if score >= threshold)


# --- Prompt -----------------------------------------------------------------


def _prompt(analysis: CompanyAnalysis, probability: str) -> str:
    """Hand the model the finished arithmetic and ask only for the prose."""
    shape = analysis.shape
    timeline = analysis.timeline

    revenue_rows = [
        {
            "year": year.label,
            "new_accounts": round(year.new_units, 1),
            "active_accounts": round(year.active_units, 1),
            "volume": round(year.volume),
            "revenue": utils.fmt_money(year.revenue),
            shape.beneficiary: round(year.beneficiaries),
            "penetration": utils.fmt_pct(analysis.reach.penetration.get(year.index)),
        }
        for year in analysis.revenue.years
    ]

    data = {
        "company": analysis.company,
        "sector": analysis.sector,
        "product": analysis.product,
        "stage": timeline.stage,
        "adoption_unit": shape.adoption_unit,
        "beneficiary": shape.beneficiary,
        "volume_metric": shape.volume_metric,
        "revenue_mechanics": shape.revenue_mechanics,
        "sales_motion": shape.sales_motion,
        "regulatory_pathway": shape.regulatory_pathway or "none stated",
        "today": timeline.today.isoformat(),
        "commercial_launch": str(timeline.launch_date),
        "launch_basis": timeline.launch_basis,
        "months_to_launch": timeline.months_to_launch,
        "three_years_from_today": timeline.year3_label,
        "five_years_from_today": timeline.year5_label,
        "milestones": analysis.facts.milestones,
        "revenue_build": revenue_rows,
        "sensitivity": {
            "driver": analysis.revenue.sensitivity_driver,
            "description": analysis.revenue.sensitivity_description,
            "cy5_upside": utils.fmt_money(
                analysis.revenue.years[-1].revenue if analysis.revenue.years else None
            ),
            "cy5_lower": utils.fmt_money(
                analysis.revenue.sensitivity_years[-1].revenue
                if analysis.revenue.sensitivity_years
                else None
            ),
        },
        "customer_value": {
            "annual_value_created": utils.fmt_money(analysis.customer_value.annual_value_created),
            "annual_spend": utils.fmt_money(analysis.customer_value.annual_spend),
            "ratio": analysis.customer_value.ratio,
        },
        "market": {
            "addressable_units": analysis.reach.addressable_units,
            "addressable_population": analysis.reach.addressable_population,
            "bottom_up_market": utils.fmt_money(analysis.reach.bottom_up_market),
            "management_tam": utils.fmt_money(analysis.reach.management_tam),
            "tam_note": analysis.reach.tam_note,
        },
        "comparables": [
            {
                "name": item.name,
                "why": item.description,
                "ramp": f"{utils.fmt_money(item.start_revenue)} ({item.start_year}) → "
                f"{utils.fmt_money(item.end_revenue)} ({item.end_year})",
                "years": item.years_elapsed,
            }
            for item in analysis.comparables
        ],
        "valuation_band": {
            "range": f"{utils.fmt_multiple(analysis.band.low)}–"
            f"{utils.fmt_multiple(analysis.band.high)} revenue",
            "basis": analysis.band.basis,
            "transactions_used": analysis.band.transaction_count,
        },
        "valuations": [
            {
                "year": point.label,
                "revenue": utils.fmt_money(point.revenue),
                "range": f"{utils.fmt_money(point.low)}–{utils.fmt_money(point.high)}",
            }
            for point in analysis.valuations
        ],
        "exit_claim": {
            "verdict": analysis.exit_claim.verdict,
            "explanation": analysis.exit_claim.explanation,
        },
        "financing": [
            {
                "name": item.name,
                "year": item.year,
                "amount": utils.fmt_money(item.amount),
                "milestone": item.milestone,
                "source": item.source_type,
            }
            for item in analysis.financing
        ],
        "ownership": {
            "instrument": analysis.ownership.instrument,
            "investment": utils.fmt_money(analysis.ownership.investment),
            "initial": utils.fmt_pct(analysis.ownership.initial_ownership, 2),
            "at_scale": utils.fmt_pct(analysis.ownership.scale_exit_ownership, 2),
            "conversion_basis": analysis.ownership.conversion_basis,
        },
        "returns": [
            {
                "outcome": item.label,
                "year": item.year_label,
                "exit_value": utils.fmt_money(item.exit_value),
                "ownership": utils.fmt_pct(item.ownership, 2),
                "moic": f"{item.moic:.1f}×",
                "irr": utils.fmt_pct(item.irr, 0),
            }
            for item in analysis.returns
        ],
        "low_confidence_assumptions": [
            {"metric": entry.metric, "value": entry.value, "why": entry.source}
            for entry in analysis.ledger.low_confidence()
        ],
        "deck_supplied": [
            entry.metric for entry in analysis.ledger.by_type(config.DECK)
        ],
        "validation_findings": [
            f"[{issue.severity}] {issue.message}" for issue in analysis.issues
        ],
        "assigned_probability_band": probability,
    }

    return "\n".join(
        [
            f"Write the success-case narrative for {analysis.company}.",
            "",
            "Everything below has already been calculated. Use these figures exactly as given, "
            "in the units given. Do not recompute, re-scale or introduce a number that is not "
            "here.",
            "",
            "=== ANALYSIS ===",
            json.dumps(data, indent=2, default=str),
            "",
            "What to write:",
            "",
            "1. `success_case` — 110–140 words tracing the path from today to commercial scale. "
            "Name the gating milestone, the launch, the adoption mechanism and the revenue "
            "inflection. Use the figures above.",
            "",
            "2. `year_by_year` — one entry per period from today through the final commercial "
            "year. Each must state what actually has to happen: approvals obtained, pilots "
            "converted, reps hired, geographies opened, rounds raised, accounts won. Be "
            "specific and causal. Never write a sentence like 'the company gains traction'.",
            "",
            "3. `conditions` — the 5 to 7 assumptions that drive most of the outcome. For each: "
            "the condition, the model's assumption, why it matters, and what breaks if it "
            "fails. Draw them from the low-confidence assumptions and the validation findings "
            "above where those are the real risks. `compact` is a four-to-six word version for "
            "a single line on the page, e.g. 'Clearance on schedule' or '500 procedures/site'.",
            "",
            "4. `benchmark_verdict` — whether the modelled adoption curve sits below, "
            "approximately at, or above the comparable companies' actual revenue ramps. If "
            "there are no comparables, say so and explain what that means for confidence.",
            "",
            "5. `headline_assumption` — one line naming the single assumption the revenue build "
            "turns on, using the sensitivity driver above.",
            "",
            "6. `success_statement`, `base_case`, `downside` — the investment interpretation, "
            f"22–28 words each, each a complete sentence. The success case has been assigned a "
            f"probability band of "
            f"{probability}; state that band in `success_statement` and do not alter it. The "
            "base case is what slower adoption, lower utilisation or delayed launch produces. "
            "The downside is the single most likely failure mode, named specifically.",
            "",
            "This is a success case, not a forecast. Write it as the outcome if the plan works.",
        ]
    )


def _schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "success_case": {
                "type": "string",
                "description": (
                    "110–140 words, ending on a complete sentence. Anything past "
                    "140 words is cut from the page, so land the point inside it."
                ),
            },
            "year_by_year": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "year": {"type": "string", "description": "e.g. 'Now–launch', 'CY1'."},
                        "text": {"type": "string", "description": "What must happen, causally."},
                    },
                    "required": ["year", "text"],
                    "additionalProperties": False,
                },
            },
            "conditions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "condition": {"type": "string"},
                        "assumption": {"type": "string"},
                        "why_it_matters": {"type": "string"},
                        "if_it_fails": {"type": "string"},
                        "confidence": {
                            "type": "string",
                            "enum": [config.HIGH, config.MEDIUM, config.LOW],
                        },
                        "compact": {"type": "string", "description": "Four to six words."},
                    },
                    "required": [
                        "condition", "assumption", "why_it_matters", "if_it_fails",
                        "confidence", "compact",
                    ],
                    "additionalProperties": False,
                },
            },
            "benchmark_verdict": {
                "type": "string",
                "description": "One sentence: below / approximately at / above comparable adoption.",
            },
            "benchmark_explanation": {"type": "string", "description": "One sentence of why."},
            "headline_assumption": {"type": "string"},
            "success_statement": {"type": "string", "description": "~25 words, states the band."},
            "base_case": {"type": "string", "description": "~25 words."},
            "downside": {"type": "string", "description": "~25 words."},
        },
        "required": [
            "success_case", "year_by_year", "conditions", "benchmark_verdict",
            "benchmark_explanation", "headline_assumption", "success_statement",
            "base_case", "downside",
        ],
        "additionalProperties": False,
    }


def _conditions(raw: list[Any]) -> list[Condition]:
    """Normalise the conditions, keeping at most seven."""
    conditions: list[Condition] = []
    for item in raw[:7]:
        condition = str(item.get("condition") or "").strip()
        if not condition:
            continue
        conditions.append(
            Condition(
                condition=condition,
                assumption=str(item.get("assumption") or "").strip(),
                why_it_matters=str(item.get("why_it_matters") or "").strip(),
                if_it_fails=str(item.get("if_it_fails") or "").strip(),
                confidence=(
                    str(item.get("confidence"))
                    if str(item.get("confidence")) in config.CONFIDENCE_LEVELS
                    else config.MEDIUM
                ),
                compact=utils.truncate_words(
                    str(item.get("compact") or condition).strip(), 7
                ),
            )
        )
    return conditions
