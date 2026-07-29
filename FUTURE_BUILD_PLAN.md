# AIOS Future Build Plan

This document is the execution roadmap after the audited U.S. research beta. It
turns future ideas into ordered engineering gates so a later developer or AI
agent can tell what belongs now, what depends on earlier work, and what must not
be represented as complete.

`ARCHITECTURE.md` remains the design authority. This file controls sequencing
and acceptance. A feature is complete only when its exit gate passes; a screen
or schema alone is not completion.

## Current baseline

AIOS currently supports supervised U.S. research, a fail-closed readiness gate,
a local paper portfolio, point-in-time factor research, a stateful engineering
backtest, checksum-verified backups, a recoverable benchmark-first exact-date
daily workflow, installed local scheduler timers, and a durable independent
local incident/job ledger with systemd failure/recovery capture.
The channel-neutral notification outbox, local no-network delivery proof,
selected fail-closed SMTP adapter, and governed proposal stress-review v1 are
implemented. SMTP activation is deferred at the user's request; when resumed it
still requires private configuration and an owner-confirmed receipt test. AIOS
does not otherwise send external alerts, preserve every original provider
response, ingest Indian-market data, connect to a broker, or make personal
buy/sell decisions.

The live 2026-07-27 operations migration to schema v4 preserved every existing
incident/message record and passed SQLite integrity and foreign-key checks. The
optional SMTP timer files are installed and native-verified but remain disabled;
the three core data/backup timers remain enabled.

The 2026-07-29 live read-only research baseline reaches the 2026-07-28 decision
close: readiness is `READY`, validation has zero failures and three warnings,
and coverage is 503 members, 500/503 PIT filings, and 503/503 reviewed prices.
Raw verification passes for 2,612 payloads and 2,111 parsed replays. The
494-test suite, Ruff, bytecode compilation, and diff check pass. Backup
`backups/aios-20260728T082412Z` passed its non-destructive restore drill.

Predecessor trial `us-qv-forward-8559d86b6a02` is archived unchanged; it was
not re-hashed, backdated, or executed. Active trial
`us-qv-forward-72c4560a442d` started prospectively from the 2026-07-27 close,
has one registered proposal, and has zero executions. The account remains
$100,000 simulated cash with zero holdings and no broker connection. On
2026-07-29 the full read-only paper review passed after the 2026-07-28 close and
provider finalization. It deliberately produced no execution command and no
simulation was recorded; explicit human confirmation remains a separate
decision before the prospective window ends. The 2026-07-29 guarded daily run
passed at 13:12 IST and host verification proved all three user timers active,
so the scheduler-visibility warning was resolved through the normal incident
lifecycle. One non-critical fundamentals-coverage incident remains open for
three SEC issuers that returned no fundamental rows, so unattended operations
are not currently claimed. The next scheduled filings run or a reviewed
bounded retry remains evidence to observe rather than claim in advance.

## Priority decision

| Capability | Decision | Why | Dependency / exit gate |
|---|---|---|---|
| Decision-first dashboard foundation | **Now — implemented** | Makes the local product operable without DuckDB or systemd knowledge | `Today` keeps Research, Paper Trial and Operations scope separate; every displayed fact reconciles to readiness, scheduler, ingest, backup, incident or forward-policy evidence |
| Institutional dashboard visual system | **Now — implemented** | A research control room must be fast to scan and hard to misread at desktop and phone widths | Light evidence canvas, navy shell, one CTA hierarchy, semantic status colors, symmetric grids, four-stage paper governance, selectable ranked rows, origin-aware URL state, rendered desktop/mobile QA |
| Canonical operator preflight | **Now — implemented** | Independent capabilities and one next action prevent a green research gate from hiding paper, operations, or real-capital blockers | Versioned checksum JSON, strict registered-proposal resolution, immutable DuckDB/SQLite reads, timing-only and full-review modes, repeatable capability requirements, no generated state-changing command |
| Cooperative cross-store mutation lease | **Now — implemented for supported CLI workflows** | Backup-covered DuckDB, raw, paper, proposal, and forward mutations must not race | Non-blocking process/thread lease covers refresh, ingest, import, repair, cleanup, backup/restore, paper, and forward CLI boundaries and fails visibly; direct-library callers remain outside the lease |
| Governed artifact publication boundary | **Now — implemented** | A caller-selected output path must never overwrite an account, database, immutable input, backup, or source file | Resolve before work; project artifacts stay under `data/` but outside governed state; refuse symlink/hard-link aliases; atomically publish single files and reviewed batch names without replacement; proposals remain confined to their validated namespace |
| Final forward-library persistence CAS | **Next intentional policy version** | A cooperative CLI lease is not a universal transaction for arbitrary direct-library callers | Add final trial/proposal identity checks at the forward-library persistence boundaries, then begin a new prospective trial; never rewrite the active trial's hashed policy file in place |
| Research Fast Path v1 | **Now — implemented** | A 29–33 second cold screen was the main daily research bottleneck | Dashboard-only decision-scoped batch facade, exact QV/QVML scalar parity, 12-query bound, read-only/temp-relation cleanup tests, serialized cold builds, no persistent score cache |
| Recoverable exact-date U.S. daily workflow | **Now — implemented** | Separate timer stamps did not prove a full run survived logout, and benchmark-last ordering could leave identity windows one session behind | SPY first, universe second, members/macro third, exact-date readiness last; durable job lifecycle, restart, startup catch-up, linger, live 503-member proof |
| Local incident ledger and systemd failure capture | **Now — implemented** | Scheduled and application failures must survive analytical DB lock/open failures | Deduplicated open/repeat/acknowledge/resolve/reopen lifecycle, immutable schema-checked CLI inspection, safe structured service evidence, dashboard history, backup archive |
| Immutable raw-data snapshots | **Now — active U.S. transport gate implemented** | Provider history can change; later anomaly work needs exact ingest evidence | Active SEC, yfinance, FRED, Treasury and reviewed Tiingo paths capture and replay honestly; backup/restore drill passes; every future adapter needs the same gate |
| Channel-neutral notification outbox | **Now — implemented** | Channels are replaceable transports, not the source of incident truth | Atomic incident/message writes, stable idempotency, exclusive leases, bounded retries, dead letters, safe attempt audit, local no-network proof, backup/restore coverage |
| SMTP email delivery adapter | **Implemented; live activation deferred** | The user selected email but deferred setup; destination and credentials still cannot be inferred | Private SMTP config, one exact-route test, owner-confirmed receipt, explicit enable; historical held messages remain quarantined |
| Governed proposal stress review v1 | **Now — implemented** | Proposal risk should be challenged before adding prediction complexity | CLI and Paper Trial use one registered-proposal CAS service; exact PIT evidence and immutable sources bind every result; mark shocks and the statistical proxy stay separate; missing evidence fails closed |
| Data-quality anomaly review cases | **Soon after incident core** | Suspect changes must be reviewed, never silently repaired | Baseline comparisons create deduplicated cases with evidence, severity, owner state, and resolution audit |
| Research experiment registry | **Before new factor experiments** | Prevents accidental cherry-picking and untraceable results | Every run records code/data/policy identity, assumptions, exclusions, metrics, artifacts, and exploratory/frozen/holdout status |
| Role-based configuration boundaries | **Before hosted or India operation** | Operator actions, research policy, account assumptions, secrets, and data have different change controls | Named policy versions are immutable; policy change starts a new forward trial; secrets are never exported |
| India adapter foundation | **Before any NSE/BSE ingest** | Market dimensions cannot be bolted onto U.S.-implicit rows safely | Market, exchange, currency, calendar, settlement, action source, benchmark, security type, ISIN, and provider-symbol intervals are first-class and parity-tested |

## Phase 1 — Reconstructable ingestion and incidents

### 1A. Immutable raw snapshots

Use a content-addressed layout:

```text
data/raw/{provider}/{dataset}/{YYYY-MM-DD}/{sha256}.json.gz
```

The extension may vary for a genuinely non-JSON response, but compression must
not change the hash of the original uncompressed bytes. Write to a temporary
file, verify, then atomically rename.

Store at minimum:

- snapshot ID, provider, dataset, request time, response time, HTTP status and
  content type;
- a secret-free request fingerprint and exact response SHA-256;
- relative snapshot path, byte counts, adapter name/version and parser version;
- parsed row count and canonical parsed-row SHA-256;
- the ingest run ID and the snapshot's role in that ingest.

One ingest can consume multiple payloads, so link snapshots to `ingest_log`
through a relationship table instead of adding one nullable snapshot column.
Never store API keys, authorization headers, cookies, or signed query tokens.

Some libraries return only a DataFrame rather than the original HTTP body. Such
an adapter must either expose the true response at the transport boundary or
label its artifact `normalized_provider_export`; it must not claim exact vendor
reconstruction. Yahoo needs this explicit treatment.

Acceptance tests:

1. identical payloads deduplicate by content hash;
2. changed provider bytes produce a new immutable snapshot;
3. parsing the snapshot reproduces the stored parsed-row hash;
4. deleting or modifying a snapshot fails validation;
5. backup/restore preserves database links and raw files;
6. a failed parse still retains the response and failure metadata.

Implemented foundation on 2026-07-22:

- atomic gzip storage under the content-addressed path;
- separate deduplicated payload and per-fetch observation records;
- adapter/parser versions, parsed-row hash and optional ingest-run links;
- `aios verify-raw-snapshots` tamper detection;
- backup inclusion and forward-only merge on restore.
- an opt-in shared HTTP capture boundary with secret-query redaction that keeps
  malformed response bytes before JSON parsing fails;
- exact-response capture for reviewed SEC Company Facts and Submissions issuer
  ingests, plus the SEC ticker-map route used by the convenience ingest;
- exact-response capture and strict replay for the year-bounded official U.S.
  Treasury CSV fallback/cross-check;
- exact-response capture and strict replay for the SEC company-ticker map and
  reviewed Tiingo EOD route;
- canonical `normalized_provider_export` capture for production yfinance
  ingests. The export retains the provider-returned price/action fields and
  request window, links to the ingest run, records the parsed-row hash, and is
  replayed by `aios verify-raw-snapshots`;
- the first live yfinance proof: a bounded reviewed AAPL ingest stored three
  parsed rows in a 792-byte normalized export, linked it to the successful
  three-row ingest, and reproduced parsed hash
  `517ebc8074c0290bcc2b9bd642b50690843b3b933a63469989b9e77b4d2abf7d`;
- canonical normalized FRED vintage exports with secret-free request
  fingerprints, release-date-aware replay, and ingest links. The first live GDP
  proof stored 318 parsed vintages in a 22,127-byte export and reproduced hash
  `2c808a74f9ed571ff0d5c670dcecec1be80a1efbfd0bf187733f614e6a159f9b`; and
- a replay-aware exact SEC lifecycle: Company Facts and Submissions are first
  retained as capture-only evidence, then atomically promoted only after a
  successful identity-safe parse. Missing, multiply linked, partial, or
  conflicting evidence fails closed;
- a live reviewed AAPL issuer proof: two linked SEC payloads, 3,913,076 original
  bytes, 4,102 Company Facts rows plus one Submissions row, with both parsed
  hashes reproduced from the exact compressed bytes; and
- a July 24 full-universe recovery that expanded live coverage to 505 yfinance
  exports and, after the July 25 macro proof, 23 FRED exports;
- live exact-response proofs for 423 Treasury yield rows, 10,429 SEC ticker-map
  rows, and four reviewed Tiingo price sessions. Treasury and FRED exactly
  matched on their latest common DGS2/DGS10/DGS30 observation; and
- a non-destructive restore drill that restored a verified 1,542-file backup
  through the real confirmed-restore path in disposable storage, opened and
  validated DuckDB, checked all 1,535 payloads, replayed 535 parsed artifacts,
  and proved the live files were untouched.

The active U.S. transport gate is complete. Stooq currently returns a
JavaScript verification page and remains explicitly unavailable; its response
is retained as capture-only evidence when the route runs under an ingest but
cannot be promoted to parsed price data. Historical GitHub/S&P
captures created before replay support remain byte-verifiable legacy evidence.
Every new production transport—including future NSE adapters—must add and
live-prove its own registered replay parser before use.

### 1B. Incident ledger and notification outbox

Implemented locally:

- permission-restricted SQLite `incidents` and append-only `incident_events`;
- stable deduplication, severity escalation, acknowledgement, resolution and reopening;
- systemd `OnFailure=` capture and post-success recovery for every managed service;
- bounded, allow-listed systemd result properties with no raw journal/environment capture;
- application incidents for strict health/readiness, refresh degradation, backup failure,
  scheduler runtime uncertainty and forward-policy drift;
- durable `running`, `success`, `failed`, and `interrupted` job lifecycle
  records independent of DuckDB, with live-owner overlap protection;
- CLI test/list/show/acknowledge/resolve commands and source-backed System Health history;
- checksum-manifest backup archive. Analytical restore deliberately does not roll the
  live incident ledger backward;
- schema-v4 `notification_outbox` with immutable message content, stable
  idempotency keys, incident-event linkage, immutable route activation and exact
  recovery dependency, exact state counts, exclusive five-minute leases, five
  bounded pre-send attempts, deterministic backoff and dead letters;
- append-only `notification_deliveries` with explicit success, temporary,
  permanent, ambiguous and abandoned outcomes. Ambiguous or lease-expired
  outcomes stop for review instead of retrying. Only allow-listed provider
  metadata is retained; raw responses and secrets are refused;
- atomic incident transition plus message creation for open, escalation,
  reopening, and recovery. Ordinary repeats do not produce message noise;
- migration with no historical-message backfill and a persistent local-only
  policy for diagnostic incidents;
- read-only dashboard/CLI inspection and a deterministic no-network channel
  that live-proves enqueue, claim, attempt and completion; and
- verified backup plus non-destructive restore-drill coverage for the new
  tables and attempt history;
- a one-recipient TLS-only SMTP adapter, deterministic Message-ID, safe error
  taxonomy, exact-config receipt gate, reconfiguration quarantine, and a
  separately activated optional timer.

Still pending for live email:

- private SMTP host/account/token/sender/recipient configuration;
- one real test accepted and visibly received by the owner, followed by explicit
  enablement. Historical held messages are never released.

Initial incident rules:

- refresh or timer failure;
- readiness transition from ready to blocked;
- stale prices, macro, filings or membership;
- backup verification failure;
- database checksum or forward-policy drift;
- unexpected factor-coverage drop.

Use bounded retry with backoff, deduplicate repeated symptoms, and send a
recovery notification only through an explicitly enabled channel policy.
Dashboard state and incidents must remain available when all external channels
are down.

Channel order: use the implemented local test path first, then the selected SMTP
adapter, then optional mobile push. Secrets stay in environment
or deployment secret storage. No channel credential belongs in DuckDB, a raw
snapshot, an experiment artifact, or Git.

## Phase 2 — Deterministic portfolio stress testing

**Proposal Stress Review v1 is implemented.** `aios stress-review --proposal
...` accepts only a registered, checksum-valid simulation proposal under an
unchanged forward trial. The production service shared by the CLI and Paper
Trial panel checks governance before opening DuckDB, uses a read-only
connection, and repeats the trial, account, proposal, and calculation-source
compare-and-swap checks before returning or publishing. The scenario policy is
also reloaded after calculation and at the final boundary, so concurrent bundle
changes fail closed. No artifact is stored by default; `--output PATH` is an
explicit write-once export.

Scenarios are named, versioned input policies rather than editable UI sliders
with no audit trail. The v1 bundle contains:

- 2008-style broad equity drawdown;
- the Federal Reserve 2026 supervisory severely adverse calibration: an
  [approximately 58% hypothetical equity-price
  decline](https://www.federalreserve.gov/publications/2026-stress-test-scenarios.htm),
  explicitly not a forecast;
- 2020 liquidity shock;
- 2022 inflation/rate shock;
- sector-specific crashes;
- volatility doubling and correlation convergence;
- the five largest proposal targets falling 20%, 30% and 40%;
- price- and revenue-evidence withholding demonstrations.

For each deterministic mark-shock result, the report includes:

- proposal-target and modeled proposal-portfolio loss;
- sector contribution and modeled post-shock concentration comparisons;
- modeled exit capacity and liquidity-horizon comparisons;
- assumptions, calibration basis, and an illustrative recovery path explicitly
  marked as unused in the loss calculation; and
- proposal/evidence hashes, scenario version, source-file bundle, and Git commit.

Exact PIT security identity, action-safe price history, the row-level liquidity
window, and release-aware revenue evidence are independently bound to each
target. Missing or mismatched evidence withholds its dependent numerical result;
unaffected calculations may appear only in a report visibly marked partial.

The volatility/correlation scenario is reported separately as a statistical
loss proxy with Euler risk contributions. It does not manufacture position
returns, stressed holdings, drawdown, concentration, or liquidation outcomes.
Generic sandbox-limit comparisons are human-readable advisory findings, never
automatic proposal approvals or rejections.

Historical labels describe calibration, not a claim that the next crisis will
behave identically. All scenario calculations are deterministic given identical
validated inputs, but the statistical proxy is not a deterministic market path
or price forecast.

Still future:

- owner-approved risk limits in place of generic sandbox references;
- a separate stress workflow for current simulated holdings after fills exist;
- multifactor and Monte Carlo risk models with independently validated numerical
  and provenance contracts; and
- owner-authored scenario policies, accepted only through immutable versioning
  and the same fail-closed evidence boundary.

## Phase 3 — Data review and research governance

### 3A. Anomaly review cases

Detect, but never silently correct:

- sudden share-count changes and impossible valuation jumps;
- duplicate or conflicting filings;
- abnormal price gaps and split/dividend mismatches;
- changed ticker/provider mappings;
- factor percentile jumps;
- coverage deterioration versus the previous comparable run.

Each detection creates or updates a review case containing old/new values,
source snapshots, rule version and suggested operator checks. Resolution must be
explicitly `accepted`, `source_corrected`, `mapping_corrected`, `false_positive`
or `deferred`, with an audit note.

### 3B. Experiment registry

Register every backtest and factor experiment with:

- experiment/run ID and purpose (`exploratory`, `frozen`, or `holdout`);
- Git commit and dirty-worktree fingerprint;
- database snapshot hash and raw-snapshot set/manifests;
- market profile, factor policy and risk policy versions;
- parameters, exclusions, costs and tax assumptions;
- metrics and artifact paths;
- parent experiment and comparison reason.

Frozen and holdout artifacts are append-only. The UI may compare compatible
runs but must not silently choose the best result.

## Phase 4 — Configuration boundaries and India foundation

The detailed, source-reviewed execution sequence is in
[`INDIA_BUILD_PLAN.md`](./INDIA_BUILD_PLAN.md). That plan starts with Nifty 50,
uses NSE as the primary venue and BSE as a first-class identity/cross-check
venue, and contains an explicit free-versus-paid historical-data gate.

Separate five configuration domains:

1. operator configuration: scheduler, retention, notification routing;
2. research policy: factor definitions, rebalance and risk constraints;
3. market profile: sources, sessions, benchmark and corporate actions;
4. account policy: capital, costs, taxes and settlement assumptions;
5. secrets: credentials and tokens, stored outside versioned policy artifacts.

Research, risk, market and account policies receive immutable names and
versions. A material change creates a new forward trial instead of modifying an
existing freeze.

Before the first Indian row is ingested, make these dimensions first-class:

- market and exchange;
- trading currency;
- trading calendar/time zone and session boundaries;
- settlement convention;
- corporate-action source;
- benchmark;
- security type and ISIN;
- date-bounded provider symbols.

Then add Indian adapters in small reviewed slices: identity and symbols,
historical membership, prices/actions, filings/fundamentals, benchmarks,
calendar/settlement, macro, fees/taxes, and finally U.S.-versus-India contract
parity tests. Do not fork the factor, portfolio or risk core merely to add India.

## Dashboard product rules

The supplied dashboard POC is a useful information-architecture reference, not
a data source or implementation specification.

Use now:

- a `Today` view with separately scoped Research, Paper Trial, and Operations
  status, followed by one highest-priority safe action;
- compact status cards and a segmented research-view switch informed by
  CopyUI examples plus Carbon, Cloudscape, and Atlassian dashboard conventions,
  implemented with native Streamlit rather than a second frontend dependency;
- URL-backed workspace, research date, scoring method, research surface, and
  selected company so one link preserves the state needed to reproduce a bug;
- a dedicated `System Health` workspace;
- coverage charts, source freshness, scheduler outcomes and next human action;
- source-backed local incident history and unresolved counts;
- exact local outbox, SMTP route, configuration-match, and optional-timer state;
- full reviewed company name together with the market symbol;
- progressive disclosure: plain language first, technical evidence on demand.
- one light institutional visual system with a navy shell, blue interaction,
  semantic status colors, symmetric CTA/card geometry, and responsive stacking;
- a selectable ranked row that opens the corresponding Company Detail view;
- a fixed four-stage Paper Trial path that keeps proposal, forward policy,
  timing review, and local record visibly distinct.

Completed performance gate on 2026-07-29:

- the cold Research path now serves the existing scalar factor contract through
  decision-scoped universe batch reads. Dated identity routing, issuer
  ownership, restatements, provider gaps, source priority, ambiguity checks,
  and scalar fail-closed fallback remain explicit;
- fresh-process 503-company checks matched every serialized QV and QVML row
  exactly. QV improved from 29.0 to 2.8 seconds and QVML from 32.5 to 3.3
  seconds, while Store query calls fell from 4,278 to 12;
- the optimization does not persist scores or evidence and leaves the active
  forward trial's frozen factor-policy files unchanged. Peak working sets were
  about 1.10/1.19 GiB, so cold builds are serialized.

Next UI gates:

- keep the Research fast path behind scalar-equivalence, bounded-query,
  temporary-relation cleanup, read-only, peak-memory, and serialized-cold-build
  regression gates;
- split the remaining Streamlit monolith by view only after the pure status
  model and rendered characterization tests protect current behavior.
- repeat desktop and phone visual QA when Streamlit is upgraded; the UI now
  relies on tested 1.58 widget and keyed-container contracts.

Do not add React, Tailwind, animation packages, or a separate frontend service
merely for visual polish. Reconsider that boundary only after a versioned
application API plus multi-user authentication, roles, mobile workflows, or
real-time interaction becomes a real product requirement.

Use only after the underlying feature exists:

- external delivery receipts and channel-specific controls;
- current-holdings and advanced-risk panels beyond the implemented proposal
  stress panel;
- experiment comparisons and exports;
- event calendars;
- sector allocation and top-holdings graphics after simulated holdings exist.

Do not copy:

- invented 503/503 coverage, returns, holdings, risk metrics or alerts;
- a green `READY` label that bypasses fail-closed checks;
- a chart that mixes backtest results with a current paper account;
- dense navigation entries with no supported workflow behind them.

## Explicitly deferred

Do not implement broker integration, live orders, autonomous policy changes,
additional factor-weight tuning, or performance marketing while establishing
the replacement U.S. forward trial. Controlled-capital discussion remains gated
by at least 8–12 weeks of untouched monitoring plus naturally triggered operations,
external alerts, raw-data versioning, broker reconciliation, and user-approved
jurisdiction/account/risk policies.

## Definition of product-ready

“Ready to use” must always name the use case:

- **Supervised research:** readiness and validation pass for the exact decision
  date; already available for the current U.S. reference market.
- **Local paper monitoring:** the scheduled close is reviewed and simulation is
  explicitly confirmed; already supported, with no broker connection.
- **Operational beta:** the active U.S. raw-snapshot and restore-testing gates
  pass; external incident delivery and sufficient untouched naturally triggered
  timer evidence remain.
- **India research beta:** every India foundation field and adapter parity gate
  passes for a bounded reviewed universe.
- **Controlled capital:** separate future approval; never implied by a
  backtest, dashboard, or elapsed calendar time alone.
