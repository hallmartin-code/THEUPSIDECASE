"""Format dispatch and transcript assembly for a pitch deck.

`read_deck` is the only entry point the pipeline uses. It hands the file to the
right parser and flattens the result into a slide-labelled transcript — the form
the extraction prompt reads, and the reason a fact can be cited to a slide.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

from . import pdf_parser, pptx_parser, utils

PDF = "pdf"
PPTX = "pptx"

_PARSERS = {PDF: pdf_parser.parse, PPTX: pptx_parser.parse}


def detect_kind(path: str | Path) -> str:
    """Return "pdf" or "pptx" from the file extension, or raise."""
    suffix = Path(path).suffix.lower()
    kind = {".pdf": PDF, ".pptx": PPTX}.get(suffix)
    if kind is None:
        raise ValueError(f"Unsupported file type {suffix!r}: a pitch deck must be .pdf or .pptx")
    return kind


def read_deck(path: str | Path) -> dict[str, Any]:
    """Read a deck into {source, kind, slide_count, slides, tables, full_text, deck_date}.

    `deck_date` prefers a date printed on the slides over file metadata: a deck
    that says "May 2026" on its cover is telling you its own as-of date, while
    metadata often reflects a later re-save.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"No deck at {path}")

    kind = detect_kind(path)
    parsed = _PARSERS[kind](path)
    slides = parsed["slides"]
    if not any(slide["text"] or slide["tables"] for slide in slides):
        raise ValueError(
            f"{path.name} yielded no extractable text. If the deck is a scan or "
            "image-only export, re-export it with selectable text."
        )

    printed = _printed_date(slides)
    return {
        "source": path.name,
        "path": str(path),
        "kind": kind,
        "slide_count": len(slides),
        "slides": slides,
        "tables": [
            {"slide": slide["index"], "rows": table}
            for slide in slides
            for table in slide["tables"]
        ],
        "full_text": render_transcript(slides),
        "deck_date": printed or parsed.get("doc_date"),
        "deck_date_basis": "printed on the deck" if printed else "file metadata",
    }


def render_transcript(slides: list[dict[str, Any]]) -> str:
    """Flatten slides into a slide-labelled transcript."""
    chunks: list[str] = []
    for slide in slides:
        header = f"--- Slide {slide['index']}"
        if slide["title"]:
            header += f": {slide['title']}"
        header += " ---"
        parts = [header]
        body = (slide["text"] or "").strip()
        if body:
            parts.append(body)
        for table in slide["tables"]:
            parts.append("[table]\n" + _table_to_text(table))
        chunks.append("\n".join(parts))
    return "\n\n".join(chunks)


def _table_to_text(rows: list[list[str]]) -> str:
    """Pipe-delimited rows, which models read more reliably than aligned columns."""
    return "\n".join(" | ".join(cell or "" for cell in row) for row in rows)


def _printed_date(slides: list[dict[str, Any]]) -> date | None:
    """Look for an as-of date on the cover or closing slides.

    Only the first two and last two slides are considered. A year appearing on a
    market or timeline slide is a projection, not the date the deck was written.
    """
    candidates = slides[:2] + slides[-2:]
    for slide in candidates:
        for line in (slide.get("text") or "").splitlines()[:12]:
            line = line.strip()
            if not line or len(line) > 60:
                continue
            parsed = utils.parse_date(line)
            if parsed and 2015 <= parsed.year <= date.today().year + 1:
                return parsed
    return None
