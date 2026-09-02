"""Emails each completed upside case to the desk inbox via Resend.

Delivery is deliberately best-effort: a send that fails must never cost the
caller the analysis it just paid five model calls for. Every entry point returns
an outcome dict — `{"sent": bool, "id": str, "error": str, "skipped": str}` —
rather than raising, and the caller decides how loudly to surface it.

The body carries the investment answer, not a link to it: the headline numbers,
what has to be true, the interpretation and any analyst flags, so a partner can
read the conclusion on a phone and open the attached PDF only when they want the
page itself.
"""

from __future__ import annotations

import base64
import html
import os
from datetime import date
from typing import Any

from . import config, utils
from .models import CompanyAnalysis

DEFAULT_TO = "Info@tencapital.group"

# The Resend sandbox sender. It only delivers to the Resend account owner, so a
# real deploy sets RESEND_FROM to an address on a domain verified at
# resend.com/domains — otherwise mail to the desk inbox is accepted and dropped.
DEFAULT_FROM = "TEN Capital Upside Case <onboarding@resend.dev>"

_OFF = {"0", "false", "no", "off"}


def is_enabled() -> bool:
    """True when a Resend key is present and delivery has not been switched off."""
    if os.getenv("EMAIL_REPORTS", "1").strip().lower() in _OFF:
        return False
    return bool((os.getenv("RESEND_API_KEY") or "").strip())


def recipients() -> list[str]:
    """Addresses the analysis is delivered to, comma-separated in REPORT_EMAIL_TO."""
    raw = (os.getenv("REPORT_EMAIL_TO") or DEFAULT_TO).strip()
    return [address.strip() for address in raw.split(",") if address.strip()]


def sender() -> str:
    """The From address. Its domain must be verified in Resend."""
    return (os.getenv("RESEND_FROM") or DEFAULT_FROM).strip()


# --- Content ----------------------------------------------------------------


def _headline(analysis: CompanyAnalysis) -> list[tuple[str, str]]:
    """The six figures that answer the investment question."""
    scale = analysis.outcome("scale")
    final = analysis.revenue.years[-1] if analysis.revenue.years else None
    return [
        ("Year 5 revenue", utils.fmt_money(final.revenue) if final else "—"),
        ("Exit value", utils.fmt_money(scale.exit_value) if scale else "—"),
        ("Gross MOIC", f"{scale.moic:.1f}×" if scale else "—"),
        ("Ownership at exit", utils.fmt_pct(scale.ownership, 2) if scale else "—"),
        (
            "Multiple band",
            f"{utils.fmt_multiple(analysis.band.low)}–"
            f"{utils.fmt_multiple(analysis.band.high)} revenue",
        ),
        ("Success case", analysis.narrative.success_probability or "—"),
    ]


def _run_facts(analysis: CompanyAnalysis, meta: dict[str, Any]) -> list[tuple[str, str]]:
    """How the analysis was produced, for anyone auditing the run."""
    ledger = analysis.ledger
    facts = [
        ("Source deck", str(meta.get("source", "—"))),
        ("Slides read", str(meta.get("slides", "—"))),
        ("Unit of adoption", analysis.shape.adoption_unit or "—"),
        ("Commercial launch", str(analysis.timeline.launch_date or "—")),
        (
            "Provenance",
            f"{len(ledger.by_type(config.DECK))} deck · "
            f"{len(ledger.by_type(config.RESEARCH))} research · "
            f"{len(ledger.by_type(config.ASSUMPTION))} assumption · "
            f"{len(ledger.by_type(config.CALCULATED))} calculated",
        ),
        (
            "Evidence",
            f"{len(analysis.comparables)} comparables · "
            f"{analysis.band.transaction_count} verified transactions",
        ),
        ("Model", str(meta.get("model", config.LLM_MODEL))),
    ]
    if meta.get("seconds"):
        facts.append(("Run time", f"{meta['seconds']:.0f}s"))
    return facts


def _flags(analysis: CompanyAnalysis) -> list[Any]:
    """One finding per distinct rule, matching what the PDF's flag band shows."""
    seen: set[str] = set()
    flags = []
    for issue in analysis.issues:
        if issue.severity not in ("ERROR", "WARNING") or issue.rule in seen:
            continue
        seen.add(issue.rule)
        flags.append(issue)
    return flags


def build_subject(analysis: CompanyAnalysis, meta: dict[str, Any]) -> str:
    """Subject line: company, the headline return, and whether anything is flagged."""
    scale = analysis.outcome("scale")
    company = analysis.company or "Unnamed company"
    parts = [f"Upside case — {company}"]
    if scale and scale.moic:
        parts.append(f"{scale.moic:.1f}× scale exit")
    if analysis.narrative.success_probability:
        parts.append(analysis.narrative.success_probability)
    errors = sum(1 for issue in analysis.issues if issue.severity == "ERROR")
    if errors:
        parts.append(f"{errors} error{'s' if errors != 1 else ''}")
    return " · ".join(parts)


def build_text(analysis: CompanyAnalysis, meta: dict[str, Any]) -> str:
    """Plain-text alternative, for clients that will not render HTML."""
    lines = [
        build_subject(analysis, meta),
        "",
        " · ".join(
            part
            for part in (analysis.sector, analysis.stage, analysis.round_summary)
            if part
        ),
        "",
        "SUCCESS CASE",
        analysis.narrative.success_case or "—",
        "",
    ]
    lines += [f"{label}: {value}" for label, value in _headline(analysis)]
    lines.append("")

    lines.append("WHAT HAS TO BE TRUE")
    for condition in analysis.conditions:
        lines.append(f"- {condition.condition} — {condition.assumption}")
    lines.append("")

    lines.append("INVESTMENT INTERPRETATION")
    for label, text in (
        ("Success case", analysis.narrative.success_statement),
        ("Base case", analysis.narrative.base_case),
        ("Downside", analysis.narrative.downside),
    ):
        if text:
            lines.append(f"{label}: {text}")
    lines.append("")

    flags = _flags(analysis)
    lines.append("ANALYST FLAGS")
    lines += [f"[{issue.severity}] {issue.message}" for issue in flags] or ["None."]
    lines.append("")

    lines += [f"{label}: {value}" for label, value in _run_facts(analysis, meta)]
    lines += [
        "",
        "The one-page PDF and the full JSON audit trail are attached.",
        "This is a success case, not a forecast or a management projection.",
        f"{config.FIRM_NAME} · {config.CONFIDENTIALITY}",
    ]
    return "\n".join(lines)


def build_html(analysis: CompanyAnalysis, meta: dict[str, Any]) -> str:
    """Brand-styled HTML body carrying the conclusion, not just a link.

    Table-based and inline-styled on purpose: email clients strip <style>
    blocks, flexbox and grid.
    """
    esc = html.escape
    company = esc(analysis.company or "Unnamed company")
    tag = esc(
        " · ".join(
            part
            for part in (analysis.sector, analysis.stage, analysis.round_summary)
            if part
        )
    )

    # Headline figures, three to a row.
    headline = _headline(analysis)
    cells = []
    for index in range(0, len(headline), 3):
        row = "".join(
            f'<td width="33%" style="padding:0 10px 14px 0;vertical-align:top;">'
            f'<div style="font:600 10px/1.4 Helvetica,Arial,sans-serif;letter-spacing:.07em;'
            f'text-transform:uppercase;color:#8A8F98;">{esc(label)}</div>'
            f'<div style="font:700 17px/1.3 Helvetica,Arial,sans-serif;color:#0A2342;'
            f'margin-top:2px;">{esc(value)}</div></td>'
            for label, value in headline[index : index + 3]
        )
        cells.append(f"<tr>{row}</tr>")
    stats = "".join(cells)

    facts = "".join(
        f'<tr><td style="padding:2px 16px 2px 0;color:#7E90A8;font-size:12px;'
        f'white-space:nowrap;">{esc(label)}</td>'
        f'<td style="padding:2px 0;color:#1B1F27;font-size:12px;font-weight:600;">'
        f"{esc(value)}</td></tr>"
        for label, value in _run_facts(analysis, meta)
    )

    conditions = "".join(
        f'<li style="margin:0 0 5px;"><b>{esc(condition.condition)}</b>'
        + (f" — {esc(condition.assumption)}" if condition.assumption else "")
        + (
            ' <span style="color:#A5402A;">(low confidence)</span>'
            if condition.confidence == config.LOW
            else ""
        )
        + "</li>"
        for condition in analysis.conditions
    )

    interpretation = "".join(
        f'<div style="margin:0 0 7px;"><b style="color:#0A2342;">{esc(label)}:</b> '
        f'{esc(text)}</div>'
        for label, text in (
            ("Success case", analysis.narrative.success_statement),
            ("Base case", analysis.narrative.base_case),
            ("Downside", analysis.narrative.downside),
        )
        if text
    )

    flags = _flags(analysis)
    if flags:
        items = "".join(
            f'<li style="margin:0 0 4px;color:'
            f'{"#A5402A" if issue.severity == "ERROR" else "#7A5A1E"};">'
            f"<b>{esc(issue.severity)}</b> {esc(issue.message)}</li>"
            for issue in flags
        )
        flag_block = (
            '<div style="margin:16px 0 0;padding:11px 13px;border-radius:6px;'
            'background:#FDF3E3;border-left:3px solid #F3A22A;'
            'font:400 12px/1.5 Helvetica,Arial,sans-serif;">'
            '<div style="font-weight:700;color:#7A5A1E;margin-bottom:6px;">Analyst flags</div>'
            f'<ul style="margin:0;padding-left:16px;">{items}</ul></div>'
        )
    else:
        flag_block = (
            '<div style="margin:16px 0 0;padding:11px 13px;border-radius:6px;'
            'background:#EAF7F6;border-left:3px solid #35BEBB;color:#1D6360;'
            'font:400 12px/1.5 Helvetica,Arial,sans-serif;">'
            "Validation raised no errors or warnings.</div>"
        )

    def block(heading: str, body: str) -> str:
        return (
            '<tr><td style="padding:16px 0 0;">'
            f'<div style="font:600 11px/1.4 Helvetica,Arial,sans-serif;letter-spacing:.08em;'
            f'text-transform:uppercase;color:#0A2342;">{esc(heading)}</div>'
            '<div style="height:1px;background:#E3E8EF;margin:5px 0 8px;"></div>'
            f'<div style="font:400 13px/1.55 Helvetica,Arial,sans-serif;color:#1B1F27;">'
            f"{body}</div></td></tr>"
        )

    sections = block(
        "1. The success case",
        esc(analysis.narrative.success_case or "—"),
    )
    if conditions:
        sections += block(
            "What has to be true",
            f'<ul style="margin:0;padding-left:18px;">{conditions}</ul>',
        )
    if interpretation:
        sections += block("Investment interpretation", interpretation)

    return f"""\
<!doctype html>
<html><body style="margin:0;padding:24px 12px;background:#F2F4F7;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:640px;margin:0 auto;background:#FFFFFF;border:1px solid #E3E8EF;border-radius:10px;overflow:hidden;">
  <tr><td style="background:#0A2342;padding:22px 26px;">
    <div style="font:700 10px/1.4 Helvetica,Arial,sans-serif;letter-spacing:.13em;text-transform:uppercase;color:#8FA9C7;">{esc(config.FIRM_NAME)}</div>
    <div style="font:700 19px/1.3 Helvetica,Arial,sans-serif;color:#FFFFFF;margin-top:6px;">Upside case — {company}</div>
    <div style="font:400 12px/1.5 Helvetica,Arial,sans-serif;color:#B9C6D8;margin-top:5px;">{tag}</div>
    <div style="font:italic 400 11px/1.5 Helvetica,Arial,sans-serif;color:#8FA9C7;margin-top:7px;">{esc(config.DOC_SUBTITLE)}</div>
  </td></tr>
  <tr><td style="padding:22px 26px 0;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0">{stats}</table>
    {flag_block}
  </td></tr>
  <tr><td style="padding:0 26px;"><table role="presentation" width="100%" cellpadding="0" cellspacing="0">{sections}</table></td></tr>
  <tr><td style="padding:18px 26px 0;">
    <div style="font:600 11px/1.4 Helvetica,Arial,sans-serif;letter-spacing:.08em;text-transform:uppercase;color:#5A6472;">Run summary</div>
    <table role="presentation" cellpadding="0" cellspacing="0" style="margin-top:9px;font-family:Helvetica,Arial,sans-serif;">{facts}</table>
  </td></tr>
  <tr><td style="padding:20px 26px 24px;">
    <div style="height:1px;background:#E3E8EF;margin-bottom:14px;"></div>
    <div style="font:400 11px/1.6 Helvetica,Arial,sans-serif;color:#8A8F98;">
      The one-page PDF and the full JSON audit trail are attached.<br>
      This is a success case, not a forecast or a management projection.<br>
      {esc(config.FIRM_NAME)} · {esc(config.CONFIDENTIALITY)}
    </div>
  </td></tr>
</table>
</body></html>"""


# --- Delivery ---------------------------------------------------------------


def send_analysis(
    analysis: CompanyAnalysis,
    pdf_bytes: bytes,
    json_bytes: bytes | None = None,
    stem: str = "upside_case",
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Email the analysis with its PDF and audit trail. Never raises."""
    meta = dict(meta or {})
    meta.setdefault("model", config.LLM_MODEL)
    meta.setdefault("date", date.today().isoformat())

    if not is_enabled():
        reason = (
            "delivery disabled via EMAIL_REPORTS"
            if (os.getenv("RESEND_API_KEY") or "").strip()
            else "RESEND_API_KEY is not set"
        )
        return {"sent": False, "skipped": reason}

    to = recipients()
    if not to:
        return {"sent": False, "skipped": "REPORT_EMAIL_TO is empty"}

    attachments = [
        {
            "filename": f"{stem}.pdf",
            "content": base64.b64encode(pdf_bytes).decode("ascii"),
            "content_type": "application/pdf",
        }
    ]
    if json_bytes:
        attachments.append(
            {
                "filename": f"{stem}.json",
                "content": base64.b64encode(json_bytes).decode("ascii"),
                "content_type": "application/json",
            }
        )

    try:
        import resend

        resend.api_key = os.environ["RESEND_API_KEY"].strip()
        response = resend.Emails.send(
            {
                "from": sender(),
                "to": to,
                "subject": build_subject(analysis, meta),
                "html": build_html(analysis, meta),
                "text": build_text(analysis, meta),
                "attachments": attachments,
            }
        )
    except Exception as error:
        return {"sent": False, "error": f"{type(error).__name__}: {error}", "to": to}

    identifier = response.get("id") if isinstance(response, dict) else getattr(response, "id", "")
    return {"sent": True, "id": identifier or "", "to": to}
