"""Flask front end: upload a pitch deck, get the one-page upside case back.

Wraps the same pipeline the CLI uses. Run locally with `python web/server.py`;
in production gunicorn serves `web.server:app` from the repository root.

The one structural difference from the CLI is that a full run takes minutes —
five model calls, two of them web searches — which is longer than a browser or
an edge proxy will hold a request open. So an upload starts a background job and
returns immediately, the browser polls for the nine stages as they complete, and
the PDF and JSON are fetched from the finished job.

That makes the job registry process-local state: run gunicorn with ONE worker
and several threads, or a poll will land on a worker that has never heard of the
job. The Procfile and railway.json are configured that way.
"""

from __future__ import annotations

import hmac
import io
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from flask import (  # noqa: E402
    Flask,
    jsonify,
    render_template,
    request,
    send_file,
    send_from_directory,
)
from werkzeug.exceptions import RequestEntityTooLarge  # noqa: E402
from werkzeug.utils import secure_filename  # noqa: E402

# Importing src.config loads ROOT/.env, so the os.getenv calls below see it.
import app as pipeline  # noqa: E402
from src import config, deck_parser, llm, mailer, pdf_generator, utils  # noqa: E402

DEFAULT_PORT = "8020"
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "40"))
JOB_TTL_SECONDS = int(os.getenv("JOB_TTL_MINUTES", "60")) * 60

# Each concurrent run costs five model calls including two web searches. A small
# ceiling keeps a shared deploy from being drained by one impatient afternoon.
MAX_CONCURRENT_JOBS = int(os.getenv("MAX_CONCURRENT_JOBS", "2"))

flask_app = Flask(__name__)
flask_app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024

_jobs: dict[str, dict[str, Any]] = {}
_lock = threading.Lock()


# --- Access control ---------------------------------------------------------


def _password_required() -> bool:
    """True when APP_PASSWORD is set, which gates a publicly reachable deploy."""
    return bool(os.getenv("APP_PASSWORD"))


def _password_ok(supplied: str) -> bool:
    """Constant-time comparison against APP_PASSWORD."""
    expected = os.getenv("APP_PASSWORD", "")
    if not expected:
        return True
    return hmac.compare_digest(supplied or "", expected)


def _key_configured() -> bool:
    return bool((os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN") or "").strip())


# --- Job registry -----------------------------------------------------------


def _slugify(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", name.lower()).strip("-") or "company"


def _reap() -> None:
    """Drop finished jobs past their TTL, so the registry cannot grow unbounded."""
    cutoff = time.time() - JOB_TTL_SECONDS
    with _lock:
        for job_id in [
            job_id
            for job_id, job in _jobs.items()
            if job["status"] in ("done", "error") and job["finished_at"] < cutoff
        ]:
            _jobs.pop(job_id, None)


def _running_count() -> int:
    with _lock:
        return sum(1 for job in _jobs.values() if job["status"] in ("queued", "running"))


def _new_job(filename: str) -> dict[str, Any]:
    job = {
        "id": uuid.uuid4().hex[:12],
        "status": "queued",
        "step": 0,
        "total": pipeline.TOTAL_STEPS,
        "step_label": "Queued",
        "log": [],
        "source": filename,
        "company": "",
        "error": "",
        "summary": {},
        "pdf": None,
        "json": None,
        "stem": "",
        "created_at": time.time(),
        "finished_at": 0.0,
    }
    with _lock:
        _jobs[job["id"]] = job
    return job


def _sink(job: dict[str, Any]):
    """Return a Progress sink that records stages and detail lines on `job`."""

    def record(step: int, kind: str, text: str) -> None:
        with _lock:
            job["step"] = step
            if kind == "step":
                job["step_label"] = text
            job["log"].append(
                {
                    "step": step,
                    "kind": kind,
                    "text": text,
                    "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                }
            )

    return record


def _public(job: dict[str, Any]) -> dict[str, Any]:
    """The view of a job the browser is allowed to see — no file bytes."""
    return {
        key: job[key]
        for key in (
            "id",
            "status",
            "step",
            "total",
            "step_label",
            "log",
            "source",
            "company",
            "error",
            "summary",
        )
    }


# --- The worker -------------------------------------------------------------


def _work(job_id: str, deck_path: Path, workspace: Path, options: dict[str, Any]) -> None:
    """Run the pipeline for one job, then hold the artefacts in memory for pickup."""
    job = _jobs[job_id]
    started = time.time()
    with _lock:
        job["status"] = "running"

    try:
        analysis = pipeline.run(
            deck_path,
            investment=options["investment"],
            use_research=options["use_research"],
            progress=pipeline.Progress(sink=_sink(job), echo=False),
        )

        pipeline.Progress(sink=_sink(job), echo=False).step(
            pipeline.TOTAL_STEPS, pipeline.STEP_LABELS[-1]
        )
        stem = f"{_slugify(analysis.company)}_upside_case"
        pdf_path = workspace / f"{stem}.pdf"
        result = pdf_generator.render(analysis, pdf_path)

        pdf_bytes = pdf_path.read_bytes()
        sidecar = json.dumps(analysis.to_dict(), indent=2, default=str).encode("utf-8")

        # Best-effort delivery, reported on the page rather than raised: a Resend
        # failure must not cost the analyst the run they just waited on.
        delivery = {"sent": False, "skipped": "email switched off for this run"}
        if options.get("email", True):
            delivery = mailer.send_analysis(
                analysis,
                pdf_bytes,
                sidecar,
                stem=stem,
                meta={
                    "source": job["source"],
                    "slides": analysis.facts.slide_count,
                    "pages": result["pages"],
                    "seconds": time.time() - started,
                },
            )
            if delivery.get("error"):
                flask_app.logger.error("Email failed for %s: %s", job_id, delivery["error"])

        summary = _summary(analysis, result, time.time() - started)
        summary["delivery"] = delivery

        with _lock:
            job["pdf"] = pdf_bytes
            job["json"] = sidecar
            job["stem"] = stem
            job["company"] = analysis.company
            job["summary"] = summary
            job["status"] = "done"
            job["finished_at"] = time.time()

    except llm.CredentialsError as error:
        _fail(job, str(error))
    except (FileNotFoundError, ValueError) as error:
        _fail(job, str(error))
    except Exception as error:  # pragma: no cover - surfaced, not swallowed
        flask_app.logger.error("Job %s failed: %s", job_id, traceback.format_exc())
        _fail(job, f"{type(error).__name__}: {error}")
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


def _fail(job: dict[str, Any], message: str) -> None:
    with _lock:
        job["status"] = "error"
        job["error"] = message
        job["finished_at"] = time.time()


def _summary(analysis: Any, result: dict[str, Any], seconds: float) -> dict[str, Any]:
    """The headline numbers the page shows once a run finishes."""
    ledger = analysis.ledger
    scale = analysis.outcome("scale")
    final = analysis.revenue.years[-1] if analysis.revenue.years else None
    errors = sum(1 for issue in analysis.issues if issue.severity == "ERROR")

    return {
        "company": analysis.company,
        "sector": analysis.sector,
        "stage": analysis.stage,
        "round": analysis.round_summary,
        "adoption_unit": analysis.shape.adoption_unit,
        "launch": str(analysis.timeline.launch_date),
        "cy5_revenue": utils.fmt_money(final.revenue) if final else "—",
        "exit_value": utils.fmt_money(scale.exit_value) if scale else "—",
        "moic": f"{scale.moic:.1f}×" if scale else "—",
        "ownership": utils.fmt_pct(scale.ownership, 2) if scale else "—",
        "probability": analysis.narrative.success_probability,
        "multiple_band": (
            f"{utils.fmt_multiple(analysis.band.low)}–{utils.fmt_multiple(analysis.band.high)}"
        ),
        "transactions_verified": analysis.band.transaction_count,
        "comparables": len(analysis.comparables),
        "provenance": {
            "deck": len(ledger.by_type(config.DECK)),
            "research": len(ledger.by_type(config.RESEARCH)),
            "assumption": len(ledger.by_type(config.ASSUMPTION)),
            "calculated": len(ledger.by_type(config.CALCULATED)),
        },
        "findings": len(analysis.issues),
        "errors": errors,
        "flags": [
            {"severity": issue.severity, "message": issue.message}
            for issue in analysis.issues
            if issue.severity in ("ERROR", "WARNING")
        ][:6],
        "pages": result["pages"],
        "seconds": round(seconds),
    }


# --- Routes -----------------------------------------------------------------


@flask_app.get("/")
def index():
    return render_template(
        "index.html",
        accept=",".join(f".{kind}" for kind in (deck_parser.PDF, deck_parser.PPTX)),
        max_mb=MAX_UPLOAD_MB,
        model=config.LLM_MODEL,
        needs_password=_password_required(),
        firm=config.FIRM_NAME,
        subtitle=config.DOC_SUBTITLE,
        default_investment=int(pipeline.DEFAULT_INVESTMENT),
        steps=pipeline.STEP_LABELS,
        email_enabled=mailer.is_enabled(),
        email_to=", ".join(mailer.recipients()) if mailer.is_enabled() else "",
    )


@flask_app.get("/favicon.ico")
def favicon():
    """Serve the icon at the root path browsers and crawlers probe by default."""
    return send_from_directory(
        flask_app.static_folder, "favicon.ico", mimetype="image/vnd.microsoft.icon"
    )


@flask_app.get("/healthz")
def healthz():
    """Readiness probe. Spends no tokens unless ?deep=1 is passed."""
    payload = {
        "ok": _key_configured(),
        "api_key_configured": _key_configured(),
        "model": config.LLM_MODEL,
        "effort": config.LLM_EFFORT,
        "research_enabled": config.RESEARCH_ENABLED,
        "password_protected": _password_required(),
        "jobs_running": _running_count(),
        "max_upload_mb": MAX_UPLOAD_MB,
        "email": {
            "enabled": mailer.is_enabled(),
            "to": mailer.recipients() if mailer.is_enabled() else [],
            "from": mailer.sender() if mailer.is_enabled() else "",
        },
    }
    if request.args.get("deep"):
        try:
            info = llm.build_client().models.retrieve(config.LLM_MODEL)
            payload["api"] = {"model": info.id, "display_name": getattr(info, "display_name", "")}
        except Exception as error:  # surfaced, not raised: this is a probe
            payload["ok"] = False
            payload["api_error"] = f"{type(error).__name__}: {error}"
    return jsonify(payload)


@flask_app.post("/analyze")
def analyze():
    """Accept a deck, start a background run, and return the job id."""
    _reap()

    if not _password_ok(request.form.get("password", "")):
        return jsonify(error="Incorrect password."), 401

    if not _key_configured():
        return jsonify(
            error="ANTHROPIC_API_KEY is not configured on the server. Add it in the Railway "
            "service variables (or your local .env) and redeploy."
        ), 503

    if _running_count() >= MAX_CONCURRENT_JOBS:
        return jsonify(
            error=f"{MAX_CONCURRENT_JOBS} analyses are already running. Each takes a few "
            "minutes; try again when one finishes."
        ), 429

    upload = request.files.get("deck")
    if upload is None or not upload.filename:
        return jsonify(error="No pitch deck was uploaded."), 400

    filename = secure_filename(upload.filename)
    suffix = Path(filename).suffix.lower()
    if suffix not in (".pdf", ".pptx"):
        return jsonify(error=f"Unsupported deck type '{suffix}'. Upload a .pdf or .pptx."), 400

    investment = _positive(request.form.get("investment"), pipeline.DEFAULT_INVESTMENT)
    use_research = request.form.get("research", "1") not in ("0", "false", "off")
    email = request.form.get("email", "1") not in ("0", "false", "off")

    workspace = Path(tempfile.mkdtemp(prefix="upside-"))
    deck_path = workspace / f"pitchdeck{suffix}"
    upload.save(deck_path)

    job = _new_job(filename)
    threading.Thread(
        target=_work,
        args=(
            job["id"],
            deck_path,
            workspace,
            {"investment": investment, "use_research": use_research, "email": email},
        ),
        daemon=True,
        name=f"upside-{job['id']}",
    ).start()

    return jsonify(_public(job)), 202


@flask_app.get("/jobs/<job_id>")
def job_status(job_id: str):
    job = _jobs.get(job_id)
    if job is None:
        return jsonify(error="No such job. It may have expired."), 404
    return jsonify(_public(job))


@flask_app.get("/jobs/<job_id>/pdf")
def job_pdf(job_id: str):
    job, error = _finished(job_id)
    if error:
        return error
    return send_file(
        io.BytesIO(job["pdf"]),
        mimetype="application/pdf",
        as_attachment=request.args.get("download") == "1",
        download_name=f"{job['stem']}.pdf",
    )


@flask_app.get("/jobs/<job_id>/json")
def job_json(job_id: str):
    job, error = _finished(job_id)
    if error:
        return error
    return send_file(
        io.BytesIO(job["json"]),
        mimetype="application/json",
        as_attachment=True,
        download_name=f"{job['stem']}.json",
    )


def _finished(job_id: str):
    """Return (job, None) when a job's artefacts are ready, else (None, response)."""
    job = _jobs.get(job_id)
    if job is None:
        return None, (jsonify(error="No such job. It may have expired."), 404)
    if job["status"] == "error":
        return None, (jsonify(error=job["error"]), 500)
    if job["status"] != "done":
        return None, (jsonify(error="Still running."), 409)
    return job, None


def _positive(raw: Any, fallback: float) -> float:
    value = utils.parse_money(raw)
    return value if value and value > 0 else fallback


@flask_app.errorhandler(RequestEntityTooLarge)
def too_large(_error):
    """Return JSON rather than Werkzeug's HTML page, so the UI can show it."""
    return jsonify(error=f"That file is larger than the {MAX_UPLOAD_MB} MB limit."), 413


# gunicorn serves `web.server:app`; the module-level name below is that entry point.
app = flask_app


if __name__ == "__main__":
    flask_app.run(host="0.0.0.0", port=int(os.getenv("PORT", DEFAULT_PORT)), debug=False)
