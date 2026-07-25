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
It does not send external alerts, preserve every original provider response,
provide a certified stress engine, ingest Indian-market data, connect to a
broker, or make personal buy/sell decisions.

The old U.S. forward trial has zero executions and is retained unchanged in the
trial archive after correctness changes. It was not re-hashed or backdated.
Replacement trial `us-qv-forward-8559d86b6a02` starts prospectively from the
2026-07-23 close with one registered, unexecuted proposal. Later material
research-policy changes must likewise start a new trial.

## Priority decision

| Capability | Decision | Why | Dependency / exit gate |
|---|---|---|---|
| System Control dashboard foundation | **Now — implemented** | Makes the local product operable without DuckDB or systemd knowledge | Every displayed fact reconciles to readiness, scheduler, ingest, backup, or forward-policy evidence |
| Recoverable exact-date U.S. daily workflow | **Now — implemented** | Separate timer stamps did not prove a full run survived logout, and benchmark-last ordering could leave identity windows one session behind | SPY first, universe second, members/macro third, exact-date readiness last; durable job lifecycle, restart, startup catch-up, linger, live 503-member proof |
| Local incident ledger and systemd failure capture | **Now — implemented** | Scheduled and application failures must survive analytical DB lock/open failures | Deduplicated open/repeat/acknowledge/resolve/reopen lifecycle, safe structured service evidence, CLI inspection, dashboard history, backup archive |
| Immutable raw-data snapshots | **In progress — SEC live, Treasury wired** | Provider history can change; later anomaly work needs exact ingest evidence | Content-addressed storage, metadata/link tables, verifier and backup merge work; SEC Company Facts/submissions now capture exact linked payloads, while remaining transports and parser/replay evidence are pending |
| Notification outbox and email/Slack delivery | **After raw snapshots** | Channels are replaceable transports, not the source of incident truth | Retry-safe outbox; user chooses one channel; secrets stay outside policy/data tables; failure and recovery delivery are tested |
| Deterministic stress testing | **After operations foundation** | Portfolio risk should be challenged before adding prediction complexity | Frozen scenarios reproduce exactly and report portfolio, sector, liquidity, concentration, and recovery assumptions |
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
- exact-response capture wired for the U.S. Treasury CSV fallback; and
- a live reviewed AAPL issuer proof: two linked SEC payloads, 3,913,076 original
  bytes, both independently verified and included in backup
  `backups/aios-20260722T125932Z`.

Still required before this gate is complete: attach canonical parsed-row hashes
and replay checks to the SEC captures; expose/capture FRED library responses or
label normalized FRED exports; live-test the Treasury path; add explicitly
labeled normalized-export capture for yfinance; instrument remaining SEC
reference, Tiingo/Stooq, and future NSE transports; link every production ingest
run; and pass a non-destructive live restore drill. The non-empty backup half of
that drill now passes; the active database has not been overwritten for testing.

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
- CLI test/list/show/acknowledge/resolve commands and source-backed System Control history;
- checksum-manifest backup archive. Analytical restore deliberately does not roll the
  live incident ledger backward.

Still pending after immutable raw snapshots:

- `notification_outbox`: channel-neutral pending messages with idempotency key;
- `notification_deliveries`: attempts, provider response, retry time and final
  outcome.

Initial incident rules:

- refresh or timer failure;
- readiness transition from ready to blocked;
- stale prices, macro, filings or membership;
- backup verification failure;
- database checksum or forward-policy drift;
- unexpected factor-coverage drop.

Use bounded retry with backoff, deduplicate repeated symptoms, and send a
recovery notification. Dashboard state and incidents must remain available when
all external channels are down.

Channel order: use the implemented local test path first, then one user-selected
channel (email or Slack webhook), then optional mobile push. Secrets stay in environment
or deployment secret storage. No channel credential belongs in DuckDB, a raw
snapshot, an experiment artifact, or Git.

## Phase 2 — Deterministic portfolio stress testing

Implement scenarios as named, versioned input policies rather than editable UI
sliders with no audit trail. Initial scenarios:

- 2008-style broad equity drawdown;
- 2020 liquidity shock;
- 2022 inflation/rate shock;
- sector-specific crashes;
- volatility doubling and correlation convergence;
- top-five holdings falling 20%, 30% and 40%;
- missing prices and delayed filings.

For each scenario report:

- portfolio and holding-level loss;
- sector contribution and concentration breaches;
- liquidation need and liquidity constraint breaches;
- assumptions, shock date/basis and recovery path;
- input holdings hash, scenario version and code commit.

Historical labels describe calibration, not a claim that the next crisis will
behave identically. A stress result is deterministic risk evidence, not a price
forecast.

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

- a prominent reviewed data date and readiness status;
- a dedicated System Control workspace;
- coverage charts, source freshness, scheduler outcomes and next human action;
- source-backed local incident history and unresolved counts;
- full reviewed company name together with the market symbol;
- progressive disclosure: plain language first, technical evidence on demand.

Use only after the underlying feature exists:

- external notification delivery status;
- stress/risk panels;
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
- **Operational beta:** raw snapshots, incident delivery, restore testing and
  naturally triggered timer evidence all pass.
- **India research beta:** every India foundation field and adapter parity gate
  passes for a bounded reviewed universe.
- **Controlled capital:** separate future approval; never implied by a
  backtest, dashboard, or elapsed calendar time alone.
