# ARCHITECTURE — AI Investment Operating System (in-house build)

> Status: **DECISIONS LOCKED — 2026-06-25**
> Build path: Path B (in-house, minimal cost, best-in-class)
> Philosophy: The LLM is the *reasoning layer*. The *data + compute + storage layer* is 80% of the work and is built first.

---

## THE 7 LOCKED DECISIONS

These are decided. We do not revisit them without a concrete, data-backed reason.

### 1. LANGUAGE & RUNTIME → **Python 3.12**
**Decision:** Python, pinned to 3.12.
**Why:** The entire quant/data ecosystem lives in Python. `pandas`, `polars`, `numpy`, `scipy`, `statsmodels`, `duckdb`, `scikit-learn`, every factor-investing and backtest library, every free data API SDK (`sec-edgar-downloader`, `fredapi`, `yfinance`) — all Python-first. Nothing else comes close for this domain. No C++, no Rust, no JS for the core. We use `uv` as the package/environment manager (fast, deterministic, replaces pip+venv).
**Cost:** $0.

### 2. DATA STORAGE → **DuckDB (local file), Parquet archives**
**Decision:** DuckDB as the primary analytical store (single `.duckdb` file), with Parquet as the archival/exchange format.
**Why:**
- DuckDB is **free, embedded, zero-server, SQL-native**, and *built* for time-series analytics. It reads/writes Parquet natively, runs OLAP queries at columnar speed, and scales to billions of rows in a single file on a laptop.
- vs PostgreSQL: Postgres is a great OLTP/row store but the wrong tool for "scan 50 years of daily prices across 8000 tickers and join to fundamentals." DuckDB is 10–100× faster for those scans and needs no server process, no config, no Docker.
- vs SQLite: SQLite can't do columnar scans or window-function analytics well; it's for small apps, not quant work.
- **Point-in-time correctness** is the non-negotiable requirement (no look-ahead bias). We enforce this by schema design (`as_of_date` on every fundamentals row) + query patterns, not by the engine choice.
**Cost:** $0.

### 3. SCHEDULING → **systemd timers (or cron fallback)**
**Decision:** OS-level scheduling via systemd user timers (cron as fallback), driving a Python CLI entrypoint.
**Why:**
- A long-term investor needs **daily batch**, not real-time. Prices after close, fundamentals after filings, macro after releases. Real-time is 10× the cost (infra, rate limits, latency monitoring) for ~1% of the value.
- systemd timers give us logging (journalctl), retries, calendar expressions, and dependency ordering for free, with no extra dependency. The installed U.S. workflow is one idempotent benchmark-first service with a durable independent run record, startup catch-up, and Linux linger so a desktop logout cannot silently strand a partial run.
- We deliberately reject Airflow/Prefect/Dagster: they solve multi-team orchestration problems we don't have, add heavy services, and over-engineer a single-user daily pipeline.
- Architecture: `timer → CLI command → job → DuckDB + Parquet`.
**Cost:** $0.

### 4. FUNDAMENTALS DATA → **SEC EDGAR XBRL (primary), SimFin (cross-check)**
**Decision:** SEC EDGAR's free XBRL Company Facts API is the primary fundamentals source. SimFin free tier is the cross-validation/fallback.
**Why:**
- EDGAR XBRL (`https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json`) is **the source of truth** — it is what the company filed, machine-readable, free, and covers all SEC filers. No vendor normalization, no lag, no paywall.
- **Rate limit: 10 requests/second** (verified, SEC policy since 2021-07-27), mandatory `User-Agent` header with contact email, exponential backoff on HTTP 429. We bake this into our HTTP client.
- SimFin free tier (5-yr fundamentals, ~5000 stocks, Python API + bulk CSV) is used for (a) cross-checking EDGAR numbers and (b) pre-computed convenience metrics. Point-in-time full history is a paid feature, so EDGAR remains primary for backtests.
- We reject paid fundamentals services (FMP, Polygon, and similar) for now:
  free covers the MVP, and EDGAR is the primary filed source.
**Cost:** $0.
**Keys needed:** None for EDGAR (User-Agent string only). Optional free SimFin account.

### 5. PRICE DATA → **yfinance (primary), optional Tiingo, Stooq fallback**
**Decision:** `yfinance` for EOD prices/adjustments/splits/dividends; optional
user-token Tiingo for explicitly reviewed symbols; Stooq (no-key CSV download)
as the final fallback.
**Why:**
- `yfinance` is free, keyless, covers US + international, gives adjusted OHLCV + dividends + splits. It is the de-facto free choice. Its only weakness is reliability (unofficial scraping endpoint) — which is why we *must* have a fallback.
- **Stooq** offers free, no-API-key CSV history and remains a possible
  independent fallback. Its downloader currently returns a JavaScript
  verification page in this environment, so the adapter validates content and
  fails closed; no HTML response can become a successful price ingest.
- **Tiingo** is provider-neutral optional plumbing, not an active dependency.
  Its token is sent in an authorization header. A symbol is usable only after
  the same bounded identity review as Yahoo/Stooq. A locally configured token
  was used for explicitly reviewed retired-symbol manifests through 2026-07-18;
  credentials are never written into manifests or logs.
- Daily EOD only for MVP. Real-time/intraday is explicitly out of scope (see Decision 3).
- We write thin common-schema adapters. Legacy ticker ingestion may fall back
  automatically; identity-aware ingestion never crosses providers unless a
  separate reviewed provider interval authorizes it.
**Cost:** $0.

### 6. MACRO DATA → **FRED API (primary), BLS + US Treasury (fallback)**
**Decision:** Federal Reserve Economic Data (FRED) API is the macro backbone. US Treasury and BLS are targeted fallbacks.
**Why:**
- FRED aggregates CPI, PCE, GDP, unemployment, Fed Funds rate, all Treasury yields, yield-curve spreads, credit spreads, money supply, etc. — one free API key, one consistent interface, decades of history.
- `fredapi` Python library makes it trivial.
- Fallbacks: US Treasury daily yield curve (free CSV, no key), BLS public API (free, key-recommended) for employment/inflation detail.
- Operational proof (2026-07-25): the official year-bounded Treasury CSV
  succeeds in this environment. Its exact bytes are retained before parsing,
  its Date/2 Yr/10 Yr/30 Yr schema and yields are strictly validated, and 423
  rows through 2026-07-24 replay from the stored response. The latest common
  DGS2/DGS10/DGS30 observation exactly matched FRED.
**Cost:** $0.
**Keys needed:** Free FRED API key (one signup, takes 2 minutes).

### 7. LLM (SYNTHESIS ONLY) → **Claude API (primary), GLM-5.2 (fallback)**
**Decision:** The LLM is used *only* as the synthesis/reporting layer. It never makes the numeric decision. Claude primary, GLM-5.2 fallback.
**Why:**
- This corrects the single biggest flaw in the original prompt: the LLM cannot run Monte Carlo, compute VaR, rank 8000 stocks by factor score, or maintain state across sessions. Those are deterministic Python jobs.
- The LLM's job is narrow and high-value: take a JSON packet of factor scores +
  fundamentals + macro regime + news, then produce a human-readable research
  report, explain a deterministic model ranking, and narrate a portfolio review.
  It does not create personal buy/sell recommendations.
- **No multi-agent "committee" theater.** If we later run multiple "agents," they will be agents running *different computations* (factor agent = numeric model, valuation agent = DCF code, news agent = NLP), and the LLM synthesizes their *outputs*. Never personas sharing one model.
- For the MVP/foundation phase (now), the LLM is not even wired in yet — we build the data pipeline first. The LLM slot is reserved but empty until the data layer is proven.
**Cost:** $0 in this phase. Later: pay-per-token, controllable.

---

## PRODUCT DIRECTION — INDIA-FIRST, US AS THE REFERENCE BUILD

The completed product is intended primarily for Indian public equities. The
current U.S. implementation is the first audited reference market: finish it,
use it to prove the PIT, identity, factor, cost, risk, and monitoring contracts,
then reuse those contracts for India. Do not mix partially reviewed NSE/BSE
data into the U.S. universe merely to appear multi-market.

U.S. completion is the active engineering scope. India schema/adapters remain
deferred until the U.S. current-date readiness, monitored paper, holdout, and
operator/deployment gates are complete.

The gated implementation and official-source feasibility decisions are tracked
in `INDIA_BUILD_PLAN.md`. India engineering may proceed while the untouched U.S.
forward observation accrues, but no Indian row may enter research tables before
the portable market-schema and source-review gates pass.

The factor, portfolio, risk, reporting, and validation cores must remain
market-neutral. Market-specific behavior belongs in a configured market
profile or provider adapter:

| Concern | U.S. reference today | Portable contract required for India |
|---|---|---|
| Instruments | SEC/security/provider identities | market, exchange, currency, stable security ID, dated provider symbols |
| Fundamentals | SEC EDGAR adapters | reviewed filing adapter with public availability dates |
| Prices/actions | reviewed Yahoo/Tiingo/Stooq paths | reviewed EOD and corporate-action adapters |
| Universe | bounded S&P 500 membership | independently sourced NSE/BSE/index membership with announcement and effective dates |
| Calendar/benchmark | explicit SPY sessions and benchmark | configured exchange calendar and benchmark; never hard-coded in factor logic |
| Macro | release-aware FRED series | India-specific release-aware series selected through a market profile |
| Costs/taxes/broker | caller-supplied U.S. research scenarios | separate user-approved India account, tax, fee, and broker policy |

The current schema and adapters are not yet India-ready: several source
defaults are U.S.-specific and market/exchange/currency are not first-class on
every required row. Before Indian data ingestion, add those dimensions and
provider interfaces with migration and parity tests. Do not encode `.NS`/`.BO`
symbol conventions, INR assumptions, exchange holidays, or tax rules directly
inside factor calculations.

### Deployment and operator contract

The owner should not need to administer DuckDB or understand Streamlit. Those
are replaceable implementation details behind supported commands and the
`Store` boundary. The deployment milestone is not complete until it provides:

- one documented setup, start, stop, upgrade, backup, restore, and health-check
  workflow;
- environment-based configuration and secrets, with a persistent mounted data
  directory that survives upgrades;
- startup schema migrations plus `aios doctor` and `aios validate` gates;
- `aios dashboard` as the supported local launcher and future container
  entrypoint, exposing one dashboard URL without direct Streamlit commands;
- safe single-instance DuckDB operation for local/paper use, and a storage
  migration path behind `aios.storage.store.Store` if multi-user concurrency is
  later required; and
- versioned market profiles so switching from the U.S. reference build to India
  selects adapters/configuration rather than forks the factor or risk engine.

The current checkout remains a local supervised research and paper-simulation
beta. `aios dashboard` hides Streamlit and the CLI hides DuckDB for ordinary
use. Until authentication and HTTPS are implemented, the supported launcher
accepts only `localhost` or an IP loopback address. Supported mutation commands
temporarily enforce a private `0077` creation umask and restore the caller's
prior value when they finish. `aios health`, `aios backup`, `aios
verify-backup`, and confirmed
`aios restore` provide fail-visible health and recovery. The normal
`aios refresh-us-daily` path refreshes action-safe SPY first, certifies the
unchanged investable universe through that completed session, refreshes member
prices and release-aware macro data inside the newly extended identity windows,
and requires broad exact-date readiness before declaring success. The
lower-level `aios refresh-us-current` remains available for selective prices,
SEC filings, SPY, and macro work through already reviewed identities.
`aios review-universe-current` archives exact S&P Global press-archive bytes
and an independent current-component CSV, checks unreviewed change headlines,
exact ticker-set equality, and reviewed CIK lineage. For a candidate headline,
it also archives the exact official detail page and strictly parses balanced,
dated S&P 500 table rows. An announced change can permit unchanged coverage
only for requested closes strictly before every effective date; it remains
pending and blocks at its effective date until formally imported. All bounded
reference domains still extend atomically or not at all. Real constituent
membership changes remain a manual provenance gate.

`aios preflight` is the canonical read-only operator contract. It resolves the
active proposal from the checksum-protected forward-trial registry, selects the
latest certified decision close, opens DuckDB read-only, and reads the
operations ledger without initializing or migrating it. Its versioned,
checksum-protected JSON keeps research, proposal creation, stress review, paper
recording, unattended operations, and real-capital execution as independent
capabilities and emits exactly one safe next action. The default uses the
lightweight timing gate; `--review-paper` runs the full governed read-only
review but still stops at an explicit human decision and deliberately produces
or executes no order or other state-changing command. Repeated `--require`
options turn independent capabilities into a machine-readable exit gate. The
Overview consumes the same pure action router.

Every supported CLI command that can change backup-covered DuckDB, immutable
raw-snapshot, paper-account, proposal, or forward-trial state acquires one
non-blocking project maintenance lease before opening or changing that state.
This includes backup/restore, paper/forward workflows, universe review,
refresh, ingest, import, repair, and cleanup commands; contention fails visibly
instead of allowing two supported mutations to overlap. Operations-only
incident and notification mutations remain transactional in SQLite, whose
online backup API captures a consistent snapshot. The lease is cooperative and
does not serialize direct Python API calls or unrelated external writers. The
active forward policy file remains frozen, so its remaining direct-library
persistence boundary is not retrofitted mid-trial. A next policy version must
add the final forward trial/proposal compare-and-set checks before this
cooperative lock can be described as universal transaction safety.

`aios backup` checkpoints DuckDB through the storage layer without constructing
a writable `Store`, so a preservation command cannot run application migrations
before its snapshot. Its failure path records an incident only when the
operations ledger already matches the current schema. `aios upgrade-local-state
--confirm` holds the same lease while it creates and re-verifies an exact
pre-upgrade backup, rehearses both DuckDB and SQLite upgrades on disposable
copies, compares their logical evidence with live state, applies the supported
migrations, and publishes a checksum-chained prepared, analytical, operations,
and verified journal. `upgrade-local-state-recover` resumes or verifies that
exact attempt after a phase-publication or process failure. An unexpected live
failure is never auto-restored across the two databases. The verified backup
supports tested forward recovery through the current release; it is not proof
that defective migration logic can be rolled back byte-for-byte. Exact rollback
requires the release-pinned code that produced the pre-upgrade schema, while the
operations ledger remains forward-only so newer incident evidence is not lost.

Caller-selected generated outputs pass a separate artifact boundary before
work begins. Project-local artifacts must stay under `data/` while remaining
outside DuckDB, operations, maintenance, raw, paper, and backup state; symlink
ancestors and hard-linked mutable workspaces are refused. Readiness, refresh,
coverage, backtest, universe, identity, and reference-batch files publish
atomically without replacing an existing target. Paper proposals are the sole
exception to the general paper-directory exclusion: they must stay under
`data/paper/proposals`, and an explicit replacement first validates the
existing document kind, paper account, and decision date. Direct library calls
remain outside this CLI path policy.

The `alerts`, `alert-show`, `notifications`, `notification-show`, `anomalies`,
`anomaly-show`, and `email-status` commands apply the same fail-closed
principle to SQLite. They require the current schema and a checkpointed ledger,
then open it with `mode=ro`, `immutable=1`, and `query_only`. Inspection never
runs schema DDL, changes journal mode, or updates the database merely to show
status.

### Governed data-quality review boundary

Anomaly review is an operations workflow, not an analytical repair path. The
first rule bundle compares accepted SEC fundamentals coverage with the exact
reviewed U.S. universe and issuer identities for one decision date. The
detector opens DuckDB read-only and resolves the exact ingest run, raw snapshot,
payload checksum, parser provenance, and issuer subject for each finding. A
missing, conflicting, or tampered required source boundary withholds the
complete scan. It never fills a value, advances readiness, or converts a
warning into an accepted research fact.

Coverage has two explicit proof paths. Newly ingested rows must carry complete,
all-or-none run, Company Facts snapshot, canonical row-set, and canonical row
lineage. Legacy rows without those fields are eligible only when the detector
replays the exact Company Facts bytes, verifies Company Facts/Submissions CIK
identity, and proves equality between the decision-date provider row set and
the stored issuer row set. That compatibility path is read-only and never
backfills inferred lineage. Both paths admit a successful ingest, or the
narrow accepted warning with positive inserted rows whose rejected rows had
`period_end` after filing date. Zero-row and all other warning outcomes do not
establish coverage.

The comparison has two explicit modes:

- `anomaly-scan --preview` constructs the complete source-bound scan in memory,
  leaves both DuckDB and the operations ledger unchanged, does not acquire the
  project maintenance lease, and does not create or update its lock file.
- `anomaly-scan --record` writes the immutable scan, appends lifecycle events,
  and reconciles only fingerprint-deduplicated current cases in the independent
  operations SQLite ledger. It does not change DuckDB, raw evidence, proposal,
  trial, paper-account, or broker state.

Operations schema v6 contains append-protected `anomaly_scans`, current
`anomaly_cases`, and append-only `anomaly_case_events` beside the existing
incident and notification truth. The first `--record` against a schema-v4
ledger performs this supported additive migration. It creates empty anomaly
tables without inventing historical scans, backfilling cases, or changing the
v4 incident/notification contract; read-only anomaly views fail closed until
the migration occurs. When schema-v5 anomaly tables already exist, the v6
migration preserves every scan, case, and event and deterministically fills
only a missing `event_sequence` from the existing append row order. This is a
structural ordering migration, not a semantic backfill of historical anomaly
observations or decisions. Each case retains old/new values, severity,
confidence, source evidence, suggested checks, rule version, owner, occurrence
count, and the latest evidence hash. Every scan separates the provider
`source_boundary_at` from its ledger `recorded_at` and carries an immutable
monotonic record sequence. SEC source-boundary policy v2 computes
`source_boundary_at` as the maximum receipt time of only the exact snapshots
actually consumed to prove coverage plus the selected warnings used for missing
issuers; unrelated or newer global ingest activity cannot advance it. The
ledger permits exactly one audited, one-way transition from a matching legacy
implicit-global v1 scan to a consumed-snapshot v2 scan even if that correction
produces an earlier boundary. The exception requires the same SEC rule bundle
and scope, exact v1/v2 policy signatures, the v2 consumed-snapshot maximum
proof, and a zero-write safety contract. A v2 scan cannot use the exception
against another v2 scan, so same-policy boundary regressions remain blocked.
Repeated identical scans are idempotent; changed evidence is auditable, and a
later recurrence with changed evidence can reopen a resolved case.

Acknowledgement-first is the recommended ownership workflow. Acknowledgement
requires a named owner, audit note, and the exact evidence hash shown during
inspection. Direct resolution is also permitted with those same three fields.
The compare-and-set hash makes stale operator decisions fail visibly. Exact
retries return the existing lifecycle result without appending duplicate
events; retries with different content fail closed. Outcomes are limited to
`accepted`,
`source_corrected`, `mapping_corrected`, `false_positive`, or `deferred`. A
corrected-source or corrected-mapping disposition requires a later complete
same-scope scan: it must be recorded after the finding, carry a distinct
source-boundary hash with a non-earlier source-boundary time, run the exact
rule, omit the fingerprint, and bind its clearance to a source-provenanced
accepted SEC ingest with positive verified rows. Before every case read or
mutation, the lifecycle verifier replays the immutable ordered events and
requires owner, state, disposition, timestamps, severity, occurrence count,
and scan/evidence pointers to equal the mutable case projection. Deferral
requires a future review time and stays unresolved. These state changes never
authorize a data edit or waive readiness. The dashboard and the
`anomalies`/`anomaly-show` commands consume the same immutable reader and expose
no inline mutation or execution control.

The legacy v1-to-v2 boundary transition is a scan-recording compatibility rule,
not a correction-clearance shortcut: a verification scan for
`source_corrected` or `mapping_corrected` must still have a non-earlier source
boundary than its finding.

This is a local structural-integrity boundary, not remote attestation. It
trusts the released AIOS code and the operating system's filesystem and access
controls. It detects an inconsistent direct database edit; it cannot
cryptographically defend against a privileged actor who can replace the
verifier, ledger, and local source evidence together.

Runtime root resolution is distribution-safe. A source or installed CLI first
discovers the AIOS workspace from the current directory and its parents; an
explicit existing `AIOS_PROJECT_ROOT` is required when launched elsewhere. It
never treats the wheel or `site-packages` directory as the data root. The
Streamlit launcher uses the dashboard module shipped beside the installed CLI
while keeping its working directory and governed data paths at the resolved
workspace.

Release artifacts have a separate source-to-wheel acceptance boundary. The
candidate verifier snapshots the reviewed package tree, refuses source changes
during review, and requires the wheel to contain exactly the expected package
assets and distribution files with no duplicate, unsafe, symlinked, missing,
unexpected, or byte-stale member. `METADATA` must reproduce the project name,
version, `Requires-Python`, every `Requires-Dist`, and the optional-extra set
from `pyproject.toml`; the console entry point must remain
`aios = aios.cli:main`. `WHEEL` must declare the reviewed pure-Python
`py3-none-any` contract. `RECORD` must cover every archive member with its exact
SHA-256 digest and byte size, except its required empty self-entry. The default
verification path finally installs the candidate with `--no-deps` into a new
temporary virtual environment and smoke-tests imports, bundled UI/risk
resources, and `aios --help`. This is a candidate gate, not evidence that an
unverified or stale `dist/` artifact is current.

The local UI deliberately remains Streamlit. The 2026-07-28 product review used
the [CopyUI component catalog](https://copyui.com/components) for component
examples, the official Claude interface and reusable-design-system guidance,
and the Carbon, Cloudscape, and Atlassian systems for institutional spacing,
table, dashboard, and grid conventions. It did not copy a React/Tailwind
runtime or add a frontend dependency. Native Streamlit theming
defines a warm ivory canvas, flat white surfaces, a light navigation rail,
near-black primary actions, a restrained clay identity accent, semantic status
colors, eight-pixel radii, a 17px base type scale, and a bounded wide content
grid. The token and component CSS lives in `aios/dashboard.css`; reusable
Streamlit renderers live in `aios.dashboard_components` rather than being
redeclared inside each page.

The Overview answers three separate questions: whether research is usable,
where the paper trial stands, and whether local operations need attention. One
safe next action and one symmetric primary CTA follow those scoped states.
Proposal targets, current readiness, paper progress, and open incidents remain
visible; only raw commands, hashes, and long technical history use progressive
disclosure. Global navigation contains Overview, Research, Paper Trial, and
Operations, plus Methodology & Sources as a utility. Company Detail remains a
deep-linkable contextual Research destination. Research date, model, and search
controls are visible in the page toolbar. Paper Trial exposes a fixed Proposal
→ Forward Trial → Timing Review → Local Record sequence without weakening its
fail-closed rules. Pure presentation logic lives in `aios.dashboard_ui`, while
every loader remains read-only and every numeric decision remains in the
existing deterministic engine. Workspace, research date, model, research
surface, company, and search selections are mirrored in URL query parameters
with origin-aware synchronization, so widget changes and external same-session
URL edits preserve exact debugging state. A separate web frontend is justified
only after a versioned application API and a real need for multi-user
authentication, roles, mobile-first workflows, or rich real-time interaction
exist.
The optional dashboard dependency declares the tested Streamlit
`>=1.58,<2.0` runtime; stable widget keys and URL query parameters jointly
preserve state across consecutive workspace and research-surface changes.

Confirmed systemd-user installation adds one New-York-clocked recoverable daily
timer after every U.S. weekday plus weekly filing and verified-backup timers.
It installs, but does not enable, a separate optional SMTP worker timer.
The daily service uses `Restart=on-failure`; its timer also runs an idempotent
check three minutes after the user manager starts. Linux linger is explicitly
enabled so the user manager remains active after desktop logout while the
computer is on. Each job writes running/success/failed/interrupted lifecycle
state to the permission-restricted SQLite operations ledger, not analytical
DuckDB. A killed process therefore leaves durable evidence that the next startup
can recover. Core data and backup services have an `OnFailure=` handler plus a
post-success recovery marker. The email worker deliberately has neither, because
its own durable delivery state must not recursively create more email. Structured incidents and lifecycle events remain
available even when DuckDB cannot be opened. The ledger
stores bounded systemd result properties rather than raw journals or process
environments. It is included as audit evidence in verified backups but is never
rolled backward during an analytical restore, preserving failures observed
after the older backup. Generated units pass `systemd-analyze verify` and are
not installed implicitly. The owner explicitly installed them on 2026-07-21; real manual
first runs of backup and filings passed. The replacement daily workflow passed
a live 503-member July 23 catch-up, exact-date readiness, and an immediate
no-download startup run. On July 25, simultaneous persistent startup catch-up
exposed that the unit exported `AIOS_DUCKDB_LOCK_WAIT_SECONDS` while the settings
field actually reads `DUCKDB_LOCK_WAIT_SECONDS`. The corrected units use the
real 300-second setting and serialize every scheduled writer and health check
through a shared 30-minute `flock` queue. The reinstalled units passed native
verification, a 503-member July 24 recovery, and a no-download systemd service
run; failure incidents resolved through normal success hooks. Raw refresh may use the
newest reviewed membership snapshot up to seven days old for collection while
showing the exact snapshot date; readiness still refuses to treat it as a
current membership decision. A reviewed issuer with no accepted Company Facts
is retried and reported as pending; zero rows for an established issuer remain
a hard failure. The benchmark-first daily dependency chain ensures dated
identity windows are available before the member-price refresh. Streamlit uses
short-lived read-only Store scopes instead of a process-global writable
connection, and managed writers have a bounded five-minute lock wait. This
secondary database wait protects brief reader overlap after scheduled writers
have already queued, preserving DuckDB's single-writer constraint without
requiring the dashboard to be closed for normal scheduled updates. Restore
still requires the dashboard to be closed. Confirmed restore copies the complete
candidate into an isolated project-shaped staging tree before a safety backup,
operations incident, raw publication, or live paper/database swap. That
pre-swap gate requires exact application-version compatibility, an openable
DuckDB with no hard data-quality failure, checksum/schema-valid account,
proposal, and active/archived forward-trial envelopes, consistent active
trial/account/proposal references, and replay of every registered raw snapshot;
immutable raw merge conflicts are also rejected before publication.
`aios restore-drill` exercises this same confirmed restore path inside a
disposable project without changing the live database, paper state, operations
ledger, or raw archive. The July 25 drill passed against a non-empty 1,542-file
backup.

Raw evidence publication is descriptor-relative and no-overwrite. Every path
component is opened from the held project root with directory and no-follow
flags; temporary files are fsynced and linked into place only if the final
content address does not exist. Verification rejects traversal, symlink swaps,
hardlinks, FIFOs, unsupported compression, unreviewed parsed-data parsers and
incomplete Company Facts rejection evidence. HTTP response streaming, stored
payloads and decompression are bounded, and replay retains at most one
decompressed payload at a time. The additive rejection-evidence migration
replays exact historical Company Facts bytes before recording its schema
marker, so mutable timestamps cannot create a legacy exemption.

The publication phase is not a cross-filesystem transaction. A later
filesystem failure uses the pre-restore safety backup and existing paper
rollback; immutable raw additions and the operations stale-evidence incident
remain forward-only audit evidence rather than being rolled back.

EOD adapters cap accepted rows at the latest completed New York session plus a
conservative 30-minute
free-provider finalization delay rather than the host's local date, preventing
an India-after-midnight or just-after-close catch-up from accepting a partial
U.S. daily bar. Container/hosted packaging,
authentication, HTTPS,
live external-alert activation, multi-user storage, and India market adapters
remain future exit conditions—not capabilities to assume today. The SMTP
adapter exists, but no external-delivery claim is valid before a real receipt
test and explicit route/timer activation.

The operations ledger is schema v6. Its v4 incident/notification contract
remains unchanged: a channel-neutral `notification_outbox` and append-only
`notification_deliveries` sit beside incident truth. An actionable incident
transition and its message copy commit in one SQLite transaction, linked by the
incident event ID; repeated observations without escalation do not create
message noise. Claims use exclusive bounded five-minute leases, and delivery
outcomes distinguish success, pre-send temporary failure, permanent failure,
and ambiguous provider state. Only temporary failures known to occur before
acceptance retry deterministically up to five attempts. A post-send disconnect
or expired worker lease has an unknown outcome and becomes operator-visible
immediately instead of risking a duplicate. Stored provider metadata is
allow-listed and redacted rather than preserving arbitrary responses.

External delivery remains fail-closed. Without an enabled route,
incident-generated messages enter `held`, which no worker can claim. Every
routable message records an immutable activation ID; claim, route alias,
configuration fingerprint, dependency, and activation are checked in one
transaction. Reconfiguration quarantines old pending messages instead of
retargeting them. A recovery message directly depends on the delivered active
message from the same incident cycle and activation. Historical migrations
create no backlog, and a channel never releases old or already-recovered events.
`aios notification-test` alone creates an eligible
test message and dispatches it to a deterministic local receiver with no
network access.

The selected external adapter is one-recipient SMTP over STARTTLS or implicit
TLS, with TLS 1.2 minimum, certificate/hostname verification, disabled TLS key
logging, a 30-second maximum per-operation timeout, deterministic Message-ID,
safe error classes, and no raw provider text or destination values in delivery
history. SMTP secrets stay only in environment configuration. The optional
worker exits before reading credentials or opening a network connection while
the route is disabled. Live activation still requires local credentials, one
exact-configuration test, owner-confirmed receipt, and explicit enablement.
The 2026-07-27 live v3-to-v4 migration retained 11 incidents, 50 incident
events, five job records, three outbox rows, and one local-test delivery with no
configured route; SQLite integrity and foreign-key checks passed. The installed
optional worker is disabled, while all three core timers remain enabled.
The subsequent additive schema-v6 migration retained those records and now
holds two immutable anomaly scans, three current review cases, and six
append-only case events. Post-migration SQLite integrity and foreign-key checks
also pass. A v5-to-v6 upgrade preserves those anomaly records and derives only
their deterministic event sequence from the pre-existing append order.

`FUTURE_BUILD_PLAN.md` is the sequencing and acceptance companion to this
architecture. It prioritizes immutable provider snapshots, external
notification delivery on top of the implemented local incident ledger, the
remaining anomaly rule families after the SEC coverage v1 slice, and the next
governance milestones after the implemented Proposal Stress Review v1.

### Reconstructable provider-evidence contract

Transport evidence must say what it actually preserves. Direct HTTP adapters
store the exact response bytes before parsing. A library adapter that does not
expose those bytes may store only an explicitly labeled
`normalized_provider_export`; it must never claim exact vendor reconstruction.
Every normalized export needs a secret-free request fingerprint, adapter/parser
version, ingest link, parsed-row count and hash, plus a deterministic replay
parser that the raw verifier executes.

Exact SEC issuer responses use a two-stage form of the same contract. The HTTP
boundary first registers Company Facts or Submissions as a capture-only parser
version, preserving bytes even when JSON, CIK, or row validation fails. After
the downstream parser succeeds, one transaction locates exactly one
run-and-role link, upgrades it to the reviewed replay parser version, and
attaches the canonical row count and SHA-256. Repeating identical evidence is
idempotent; missing, partial, multiply linked, or conflicting evidence fails.
The verifier re-runs those parsers directly from the compressed exact bytes.

Production yfinance price ingests now follow this weaker-but-honest contract:
the canonical export preserves the library-returned OHLC, adjusted close,
volume, dividends, splits, requested window, and completed-session boundary.
The same reviewed parser both supplies live normalized rows and replays stored
exports. The bounded live AAPL proof retained three rows and reproduced their
parsed hash.

FRED follows the normalized-export contract because `fredapi` also hides original response
bytes. Its canonical export preserves the requested realtime window, selected
vintage dates, observation dates, public release dates, values, and units. The
first live incremental GDP proof replayed 318 rows exactly. The July 24
full-universe recovery expanded coverage to 505 yfinance exports; the current
archive also replays 23 FRED exports.

Direct HTTP routes now apply the stronger exact-response contract to two SEC
Company Facts responses, two SEC Submissions responses, the 10,429-row SEC
company-ticker map, a four-session reviewed Tiingo sample, and 423 official
Treasury yield-curve rows. The verifier checks 1,535 unique payloads and
executes 535 parsed replays in total. These artifacts prove deterministic
reconstruction of what was preserved; normalized exports do not claim to be
original Yahoo or FRED HTTP bodies.

### Untouched forward-policy contract

Forward evidence must distinguish changing public inputs from changing research
rules. `forward-freeze` therefore fingerprints the QV and macro-regime logic,
portfolio/risk/cost/tax rules, U.S. calendar, readiness gate, and paper workflow,
and stores the reviewed target count and policy configuration in a
checksum-protected local document. It registers each later proposal by payload
checksum. `forward-status` fails on missing/changed policy files, changed
cost/tax configuration, altered registered proposals, or unregistered new
proposals; the CLI refuses simulated execution while that drift exists.
`forward-restart` is the guarded lifecycle transition after genuine drift: it
builds a later simulation-only baseline, moves the predecessor unchanged into
the forward-trial archive, and atomically activates the replacement. It refuses
to replace an unchanged trial and rolls the archive move back if activation
fails.

An unchanged trial with one expired, unexecuted proposal is a different
transition. `forward-rollover` defaults to a deterministic, read-only v4
plan over the exact trial, account, proposal, execution registry, readiness,
normalized proposal blueprint, successor deadline, policy bundle, archive
paths, no-fill disposition, and required transaction invariants. Volatile
preflight time, operations availability, and current blockers remain in the
observation envelope outside the plan hash. `--write-plan` can publish the plan
content-addressed under `data/reports`. Activation consumes only that exact
artifact with its exact SHA and explicit confirmation. It creates and verifies
a fresh backup, takes the project and fixed-order document locks, repeats
readiness/operations/deadline/source checks, publishes byte-identical
predecessor archives and successor outputs write-once, then performs one atomic
active-trial swap. Checksum-protected append-only phases make an interruption
recoverable: the confirmed recovery command either verifies the successor or
removes only exact plan-owned outputs while the predecessor remains
authoritative. A missed generation window moves to a later certified close; it
never permits a retrospective fill. The v4 mechanism is implemented; live
activation remains gate-dependent and must stay blocked until current
readiness, operations, and deadline checks are clean in a naturally prospective
window.

DuckDB is intentionally outside the policy checksum. Prices, filings, macro
vintages, and reviewed membership must continue to advance under their existing
PIT/provenance contracts. A deliberate policy change starts a new trial rather
than rewriting the old baseline.

Paper execution also has a wall-clock provenance boundary. A proposal must be
generated before the 9:30 a.m. New York open of its scheduled simulation
session, preventing an operator from observing that session before deciding
whether to participate. `paper-review` applies the same account checksum,
forward registration, readiness, factor-evidence, risk, and price checks
without persisting a fill. Confirmed simulation is allowed only after that
session's conservative 4:00 p.m. close and before the next U.S. session opens.
Missing prices wait; an expired window is never converted into a retrospective
fill. The calendar remains conservative on early-close days: waiting until
4:00 p.m. reduces availability but cannot introduce look-ahead. The wall clock
is checked again after expensive evidence work and immediately before atomic
replacement. A cross-process document lock plus checksum compare-and-swap
prevents concurrent account/proposal writers from silently losing an update.
Every paper-account mutation also holds a non-blocking per-account operating
system file lock and rechecks the account checksum immediately before atomic
replacement. A second writer fails visibly, and an out-of-band edit made while
evidence is recomputed cannot be silently overwritten.

### Governed proposal stress-review contract

Stress review is an advisory read model over a registered paper proposal, not a
new approval or execution gate. `aios stress-review` delegates to one production
service shared with the Paper Trial dashboard panel. The service validates the
active forward trial and proposal registration before opening DuckDB, opens
DuckDB read-only, and binds the account, proposal, exact PIT security identity,
action-safe price history, row-level liquidity window, release-aware revenue
fact, scenario policy, and calculation sources by SHA-256. It then repeats the
trial/account/proposal/scenario/source compare-and-swap identity closure before
returning. The scenario bundle is reloaded after calculation and again at the
final boundary, so a concurrent policy edit refuses the result instead of
publishing an older in-memory policy. Default use writes nothing. An explicit
`--output` uses a write-once, checksum-protected artifact and repeats the final
closure immediately before publication and again afterward. If that last
identity check detects drift, AIOS removes only the exact matching artifact it
just created; if exact rollback cannot be proven, it fails with a quarantine
instruction rather than deleting an ambiguous file.

Scenario coefficients live in an immutable versioned policy bundle with
per-assumption provenance and no probabilities. Deterministic mark shocks may
produce modeled proposal-target values, proposal-portfolio drawdown, post-shock
concentration, and exit-capacity comparisons. If a scenario's required evidence
is missing or mismatched, its numerical result is withheld; independent
calculations may remain visible only in a report explicitly marked partial. The
volatility/correlation calculation is a separate statistical loss proxy with
Euler risk contributions; those contributions must never be converted into
invented position returns, stressed holdings, drawdown, concentration, or
liquidation outcomes. Historical and supervisory labels calibrate magnitude
only. In particular, the 2026 Federal Reserve severely adverse scenario supplies
an [approximately 58% hypothetical equity-price
decline](https://www.federalreserve.gov/publications/2026-stress-test-scenarios.htm),
not a forecast. Generic sandbox-limit comparisons remain human-readable
advisory findings and cannot approve, reject, alter, or execute a proposal.

The dashboard renders this same governed result immediately below pending
proposal targets. It does not create another Paper Trial stage and does not
recast targets as current holdings. Recorded-holdings stress, multifactor and
Monte Carlo risk models, and owner-authored scenarios remain separate future
contracts.

### U.S. operational readiness and generic risk contract

Raw recency and certified readiness are separate states. `aios readiness`
checks a requested date against database integrity, dated S&P 500 membership,
stable security identities, PIT filing coverage, identity-safe prices,
action/split evidence, SPY calendar/benchmark freshness, and release-aware
macro evidence. `paper` also requires a recent decision date and returns a
non-zero exit status if any blocking gate fails. `historical_research` may pass
inside an older bounded window without claiming that it is current.
`latest_reviewed_market_close` is used for valuing existing simulated holdings;
`latest_paper_decision_date` additionally requires a 450–550-member certified
universe. This prevents one newer SPY row from turning one missing universe edge
into several misleading downstream 0/0 failures.

As of the 2026-07-29 engineering check, the reviewed current decision close is
2026-07-29. It has 503
dated members and stable identities, 500/503 PIT filing coverage, 503/503
identity-safe/action-safe price coverage, SPY through the same close, and macro
releases through 2026-07-29. Research-data readiness passes with zero hard
database failures and three visible historical-audit warnings. Active trial
`us-qv-forward-72c4560a442d` has one registered proposal, zero executions, and
intact forward-policy evidence; recording remains timing-gated and explicitly
supervised.

`aios.risk.policy` is a deterministic, jurisdiction-neutral pre-trade contract.
It rejects malformed or duplicate targets, short/leverage exposure, too few or
too many positions, single-name and sector concentration, excessive one-way
turnover, missing/oversized liquidity evidence, and a breached portfolio
drawdown. These conservative defaults are not personalized limits and do not
authorize orders. The local `aios.paper` workflow calls both data readiness and
portfolio risk before it persists a proposal, ties the proposal to hashed
factor/account evidence, and requires a reviewed next-session close plus
explicit `--confirm-simulated` before changing simulated holdings. It contains
no broker credential or order path.

---

## DATA ARCHITECTURE (how the pieces connect)

```
                         ┌──────────────────────────────────┐
                         │     FREE DATA SOURCES (web)      │
   SEC EDGAR XBRL ───────┤  yfinance ─── Stooq (fallback)   │
   (fundamentals)        │  FRED ─── US Treasury / BLS       │
                         └──────────────┬───────────────────┘
                                        │  rate-limited HTTP
                                        ▼
                         ┌──────────────────────────────────┐
                         │      INGEST LAYER (Python)       │
                         │  edgar.py  prices.py  fred.py     │
                         │  http_client.py (10 req/s, retry) │
                         └──────────────┬───────────────────┘
                                        │  validated, typed rows
                                        ▼
                         ┌──────────────────────────────────┐
                         │   STORAGE (point-in-time safe)   │
                         │  DuckDB (.duckdb file) ← primary  │
                         │  Parquet (archive / snapshots)    │
                         │  ingest_log (audit trail)         │
                         └──────────────┬───────────────────┘
                                        │
              ┌─────────────────────────┼─────────────────────────┐
              ▼                         ▼                         ▼
   ┌──────────────────┐    ┌────────────────────┐    ┌─────────────────────┐
   │  FACTOR ENGINE    │    │  BACKTEST ENGINE   │    │  LLM SYNTHESIS      │
   │  (polars/numpy)   │    │  (vectorbt-style)  │    │  (Claude / GLM-5.2) │
   │  Quality/Value/   │    │  costs+tax aware   │    │  reports/explain    │
   │  Momentum/LowVol  │    │                    │ │  (reserved for later)│
   └──────────────────┘    └────────────────────┘    └─────────────────────┘
```

**Key invariant — Point-in-Time (PIT) correctness:**
Every fundamentals row carries an `as_of_date` (the date the data was *knowable*, i.e. filing/report date, NOT the period it describes). Every factor computation and backtest joins on "what was known as of date X." This is the #1 source of fake alpha in retail quant projects, and we design it out from row one.

`period_end` must also be no later than `as_of_date`. SEC Company Facts can
contain internally inconsistent observations; extraction and storage reject
those rows, validation treats any active copy as a hard failure, and the narrow
`quarantine-invalid-fundamentals` command moves legacy copies intact to
`fundamentals_quarantine` instead of destroying their provenance.

Filing time alone is not enough for reproducibility: a fact with an old filing
date can be accepted by AIOS after an earlier decision was made. The current
`fundamentals` relation is therefore paired with append-only
`fundamental_versions`. Each accepted upsert first resolves the current
projection and then appends that exact post-merge row in the same DuckDB
transaction. Governed cleanup appends a deletion tombstone before removing a
projection row, and duplicate incoming economic keys fail atomically. A named
`fundamental_evidence_generation` captures an immutable maximum version
sequence. Scalar and universe-batch factor reads can bind to that generation,
so later inserts and same-key corrections cannot alter the earlier result.
Legacy projections are seeded transactionally and marked in
`schema_migrations`; an unmarked partial history or any difference between the
latest versions and current projection is a hard integrity failure. This v1
boundary covers fundamental rows only. Its issuer/security routing still uses
the mutable reference projection. Price, identity, membership, macro, and
policy generations must be bound before the mechanism is activated for a new
governed paper policy.

### Macro release/vintage contract

Macro data has revision risk, so `macro` is versioned by both observation and
public availability:

| Field | Meaning |
|---|---|
| `series_id` | FRED or fallback series identifier |
| `date` | Economic observation period |
| `release_date` | Date that this vintage became public |
| `value` | Value for that vintage |
| `source` | Provider provenance (`fred`, `treasury`, or `legacy_unversioned`) |

`Store.pit_macro_history()` first filters `release_date <= decision_date` and
`date <= decision_date`, then selects the newest eligible vintage for each
observation date. The regime layer never reads the table directly. Rows from
the pre-vintage schema are retained in `macro_legacy` and copied with a NULL
release date for auditability, but they are excluded from PIT reads. New macro
upserts reject missing release dates. After a complete replacement ingest,
`cleanup-legacy-macro` removes only the active-table copies when every
observation has a release-aware replacement; `macro_legacy` remains as the
audit copy.

FRED's XML/JSON observation endpoint caps one request at 2,000 vintage dates.
The adapter obtains each series' actual vintage-date list, fetches non-
overlapping chunks of at most 1,900 dates, deduplicates by observation/vintage,
and retries transient truncated responses. After the initial backfill, a
31-day overlap provides an efficient incremental refresh while retaining
idempotence.

`aios.macro.regime.compute_regime()` classifies growth, CPI year-over-year
inflation, the 10Y–2Y curve, VIX, and Baa/10Y credit stress into an
interpretable regime. Missing mandatory evidence produces `unknown` with an
explicit missing list. `aios.factors.policy` maps each known regime to a
normalized Quality/Value blend, and `compute_composite()` applies that blend
only when the snapshot is PIT-ready for the same decision date. Otherwise it
uses the baseline 60/40 weights and marks the result
`macro_regime_pit_unavailable`. These starting tilts are policy hypotheses,
not backtest-validated alpha. The release-aware refresh and audit completed on
2026-07-16. Treasury's daily CSV does not expose a separate release timestamp,
so its fallback rows use the observation date under the system's
after-close/next-session decision convention. FRED observations normally
receive a later vintage date. Macro selection therefore ranks the greatest
public release date first and uses FRED precedence only when release dates tie;
this lets the official Treasury close fill a current yield without backdating
knowledge. A same-observation-date FRED/Treasury difference above five basis
points is a visible data-quality warning.

### Historical universe and membership PIT contract

`universe_membership` is the backtest universe boundary. Each row stores a
half-open effective interval `[effective_start, effective_end)`, the date its
start became public (`known_date`), the independently public date of a finite
end (`end_known_date`), and source provenance. `Store.universe_membership_on()`
uses one date for both knowledge and effect. The backtest instead calls
`universe_membership_known_on(universe, decision, execution)`: starts and ends
must be known at the decision close, while membership must be effective when
the scheduled trade occurs. It is not valid to replace either knowledge date
with an effective date.

`aios import-universe path.csv` is the controlled input. The importer rejects
missing known dates, invalid intervals, and unsupported columns; the storage
layer rejects overlapping intervals and records an ingest audit row.
`aios build-universe-membership` is the preceding provenance gate: it creates a
bounded baseline, replays announced additions/deletions as a state machine,
supports re-entry, reconciles every event identity against an independent span
source, and refuses missing or contradictory boundaries. Small official event
manifests live under `examples/`; bulk source/output data stays gitignored.
Finite intervals without `end_known_date` are a hard data-quality failure.

The original audited S&P 500 manifest covers 2023-08-01 through 2024-12-31.
Reviewed event/reference batches extend the current operating path through
2026-07-21. Immutable no-change attestations can roll that edge forward one
reviewed close at a time; two accepted attestations currently extend it through
2026-07-23. Each attestation links exact raw responses and records source/set
hashes, candidate headlines, CIK-lineage checks, and per-table update counts.
The transaction covers `universe_membership`,
`security_identity_assignments`, `security_issuer_assignments`,
`issuer_cik_history`, and `provider_symbol_history`, so a partial reference
extension is impossible. XOM demonstrates why CIK lineage—not naive current-CIK
equality—is required: the stable security has an officially reviewed
predecessor/successor issuer transition, while the secondary component file
still names the predecessor CIK.

Issuer announcements identify same-security ticker transitions; S&P Global
releases identify index decisions. Three one-to-two-day conflicts in the free
reference spans are retained as explicit warnings and resolved to official
release dates. See `SP500_DATA_PROVENANCE.md`. Neither slice claims a complete
1996-present announcement archive or investable performance history.

### Stable security identity contract

A ticker is a dated market label, not a permanent primary key. The internal
`security_master.security_id` identifies one listed security across a verified
ticker change. `security_identity_assignments` preserves the interval,
knowable date, evidence source, and confidence status used to attach that ID to
each `universe_membership` row. Existing databases receive the nullable link
through an additive migration; the identity import then fills it
transactionally without rewriting prices or fundamentals.

`aios build-security-identities` defaults each otherwise-unlinked ticker to an
explicitly bounded ID and only joins tickers listed in the reviewed transition
manifest. The bounded IDs are useful inside the certified window but are not
CUSIPs, FIGIs, or permanent global identifiers. The importer requires an exact
membership interval match, rejects remapping an existing interval, and rejects
overlapping ticker assignments for one security. Missing, orphaned, mismatched,
future-known, or overlapping identities are hard validation failures.

The current evidence joins ABC→COR, CDAY→DAY, FLT→CPAY, and Healthpeak's
PEAK→post-merger DOC. The pre-merger security that previously used DOC is not
joined to Healthpeak. WRK→SW is also not joined: Smurfit Westrock is a new
combined parent and WestRock holders received new-parent shares plus cash.

### Issuer and provider identity contract

The first reviewed issuer/provider layer is implemented. `issuer_master` and
`issuer_cik_history` identify the legal SEC filer; CIK is an issuer identifier
and must never be treated as a share-class/security ID.
`security_issuer_assignments` records dated ownership, while
`provider_symbol_history` records bounded symbols separately for each data
provider. Reference imports are atomic and reject invalid dates, overlapping
ownership, provider-symbol reuse, future verification dates, and provenance
conflicts.

Fundamental rows are tagged with `issuer_id` and optionally `security_id`;
factor reads follow the reviewed issuer across ticker changes. Price rows are
tagged with `security_id` and `provider_symbol`; ingestion clips every response
to its verified provider interval and relabels each row to the market ticker
valid on that date. `verified`, `unavailable`, and
`blocked_wrong_security` are explicit mapping states. The latter two must not
trigger an alias guess or cross-provider fallback.

The first controlled corporate-action manifest covers six issuers/CIKs and six
security-owner intervals, with eight yfinance intervals. It safely joins
ABC→COR and FLT→CPAY price history, permits Healthpeak `DOC` only from
2024-03-04, and permits Smurfit Westrock `SW` only from 2024-07-08. DAY and
retired WRK history remain explicitly unavailable from the reviewed provider.

### Held-security conversion and liquidation contract

Universe membership and portfolio ownership are separate. A security may leave
an index while an already-held position still needs a priced exit. The engine
must not solve this by extending membership or reintroducing factor eligibility.

`security_conversions` records a reviewed identity-changing share event with
source/target security IDs, effective and known dates, share ratio, basis
policy, and separate event/basis evidence. The first event is HES→CVX on
2025-07-18 at 1.025 CVX shares per HES share. The portfolio transfers quantity,
acquisition date, and carry-over basis and records a conversion audit row.

`security_ticker_extensions` is the narrow alternative for a removed security
that remains listed. It can extend only from an exact prior identity/provider
end anchor, for at most 45 calendar days, with complete scheduled sessions,
reviewed actions/split basis, HTTPS sources, and a canonical payload hash. The
extension is visible to held-position pricing only; membership and factor reads
remain unchanged. `aios validate` recomputes the payload hash so a later price
overwrite cannot silently change accepted backtest evidence.

The second reviewed manifest adds 25 unchanged full-window securities. It was
generated by `aios build-reference-batch`, accepted 25/25 candidates, and was
ingested with `aios ingest-reference-batch`. It brought the aggregate reference
layer to 31 issuer/CIK intervals, 31 dated owner assignments, and 33
provider-symbol intervals.

The third reviewed manifest accepted 24/25 unchanged full-window candidates.
ANSS failed closed because the retired ticker has no record in the SEC current
ticker map after its acquisition; it remains in the review CSV and has no
issuer, owner, provider, fundamental, or price rows from this batch. The 24
accepted names bring the aggregate reference layer to 55 issuer/CIK intervals,
55 dated owner assignments, and 57 provider-symbol intervals.

The fourth reviewed manifest accepted 19/25 candidates. BF.B and BRK.B failed
the exact SEC ticker-map check, BK also had no exact current-map record, and BG,
BLK, and C lacked submissions filing continuity back to the certified window
start. Those six stayed evidence-backed rejections rather than guessed aliases
or CIKs until the separate exception review. The 19 accepted names brought the
aggregate reference layer to 74
issuer/CIK intervals, 74 dated owner assignments, and 76 provider-symbol
intervals.

The first exception manifest revisits ANSS plus the six Batch 04 rejections
with primary evidence appropriate to each failure. Exact SEC dot/hyphen
notation resolves BF.B and BRK.B; Citi's SEC-named older submissions shard
restores filing continuity; official 8-Ks create dated issuer handoffs for BG
and BLK; and BK's 2023 10-K plus the issuer's 2026 BK→BNY release establishes a
bounded provider relabel. Six securities have verified 358-session price paths.
ANSS has a verified historical CIK/owner and PIT fundamentals, but Yahoo and
Stooq returned zero rows, so both mappings remain explicitly `unavailable`.

The fifth stable manifest accepted 25/25 candidates. Five legacy-baseline names
were upgraded from the ticker fallback to reviewed issuer/provider identities,
and 20 additional members gained complete inputs. Together these manifests
brought the reference layer to 108 issuer/CIK intervals, 108 dated owner
assignments, and 109 provider-symbol intervals.

The sixth stable manifest accepted 23/25 candidates and left CTRA and DFS in
its review CSV because both retired tickers had disappeared from SEC's current
ticker map after later mergers. Exception Batch 02 then used each issuer's
official 2023 10-K plus later merger/delisting 8-K evidence and explicit Tiingo
histories. ANSS, CTRA, and DFS each returned the complete 358-session certified
window. Subsequent reviewed Batches 07–20, Window Batch 01, and Exception
Batches 03–10 complete the identity review for the certified 2023-08 through
2024-12 window. The live reference layer now has 528 issuer/CIK intervals, 531
dated owner assignments, and 534 provider-symbol intervals. Every one of the
533 membership-assignment spans overlaps a reviewed owner interval. Dated
complete-input coverage is 501/503 on 2023-09-29 and 503/504 on 2024-09-30.
The remaining gaps are explicit rather than unresolved: pre-change PEAK and
retired WRK have no safe historical provider response after Yahoo, Tiingo, and
Stooq checks, while AMTM did not publish Company Facts until 2024-12-17 and
therefore has no facts that can legally be backdated to 2024-09-30.

The full-window builder remains the efficient path for unchanged securities.
The separate `build-reference-window-batch` command accepts strict
`ticker,start,end` rows for shorter independent spans, runs the same fail-closed
SEC/provider checks per span, preserves every rejection, and merges only
non-conflicting accepted intervals. Window Batch 01 adjudicated all 53 shorter
spans: 43 passed automatically, six retired-current-map cases passed primary
SEC plus Tiingo review in Exception Batch 09, and both CDAY and DAY segments
passed a same-security relabel review in Exception Batch 10. PEAK and WRK are
the two terminal provider exceptions.

The full-window batch builder is deliberately narrower than the storage model.
A candidate is accepted automatically only when one ticker/security assignment
covers the entire certified window and the SEC ticker file has exactly one
matching CIK. The issuer's official submissions record must confirm that
CIK/ticker pair, and the
submissions filing history reaches both sides of the certified window. The
builder follows only older shard filenames supplied by that official
submissions record and allows only the exact market-dot/SEC-hyphen share-class
notation transform. `formerNames` contains issuer names and is never treated as
ticker history. The provider must return unique, positive, sufficiently dense
rows that reach both window boundaries. Every returned date must relabel to
exactly one dated market ticker. The review artifact retains
acceptance/rejection reasons and SHA-256 fingerprints of the SEC submissions
payload and provider sample. Ticker
transitions, mergers, share classes, retired securities, ambiguous SEC records,
new CIKs without historical continuity, and thin provider histories fail closed
and require an exception manifest.

The provider fingerprint is a review-time drift detector, not a claim that a
free provider never changes. A later ingest may observe provider corrections.
The immutable snapshot layer retains exact SEC Company Facts, Submissions,
company-ticker-map, Treasury and Tiingo bytes with request/response timestamps,
secret-free request fingerprints, content hashes, adapter/parser versions, and
ingest-run links. Reviewed parsers attach and replay canonical row hashes only
after a successful identity-safe parse. yfinance and FRED normalized replay are
also live and are never presented as original vendor HTTP bodies. Stooq's
capture-before-parse path is tested to retain an HTML verification response as
capture-only evidence during an ingest and never promote it.
Historical GitHub/S&P reference captures created before replay parsers remain
byte-verifiable but honestly labeled as non-replayable legacy evidence. Every
future production adapter, including NSE transports, must add its own
capture/replay contract before crossing the corresponding readiness gate.
The shared HTTP boundary forces `Accept-Encoding: identity`; provider bytes are
bounded before capture and compressed deterministically by AIOS. Current bulk
refreshes also open a per-source circuit after three consecutive transport
failures, preserving attempted failures while refusing hundreds of identical
retries; independent refresh areas may still continue.

`ingest-reference-batch --companyfacts-zip` is a reserved, fail-closed
interface. Supplying it currently refuses the operation before reference import
because bulk archive members do not yet have governed, immutable, run-bound
source lineage. Reviewed batch ingestion therefore uses the per-CIK Company
Facts capture path and the separate Submissions metadata request.

`companyfacts-v3-plan --as-of YYYY-MM-DD` is a separate read-only planner over
already-captured exact Company Facts v2 responses. It bounds reviewed identities
and observations to the decision date, verifies accepted issuer-scoped ingest
evidence and the current issuer relation, replays the archived v2 parse and the
candidate v3 parser, and classifies each issuer without provider access. By
default it publishes nothing; `--write-plan` creates only a content-addressed
review artifact under `data/reports/companyfacts_replays/plans`. No v3
activation path exists in this build, and the planner cannot mutate DuckDB, raw
snapshots, paper state, or broker state.

Reference import remains one atomic transaction. Data ingestion is isolated per
accepted issuer and security so a network/provider failure cannot hide or roll
back successful peers; each failure is reported and logged independently. A
full issuer refresh must preserve every existing storage key for that
`issuer_id`; an omitted key fails and rolls back the transaction. No existing
fact is silently deleted as an obsolete ticker. A future shrinking replacement
path must add explicit confirmation, backup, and compare-and-set evidence.

`aios universe-coverage` reports member-level availability using the dated
membership ticker and stable IDs. For reviewed identities it only counts
issuer-tagged fundamentals and security-tagged prices; unreviewed names retain
the legacy ticker path until their reference manifests are added. This makes
partial migration visible without allowing old-DOC or pre-combination-SW
contamination.

### Factor snapshot and cache contract

Quality and Value share repeated PIT inputs, but a persistent application cache
would be unsafe after an ingest or identity correction. `compute_composite`
therefore opens one decision-scoped `FactorDataCache` and discards it before
returning:

- the cache is bound to one `Store` instance and never crosses a decision;
- each ticker/date loads all required factor fundamentals through one
  `pit_factor_fundamentals` query, which uses the existing reviewed
  security-to-issuer routing and fails closed across owner gaps;
- histories are deduplicated by metric/period end using the latest filing known
  on that decision date; latest instant values preserve the original
  `as_of_date`, then `period_end`, ordering;
- Quality and Value reuse the same rows, TTM values, prices, and SIC
  classification without changing their public APIs; and
- a new factor call after an ingest creates a fresh scope, so revised filings
  become visible without manual cache invalidation.

The interactive Research path adds a storage optimization underneath this
unchanged factor contract. `DecisionScopedFactorStore` exposes the same scalar
methods to `FactorDataCache`, but serves them from set-based Store reads for the
requested universe:

- fundamental history is batched separately for each requested decision date,
  including the prior-year dates used by Quality;
- latest prices and QVML price histories preserve dated security identity,
  reviewed owner/provider gaps, source priority, and deterministic tie order;
- every batch must return exactly the normalized requested ticker set before it
  can be reused;
- an exception or malformed result falls back to the existing scalar method;
  ambiguity is never converted into empty evidence; and
- the facade exists only inside one dashboard loader call and never persists a
  score, filing, route, or price window.

The active forward trial hashes `factors/common.py` and `factors/composite.py`.
This optimization deliberately leaves both files unchanged: it changes how the
interactive read-only Store contract is fulfilled, not the scoring policy.
Fresh-process parity checks on the certified 2026-07-27 universe produced
identical 503-row QV and QVML payloads. QV calculation time fell from 25.5 to
2.9 seconds, and QVML from 28.5 to 3.4 seconds.

The 2023-09-29 503-member profile dropped from 42,235 DuckDB queries and 174.1
seconds to 3,874 queries and 17.4 seconds. The first optimized five-decision
bounded run completed in about 92 seconds and exactly matched its pre-cache
payload. The later schema-v3 run completed in about 77 seconds, but its Value
rankings were subsequently superseded by the split/share-basis correction
described below. Performance changes are allowed only when an explicitly
versioned data or accounting correction explains them.

### Contemporaneous price basis and factor warm-up contract

Yahoo retrospectively split-normalizes historical `Close`. SEC shares and
per-share facts remain on the basis known at their filing date. Combining those
inputs directly understates pre-split market capitalization and distorts Value.
Each reviewed Yahoo row therefore stores `split_normalization_factor`, the
cumulative split ratio after that row through `split_normalization_through`.
Value restores `Close × split_normalization_factor`; return paths continue to
use Yahoo's internally consistent normalized close and never apply the same
split twice. Unknown factors fail validation.

Momentum and Low Volatility also need observations before the bounded universe
window. That history cannot justify backdating a market ticker. The separate
`factor_price_provenance` and `factor_prices` tables therefore enforce:

- warm-up rows carry immutable `security_id`, provider, and provider symbol,
  but intentionally no ticker;
- each snapshot ends exactly at the start of an existing verified provider
  mapping and compares at least five fresh overlap sessions with stored reviewed
  `Close`, dividends, splits, basis, and normalization factor;
- Yahoo's daily split scan-through date is retained as provenance but is not an
  identity equality field. Cache reuse rechecks the hashed economic overlap
  against current stored rows, so a real basis change still fails closed;
- retrospective `Adj Close` is retained in the snapshot hash but is not an
  equality gate because later dividends legitimately revise it and QVML does
  not consume it;
- blocked/wrong-security predecessor intervals and insufficient listing history
  remain explicit rejections;
- accepted compressed snapshots and overlap evidence are hashed, resumable, and
  imported atomically; and
- factor reads may union this security-level history only when the decision date
  itself has an active verified mapping for that security. Provider gaps still
  fail closed.

The v3 review completed on 2026-07-21: 520/528 identities passed and 122,466
security-keyed rows were imported. AMTM, GEHC, GEV, KVUE, SOLV, and VLTO lacked
210 pre-anchor sessions; SW and Healthpeak/DOC were blocked by reviewed
wrong-security/unavailable predecessor intervals. No transient or economic-
overlap rejection remained.

### Market factors and QVML publication contract

Momentum and Low Volatility are additive; QV remains the preserved backtest
default:

- Momentum is 12-minus-1 total return: 253 trading-session observations with
  the most recent 21 sessions skipped;
- Low Volatility is the annualized sample standard deviation of the latest 252
  daily total returns, ranked so lower volatility scores higher;
- both require a latest observation no more than seven calendar days before
  the decision, complete corporate-action fields, and one consistent declared
  split-adjustment basis across the window;
- QVML preserves the regime-relative Q/V tilt inside a 60% core, then adds 25%
  Momentum and 15% Low Volatility; and
- QVML is published only when Q, V, M, and L all exist. Missing factors are
  never reweighted away.

Across the five certified execution universes, valid Momentum/Low-Volatility
counts are 499, 498, 498, 496, and 497; complete QVML counts are 291, 293, 347,
307, and 300. This is coverage evidence, not a QVML performance claim.

### Cost, tax, and benchmark contract

`aios.backtest.portfolio` keeps execution state separate from factor ranking;
`aios.backtest.costs.simulate_period` remains only as the interval-level
compatibility harness:

- positions and FIFO lots are keyed by stable security identity and persist
  across decision dates;
- commission and slippage are charged only on traded rebalance deltas;
- fixed fees are charged once per non-zero order and deducted from book cash;
- provider `close` and explicit dividends drive daily valuation and tax
  accounting; each row declares whether close is already split-normalized, and
  split ratios are applied only when it is not, preventing both omitted and
  double-counted corporate actions;
- buy costs enter lot basis and sell costs reduce proceeds; realized gains use
  FIFO holding periods, while gains/losses net within each short/long bucket
  over the run and dividends accrue tax at the supplied rate;
- net and zero-friction shadow books use the same selection schedule instead of
  approximating gross return by adding costs back; and
- benchmarks are explicit persistent zero-friction books on the same calendar,
  execution dates, provider-basis/action convention, and daily sessions.

Wash sales, cross-bucket offsets, carryforwards, filing timing, and other
jurisdiction-specific rules remain out of scope and must not be implied by the
defaults. A failed period transition is atomic: all four policy books remain at
the previous certified close and later periods stop rather than fabricating a
state bridge. Individual missing daily marks may be carried forward and are
listed as `stale_tickers`; scheduled entry and exit prices remain strict.

The Python API defaults costs and tax rates to zero for reproducible unit-level
diagnostics. The CLI uses 5 bps commission and 5 bps slippage per side, but
keeps tax rates at zero until the operator supplies jurisdiction-appropriate
assumptions. Every result reports gross return, net return, costs, accrued tax,
turnover, order evidence, open-lot counts, and daily equity observations.

### QV policy-validation backtest

`aios backtest-qv` compares the regime-aware QV or opt-in QVML ranking with its
same-date fixed baseline. Each quarter uses only factor, macro, and historical
membership evidence known at the decision date, selects equal-weight top-N
holdings, keeps prior positions invested through the next-session close, and
then trades only the target-weight deltas. Quarter boundaries and every daily
valuation session come from one explicit market-calendar ticker. The target
membership is known at the decision close but effective on that scheduled
entry session; this prevented the September 2024 decision from buying BBWI
after its announced October 1 removal. Price paths
use immutable security IDs, so a reviewed ticker change does not become a false
sale or missing delisting. Historical membership is required by default.
`--factor-model qvml` activates the four-sleeve eligibility and weights; QV
remains the default. `--allow-current-universe` is available only as an
explicitly labeled survivorship-biased diagnostic. Each schema-v4 result records the member-list
hash, raw coverage, factor eligibility/exclusion reasons, macro snapshot,
ranked selections, orders, ending holdings/lots, daily net/gross/benchmark
curves, data-quality report, database hash, and Git state in optional JSON.

The earlier 2015-01-01 through 2026-06-30 current-universe diagnostic completed
45 quarterly periods: regime-aware 29.81% annualized versus fixed 60/40 at
30.79%, before costs and taxes. That result remains an in-sample policy
hypothesis, not an investable performance claim. A new historical-universe run
must be treated as a separate experiment with its source, coverage, cost
assumptions, tax assumptions, and benchmark recorded.

The first bounded historical-membership smoke run was recorded on 2026-07-16:
2023-08-01 through 2024-12-31, the 22 locally covered names, top 10, five
quarterly periods, 5 bps commission and 5 bps slippage per side, zero tax
rates, and SPY. Both policies returned 44.99% net versus SPY at 41.49% because
both selected the same equal-weight holdings in every period. Only 11-12 names
were factor-eligible, so this validates the PIT/cost/benchmark path but does not
validate policy alpha. See `SP500_DATA_PROVENANCE.md` for the exact command and
full caveats.

The interval-v1 bounded rerun was recorded on 2026-07-18 after all reviewed
identity batches and the TTM/financial-factor correction. It preserved the
real 503–504 members and published 291, 293, 350, 308, and 301 eligible names.
PEAK, WRK, and pre-filing AMTM were explicit policy exclusions with member-level
reasons. Five strategy periods and five SPY observations completed on identical
dates. Regime-aware QV returned 74.24% net versus fixed 60/40 at 76.55% and SPY
at 41.49%. It is retained as the forced-liquidation regression baseline.

The provider-basis schema-v3 rerun on 2026-07-20 preserved every selection and
eligibility count while carrying positions/lots and producing 316 aligned daily
observations. Regime-aware returned 74.25% net (75.14% gross), fixed 60/40
returned 76.73% net (77.68% gross), and persistent SPY returned 39.47%. Daily
max drawdowns were -9.87%, -8.25%, and -8.41%; strategy turnover was
5.16x/5.36x. The earlier schema-v2 result is superseded because its yfinance
downloads omitted explicit actions; a subsequent impossible 764.69% diagnostic
was rejected when it exposed double-applied splits on Yahoo's already
split-normalized closes. It was later superseded: the return path was
basis-correct, but Value still combined retrospectively normalized historical
closes with contemporaneous SEC shares for securities that split later. The
stored close-restoration factors now repair that input. No schema-v3 strategy
return is current certification; the replacement schema-v4 QV and QVML audits
below completed that engineering gate.
See `SP500_DATA_PROVENANCE.md` for exact assumptions and evidence.

The matched schema-v4 audits completed on 2026-07-21 after the warm-up and
membership-end knowledge repairs. Both use the same DuckDB SHA-256, five paired
periods, 316 daily observations, 5+5 bps per-side costs, zero taxes, SPY, and
explicit PEAK/WRK/AMTM exclusions. Regime-aware/fixed QV returned 51.12%/50.98%
net with -8.35%/-8.01% max drawdown; regime-aware/fixed QVML returned
24.97%/34.96% with -11.08%/-7.95% drawdown; SPY returned 39.47% with -8.41%
drawdown. This is short in-sample engineering evidence. It does not certify
alpha and it is not a basis for optimizing QVML weights.

The newer 2025-01-01 through 2026-07-20 stateful QV engineering audit completed
all six periods on 2026-07-21 after the reviewed MTCH/PAYC liquidation-only
extensions were imported. It contains 327 exactly aligned regime-aware, fixed,
and SPY observations, no stale strategy points, exact state continuity, the
2025-07-18 HES→CVX conversion, and 2026-04-01 MTCH/PAYC exits. With 5 bps
commission plus 5 bps slippage per side and zero taxes, regime-aware QV returned
31.13% net, fixed QV returned 27.73%, and SPY returned 34.12%; maximum drawdowns
were -14.69%, -14.69%, and -12.05%. This proves the stateful PIT, held-security,
cost, and benchmark paths for the reviewed window. It is short in-sample
engineering evidence, not alpha evidence, an after-tax result, or a basis for
real-money use.

### Open-source design review

The implementation borrows contracts and ideas, not copied code:

- [QuantConnect LEAN](https://github.com/QuantConnect/Lean) informed the
  pluggable-model boundary and the principle that universe events belong in
  the engine's data contract.
- [vectorbt](https://github.com/polakowo/vectorbt/blob/master/vectorbt/portfolio/base.py)
  informed keeping percentage fees, fixed fees, slippage, order evidence, and
  no-lookahead signal timing as separate concepts.
- [QSTrader](https://github.com/quantstart/qstrader) informed event-oriented
  portfolio reporting and explicit benchmark comparison; its alpha-stage
  status is why we kept this repository's engine small and auditable.
- [Historical S&P 500 constituents](https://github.com/hanshof/sp500_constituents)
  is a possible input source for an S&P 500 universe, not an automatic data
  dependency. Its constituent dates still need security-identity mapping,
  announcement/known-date provenance, and coverage QA before ingestion.

---

## WHAT WE DELIBERATELY DO NOT BUILD (yet)

- No real-time/intraday. Daily EOD only.
- No broker API integration or live trading. The local paper account changes
  only through an explicitly confirmed simulated next-session close.
- No multi-agent committee. Numeric models only; LLM synthesizes.
- The current UI is a local Streamlit research control room: `Overview`
  separates research, paper-trial, and operating status before one next action;
  `Research`, contextual `Company Detail`, `Paper Trial`, `Operations`, and
  `Methodology & Sources` provide deliberate drill-downs. It is read-only and
  does not place trades. A richer multi-user web product remains out of scope.
- No paid data feeds in the active build. Norgate Platinum/Diamond is the first
  reviewed upgrade candidate for delisted prices and effective membership, but
  adoption requires explicit user authorization, a Windows pilot, and parity
  checks. It would not replace announcement-date provenance.

---

## STACK SUMMARY (one line each)

| Layer        | Choice                          | Cost |
|--------------|---------------------------------|------|
| Language     | Python 3.12 + `uv`              | $0   |
| Storage      | DuckDB + Parquet                | $0   |
| Scheduling   | systemd timers / cron           | $0   |
| Fundamentals | SEC EDGAR XBRL + SimFin         | $0   |
| Prices       | yfinance + optional Tiingo + Stooq | $0 active |
| Macro        | FRED + US Treasury / BLS        | $0   |
| LLM          | Claude (later) + GLM-5.2        | $0 now |

**Total recurring cost to run this system: $0.** All keys are free. All tools are free/open-source. We pay only if we later choose paid LLM tokens.
