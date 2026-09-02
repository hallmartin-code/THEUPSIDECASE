"""PDF pitch-deck extraction, page by page.

A PDF deck is one slide per page, so pages become slides with a 1-based index —
that is what lets the analysis cite "slide 12" the way a reader would.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Any

_PDF_DATE = re.compile(r"D:(\d{4})(\d{2})(\d{2})")


def parse(path: str | Path) -> dict[str, Any]:
    """Return {"slides", "doc_date"} for a PDF deck.

    Each slide is {"index", "title", "text", "tables"}. Table extraction failures
    on a single page are swallowed: a malformed table should not abort a deck.
    """
    import pdfplumber

    path = Path(path)
    slides: list[dict[str, Any]] = []
    doc_date: date | None = None

    with pdfplumber.open(str(path)) as pdf:
        doc_date = _metadata_date(pdf.metadata or {})
        for number, page in enumerate(pdf.pages, start=1):
            text = page.extract_text() or ""
            try:
                raw_tables = page.extract_tables() or []
            except Exception:
                raw_tables = []
            tables = [cleaned for cleaned in (_clean_table(t) for t in raw_tables) if cleaned]
            slides.append(
                {
                    "index": number,
                    "title": _first_line(text),
                    "text": text.strip(),
                    "tables": tables,
                }
            )
    return {"slides": slides, "doc_date": doc_date}


def _metadata_date(metadata: dict[str, Any]) -> date | None:
    """Read the authored date from PDF metadata, preferring creation over modification.

    A deck's creation date is the most reliable "as of" signal available when the
    slides themselves carry no date — and step 1 needs one to fix the clock.
    """
    for key in ("CreationDate", "ModDate"):
        raw = str(metadata.get(key) or "")
        match = _PDF_DATE.search(raw)
        if match:
            try:
                return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
            except ValueError:
                continue
    return None


def _first_line(text: str) -> str:
    """The first non-empty line, used as the slide title."""
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped:
            return stripped[:120]
    return ""


def _clean_table(rows: list[list[Any]]) -> list[list[str]]:
    """Normalise cells to stripped strings and drop rows that hold no content."""
    cleaned: list[list[str]] = []
    for row in rows or []:
        cells = [(str(cell).strip() if cell is not None else "") for cell in row]
        if any(cells):
            cleaned.append(cells)
    return cleaned
