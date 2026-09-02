"""Steps 7 and 8 — external research, kept separate from deck extraction.

Two rules govern this module.

Nothing enters the model without a URL. A remembered revenue figure or a
half-recalled acquisition price is worse than no comparable at all, so a finding
that arrives without a retrievable source is dropped.

And no multiple is calculated unless both halves are present. The model reports
what a filing said; Python divides. A transaction with a price but no revenue
stays in the record as context and contributes nothing to the valuation band.

The provider interface exists so the search backend can be swapped without the
analytical logic moving. Results are cached on disk, keyed by the query.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path
from typing import Any, Protocol

from . import config, llm, utils
from .models import Comparable, ResearchFinding, Transaction

# --- Providers --------------------------------------------------------------


class Provider(Protocol):
    """A research backend: given a question, return prose and the sources behind it."""

    name: str

    def run(self, system: str, prompt: str) -> dict[str, Any]: ...


class AnthropicWebSearchProvider:
    """Research through the server-side web-search tool."""

    name = "anthropic-web-search"

    def __init__(self, client: Any | None = None, max_uses: int = 8) -> None:
        self._client = client
        self._max_uses = max_uses

    def run(self, system: str, prompt: str) -> dict[str, Any]:
        return llm.search(system, prompt, client=self._client, max_uses=self._max_uses)


class NullProvider:
    """Used when research is switched off. The analysis still runs, and says so."""

    name = "disabled"

    def run(self, system: str, prompt: str) -> dict[str, Any]:
        return {"text": "", "sources": []}


# --- Cache ------------------------------------------------------------------


class Cache:
    """A flat JSON cache of research results, keyed by a hash of the query."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._data: dict[str, Any] = {}
        if path.exists():
            try:
                self._data = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                self._data = {}

    @staticmethod
    def key(task: str, subject: str) -> str:
        digest = hashlib.sha256(f"{task}|{subject}".encode("utf-8")).hexdigest()[:20]
        return f"{task}:{digest}"

    def get(self, key: str) -> dict[str, Any] | None:
        return self._data.get(key)

    def put(self, key: str, value: dict[str, Any]) -> None:
        self._data[key] = value
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self._data, indent=2), encoding="utf-8")
        except OSError:
            pass  # a cache that cannot be written should not fail a run


# --- Prompts ----------------------------------------------------------------

SYSTEM_PROMPT = (
    "You are a research analyst at TEN Capital. You retrieve verified financial facts from "
    "primary sources — SEC filings, annual reports, investor presentations, acquisition "
    "announcements, and credible financial press — and you report only what you actually "
    "found. You never supply a figure from memory, and you never estimate a number that a "
    "source did not state. Saying 'no credible comparable exists' is a correct answer."
)

_COMPARABLE_PROMPT = """
Find 2–3 real companies in the closest comparable category to the one described below that
successfully scaled commercially, and retrieve their actual year-by-year revenue during the
scale-up period.

{context}

What to retrieve for each company:
- the company name and one line on why it is comparable
- revenue in an early commercial year, with the year
- revenue in a later year, with the year
- the source: organisation, document title, date, and URL

Requirements:
- Search for and open primary sources. Prefer SEC filings (S-1, 10-K), annual reports, and
  acquisition filings over secondary coverage.
- Report only revenue figures you actually saw in a source you retrieved. If you cannot verify
  a company's revenue, leave that company out.
- Comparability is about the adoption unit and the sales motion, not the sector label: a company
  selling capital equipment into hospitals is comparable to another doing the same, whatever the
  clinical area.
- If no credible comparable exists, say so plainly rather than forcing one.
""".strip()

_TRANSACTION_PROMPT = """
Find recent acquisitions — preferably within the last 5–7 years — that would price a company
like the one described below.

{context}

What to retrieve for each transaction:
- target, acquirer, and announcement date
- the transaction value
- the target's trailing revenue at the time of the transaction
- the source: organisation, document title, date, and URL

Requirements:
- Search for and open primary sources: the acquisition announcement, the acquirer's filings,
  or the target's last public financials.
- Report the transaction value and trailing revenue only where a source actually states them.
  Do NOT calculate or estimate a revenue multiple — report the two figures and leave the
  arithmetic alone.
- A transaction whose revenue is not disclosed is still worth reporting; say that revenue was
  not disclosed.
- If public-company trading multiples are the better evidence for this category, report those
  instead, with the same sourcing discipline.
""".strip()


# --- Extraction schemas -----------------------------------------------------


def _comparable_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "comparables": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "description": {"type": "string", "description": "Why comparable."},
                        "start_year": {"type": "integer"},
                        "start_revenue": {"type": "number", "description": "Plain dollars."},
                        "end_year": {"type": "integer"},
                        "end_revenue": {"type": "number", "description": "Plain dollars."},
                        "source_name": {"type": "string"},
                        "url": {"type": "string"},
                        "published": {"type": "string"},
                        "note": {"type": "string"},
                    },
                    "required": [
                        "name", "description", "start_year", "start_revenue", "end_year",
                        "end_revenue", "source_name", "url", "published", "note",
                    ],
                    "additionalProperties": False,
                },
            },
            "verdict": {
                "type": "string",
                "description": "One sentence on the quality of the comparable set.",
            },
        },
        "required": ["comparables", "verdict"],
        "additionalProperties": False,
    }


def _transaction_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "transactions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "target": {"type": "string"},
                        "acquirer": {"type": "string"},
                        "transaction_date": {"type": "string"},
                        "price": {
                            "type": "number",
                            "description": "Transaction value in plain dollars; 0 if undisclosed.",
                        },
                        "trailing_revenue": {
                            "type": "number",
                            "description": "Trailing revenue in plain dollars; 0 if undisclosed.",
                        },
                        "source_name": {"type": "string"},
                        "url": {"type": "string"},
                        "published": {"type": "string"},
                        "note": {"type": "string"},
                    },
                    "required": [
                        "target", "acquirer", "transaction_date", "price", "trailing_revenue",
                        "source_name", "url", "published", "note",
                    ],
                    "additionalProperties": False,
                },
            },
            "verdict": {"type": "string"},
        },
        "required": ["transactions", "verdict"],
        "additionalProperties": False,
    }


# --- The research layer -----------------------------------------------------


class ResearchLayer:
    """Runs the two research tasks the analysis needs and normalises their output."""

    def __init__(
        self,
        provider: Provider | None = None,
        cache_path: Path | None = None,
        client: Any | None = None,
    ) -> None:
        self.provider: Provider = provider or (
            AnthropicWebSearchProvider(client) if config.RESEARCH_ENABLED else NullProvider()
        )
        self.cache = Cache(cache_path or config.RESEARCH_CACHE)
        self._client = client
        self.findings: list[ResearchFinding] = []

    @property
    def enabled(self) -> bool:
        return not isinstance(self.provider, NullProvider)

    def comparables(self, context: str, subject: str) -> tuple[list[Comparable], str]:
        """Step 7: verified revenue ramps from companies that scaled in this category."""
        parsed = self._task(
            "comparables",
            subject,
            _COMPARABLE_PROMPT.format(context=context),
            _comparable_schema(),
        )
        found: list[Comparable] = []
        for raw in parsed.get("comparables") or []:
            comparable = Comparable(
                name=str(raw.get("name") or "").strip(),
                description=str(raw.get("description") or "").strip(),
                start_year=_year(raw.get("start_year")),
                start_revenue=_positive(raw.get("start_revenue")),
                end_year=_year(raw.get("end_year")),
                end_revenue=_positive(raw.get("end_revenue")),
                source_name=str(raw.get("source_name") or "").strip(),
                url=_url(raw.get("url")),
                published=str(raw.get("published") or "").strip(),
                note=str(raw.get("note") or "").strip(),
            )
            if comparable.start_year and comparable.end_year:
                comparable.years_elapsed = comparable.end_year - comparable.start_year
            if comparable.usable and comparable.name:
                found.append(comparable)
                self._note(comparable)
        return found, str(parsed.get("verdict") or "").strip()

    def transactions(self, context: str, subject: str) -> tuple[list[Transaction], str]:
        """Step 8: acquisitions with enough verified detail to carry a multiple."""
        parsed = self._task(
            "transactions",
            subject,
            _TRANSACTION_PROMPT.format(context=context),
            _transaction_schema(),
        )
        found: list[Transaction] = []
        for raw in parsed.get("transactions") or []:
            transaction = Transaction(
                target=str(raw.get("target") or "").strip(),
                acquirer=str(raw.get("acquirer") or "").strip(),
                transaction_date=str(raw.get("transaction_date") or "").strip(),
                price=_positive(raw.get("price")),
                trailing_revenue=_positive(raw.get("trailing_revenue")),
                source_name=str(raw.get("source_name") or "").strip(),
                url=_url(raw.get("url")),
                published=str(raw.get("published") or "").strip(),
                note=str(raw.get("note") or "").strip(),
            )
            # The multiple is calculated here, from two verified figures, or not at all.
            transaction.multiple = utils.safe_div(transaction.price, transaction.trailing_revenue)
            if transaction.target and transaction.url:
                found.append(transaction)
                self._note(transaction)
        return found, str(parsed.get("verdict") or "").strip()

    def record(
        self, ledger: Any, comparables: list[Comparable], transactions: list[Transaction]
    ) -> None:
        """Write each retrieved figure into the ledger as a RESEARCH entry.

        The audit trail should show what research contributed, including the
        transactions that were retrieved but could not carry a multiple — that
        absence is itself a finding about the evidence available.
        """
        for item in comparables:
            ledger.record(
                f"comparable_{_slug(item.name)}_revenue",
                [item.start_revenue, item.end_revenue],
                config.RESEARCH,
                item.url,
                config.MEDIUM,
                notes=f"{item.source_name}: {item.start_year} to {item.end_year}",
                unit="$",
            )
        for item in transactions:
            ledger.record(
                f"transaction_{_slug(item.target)}",
                {
                    "price": item.price,
                    "trailing_revenue": item.trailing_revenue,
                    "multiple": round(item.multiple, 2) if item.multiple else None,
                },
                config.RESEARCH,
                item.url,
                config.MEDIUM if item.usable else config.LOW,
                notes=(
                    f"{item.acquirer}/{item.target} {item.transaction_date}"
                    + ("" if item.usable else " — revenue undisclosed, no multiple derived")
                ),
            )

    # --- internals ---

    def _task(
        self, task: str, subject: str, prompt: str, schema: dict[str, Any]
    ) -> dict[str, Any]:
        """Run one research task: cache, then search, then structure the answer."""
        if not self.enabled:
            return {}

        key = Cache.key(task, subject)
        cached = self.cache.get(key)
        if cached is not None:
            return cached.get("parsed", {})

        try:
            result = self.provider.run(SYSTEM_PROMPT, prompt)
        except Exception as error:  # research must never abort the analysis
            self.findings.append(
                ResearchFinding(
                    query=task,
                    fact=f"Research unavailable: {error}",
                    retrieved=date.today().isoformat(),
                    confidence=config.LOW,
                )
            )
            return {}

        parsed = self._structure(result, schema)
        self.cache.put(
            key,
            {
                "query": task,
                "subject": subject,
                "provider": self.provider.name,
                "retrieved": date.today().isoformat(),
                "sources": result.get("sources", []),
                "text": result.get("text", ""),
                "parsed": parsed,
            },
        )
        return parsed

    def _structure(self, result: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
        """Turn the research prose into the schema, attributing each fact to a URL."""
        text = (result.get("text") or "").strip()
        if not text:
            return {}
        sources = "\n".join(
            f"- {source.get('title') or 'untitled'} — {source['url']}"
            for source in result.get("sources", [])
        )
        prompt = "\n".join(
            [
                "Convert the research notes below into the response schema.",
                "",
                "Rules:",
                "- Every entry must carry the URL of the source the figure came from. Use one of "
                "the URLs listed under SOURCES; if a figure has no source in that list, omit the "
                "entry entirely.",
                "- Report figures in plain dollars: 47300000, not '$47.3M'.",
                "- Use 0 for a figure the notes describe as undisclosed. Do not estimate it.",
                "- Do not calculate ratios or multiples. Report only the figures stated.",
                "",
                "=== RESEARCH NOTES ===",
                text,
                "",
                "=== SOURCES ===",
                sources or "(none recorded)",
            ]
        )
        try:
            return llm.complete_json(
                SYSTEM_PROMPT, prompt, schema, client=self._client, max_tokens=8000
            )
        except (ValueError, RuntimeError):
            return {}

    def _note(self, item: Comparable | Transaction) -> None:
        """Record a usable result in the finding log that backs the PDF's footnotes."""
        if isinstance(item, Comparable):
            fact = (
                f"{item.name}: {utils.fmt_money(item.start_revenue)} ({item.start_year}) → "
                f"{utils.fmt_money(item.end_revenue)} ({item.end_year})"
            )
        else:
            fact = (
                f"{item.acquirer} acquired {item.target} for {utils.fmt_money(item.price)}"
                + (
                    f" on {utils.fmt_money(item.trailing_revenue)} revenue"
                    if item.trailing_revenue
                    else " (revenue undisclosed)"
                )
            )
        self.findings.append(
            ResearchFinding(
                query=type(item).__name__.lower(),
                fact=fact,
                source_name=item.source_name,
                title=getattr(item, "note", "") or item.source_name,
                url=item.url,
                published=item.published,
                retrieved=date.today().isoformat(),
                confidence=config.MEDIUM,
            )
        )


# --- Normalisation helpers --------------------------------------------------


def _positive(raw: Any) -> float | None:
    value = utils.parse_number(raw)
    return value if value and value > 0 else None


def _year(raw: Any) -> int | None:
    value = utils.parse_number(raw)
    if value is None:
        return None
    year = int(value)
    return year if 1980 <= year <= 2100 else None


def _slug(name: str) -> str:
    """A ledger-safe key from a company name."""
    cleaned = "".join(character if character.isalnum() else "_" for character in name.lower())
    return "_".join(part for part in cleaned.split("_") if part)[:40] or "unnamed"


def _url(raw: Any) -> str:
    url = str(raw or "").strip()
    return url if url.startswith("http") else ""
