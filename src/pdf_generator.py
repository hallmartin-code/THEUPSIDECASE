"""ReportLab renderer: the one-page TEN Capital upside-case PDF.

The page is landscape letter with a fixed four-zone structure — a navy header
band, three body columns, and a footer band carrying analyst flags, numbered
sources and the confidentiality line. Seven sections and three tables read badly
down a single portrait column; across three landscape columns they fit at a size
a partner can actually read.

Vertical geometry is measured from the content on every build, and the build is
retried at progressively tighter type profiles until it fits on one page. One
page is a hard constraint, so the last profile also trims the narrative sections
to a word budget.
"""

from __future__ import annotations

import io
from datetime import date
from html import escape
from pathlib import Path
from typing import Any, Iterable

from reportlab.lib.colors import HexColor
from reportlab.lib.enums import TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import landscape, letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.lib.utils import ImageReader
from reportlab.platypus import (
    BaseDocTemplate,
    Flowable,
    Frame,
    FrameBreak,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)

from . import config, utils
from .models import CompanyAnalysis

PAGE_WIDTH, PAGE_HEIGHT = landscape(letter)

# (body size, leading, heading size, table size, space before heading, narrative word budget)
#
# The build takes the first profile whose three columns all fit, so the page is
# set as large as it can be rather than as small as it must be.
_PROFILES: tuple[tuple[float, float, float, float, float, int], ...] = (
    (11.0, 13.6, 10.4, 8.6, 11.0, 165),
    (10.3, 12.8, 9.9, 8.3, 10.0, 160),
    (9.6, 12.0, 9.4, 8.0, 9.0, 158),
    (9.0, 11.3, 8.9, 7.7, 8.0, 155),
    (8.4, 10.6, 8.4, 7.4, 7.0, 150),
    (7.9, 10.0, 8.0, 7.1, 6.2, 145),
    (7.4, 9.4, 7.6, 6.8, 5.5, 135),
    (7.0, 8.9, 7.2, 6.5, 5.0, 120),
    (6.6, 8.4, 6.9, 6.2, 4.4, 105),
    (6.2, 7.9, 6.5, 5.9, 3.8, 90),
)

_MIN_FOOTER = 0.62 * inch
_MAX_FOOTER = 1.55 * inch

# Largest gap inserted between sections when a column has space left over.
_MAX_SECTION_GAP = 40.0


def render(
    analysis: CompanyAnalysis, out_path: str | Path, report_date: date | None = None
) -> dict[str, Any]:
    """Render `analysis` to a single-page PDF. Returns {"path", "pages", "profile"}."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    stamp = report_date or analysis.analysis_date

    sources = _sources(analysis)
    analysis.sources = sources

    pdf_bytes, pages, index = b"", 0, 0
    for index, profile in enumerate(_PROFILES):
        pdf_bytes, pages = _build(analysis, sources, profile, stamp)
        if pages <= 1:
            break

    out_path.write_bytes(pdf_bytes)
    return {"path": out_path, "pages": pages, "profile": index}


# --- Sources ----------------------------------------------------------------


def _sources(analysis: CompanyAnalysis) -> list[dict[str, str]]:
    """Number every external source once, so footnote markers can point at it."""
    numbered: list[dict[str, str]] = []
    seen: dict[str, int] = {}

    def add(name: str, title: str, published: str, url: str) -> int:
        if not url:
            return 0
        if url in seen:
            return seen[url]
        number = len(numbered) + 1
        seen[url] = number
        numbered.append(
            {
                "number": str(number),
                "source_name": name,
                "title": title,
                "published": published,
                "url": url,
            }
        )
        return number

    for item in analysis.comparables:
        item_number = add(item.source_name, item.name, item.published, item.url)
        setattr(item, "_footnote", item_number)
    for item in analysis.transactions:
        item_number = add(
            item.source_name, f"{item.acquirer}/{item.target}", item.published, item.url
        )
        setattr(item, "_footnote", item_number)
    return numbered


def _marker(item: Any) -> str:
    number = getattr(item, "_footnote", 0)
    return f"<super>{number}</super>" if number else ""


def _markers(items: list[Any]) -> str:
    """One combined marker for a group of sources: a run of digits reads as a number."""
    numbers = sorted({getattr(item, "_footnote", 0) for item in items} - {0})
    return f"<super> {','.join(str(number) for number in numbers)}</super>" if numbers else ""


# --- Geometry ---------------------------------------------------------------


def _columns() -> tuple[float, list[float], float]:
    """Return (column width, the three column x positions, usable width)."""
    margin = config.PAGE_MARGIN * inch
    usable = PAGE_WIDTH - 2 * margin
    gap = config.COLUMN_GAP * inch
    width = (usable - 2 * gap) / 3
    return width, [margin + index * (width + gap) for index in range(3)], usable


def _body_bounds(footer_height: float) -> tuple[float, float]:
    """Return (bottom y, height) of the body columns."""
    margin = config.PAGE_MARGIN * inch
    header_bottom = PAGE_HEIGHT - margin - config.HEADER_HEIGHT * inch
    top = header_bottom - config.BAND_GAP * inch
    bottom = margin + footer_height + config.BAND_GAP * inch
    return bottom, max(top - bottom, 60.0)


def _frames(footer_height: float) -> list[Frame]:
    width, positions, _ = _columns()
    bottom, height = _body_bounds(footer_height)
    common = dict(leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
    return [
        Frame(x, bottom, width, height, id=f"col{index}", **common)
        for index, x in enumerate(positions)
    ]


def _measure(flowables: Iterable[Any], width: float) -> float:
    total = 0.0
    for flowable in flowables:
        _, height = flowable.wrap(width, 10_000)
        total += height
        total += float(getattr(flowable, "getSpaceBefore", lambda: 0)() or 0)
        total += float(getattr(flowable, "getSpaceAfter", lambda: 0)() or 0)
    return total


# --- Build ------------------------------------------------------------------


def _build(
    analysis: CompanyAnalysis,
    sources: list[dict[str, str]],
    profile: tuple[float, float, float, float, float, int],
    stamp: date,
) -> tuple[bytes, int]:
    """Build the document once at a given type profile; return (bytes, page count)."""
    styles = _styles(profile)
    width, _, usable = _columns()

    footer_height = _footer_height(analysis, sources, styles, usable)

    buffer = io.BytesIO()
    doc = BaseDocTemplate(
        buffer,
        pagesize=landscape(letter),
        leftMargin=config.PAGE_MARGIN * inch,
        rightMargin=config.PAGE_MARGIN * inch,
        topMargin=config.PAGE_MARGIN * inch,
        bottomMargin=config.PAGE_MARGIN * inch,
        title=f"{analysis.company} — Upside Case",
        author=config.FIRM_SHORT,
        subject="Upside-case scenario analysis",
    )
    doc.addPageTemplates(
        [
            PageTemplate(
                id="upside",
                frames=_frames(footer_height),
                onPage=_page_furniture(analysis, sources, styles, stamp, footer_height),
            )
        ]
    )

    columns = [
        _column_one(analysis, styles, profile[5], width),
        _column_two(analysis, styles, width),
        _column_three(analysis, styles, width, profile[5]),
    ]

    _, available = _body_bounds(footer_height)
    story: list[Any] = []
    overflows = False
    for index, sections in enumerate(columns):
        flowables = _assemble(sections, available, width)
        overflows = overflows or _measure(flowables, width) > available
        story += flowables
        if index < len(columns) - 1:
            story.append(FrameBreak())

    doc.build(story)
    # A column that overruns its frame spills into the next one, mixing sections
    # on what is still a single page. Treat that as a failed fit so the retry
    # loop moves to a tighter profile rather than shipping a scrambled page.
    return buffer.getvalue(), max(doc.page, 2 if overflows else 1)


def _assemble(sections: list[list[Any]], available: float, width: float) -> list[Any]:
    """Join a column's sections, distributing leftover height between them."""
    flowables: list[Any] = [item for section in sections for item in section]
    slots = len(sections) - 1
    if slots <= 0:
        return flowables

    leftover = available - _measure(flowables, width)
    if leftover <= 0:
        return flowables

    gap = min(leftover / slots, _MAX_SECTION_GAP)
    spaced: list[Any] = []
    for index, section in enumerate(sections):
        if index:
            spaced.append(Spacer(1, gap))
        spaced += section
    return spaced


def _footer_height(
    analysis: CompanyAnalysis,
    sources: list[dict[str, str]],
    styles: dict[str, ParagraphStyle],
    usable: float,
) -> float:
    """Size the footer band to the flags and source lines it has to carry."""
    flowables = _footer_flowables(analysis, sources, styles)
    measured = _measure(flowables, usable - 16) + 26.0
    return min(max(measured, _MIN_FOOTER), _MAX_FOOTER)


# --- Styles -----------------------------------------------------------------


def _styles(
    profile: tuple[float, float, float, float, float, int]
) -> dict[str, ParagraphStyle]:
    body_size, leading, heading_size, table_size, space_before, _ = profile
    return {
        "sizes": {"body": body_size, "table": table_size, "leading": leading},
        "heading": ParagraphStyle(
            "heading",
            fontName=config.BOLD_FONT,
            fontSize=heading_size,
            leading=heading_size + 1.6,
            textColor=HexColor(config.NAVY),
            spaceBefore=space_before,
            spaceAfter=2.0,
            alignment=TA_LEFT,
        ),
        "body": ParagraphStyle(
            "body",
            fontName=config.BASE_FONT,
            fontSize=body_size,
            leading=leading,
            textColor=HexColor(config.INK),
            spaceAfter=2.5,
        ),
        "small": ParagraphStyle(
            "small",
            fontName=config.BASE_FONT,
            fontSize=body_size - 0.6,
            leading=leading - 0.9,
            textColor=HexColor(config.MUTED),
            spaceAfter=2.0,
        ),
        "bullet": ParagraphStyle(
            "bullet",
            fontName=config.BASE_FONT,
            fontSize=body_size,
            leading=leading,
            textColor=HexColor(config.INK),
            leftIndent=8,
            bulletIndent=0,
            spaceAfter=2.2,
        ),
        "note": ParagraphStyle(
            "note",
            fontName=config.ITALIC_FONT,
            fontSize=body_size - 0.6,
            leading=leading - 0.9,
            textColor=HexColor(config.MUTED),
            spaceBefore=1.5,
            spaceAfter=1.5,
        ),
        "footnote": ParagraphStyle(
            "footnote",
            fontName=config.BASE_FONT,
            fontSize=max(5.2, body_size - 1.6),
            leading=max(6.0, body_size - 0.8),
            textColor=HexColor(config.MUTED),
            spaceAfter=0.8,
        ),
        "flag": ParagraphStyle(
            "flag",
            fontName=config.BASE_FONT,
            fontSize=max(5.4, body_size - 1.4),
            leading=max(6.2, body_size - 0.6),
            textColor=HexColor(config.FLAG),
            spaceAfter=0.8,
        ),
    }


def _heading(text: str, styles: dict[str, Any], first: bool = False) -> list[Any]:
    """A numbered section heading with the hairline rule under it."""
    style = styles["heading"]
    if first:
        style = ParagraphStyle("heading-first", parent=style, spaceBefore=0)
    return [Paragraph(escape(text).upper(), style), _Rule(HexColor(config.RULE)), Spacer(1, 2.0)]


# --- Column one: sections 1 and 2 -------------------------------------------


def _column_one(
    analysis: CompanyAnalysis, styles: dict[str, Any], budget: int, width: float
) -> list[list[Any]]:
    success = _heading("1. The success case", styles, first=True)
    success.append(
        Paragraph(
            escape(utils.truncate_words(analysis.narrative.success_case, budget)),
            styles["body"],
        )
    )

    value = _heading("2. Five-year value creation", styles)
    value.append(_value_table(analysis, styles, width))
    value.append(
        Paragraph(
            f"Horizons are measured from today ({analysis.analysis_date.strftime('%d %b %Y')}), "
            f"not from the deck's calendar. Commercial launch is modelled at "
            f"{analysis.timeline.launch_date} — {escape(analysis.timeline.launch_basis)}.",
            styles["small"],
        )
    )
    if analysis.reach.tam_note:
        value.append(
            Paragraph(escape(utils.truncate_words(analysis.reach.tam_note, 44)), styles["note"])
        )
    return [success, value]


# --- Column two: sections 3 and 4 -------------------------------------------


def _column_two(analysis: CompanyAnalysis, styles: dict[str, Any], width: float) -> list[list[Any]]:
    flow: list[Any] = _heading("3. Revenue build", styles, first=True)
    flow.append(_revenue_table(analysis, styles, width))

    assumption = analysis.narrative.headline_assumption or (
        f"Key assumption: {analysis.revenue.sensitivity_description}."
    )
    flow.append(Paragraph(f"<b>{escape(assumption)}</b>", styles["small"]))

    upside = analysis.revenue.years[-1] if analysis.revenue.years else None
    lower = analysis.revenue.sensitivity_years[-1] if analysis.revenue.sensitivity_years else None
    if upside and lower:
        flow.append(
            Paragraph(
                f"At {config.SENSITIVITY_UTILISATION:.0%} of "
                f"{escape(analysis.revenue.sensitivity_driver)} → {escape(upside.label)} revenue "
                f"falls from {utils.fmt_money(upside.revenue)} to {utils.fmt_money(lower.revenue)}.",
                styles["note"],
            )
        )

    if analysis.customer_value.computed:
        flow.append(
            Paragraph(
                f"Customer economics: {utils.fmt_money(analysis.customer_value.annual_value_created)} "
                f"of annual value against {utils.fmt_money(analysis.customer_value.annual_spend)} "
                f"of spend — {analysis.customer_value.ratio:.1f}× value-to-spend.",
                styles["small"],
            )
        )
    return [flow, _valuation_section(analysis, styles, width)]


def _valuation_section(
    analysis: CompanyAnalysis, styles: dict[str, Any], width: float
) -> list[Any]:
    """Section 4: the two exit outcomes, the band behind them, and the deck's claim."""
    flow: list[Any] = _heading("4. Valuation & investor return", styles)
    flow.append(_returns_table(analysis, styles, width))

    band = analysis.band
    markers = _markers([t for t in analysis.transactions if t.usable])
    flow.append(
        Paragraph(
            f"Multiple range {utils.fmt_multiple(band.low)}–{utils.fmt_multiple(band.high)} "
            f"revenue — {escape(band.basis)}.{markers}",
            styles["small"],
        )
    )

    ownership = analysis.ownership
    flow.append(
        Paragraph(
            f"{utils.fmt_money(ownership.investment)} into a {escape(ownership.instrument)} at "
            f"{escape(ownership.conversion_basis or 'terms not stated')} = "
            f"{utils.fmt_pct(ownership.initial_ownership, 2)} at entry, diluting to "
            f"{utils.fmt_pct(ownership.scale_exit_ownership, 2)} at scale across "
            f"{utils.fmt_money(sum(r.amount for r in analysis.financing))} of modelled financing.",
            styles["small"],
        )
    )
    if analysis.exit_claim.stated:
        flow.append(
            Paragraph(
                f"<b>Deck exit claim: {escape(analysis.exit_claim.verdict)}.</b> "
                + escape(utils.truncate_words(analysis.exit_claim.explanation, 42)),
                styles["note"],
            )
        )
    return flow


def _value_table(analysis: CompanyAnalysis, styles: dict[str, Any], width: float) -> Table:
    """The headline table: today against three and five years from today."""
    shape = analysis.shape
    three = _horizon(analysis, 3)
    five = _horizon(analysis, 5)

    rows = [
        ["Metric", "Today", analysis.timeline.year3_label, analysis.timeline.year5_label],
        ["Revenue", _today_revenue(analysis), _cell(three, _revenue_cell), _cell(five, _revenue_cell)],
        [
            f"Active {shape.adoption_unit_plural}",
            _today_count(analysis, ("commercial", "current_customers")),
            _cell(three, lambda y: utils.fmt_number(y.active_units)),
            _cell(five, lambda y: utils.fmt_number(y.active_units)),
        ],
        [
            utils.titlecase(shape.beneficiary),
            _today_beneficiaries(analysis),
            _cell(three, lambda y: utils.fmt_number(y.beneficiaries)),
            _cell(five, lambda y: utils.fmt_number(y.beneficiaries)),
        ],
        [
            "Penetration",
            "—",
            _penetration(analysis, three),
            _penetration(analysis, five),
        ],
        [
            "Implied value",
            _today_valuation(analysis),
            _valuation_cell(analysis, three),
            _valuation_cell(analysis, five),
        ],
    ]
    return _table(rows, styles, width, (0.295, 0.235, 0.235, 0.235))


def _revenue_table(analysis: CompanyAnalysis, styles: dict[str, Any], width: float) -> Table:
    shape = analysis.shape
    rows = [
        [
            "Year",
            "New",
            "Active",
            utils.titlecase(shape.beneficiary),
            "Revenue",
        ]
    ]
    for year in analysis.revenue.years:
        rows.append(
            [
                year.label.split(" ")[0],
                utils.fmt_number(year.new_units),
                utils.fmt_number(year.active_units),
                utils.fmt_number(year.beneficiaries),
                utils.fmt_money(year.revenue),
            ]
        )
    return _table(rows, styles, width, (0.16, 0.15, 0.17, 0.24, 0.28))


# --- Column three: valuation, benchmark, interpretation ----------------------


def _column_three(
    analysis: CompanyAnalysis, styles: dict[str, Any], width: float, budget: int
) -> list[list[Any]]:
    flow: list[Any] = _heading("5. Real-world benchmark", styles, first=True)
    if analysis.comparables:
        for comparable in analysis.comparables[:3]:
            years = comparable.years_elapsed
            span = f" over {years} year{'s' if years != 1 else ''}" if years else ""
            flow.append(
                Paragraph(
                    f"<b>{escape(comparable.name)}</b>{_marker(comparable)} — "
                    f"{utils.fmt_money(comparable.start_revenue)} → "
                    f"{utils.fmt_money(comparable.end_revenue)}{span}.",
                    styles["small"],
                )
            )
    else:
        flow.append(
            Paragraph(
                "No comparable company revenue history could be verified from a retrievable "
                "source, so the modelled ramp is unbenchmarked.",
                styles["small"],
            )
        )
    verdict = analysis.narrative.benchmark_verdict
    if verdict:
        flow.append(Paragraph(f"<b>{escape(verdict)}</b>", styles["small"]))
    if analysis.narrative.benchmark_explanation:
        flow.append(
            Paragraph(escape(analysis.narrative.benchmark_explanation), styles["small"])
        )

    return [flow, _conditions_section(analysis, styles), _interpretation(analysis, styles, budget)]


def _conditions_section(analysis: CompanyAnalysis, styles: dict[str, Any]) -> list[Any]:
    """Section 6: the conditions the outcome turns on, compact form first."""
    flow: list[Any] = _heading("6. What has to be true", styles)

    compact = " | ".join(c.compact for c in analysis.conditions[:5] if c.compact)
    if compact:
        flow.append(Paragraph(f"<b>{escape(compact)}</b>", styles["small"]))
        flow.append(Spacer(1, 2.0))

    for condition in analysis.conditions[:6]:
        text = f"<b>{escape(condition.condition)}</b>"
        if condition.assumption:
            text += f" — {escape(condition.assumption)}"
        if condition.confidence == config.LOW:
            text += f' <font color="{config.FLAG}">(low confidence)</font>'
        flow.append(Paragraph(text, styles["bullet"], bulletText="›"))

    if not analysis.conditions:
        flow.append(
            Paragraph("No governing conditions were generated for this deck.", styles["small"])
        )
    return flow


def _interpretation(
    analysis: CompanyAnalysis, styles: dict[str, Any], budget: int
) -> list[Any]:
    """Section 7: success, base and downside, in that order."""
    flow: list[Any] = _heading("7. Investment interpretation", styles)
    for label, text in (
        ("Success case", analysis.narrative.success_statement),
        ("Base case", analysis.narrative.base_case),
        ("Downside", analysis.narrative.downside),
    ):
        if text:
            flow.append(
                Paragraph(
                    f"<b>{label}:</b> {escape(utils.truncate_words(text, max(40, budget // 3)))}",
                    styles["body"],
                )
            )
    return flow


def _returns_table(analysis: CompanyAnalysis, styles: dict[str, Any], width: float) -> Table:
    rows = [["Outcome", "Revenue", "Exit value", "Own.", "MOIC"]]
    for outcome in analysis.returns:
        rows.append(
            [
                f"{outcome.label.split(' ')[0]} · {outcome.year_label.split(' ')[0]}",
                utils.fmt_money(outcome.revenue),
                utils.fmt_money(outcome.exit_value),
                utils.fmt_pct(outcome.ownership, 2),
                f"{outcome.moic:.1f}×",
            ]
        )
    if len(rows) == 1:
        rows.append(["Not computable", "—", "—", "—", "—"])
    return _table(rows, styles, width, (0.245, 0.195, 0.22, 0.17, 0.17))


# --- Cell helpers -----------------------------------------------------------


def _horizon(analysis: CompanyAnalysis, years: int) -> Any:
    """The modelled year at the 3- or 5-year mark, or None when it precedes launch."""
    index = (
        analysis.timeline.year3_commercial_index
        if years == 3
        else analysis.timeline.year5_commercial_index
    )
    if index <= 0:
        return None
    return analysis.revenue.year(min(index, config.COMMERCIAL_YEARS))


def _cell(year: Any, formatter) -> str:
    return formatter(year) if year is not None else "Pre-launch"


def _revenue_cell(year: Any) -> str:
    return utils.fmt_money(year.revenue)


def _penetration(analysis: CompanyAnalysis, year: Any) -> str:
    if year is None:
        return "—"
    return utils.fmt_pct(analysis.reach.penetration.get(year.index), 1)


def _valuation_cell(analysis: CompanyAnalysis, year: Any) -> str:
    if year is None:
        return "—"
    point = analysis.valuation(year.index)
    if point is None:
        return "—"
    return utils.fmt_money_span(point.low, point.high)


def _today_revenue(analysis: CompanyAnalysis) -> str:
    """What the deck says the company earns today, or 'Pre-revenue'."""
    for group, field in (("commercial", "revenue_model"), ("commercial", "contracts")):
        value = utils.parse_money(analysis.facts.text(group, field))
        if value:
            return utils.fmt_money(value)
    return "Pre-revenue"


def _today_count(analysis: CompanyAnalysis, field: tuple[str, str]) -> str:
    value = utils.parse_number(analysis.facts.text(*field))
    return utils.fmt_number(value) if value is not None else "—"


def _today_beneficiaries(analysis: CompanyAnalysis) -> str:
    """Beneficiaries served today, from whichever field the deck actually used."""
    candidates = [analysis.shape.beneficiary, "users", "patients", "procedures"]
    for field in candidates:
        value = utils.parse_number(analysis.facts.text("commercial", field))
        if value is not None:
            return utils.fmt_number(value)
    return "—"


def _today_valuation(analysis: CompanyAnalysis) -> str:
    for field in ("post_money", "safe_cap", "pre_money"):
        value = utils.parse_money(analysis.facts.text("financing", field))
        if value:
            return utils.fmt_money(value)
    return "—"


def _table(
    rows: list[list[str]],
    styles: dict[str, Any],
    width: float,
    proportions: tuple[float, ...],
) -> Table:
    """A compact table with a navy header row and hairline row rules.

    Cells are Paragraphs rather than plain strings so a long value — a valuation
    range, a wide account label — wraps inside its column instead of running
    silently over its neighbour.
    """
    size = styles["sizes"]["table"]
    widths = [width * proportion for proportion in proportions]
    cells = [
        [
            Paragraph(text, _cell_style(size, header=row_index == 0, first=column_index == 0))
            for column_index, text in enumerate(row)
        ]
        for row_index, row in enumerate(rows)
    ]
    table = Table(cells, colWidths=widths, hAlign="LEFT")
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), HexColor(config.NAVY)),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 3),
                ("RIGHTPADDING", (0, 0), (-1, -1), 3),
                ("TOPPADDING", (0, 0), (-1, -1), 2.0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2.0),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [HexColor("#FFFFFF"), HexColor(config.BAND)]),
                ("LINEBELOW", (0, 1), (-1, -2), 0.25, HexColor(config.RULE)),
                ("BOX", (0, 0), (-1, -1), 0.4, HexColor(config.RULE)),
            ]
        )
    )
    return table


def _cell_style(size: float, header: bool, first: bool) -> ParagraphStyle:
    """Header rows are white on navy; the first column labels, the rest are figures."""
    return ParagraphStyle(
        f"cell-{int(header)}-{int(first)}-{size}",
        fontName=config.BOLD_FONT if header or first else config.BASE_FONT,
        fontSize=size,
        leading=size + 1.4,
        textColor=HexColor("#FFFFFF" if header else config.INK),
        alignment=TA_LEFT if first else TA_RIGHT,
    )


class _Rule(Flowable):
    """A hairline rule under a section heading."""

    def __init__(self, color, thickness: float = 0.6):
        super().__init__()
        self.color = color
        self.thickness = thickness
        self.width = 0.0
        self.height = thickness
        self.keepWithNext = True

    def wrap(self, available_width: float, _available_height: float):
        self.width = available_width
        return available_width, self.height

    def draw(self) -> None:
        self.canv.setStrokeColor(self.color)
        self.canv.setLineWidth(self.thickness)
        self.canv.line(0, 0, self.width, 0)


# --- Page furniture ---------------------------------------------------------


def _footer_flowables(
    analysis: CompanyAnalysis, sources: list[dict[str, str]], styles: dict[str, Any]
) -> list[Any]:
    """Analyst flags then numbered sources, both sized to the footer band."""
    flow: list[Any] = []

    # One line per distinct rule: three penetration warnings for CY3, CY4 and CY5
    # are one finding about the model, not three, and repeating them crowds out
    # the findings a reader has not yet seen.
    flags: list[Any] = []
    seen_rules: set[str] = set()
    for issue in analysis.issues:
        if issue.severity not in ("ERROR", "WARNING") or issue.rule in seen_rules:
            continue
        seen_rules.add(issue.rule)
        flags.append(issue)

    if flags:
        text = "  ".join(f"▲ {utils.truncate_words(issue.message, 30)}" for issue in flags[:4])
        remaining = len(flags) - 4
        if remaining > 0:
            text += f"  (+{remaining} more in the JSON)"
        flow.append(Paragraph(f"<b>ANALYST FLAGS</b>  {escape(text)}", styles["flag"]))

    if sources:
        parts = []
        for source in sources:
            label = " · ".join(
                part
                for part in (source["source_name"], utils.truncate_words(source["title"], 8))
                if part
            )
            parts.append(f"<super>{source['number']}</super> {escape(label)} {escape(source['url'])}")
        flow.append(Paragraph("   ".join(parts), styles["footnote"]))
    else:
        flow.append(
            Paragraph(
                "Sources: no external source was retrieved for this analysis; all figures are "
                "deck-derived, analyst assumptions, or calculated from them.",
                styles["footnote"],
            )
        )
    return flow


def _page_furniture(
    analysis: CompanyAnalysis,
    sources: list[dict[str, str]],
    styles: dict[str, Any],
    stamp: date,
    footer_height: float,
):
    """Return an onPage callback painting the header band and the footer band."""

    def draw(canvas, _doc):
        canvas.saveState()
        _draw_header(canvas, analysis, stamp)
        _draw_footer(canvas, analysis, sources, styles, footer_height)
        canvas.restoreState()

    return draw


def _fit(canvas, text: str, font: str, size: float, max_width: float, floor: float) -> tuple[str, float]:
    """Shrink then ellipsize `text` until it fits `max_width`."""
    while size > floor and canvas.stringWidth(text, font, size) > max_width:
        size -= 0.2
    if canvas.stringWidth(text, font, size) <= max_width:
        return text, size
    while text and canvas.stringWidth(text + "…", font, size) > max_width:
        text = text[:-1]
    return text.rstrip(" ·,;-") + "…", size


def _draw_header(canvas, analysis: CompanyAnalysis, stamp: date) -> None:
    """Paint the navy header: firm, company, subtitle, and the fact strip."""
    margin = config.PAGE_MARGIN * inch
    height = config.HEADER_HEIGHT * inch
    top = PAGE_HEIGHT - margin
    bottom = top - height
    width = PAGE_WIDTH - 2 * margin

    canvas.setFillColor(HexColor(config.NAVY))
    canvas.rect(margin, bottom, width, height, stroke=0, fill=1)

    left = margin + 12
    right = PAGE_WIDTH - margin - 12

    facts = " · ".join(
        part
        for part in (
            analysis.sector,
            analysis.round_summary,
            utils.truncate_words(analysis.stage, 6),
            stamp.strftime("%d %B %Y"),
        )
        if part
    )
    facts_width = canvas.stringWidth(facts, config.BASE_FONT, 7.4)

    canvas.setFont(config.BOLD_FONT, 7.6)
    canvas.setFillColor(HexColor("#8FA9C7"))
    canvas.drawString(left, top - 13, config.FIRM_NAME)

    title = f"UPSIDE CASE  |  {analysis.company.upper()}"
    title, size = _fit(
        canvas, title, config.BOLD_FONT, 19, right - facts_width - 24 - left, floor=11.5
    )
    canvas.setFont(config.BOLD_FONT, size)
    canvas.setFillColor(HexColor("#FFFFFF"))
    canvas.drawString(left, top - 13 - size - 3, title)

    canvas.setFont(config.ITALIC_FONT, 7.8)
    canvas.setFillColor(HexColor("#B9C6D8"))
    canvas.drawString(left, bottom + 9, config.DOC_SUBTITLE)

    canvas.setFont(config.BASE_FONT, 7.4)
    canvas.setFillColor(HexColor("#D6DFEA"))
    canvas.drawRightString(right, top - 13, facts)
    canvas.setFont(config.ITALIC_FONT, 6.8)
    canvas.setFillColor(HexColor("#8FA9C7"))
    canvas.drawRightString(
        right,
        bottom + 9,
        "Success-case scenario, not a forecast or a management projection.",
    )


def _draw_footer(
    canvas,
    analysis: CompanyAnalysis,
    sources: list[dict[str, str]],
    styles: dict[str, Any],
    footer_height: float,
) -> None:
    """Paint the footer band, its rule, the flags and sources, and the branding line."""
    margin = config.PAGE_MARGIN * inch
    width = PAGE_WIDTH - 2 * margin

    canvas.setFillColor(HexColor(config.BAND))
    canvas.rect(margin, margin, width, footer_height, stroke=0, fill=1)
    canvas.setStrokeColor(HexColor(config.NAVY))
    canvas.setLineWidth(1.2)
    canvas.line(margin, margin + footer_height, margin + width, margin + footer_height)

    frame = Frame(
        margin + 8,
        margin + 13,
        width - 16,
        max(footer_height - 22, 12),
        leftPadding=0,
        rightPadding=0,
        topPadding=0,
        bottomPadding=0,
    )
    frame.addFromList(_footer_flowables(analysis, sources, styles), canvas)

    canvas.setFont(config.BASE_FONT, 6.4)
    canvas.setFillColor(HexColor(config.MUTED))
    canvas.drawString(margin + 8, margin + 5, config.CONFIDENTIALITY)

    if config.FIRM_LOGO.exists():
        try:
            image = ImageReader(str(config.FIRM_LOGO))
            image_width, image_height = image.getSize()
            draw_height = 11.0
            draw_width = image_width * (draw_height / image_height)
            canvas.drawImage(
                image,
                PAGE_WIDTH - margin - 8 - draw_width,
                margin + 3.5,
                width=draw_width,
                height=draw_height,
                mask="auto",
                preserveAspectRatio=True,
            )
        except Exception:
            pass
