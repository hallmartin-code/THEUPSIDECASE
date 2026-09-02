# TEN Capital — Upside Case Analyzer

Takes an investor pitch deck (`.pdf` or `.pptx`) and answers one question:

> **If this company executes the plan in the deck, what could the business realistically look like three to five years from today — and what would today's investor make?**

The output is a one-page, TEN Capital-branded PDF built to function as an investment
decision document, plus a JSON sidecar holding every input, assumption, source and
calculation behind it.

This is a **success case**, not a forecast and not a management projection. The page
says so.

---

## Install and run

```powershell
pip install -r requirements.txt
copy .env.example .env      # then add your ANTHROPIC_API_KEY

python app.py pitchdeck.pdf
python app.py pitchdeck.pptx --output output\acme_upside_case.pdf
```

Or run it as a web app:

```powershell
python web\server.py         # http://127.0.0.1:8020
```

Options:

| Flag | Meaning |
| --- | --- |
| `--output`, `-o` | PDF path. The JSON sidecar always shares its stem. |
| `--investment` | The cheque modelled for the ownership and return maths. Default `100,000`. |
| `--no-research` | Skip external research. The analysis still runs; the multiple range is then labelled an analyst assumption on the page. |
| `--date` | Analysis date as `YYYY-MM-DD`. Default: today. |
| `--no-email` | Do not email the result, even when Resend is configured. |

The run prints its nine stages and finishes with a provenance count and the headline
outcome:

```
[1/9] Reading deck
[2/9] Extracting company data
[3/9] Resolving commercialization timeline
[4/9] Building revenue model
[5/9] Researching comparables
[6/9] Modeling valuation
[7/9] Modeling investor returns
[8/9] Validating analysis
[9/9] Generating PDF
```

---

## What the analysis does

**1. Fixes the clock.** The calendar years on a deck's timeline slide are usually stale.
[timeline_engine.py](src/timeline_engine.py) resolves when commercial launch actually
falls relative to *today* — from a stated launch date, a stated time-to-commercialisation,
the gating regulatory milestone, or the company's stage — then indexes five commercial
years off that and reports where the 3- and 5-year marks land. Contradictions (an exit
before launch, a clearance dated after the launch it gates, a runway shorter than the
timeline it funds) are **flagged, never repaired**.

**2. Builds revenue bottoms-up.** No CAGR is applied to anyone's TAM.
[revenue_model.py](src/revenue_model.py) identifies the unit of adoption — hospital,
clinic, enterprise account, seat, installed device — and rolls cohorts forward: accounts
are won each year, each cohort ramps toward mature utilisation on its own clock, and
revenue is what those accounts actually consume at the modelled price.

**3. Sensitivity-tests the driver that matters.** Usually mature volume per account. But a
model whose revenue is mostly one-off hardware turns on how many accounts get won, and a
subscription model turns on price per account — [revenue_model.py](src/revenue_model.py)
picks the driver from the revenue mix and flexes that one.

**4. Measures reach against a defensible base.** [market_model.py](src/market_model.py)
builds an independent bottoms-up addressable market from the same unit economics that
drive revenue. Where that disagrees with management's TAM, the discrepancy is stated on
the page rather than reconciled away.

**5. Researches, rather than remembers.** [research.py](src/research.py) retrieves
comparable revenue ramps and acquisition transactions through the web-search tool. Two
rules: nothing enters the model without a URL, and no revenue multiple is calculated
unless a source stated *both* the price and the trailing revenue. Fewer than three
verified transactions cannot carry a range, so the band is blended toward the analyst
default and recorded as an assumption — a single high-multiple comp otherwise propagates
straight through the exit value into the MOIC.

**6. Models the capital the plan actually needs.** The rounds in the deck are not assumed
sufficient. [dilution_model.py](src/dilution_model.py) builds a financing path tied to
milestones, converts the instrument on offer (priced equity, SAFE, or a convertible note
with accrued interest) into ownership, and dilutes it through every round and option-pool
top-up on that path.

**7. Validates before it renders.** [validation.py](src/validation.py) re-derives revenue
from volume × price, enterprise value from revenue × multiple, and checks penetration,
account counts, ownership bounds, financing sufficiency and the exit claim. A failure is
reported on the page under **ANALYST FLAGS** — the analysis never edits an assumption to
make a check pass.

---

## Provenance

Every material number is classified and carried in a ledger:

| Type | Meaning |
| --- | --- |
| `DECK` | Stated in the pitch deck, cited to a slide, with the supporting quote. |
| `RESEARCH` | Retrieved from an external source, with a URL. |
| `ASSUMPTION` | Introduced by the analysis because the information was unavailable. |
| `CALCULATED` | Derived in Python from other inputs, carrying its formula. |

An assumption is never presented as though management provided it, and an assumption is
never `HIGH` confidence — only the deck or a source can be. The full ledger is in the
JSON sidecar:

```json
{
  "metric": "mature_volume_per_unit",
  "value": 500.0,
  "source_type": "DECK",
  "source": "slide 9",
  "confidence": "HIGH",
  "notes": "500 procedures per hospital per year",
  "unit": "procedures per hospital per year"
}
```

---

## Division of labour

The model reads, classifies and writes. Python calculates. That split is enforced by the
module boundary, not by instruction:

| The model does | Python does |
| --- | --- |
| Deck interpretation and extraction | Revenue and cohort calculations |
| Identifying the business model and unit of adoption | Market penetration and customer ROI |
| Proposing assumptions where the deck is silent | Valuation, ownership, dilution |
| Selecting comparables and reporting sourced figures | MOIC, IRR, sensitivities |
| Narrative and the "what has to be true" conditions | Every identity check in validation |

The extractor returns deck figures **verbatim** (`"$4.5M"`, `"65%"`); reading those into
numbers happens in [utils.py](src/utils.py), so a misread is a Python bug rather than a
prompt failure. Money is read from the first *currency-marked* figure and percentages from
the first *percent-marked* one, because a deck cell reading `1 DEVICE MSRP $4,000` should
give 4,000 rather than 1.

Two guards keep the model's own choices coherent, since both failures inflate every figure
downstream of revenue:

- **Price and volume must share a denomination.** If volume is counted in consumables, ASP
  is the price of one consumable and the device price goes to one-off revenue. Multiplying
  a device price by a consumable count moves the model by two orders of magnitude.
- **The addressable base must count the adoption unit.** A deck's "addressable sites"
  figure is only used when its own wording refers to the unit being modelled; otherwise it
  becomes a labelled assumption. A denominator counting something else makes penetration
  meaningless and every penetration check useless with it.

---

## Architecture

```
app.py                      CLI and the nine-stage pipeline both front ends drive
web/
  server.py                 Flask front end: upload, background job, polling, downloads
  templates/index.html      Single-page UI on the TEN Capital Network design system
  static/                   Favicon set, touch icon and web manifest, served at /static
templates/
  upside_case_template.json Document structure: sections, fields, tables, rules, styling
src/
  config.py                 environment, branding, modelling defaults, style
  models.py                 Sourced / Ledger / CompanyAnalysis
  utils.py                  money, percent and date parsing; formatting
  deck_parser.py            format dispatch and slide-labelled transcript
  pdf_parser.py             PDF extraction (pdfplumber)
  pptx_parser.py            PPTX extraction, notes and charts included
  llm.py                    Anthropic client, structured output, web search
  data_extractor.py         deck -> DeckFacts, quoted and slide-cited
  assumption_engine.py      deck -> proposal -> fallback, all ledgered
  timeline_engine.py        step 1: fix the clock
  revenue_model.py          steps 3-4: cohort build and sensitivity
  market_model.py           steps 5-6: reach and customer value
  research.py               steps 7-8: comparables and transactions
  valuation_model.py        steps 8-9: band, values, exit-claim test
  dilution_model.py         steps 10-11: capital path and ownership
  returns_model.py          step 12: proceeds, MOIC, IRR
  mailer.py                 Resend delivery: PDF and audit trail to the desk inbox
  validation.py             identity and coherence checks
  narrative.py              steps 2, 13-14: the written layer
  pdf_generator.py          the one-page render
```

Research results are cached in `.cache/research.json`, keyed by query, so re-running a
deck does not re-spend on searches.

---

## The page

Landscape letter, strictly one page, seven numbered sections across three columns:

1. **The success case** — 100–150 words from today to commercial scale
2. **Five-year value creation** — today vs. three and five years *from today*
3. **Revenue build** — the cohort table, the headline assumption, the sensitivity
4. **Valuation & investor return** — both exit outcomes, the multiple range, the deck's exit claim tested
5. **Real-world benchmark** — comparable revenue ramps, each footnoted to a source
6. **What has to be true** — the five to seven conditions that drive the outcome
7. **Investment interpretation** — success band, base case, downside

The footer carries analyst flags, numbered sources with URLs, and
*Confidential – for recipients only.*

Type is set as large as the content allows: the renderer tries ten profiles from 11pt
down and takes the first whose three columns all fit.

---

## The web app

`web/server.py` wraps the same pipeline behind a Flask front end: upload a deck, watch the
nine stages, download the PDF and the JSON.

The one structural difference from the CLI is that a run takes minutes — five model calls,
two of them web searches — which is longer than a browser or an edge proxy will hold a
request open. So an upload starts a background job and returns immediately, the browser
polls, and the artefacts are fetched from the finished job.

The page is three stacked cards on the TEN Capital Network dark palette — form, live
progress, result — with the deck types, size cap, model name and email recipient all
rendered from server configuration rather than hard-coded, so the page cannot drift from
what the app actually does.

| Route | Purpose |
| --- | --- |
| `GET /` | Upload form and live progress |
| `GET /healthz` | Readiness probe. `?deep=1` also checks the API key against the Models endpoint — no tokens spent |
| `POST /analyze` | Accepts the deck, starts a job, returns `202` with the job id |
| `GET /jobs/<id>` | Status, current stage, and the detail lines emitted so far |
| `GET /jobs/<id>/pdf` | The one-pager. `?download=1` for an attachment |
| `GET /jobs/<id>/json` | The audit trail |

**The job registry is process-local.** Run gunicorn with **one worker and several threads**,
or a poll will land on a worker that has never heard of the job. The `Procfile` and
`railway.json` are configured that way; don't raise `--workers`.

## Email delivery

Every completed analysis is emailed to the desk inbox through Resend, with the one-page
PDF and the JSON audit trail attached. [mailer.py](src/mailer.py) builds the message from
the same `CompanyAnalysis` object the renderer uses, so the body carries the conclusion
rather than a link to it: the six headline figures, the success-case narrative, what has to
be true, the interpretation, the analyst flags and the provenance counts. A partner can
read the answer on a phone and open the PDF only when they want the page itself.

**Delivery is best-effort and never raises.** A Resend outage must not cost an analyst the
run they just waited five model calls for, so `send_analysis` returns an outcome dict —
`{"sent", "id", "error", "skipped", "to"}` — which the CLI prints and the web UI shows on
the result panel. The PDF and JSON are produced and downloadable either way.

| Variable | Default | Notes |
| --- | --- | --- |
| `RESEND_API_KEY` | — | Leave blank to disable delivery entirely |
| `REPORT_EMAIL_TO` | `Info@tencapital.group` | Comma-separated for several recipients |
| `RESEND_FROM` | sandbox sender | Must be on a domain verified at resend.com/domains |
| `EMAIL_REPORTS` | `1` | `0` disables delivery without removing the key |

The From domain matters: Resend's sandbox sender `onboarding@resend.dev` only delivers to
the Resend account owner, so a deploy that leaves it in place will appear to send and
quietly reach nobody else. `tencapital.group` is verified on this account, and
`RESEND_FROM` is set to `reports@tencapital.group`.

Both front ends can opt out per run — `--no-email` on the CLI, an unticked box in the web
form — and `/healthz` reports whether delivery is configured and where it is addressed.

## Deploying to Railway

```powershell
git init && git add -A && git commit -m "Upside case analyzer"
railway init && railway up
```

Or point Railway at the GitHub repo — `railway.json` and `nixpacks.toml` are committed, so
the build and start command need no configuration in the dashboard.

Then set the service variables:

| Variable | Required | Notes |
| --- | --- | --- |
| `ANTHROPIC_API_KEY` | **yes** | From console.anthropic.com. Set it in Railway, not in a committed file |
| `RESEND_API_KEY` | for email | From resend.com. Without it the app runs and simply does not send |
| `REPORT_EMAIL_TO` | no | Default `Info@tencapital.group` |
| `RESEND_FROM` | for email | Must be on a domain verified in Resend |
| `APP_PASSWORD` | recommended | Gates the public URL. Without it, anyone with the link can spend your tokens |
| `MAX_UPLOAD_MB` | no | Default 40 |
| `MAX_CONCURRENT_JOBS` | no | Default 2. Each run is five model calls, so this is a spend ceiling as much as a load one |
| `JOB_TTL_MINUTES` | no | Default 60. How long a finished job's PDF stays downloadable |
| `UPSIDE_RESEARCH` | no | `0` disables external research globally |
| `UPSIDE_MODEL`, `UPSIDE_EFFORT` | no | Default `claude-opus-5` at `high` |

`/healthz` is the health check and reports `ok: false` when the key is missing, so a
misconfigured deploy fails its probe rather than accepting uploads it cannot serve.

Two notes on the runtime. Railway's filesystem is ephemeral: the research cache in
`.cache/` and any finished job are lost on redeploy or restart, which costs a re-search
rather than correctness. And `.env` is gitignored — it is for local development, and the
deployed service should read its key from the service variables.

## Missing data

The application is built to continue. A deck without revenue projections, a TAM, pricing,
launch timing, exit assumptions or financing detail still produces a full analysis — the
missing input is derived where possible, researched where appropriate, and otherwise
becomes a labelled assumption at reduced confidence. Their absence is not a finding.

Execution stops only when the deck yields no extractable text at all (a scan or an
image-only export).
