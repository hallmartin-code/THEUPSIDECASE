"""Structured data model for one upside-case analysis.

Two ideas carry the whole file:

`Sourced` — a value that knows where it came from. Every material number in the
analysis is wrapped in one, classified DECK / RESEARCH / ASSUMPTION / CALCULATED,
so nothing reaches the PDF without provenance.

`Ledger` — the ordered collection of those values, which becomes the audit trail
in the JSON sidecar.

`CompanyAnalysis` is the top-level object the pipeline fills in and the renderer
and the JSON writer read from.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import date
from typing import Any

from . import config


# --- Provenance -------------------------------------------------------------


@dataclass
class Sourced:
    """One value plus the story of where it came from.

    `source` is the citation: a slide reference for DECK, a URL for RESEARCH,
    the reasoning for an ASSUMPTION, or the formula for a CALCULATED value.
    """

    metric: str
    value: Any
    source_type: str
    source: str = ""
    confidence: str = config.MEDIUM
    notes: str = ""
    unit: str = ""

    def __post_init__(self) -> None:
        if self.source_type not in config.SOURCE_TYPES:
            raise ValueError(
                f"{self.metric}: source_type {self.source_type!r} "
                f"must be one of {config.SOURCE_TYPES}"
            )
        if self.confidence not in config.CONFIDENCE_LEVELS:
            raise ValueError(f"{self.metric}: confidence {self.confidence!r} is not a level")

    @property
    def from_deck(self) -> bool:
        return self.source_type == config.DECK

    def __float__(self) -> float:
        return float(self.value)


@dataclass
class Ledger:
    """The source/assumption ledger: metric | value | source_type | source | confidence."""

    entries: list[Sourced] = field(default_factory=list)

    def record(
        self,
        metric: str,
        value: Any,
        source_type: str,
        source: str = "",
        confidence: str = config.MEDIUM,
        notes: str = "",
        unit: str = "",
    ) -> Sourced:
        """Add (or replace) an entry and return it, so callers can use the value inline."""
        entry = Sourced(metric, value, source_type, source, confidence, notes, unit)
        self.entries = [e for e in self.entries if e.metric != metric]
        self.entries.append(entry)
        return entry

    def add(self, entry: Sourced) -> Sourced:
        self.entries = [e for e in self.entries if e.metric != entry.metric]
        self.entries.append(entry)
        return entry

    def get(self, metric: str) -> Sourced | None:
        return next((e for e in self.entries if e.metric == metric), None)

    def value(self, metric: str, default: Any = None) -> Any:
        entry = self.get(metric)
        return default if entry is None else entry.value

    def number(self, metric: str, default: float | None = None) -> float | None:
        """Return a metric as a float, or `default` when absent or non-numeric."""
        value = self.value(metric)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return default
        return float(value)

    def by_type(self, source_type: str) -> list[Sourced]:
        return [e for e in self.entries if e.source_type == source_type]

    def low_confidence(self) -> list[Sourced]:
        return [e for e in self.entries if e.confidence == config.LOW]

    def __len__(self) -> int:
        return len(self.entries)


# --- Deck extraction --------------------------------------------------------


@dataclass
class DeckValue:
    """A single field as the extractor found it, quoted rather than interpreted.

    `text` is verbatim from the deck; the numeric reading is done in Python so
    the model never performs the arithmetic.
    """

    text: str = ""
    slide: int | None = None
    quote: str = ""

    @property
    def present(self) -> bool:
        return bool(self.text.strip())

    def citation(self) -> str:
        return f"slide {self.slide}" if self.slide else "deck"


@dataclass
class DeckFacts:
    """Everything the extractor found, grouped the way the spec's INPUT section is."""

    company: dict[str, DeckValue] = field(default_factory=dict)
    financing: dict[str, DeckValue] = field(default_factory=dict)
    commercial: dict[str, DeckValue] = field(default_factory=dict)
    market: dict[str, DeckValue] = field(default_factory=dict)
    development: dict[str, DeckValue] = field(default_factory=dict)
    exit: dict[str, DeckValue] = field(default_factory=dict)
    milestones: list[dict[str, Any]] = field(default_factory=list)
    slide_count: int = 0
    source_file: str = ""

    def groups(self) -> dict[str, dict[str, DeckValue]]:
        return {
            "company": self.company,
            "financing": self.financing,
            "commercial": self.commercial,
            "market": self.market,
            "development": self.development,
            "exit": self.exit,
        }

    def get(self, group: str, key: str) -> DeckValue:
        return self.groups().get(group, {}).get(key) or DeckValue()

    def text(self, group: str, key: str, default: str = "") -> str:
        value = self.get(group, key)
        return value.text.strip() if value.present else default


@dataclass
class BusinessShape:
    """How this company actually makes money — the model's structural read of the deck.

    The unit of adoption is the hinge of the whole revenue model: hospital,
    clinic, enterprise seat, installed device, distributor, subscriber.
    """

    adoption_unit: str = "customer"
    adoption_unit_plural: str = "customers"
    beneficiary: str = "users"
    volume_metric: str = "units"
    revenue_mechanics: str = ""
    has_recurring: bool = False
    has_consumables: bool = False
    requires_regulatory_approval: bool = False
    regulatory_pathway: str = ""
    sales_motion: str = ""
    rationale: str = ""


# --- Timeline ---------------------------------------------------------------


@dataclass
class Timeline:
    """The fixed clock: today, launch, and where 3 and 5 years from today land."""

    today: date = field(default_factory=date.today)
    deck_date: date | None = None
    deck_date_basis: str = ""
    stage: str = ""
    months_to_launch: int = 0
    launch_date: date | None = None
    launch_basis: str = ""
    commercial_years: list[dict[str, Any]] = field(default_factory=list)
    year3_label: str = ""
    year5_label: str = ""
    year3_commercial_index: int = 0
    year5_commercial_index: int = 0
    stated_exit_year: int | None = None
    contradictions: list[str] = field(default_factory=list)

    def commercial_year(self, index: int) -> dict[str, Any] | None:
        return next((y for y in self.commercial_years if y["index"] == index), None)


# --- Revenue and reach ------------------------------------------------------


@dataclass
class RevenueYear:
    """One modelled commercial year of the cohort build."""

    index: int
    label: str
    new_units: float = 0.0
    active_units: float = 0.0
    mature_units: float = 0.0
    effective_units: float = 0.0
    volume: float = 0.0
    asp: float = 0.0
    product_revenue: float = 0.0
    recurring_revenue: float = 0.0
    revenue: float = 0.0
    gross_profit: float = 0.0
    beneficiaries: float = 0.0
    cumulative_beneficiaries: float = 0.0


@dataclass
class RevenueModel:
    """The bottoms-up cohort build plus its sensitivity twin."""

    unit_label: str = "customers"
    volume_label: str = "units"
    years: list[RevenueYear] = field(default_factory=list)
    sensitivity_years: list[RevenueYear] = field(default_factory=list)
    sensitivity_driver: str = ""
    sensitivity_description: str = ""
    ramp: tuple[float, ...] = config.DEFAULT_RAMP
    mature_volume_per_unit: float = 0.0
    sensitivity_volume_per_unit: float = 0.0
    asp: float = 0.0
    gross_margin: float = 0.0

    def year(self, index: int) -> RevenueYear | None:
        return next((y for y in self.years if y.index == index), None)

    def sensitivity_year(self, index: int) -> RevenueYear | None:
        return next((y for y in self.sensitivity_years if y.index == index), None)


@dataclass
class ReachModel:
    """Market penetration measured against a base the analysis can defend."""

    addressable_units: float | None = None
    addressable_units_basis: str = ""
    addressable_population: float | None = None
    addressable_population_basis: str = ""
    bottom_up_market: float | None = None
    management_tam: float | None = None
    tam_reconciles: bool | None = None
    tam_note: str = ""
    penetration: dict[int, float] = field(default_factory=dict)
    population_reach: dict[int, float] = field(default_factory=dict)


@dataclass
class CustomerValue:
    """Step 6: what one account gets back for what it pays."""

    annual_value_created: float | None = None
    annual_spend: float | None = None
    ratio: float | None = None
    basis: str = ""
    computed: bool = False


# --- Research ---------------------------------------------------------------


@dataclass
class ResearchFinding:
    """One externally sourced fact. Without a URL it never enters the model."""

    query: str = ""
    fact: str = ""
    value: float | None = None
    unit: str = ""
    source_name: str = ""
    title: str = ""
    url: str = ""
    published: str = ""
    retrieved: str = ""
    confidence: str = config.MEDIUM


@dataclass
class Comparable:
    """A company that scaled in the closest comparable category."""

    name: str = ""
    description: str = ""
    start_year: int | None = None
    start_revenue: float | None = None
    end_year: int | None = None
    end_revenue: float | None = None
    years_elapsed: int | None = None
    source_name: str = ""
    url: str = ""
    published: str = ""
    note: str = ""

    @property
    def usable(self) -> bool:
        return bool(self.url) and self.start_revenue is not None and self.end_revenue is not None


@dataclass
class Transaction:
    """An acquisition with enough verified detail to carry a revenue multiple."""

    target: str = ""
    acquirer: str = ""
    transaction_date: str = ""
    price: float | None = None
    trailing_revenue: float | None = None
    multiple: float | None = None
    source_name: str = ""
    url: str = ""
    published: str = ""
    note: str = ""

    @property
    def usable(self) -> bool:
        return bool(self.url) and self.multiple is not None


# --- Valuation, financing, returns -----------------------------------------


@dataclass
class ValuationBand:
    """The revenue-multiple range applied to modelled revenue."""

    low: float = 0.0
    high: float = 0.0
    midpoint: float = 0.0
    basis: str = ""
    source_type: str = config.ASSUMPTION
    transaction_count: int = 0
    strategic_premium: float = 0.0


@dataclass
class ValuationPoint:
    """Enterprise value implied at one commercial year."""

    index: int
    label: str
    revenue: float = 0.0
    low: float = 0.0
    high: float = 0.0
    midpoint: float = 0.0
    strategic: float = 0.0


@dataclass
class ExitClaimTest:
    """Step 9: management's stated exit measured against the model."""

    stated: bool = False
    stated_value: float | None = None
    stated_year: int | None = None
    modelled_low: float | None = None
    modelled_high: float | None = None
    modelled_year: int | None = None
    verdict: str = ""
    explanation: str = ""


@dataclass
class FinancingRound:
    """One financing event the success case requires, tied to the milestone it funds."""

    name: str = ""
    year: int | None = None
    amount: float = 0.0
    milestone: str = ""
    source_type: str = config.ASSUMPTION
    dilution: float = 0.0
    option_pool: float = 0.0
    note: str = ""


@dataclass
class OwnershipStep:
    """Investor ownership after one event in the financing path."""

    event: str = ""
    ownership: float = 0.0
    note: str = ""


@dataclass
class OwnershipPath:
    """How today's cheque converts, then dilutes, through to exit."""

    instrument: str = ""
    investment: float = 0.0
    conversion_valuation: float | None = None
    conversion_basis: str = ""
    initial_ownership: float = 0.0
    steps: list[OwnershipStep] = field(default_factory=list)
    early_exit_ownership: float = 0.0
    scale_exit_ownership: float = 0.0
    notes: list[str] = field(default_factory=list)


@dataclass
class ReturnOutcome:
    """One exit scenario carried through to MOIC and IRR."""

    name: str = ""
    label: str = ""
    year_index: int = 0
    year_label: str = ""
    years_held: float = 0.0
    revenue: float = 0.0
    exit_value: float = 0.0
    ownership: float = 0.0
    proceeds: float = 0.0
    moic: float = 0.0
    irr: float | None = None


@dataclass
class Condition:
    """One of the 5–7 assumptions that drive most of the outcome."""

    condition: str = ""
    assumption: str = ""
    why_it_matters: str = ""
    if_it_fails: str = ""
    confidence: str = config.MEDIUM
    compact: str = ""


@dataclass
class ValidationIssue:
    """A validation finding. Flagged, never silently repaired."""

    severity: str = "WARNING"  # ERROR | WARNING | NOTE
    rule: str = ""
    message: str = ""


@dataclass
class Narrative:
    """The written layer, generated once the arithmetic is settled."""

    success_case: str = ""
    year_by_year: list[dict[str, str]] = field(default_factory=list)
    benchmark_verdict: str = ""
    benchmark_explanation: str = ""
    success_probability: str = ""
    success_statement: str = ""
    base_case: str = ""
    downside: str = ""
    headline_assumption: str = ""


# --- Top level --------------------------------------------------------------


@dataclass
class CompanyAnalysis:
    """The complete analysis: what the PDF renders and the JSON records."""

    company: str = "Unnamed Company"
    sector: str = ""
    product: str = ""
    geography: str = ""
    stage: str = ""
    business_model: str = ""
    round_summary: str = ""
    instrument: str = ""
    analysis_date: date = field(default_factory=date.today)
    deck_date: date | None = None

    facts: DeckFacts = field(default_factory=DeckFacts)
    shape: BusinessShape = field(default_factory=BusinessShape)
    timeline: Timeline = field(default_factory=Timeline)
    revenue: RevenueModel = field(default_factory=RevenueModel)
    reach: ReachModel = field(default_factory=ReachModel)
    customer_value: CustomerValue = field(default_factory=CustomerValue)
    comparables: list[Comparable] = field(default_factory=list)
    transactions: list[Transaction] = field(default_factory=list)
    findings: list[ResearchFinding] = field(default_factory=list)
    band: ValuationBand = field(default_factory=ValuationBand)
    valuations: list[ValuationPoint] = field(default_factory=list)
    exit_claim: ExitClaimTest = field(default_factory=ExitClaimTest)
    financing: list[FinancingRound] = field(default_factory=list)
    ownership: OwnershipPath = field(default_factory=OwnershipPath)
    returns: list[ReturnOutcome] = field(default_factory=list)
    conditions: list[Condition] = field(default_factory=list)
    issues: list[ValidationIssue] = field(default_factory=list)
    narrative: Narrative = field(default_factory=Narrative)
    ledger: Ledger = field(default_factory=Ledger)
    sources: list[dict[str, str]] = field(default_factory=list)

    def valuation(self, index: int) -> ValuationPoint | None:
        return next((v for v in self.valuations if v.index == index), None)

    def outcome(self, name: str) -> ReturnOutcome | None:
        return next((r for r in self.returns if r.name == name), None)

    def to_dict(self) -> dict[str, Any]:
        """Serialise the whole analysis, dates included, for the JSON audit trail."""
        return _plain(self)


def _plain(value: Any) -> Any:
    """Recursively convert dataclasses, dates and dict keys into JSON-safe values."""
    if is_dataclass(value) and not isinstance(value, type):
        return {key: _plain(item) for key, item in asdict(value).items()}
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if isinstance(value, date):
        return value.isoformat()
    return value
