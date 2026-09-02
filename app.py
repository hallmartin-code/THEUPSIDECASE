"""TEN Capital Upside Case Analyzer.

    python app.py pitchdeck.pdf
    python app.py pitchdeck.pptx --output company_upside_case.pdf

Deck -> Extract -> Normalize -> Fix Timeline -> Build Assumptions -> Research ->
Model -> Validate -> Generate Narrative -> Render PDF.

The run produces two files: the one-page PDF an investor reads, and a JSON
sidecar holding every input, assumption, source and calculation behind it.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path
from typing import Any

from src import (
    assumption_engine,
    config,
    data_extractor,
    deck_parser,
    dilution_model,
    llm,
    mailer,
    market_model,
    narrative,
    pdf_generator,
    research,
    returns_model,
    revenue_model,
    timeline_engine,
    utils,
    validation,
    valuation_model,
)
from src.models import CompanyAnalysis

TOTAL_STEPS = 9

# The default cheque modelled when the user does not name one. It is an input to
# the ownership maths, not a recommendation, and is labelled as such on the page.
DEFAULT_INVESTMENT = 100_000.0

STEP_LABELS = (
    "Reading deck",
    "Extracting company data",
    "Resolving commercialization timeline",
    "Building revenue model",
    "Researching comparables",
    "Modeling valuation",
    "Modeling investor returns",
    "Validating analysis",
    "Generating PDF",
)


class Progress:
    """Nine-stage progress reporting, shared by the CLI and the web front end.

    A full run takes minutes, most of it waiting on the model, so the stages are
    worth surfacing wherever the run is watched. The CLI prints them; the web app
    passes a sink that records them against a job for the browser to poll.
    """

    def __init__(self, sink: Any | None = None, echo: bool = True) -> None:
        self._sink = sink
        self._echo = echo
        self.step_number = 0

    def step(self, number: int, label: str) -> None:
        self.step_number = number
        if self._echo:
            print(f"[{number}/{TOTAL_STEPS}] {label}")
        self._emit("step", label)

    def detail(self, text: str, level: str = "info") -> None:
        """A line of substance under the current stage: a figure, a flag, a count."""
        if self._echo:
            marker = {"flag": "      ! ", "warn": "      "}.get(level, "      ")
            print(f"{marker}{text}")
        self._emit(level, text)

    def _emit(self, kind: str, text: str) -> None:
        if self._sink is not None:
            self._sink(self.step_number, kind, text)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="app.py",
        description="Build a TEN Capital upside-case analysis from an investor pitch deck.",
    )
    parser.add_argument("deck", help="Path to the pitch deck (.pdf or .pptx)")
    parser.add_argument("--output", "-o", help="Output PDF path (default: output/<company>.pdf)")
    parser.add_argument(
        "--investment",
        type=float,
        default=DEFAULT_INVESTMENT,
        help=f"Investment modelled for the ownership and return maths (default {DEFAULT_INVESTMENT:,.0f})",
    )
    parser.add_argument(
        "--no-research",
        action="store_true",
        help="Skip external research. The analysis still runs and says the band is unevidenced.",
    )
    parser.add_argument("--date", help="Analysis date as YYYY-MM-DD (default: today)")
    parser.add_argument(
        "--no-email",
        action="store_true",
        help="Do not email the result, even when Resend is configured.",
    )
    args = parser.parse_args(argv)

    try:
        analysis = run(
            Path(args.deck),
            investment=args.investment,
            use_research=not args.no_research,
            analysis_date=date.fromisoformat(args.date) if args.date else None,
        )
    except llm.CredentialsError as error:
        print(f"\n{error}", file=sys.stderr)
        return 2
    except (FileNotFoundError, ValueError) as error:
        print(f"\nCould not complete the analysis: {error}", file=sys.stderr)
        return 1

    pdf_path, json_path = _paths(analysis, args.output)

    Progress().step(9, STEP_LABELS[8])
    result = pdf_generator.render(analysis, pdf_path)
    sidecar = json.dumps(analysis.to_dict(), indent=2, default=str)
    json_path.write_text(sidecar, encoding="utf-8")

    # Best-effort delivery: a Resend failure must not cost the analyst the
    # analysis they just waited on, so the outcome is reported, never raised.
    delivery = {"sent": False, "skipped": "disabled with --no-email"}
    if not args.no_email:
        delivery = mailer.send_analysis(
            analysis,
            pdf_path.read_bytes(),
            sidecar.encode("utf-8"),
            stem=pdf_path.stem,
            meta={
                "source": Path(args.deck).name,
                "slides": analysis.facts.slide_count,
                "pages": result["pages"],
            },
        )

    _report(analysis, result, json_path, delivery)
    return 0


def run(
    deck_path: Path,
    investment: float = DEFAULT_INVESTMENT,
    use_research: bool = True,
    analysis_date: date | None = None,
    client: Any | None = None,
    progress: Progress | None = None,
) -> CompanyAnalysis:
    """Run the full pipeline and return the completed analysis."""
    today = analysis_date or date.today()
    analysis = CompanyAnalysis(analysis_date=today)
    progress = progress or Progress()

    progress.step(1, STEP_LABELS[0])
    deck = deck_parser.read_deck(deck_path)
    progress.detail(f"{deck['slide_count']} slides from {deck['source']}")

    progress.step(2, STEP_LABELS[1])
    facts, shape = data_extractor.extract(deck, client=client)
    analysis.facts, analysis.shape = facts, shape
    _describe(analysis, deck, facts, shape)
    progress.detail(
        f"{analysis.company} · unit of adoption: {shape.adoption_unit} · "
        f"beneficiary: {shape.beneficiary}"
    )

    progress.step(3, STEP_LABELS[2])
    analysis.timeline = timeline_engine.build(deck, facts, shape, analysis.ledger, today)
    progress.detail(
        f"launch {analysis.timeline.launch_date} "
        f"({analysis.timeline.months_to_launch} months out) · "
        f"3yr = {analysis.timeline.year3_label} · 5yr = {analysis.timeline.year5_label}"
    )
    for contradiction in analysis.timeline.contradictions:
        progress.detail(utils.truncate_words(contradiction, 22), "flag")

    progress.step(4, STEP_LABELS[3])
    inputs = assumption_engine.resolve(facts, shape, analysis.timeline, analysis.ledger, client)
    deck_count = len(analysis.ledger.by_type(config.DECK))
    progress.detail(
        f"{deck_count} inputs from the deck, {len(analysis.ledger) - deck_count} modelled"
    )
    analysis.revenue = revenue_model.build(inputs, shape, analysis.timeline, analysis.ledger)
    analysis.reach, analysis.customer_value = market_model.build(
        inputs, facts, shape, analysis.revenue, analysis.ledger
    )
    final = analysis.revenue.years[-1] if analysis.revenue.years else None
    if final:
        progress.detail(f"{final.label} revenue {utils.fmt_money(final.revenue)}")

    progress.step(5, STEP_LABELS[4])
    layer = research.ResearchLayer(
        provider=None if use_research else research.NullProvider(), client=client
    )
    if layer.enabled:
        context = _research_context(analysis)
        subject = f"{analysis.company}|{analysis.sector}|{shape.adoption_unit}"
        analysis.comparables, _ = layer.comparables(context, subject)
        analysis.transactions, _ = layer.transactions(context, subject)
        analysis.findings = layer.findings
        layer.record(analysis.ledger, analysis.comparables, analysis.transactions)
        usable = sum(1 for item in analysis.transactions if item.usable)
        progress.detail(
            f"{len(analysis.comparables)} comparables · "
            f"{len(analysis.transactions)} transactions retrieved, "
            f"{usable} with disclosed revenue"
        )
    else:
        progress.detail("research disabled; the multiple range will be an analyst assumption")

    progress.step(6, STEP_LABELS[5])
    analysis.band = valuation_model.build_band(analysis.transactions, analysis.ledger)
    analysis.valuations = valuation_model.build(analysis.revenue, analysis.band, analysis.ledger)
    analysis.exit_claim = valuation_model.test_exit_claim(
        facts, analysis.timeline, analysis.valuations, analysis.ledger
    )
    progress.detail(
        f"band {utils.fmt_multiple(analysis.band.low)}–"
        f"{utils.fmt_multiple(analysis.band.high)} revenue "
        f"({analysis.band.transaction_count} verified transactions)"
    )

    progress.step(7, STEP_LABELS[6])
    analysis.financing, analysis.ownership = dilution_model.build(
        facts, inputs, analysis.timeline, investment, analysis.ledger
    )
    analysis.returns = returns_model.build(
        analysis.valuations, analysis.ownership, analysis.timeline, analysis.ledger
    )
    for outcome in analysis.returns:
        progress.detail(
            f"{outcome.label}: {utils.fmt_money(outcome.proceeds)} on "
            f"{utils.fmt_pct(outcome.ownership, 2)} = {outcome.moic:.1f}× gross"
        )

    progress.step(8, STEP_LABELS[7])
    analysis.issues = validation.run(analysis, inputs)
    errors = sum(1 for issue in analysis.issues if issue.severity == "ERROR")
    progress.detail(f"{len(analysis.issues)} findings ({errors} errors)")
    for issue in analysis.issues[:4]:
        progress.detail(
            f"[{issue.severity}] {utils.truncate_words(issue.message, 20)}",
            "flag" if issue.severity == "ERROR" else "warn",
        )

    progress.detail("writing narrative")
    analysis.narrative = narrative.build(analysis, client=client)
    return analysis


# --- Assembly helpers -------------------------------------------------------


def _describe(
    analysis: CompanyAnalysis, deck: dict[str, Any], facts: Any, shape: Any
) -> None:
    """Fill the header-level identity fields from the extracted deck."""
    analysis.company = facts.text("company", "name") or Path(deck["source"]).stem
    analysis.sector = facts.text("company", "sector")
    analysis.product = facts.text("company", "product")
    analysis.geography = facts.text("company", "geography")
    analysis.stage = facts.text("company", "stage")
    analysis.business_model = facts.text("company", "business_model") or shape.revenue_mechanics
    analysis.instrument = facts.text("financing", "instrument")
    analysis.deck_date = deck.get("deck_date")

    raise_amount = utils.parse_money(facts.text("financing", "raise_amount"))
    parts = [utils.fmt_money(raise_amount) if raise_amount else "", analysis.instrument]
    analysis.round_summary = " ".join(part for part in parts if part).strip()


def _research_context(analysis: CompanyAnalysis) -> str:
    """The company description the research layer searches on."""
    shape = analysis.shape
    return "\n".join(
        [
            f"Company: {analysis.company}",
            f"Sector: {analysis.sector or 'not stated'}",
            f"Product: {analysis.product or 'not stated'}",
            f"Business model: {analysis.business_model or 'not stated'}",
            f"Geography: {analysis.geography or 'not stated'}",
            f"Stage: {analysis.stage or 'not stated'}",
            f"Unit of adoption: {shape.adoption_unit}; revenue per unit comes from "
            f"{shape.volume_metric}.",
            f"Sales motion: {shape.sales_motion or 'not stated'}",
            f"Regulatory pathway: {shape.regulatory_pathway or 'none stated'}",
        ]
    )


def _paths(analysis: CompanyAnalysis, override: str | None) -> tuple[Path, Path]:
    """Resolve the PDF and JSON output paths, which always share a stem."""
    if override:
        pdf_path = Path(override)
        if pdf_path.suffix.lower() != ".pdf":
            pdf_path = pdf_path.with_suffix(".pdf")
    else:
        slug = re.sub(r"[^a-z0-9]+", "-", analysis.company.lower()).strip("-") or "company"
        pdf_path = config.OUTPUT_DIR / f"{slug}_upside_case.pdf"
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    return pdf_path, pdf_path.with_suffix(".json")


def _report(
    analysis: CompanyAnalysis,
    result: dict[str, Any],
    json_path: Path,
    delivery: dict[str, Any] | None = None,
) -> None:
    """Print the closing summary: where the files are and what the model concluded."""
    ledger = analysis.ledger
    print()
    print(f"  PDF   {result['path']}  ({result['pages']} page)")
    print(f"  JSON  {json_path}")
    print()
    print(
        f"  Provenance: {len(ledger.by_type(config.DECK))} deck · "
        f"{len(ledger.by_type(config.RESEARCH))} research · "
        f"{len(ledger.by_type(config.ASSUMPTION))} assumption · "
        f"{len(ledger.by_type(config.CALCULATED))} calculated"
    )
    scale = analysis.outcome("scale")
    if scale:
        print(
            f"  Scale exit: {utils.fmt_money(scale.exit_value)} at {scale.year_label} · "
            f"{scale.moic:.1f}× gross on {utils.fmt_pct(scale.ownership, 2)} · "
            f"success case {analysis.narrative.success_probability}"
        )
    if delivery:
        if delivery.get("sent"):
            print(f"  Emailed to {', '.join(delivery.get('to', []))}")
        elif delivery.get("error"):
            print(f"  ! Email failed: {delivery['error']}")
        elif delivery.get("skipped"):
            print(f"  Email skipped: {delivery['skipped']}")
    if result["pages"] > 1:
        print("  ! Content exceeded one page at the tightest profile; review the PDF.")


if __name__ == "__main__":
    raise SystemExit(main())
