# AI Investment OS — compact agent handoff

Read this file first. It contains the current operating truth and the rules an
agent must preserve. Open `ARCHITECTURE.md` for design rationale,
`SP500_DATA_PROVENANCE.md` for source history, and `BEGINNER_GUIDE.md` for the
non-technical operator guide. Use `FUTURE_BUILD_PLAN.md` for sequencing; do not
implement a later milestone before its listed dependencies and exit gates. Use
`INDIA_BUILD_PLAN.md` for the Nifty-50-first market-adapter sequence.

## Mission

- Build a local-first, low-cost, auditable investment research system.
- Finish the U.S. S&P 500 reference implementation, then add India through
  market-specific adapters without forking the factor, risk, or portfolio core.
- Use deterministic code for dates, identities, calculations, risk, accounting,
  and validation. Language models may explain outputs but are never evidence.
- This is not a trading bot. No broker, order API, unattended execution, or
  personal buy/sell advice exists.
- Normal operation must not require DuckDB or Streamlit knowledge.

## Verified checkpoint — 2026-07-29

Current U.S. decision close: **2026-07-28**.

- `aios health --report-only`: healthy for supervised research and paper
  monitoring; database integrity is 0 failures/3 warnings and the active
  forward policy is unchanged.
- 503 dated S&P 500 members; 503/503 stable security identities.
- 500/503 members have PIT fundamentals; 503/503 have identity-safe prices.
- 503/503 have recent prices with reviewed dividend/split fields.
- SPY benchmark/calendar is reviewed through 2026-07-28. The current raw source
  clocks are prices 2026-07-28, filings 2026-07-24, and macro releases
  2026-07-28.
- The immutable archive verifier checks 3,738 unique payloads and replays 3,900
  parsed artifacts.
- The replay-aware AAPL SEC ingest linked 4,102 canonical Company Facts rows
  (parsed SHA-256
  `4aa426f42980401992c7b6f28659d9cc5b3ecd6ebb19bdb865e0b4ff2110665b`)
  and one Submissions metadata row (parsed SHA-256
  `5b0495ce0bc6f27cc5147cc63617a6423ed53ef52b49ab1626d99c0dd39c7cde`)
  to one successful reviewed-issuer run.
- Database quality: zero hard failures, three visible warning categories.
- Full regression baseline: 494 passed; Ruff, bytecode compilation, and
  `git diff --check` are clean. Repository-wide Ruff formatting is not a
  current gate because existing files intentionally predate that formatter
  baseline.
- `aios preflight --review-paper` passed read-only and returned a
  `human_decision` with no command. Account, proposal, trial, DuckDB, and
  operations hashes were unchanged; no simulation was recorded.
- Host scheduler verification found the daily, filings, and backup timers
  active and waiting; the guarded daily service passed at 13:12 IST and the
  normal scheduler-status lifecycle resolved its stale sandbox-visibility
  warning. One non-critical fundamentals-coverage incident remains open for
  three SEC issuers that returned no fundamental rows.
- Every supported CLI writer of backup-covered DuckDB, raw, paper, proposal,
  and forward state shares one fail-visible project maintenance lease.
  Caller-selected generated outputs cannot target or alias governed state;
  normal files publish atomically without replacement. Read-only incident and
  notification inspection uses an immutable exact-schema SQLite connection and
  leaves the live operations ledger byte-for-byte unchanged.
- Live operations ledger schema v4 preserves all 11 incidents, 50 incident
  events, five job records, three outbox rows, and one local-test delivery;
  SQLite integrity is `ok`, foreign-key check is empty, and no external route
  exists. New routable messages bind to immutable activations, old route state
  is quarantined, and ambiguous/expired outcomes are terminal. Migration,
  concurrency, TLS, exact-config, dependency, CLI, scheduler, and dashboard
  tests pass.
- Pre-v4-migration backup `backups/aios-20260727T115222Z` verified 1,542 files
  (378,754,847 bytes; manifest SHA-256
  `a038652296c4947d27ba757768802e7c6c6dea06f23b37ca6788c7620eb5fd0c`).
  `.env`, logs, caches, and backtest artifacts are excluded.
- Latest checkpoint backup `backups/aios-20260728T082412Z` verified 2,622 files
  (380,408,599 bytes; manifest SHA-256
  `cd4ce6e3c8256013483eee438b3167cf8ef12815b7ffbb04e03b3e4ea629d25b`)
  and passed a non-destructive restore drill.

Stabilization note for 2026-07-29: `aios preflight` is now the canonical
read-only operator entrypoint. It keeps research, proposal creation, stress
review, paper recording, unattended operations, and real capital independent,
then returns exactly one safe next action. `--review-paper` performs the full
governed paper review but stops at a human decision and generates no
state-changing command; repeatable `--require` flags provide a machine gate.
Readiness without `--as-of` resolves the newest reviewed U.S. decision-date
candidate rather than the wall date. `health --report-only` no longer changes
the incident ledger. Dashboard incident and notification reads use the same
immutable SQLite adapter and refuse an uncheckpointed WAL.

Supported refresh, ingest, import, repair, cleanup, backup, restore, paper, and
forward CLI mutations now share one non-blocking project maintenance lease
whenever they can change backup-covered state. Generated artifact paths are
validated before work; ordinary report/review files are write-once, proposal
replacement is namespace/account/date constrained, and mutable review
workspaces reject filesystem aliases. These boundaries close ordinary CLI
races without editing the frozen active paper/forward policy. They are
deliberately documented as cooperative: direct imports and external writers do
not participate, and final compare-and-set hardening inside the frozen
persistence functions belongs in a new policy version and prospective trial.

Current local row counts:

| Table | Rows |
|---|---:|
| securities | 561 |
| security_master | 560 |
| security_identity_assignments | 568 |
| issuer_master | 564 |
| issuer_cik_history | 1,063 |
| security_issuer_assignments | 1,070 |
| provider_symbol_history | 1,074 |
| security_conversions | 1 |
| security_ticker_extensions | 2 |
| factor_price_provenance | 543 |
| factor_prices | 131,252 |
| prices | 524,350 |
| fundamentals | 1,270,623 |
| fundamentals_quarantine | 42 |
| macro | 733,934 |
| universe_membership | 568 |
| universe_coverage_attestations | 6 |
| raw_payloads | 3,738 |
| raw_snapshots | 4,914 |
| ingest_raw_snapshots | 4,914 |

Operational note for 2026-07-22: the naturally triggered weekday refresh first
failed on transient FRED DNS resolution and isolated Yahoo empty responses. A
second run failed on different valid symbols, proving provider instability
rather than identity corruption. Yahoo now gets three bounded exponential
attempts without changing provider or PIT limits. The managed 13:45-13:55 IST
rerun passed refresh, health, and recovery hooks; unresolved incidents are zero.
The immutable snapshot transport captures exact SEC Company Facts, Submissions,
company-ticker-map, Treasury, and Tiingo responses for their active reviewed
paths. yfinance and FRED use honestly labeled normalized-library exports. On
2026-07-25 the year-bounded official Treasury CSV retained and replayed 423
rows through 2026-07-24; DGS2, DGS10, and DGS30 exactly matched FRED on their
latest common observation. A reviewed Tiingo CTRA sample retained and replayed
four identity-tagged sessions, and the SEC ticker map replayed 10,429 canonical
rows. Stooq returned a JavaScript verification page instead of CSV in this
environment. The capture-before-parse path is tested to retain such a response
as capture-only evidence during an ingest, and it fails closed rather than
being promoted as price data; the live archive has no successful Stooq replay.

On 2026-07-24 the free-source no-change review archived 99 S&P Global press
releases plus the 503-row fja05680 component snapshot. Their ticker-set SHA-256
matched exactly. The accepted attestation extended 503 memberships, 503
security assignments, 503 issuer-owner assignments, 500 current issuer CIKs,
and 503 provider mappings through July 22 in one transaction. XOM correctly
matched through its reviewed CIK 34088→2115436 successor lineage; do not weaken
this to naive current-CIK equality.

On 2026-07-24 the old separate current-data service was terminated when the
desktop user manager shut down after macro and part of the 503-stock refresh.
The database remained safe through July 22, but `Persistent=true` alone could
not distinguish a triggered timer from a completed workflow. The replacement
`refresh-us-daily` workflow now refreshes SPY first, checks/extends the unchanged
universe through that completed session, refreshes members and macro inside the
new identity windows, and requires broad exact-date readiness before success.
Its independent SQLite job record remains `running` if the process disappears,
so the next startup can identify and recover an interrupted run.

The first live benchmark-first run processed 503/503 members, 2,537 member-price
rows, and 98,006 macro rows, then certified 2026-07-23. The scheduler now has
three timers: the recoverable daily workflow at 02:00 New York time, weekly
filings, and weekly backup. The daily timer also checks three minutes after the
user manager starts, uses `Restart=on-failure`, and Linux linger is enabled so
it remains active after desktop logout while the computer is on. The immediate
startup proof correctly detected an already-certified session, made no network
requests, passed health, and scheduled the next run.

On 2026-07-25 a simultaneous startup catch-up exposed a scheduler bug rather
than corrupt data: the weekly filing writer held DuckDB while the daily service
used an incorrectly prefixed lock-wait environment variable and exhausted its
restart limit. The permanent unit contract now exports the real
`DUCKDB_LOCK_WAIT_SECONDS=300` setting and serializes daily, filing, backup, and
health commands through one 30-minute `flock` queue. The corrected units pass
`systemd-analyze verify`, are installed with linger enabled, and passed a real
systemd no-download recovery run. A controlled recovery refreshed 503/503
member securities, stored 2,515 member-price rows and 98,013 macro rows, and
certified 2026-07-24. Daily and systemd failure incidents resolved through
normal success hooks; one warning remains open for three genuinely pending new
issuer Company Facts responses.

On 2026-07-28 the price path was hardened after Yahoo returned non-empty
completed-session candles with malformed OHLC values. New yfinance captures use
the replayable `yfinance-normalized-v2` parser: raw malformed evidence is
retained before the eligibility check, but missing, non-finite, non-positive, or
internally inconsistent OHLC/adjusted-close rows cannot reach `prices`. The
storage layer independently rejects an invalid close before any upsert, so bad
input cannot overwrite a valid stored close. Legacy v1 evidence remains
replayable. `validate`, `readiness`, and `health` now use read-only DuckDB
connections; when readiness is blocked, health labels the examined date as a
candidate rather than falsely calling it certified.

Dashboard loaders use short-lived read-only `store_scope` connections and
release the old global connection on hot reload. Managed services set a bounded
300-second DuckDB lock wait; ordinary commands default to 10 seconds. The
dashboard can remain open during normal scheduled runs, but it must be closed
for restore. Judge EOD freshness against the latest completed New York session
plus the 30-minute provider-finalization delay, never India midnight. All EOD
adapters enforce that boundary, including manual and startup catch-up runs.

The latest verified backup is `backups/aios-20260728T082412Z` (2,622 files,
380,408,599 bytes; manifest SHA-256
`cd4ce6e3c8256013483eee438b3167cf8ef12815b7ffbb04e03b3e4ea629d25b`).
It contains the live database, both forward-trial histories, paper/operations
state, and all registered immutable provider evidence. A non-destructive
`aios restore-drill` restored it through the real restore path into a disposable
project, opened the recovered DuckDB, passed hard data-quality checks, verified
all 2,612 payloads, replayed 2,111 parsed artifacts, and left the live files
untouched.

The three quality warnings are historical audit debt: old action-incomplete
price rows plus retained failed/zero-row ingest records. They do not override
the dated current-use checks. Never describe all historical rows as action-safe.

## Product readiness boundary

Ready now:

- supervised U.S. research in the CLI/dashboard;
- a `Today` dashboard that separates Research, Paper Trial, and Operations
  status before one safe next action; detailed gates and incidents are
  progressively disclosed;
- a production visual system with a light evidence canvas, navy navigation,
  symmetric primary CTA, semantic status colors, compact Company Detail,
  failure-first System Health, a fixed four-stage Paper Trial, and responsive
  desktop/mobile layouts;
- current fail-closed readiness checks;
- local paper proposals and paper-account monitoring; the current proposal
  remains separate from holdings until its scheduled close is reviewed;
- QV baseline and experimental QVML research rankings;
- stateful PIT engineering backtests with costs, holdings, lots, daily curves,
  corporate actions, security conversions, and SPY.
- plain-language health plus checksum-verified backup/verify/confirmed-restore
  commands; restore always creates a pre-restore safety snapshot.
- a fail-visible benchmark-first U.S. daily workflow plus a fail-closed
  free-source no-change universe review. AIOS manages three user timers for the
  recoverable daily workflow, filings, and backups. Startup catch-up and
  keep-running-after-logout are live-proven; no timer installation is implicit.
- Scheduler status queries are process-group bounded. If the desktop user bus
  is temporarily unavailable, report managed file enablement as unverified
  runtime evidence; never hang or claim that a timer passed/stopped.
- Managed services use `OnFailure=` and post-success recovery hooks. Incidents
  live in permission-restricted `data/operations/alerts.sqlite3`, separate from
  DuckDB, with deduplicated open/acknowledged/resolved/reopened history. Backups
  archive this ledger, but analytical restore never rolls it backward.
- The same SQLite ledger now contains the schema-v4 channel-neutral notification
  outbox and append-only delivery attempts. Actionable incident transitions and
  their message copies commit atomically; ordinary repeats do not enqueue.
  Legacy incidents were deliberately not backfilled. Incident-generated copies
  remain `held` while external delivery is off. A deterministic local test
  proves enqueue, lease, attempt, and completion without network access.
  The selected one-recipient TLS-only SMTP adapter is implemented and
  fail-closed. It is not live until private SMTP settings, an exact-config test,
  owner-confirmed receipt, and explicit optional-timer activation are complete.
  Its optional unit files pass native systemd verification and are installed
  disabled; the three core timers remain enabled/waiting and linger remains on.

Raw refresh may use the newest reviewed membership snapshot up to seven days
old solely to collect prices/filings for known identities. It prints that date
and never turns it into a current membership or portfolio-decision claim.
Existing simulated holdings may use the newest action-safe close for valuation,
but a new proposal uses the newest close that also has a certified 450–550
member universe.
New reviewed issuers with no accepted Company Facts remain visible as
`fundamentals_pending` and are retried. An issuer that previously had accepted
facts but unexpectedly returns none is still a hard refresh failure.

Not approved:

- personalized buy/hold/sell instructions;
- broker or real-money execution;
- after-tax claims (jurisdiction and account type are unset);
- alpha/performance claims from short in-sample artifacts;
- Indian-market rankings (NSE/BSE data is not loaded);
- complete 1996-present announcement/delisting coverage.

The paper account at `data/paper/us_qv_sandbox.json` remains entirely simulated:
$100,000 cash, zero holdings, zero executions, and no broker connection. Active
trial `us-qv-forward-72c4560a442d` began prospectively from the 2026-07-27
decision close and has one registered, approved simulation-only proposal for
the 2026-07-28 session. Predecessor `us-qv-forward-8559d86b6a02` is archived
unchanged. Do not restart the active trial or treat the proposal as a holding.

## Completed U.S. stateful engineering gate

The 2025-01-01 through 2026-07-20 stateful QV rerun now completes all six
periods. Its 327 strategy and SPY observations use identical dates, quarter-to-
quarter capital continuity is exact, and no strategy observation is stale.
The reviewed HES→CVX conversion was applied on 2025-07-18. MTCH and PAYC were
liquidated on 2026-04-01 from their short reviewed price continuations without
restoring either security to membership or factor eligibility.

The retained schema-v4 artifact is
`data/backtests/us_pit_2025_to_2026-07-20_qv.json` (run
`ccbff90a-c339-42e8-a9c4-ea93aa7f344d`, database SHA-256
`86c7ff27df5801f32b08255d40c5e78a41ac73b3c8713e633a01933cd81c51d7`).
With 5 bps commission plus 5 bps slippage per side and zero taxes, regime-aware
QV returned 31.13% net, fixed QV returned 27.73%, and SPY returned 34.12%.
Maximum drawdowns were -14.69%, -14.69%, and -12.05%. This short, in-sample run
is engineering evidence, not alpha evidence or an after-tax claim; SPY
outperformed both strategies during the window.

Implemented held-security protections:

- `security_conversions` handles reviewed share-for-share identity changes.
  HES→CVX effective 2025-07-18 uses 1.025 CVX shares per HES share and
  carry-over acquisition date/tax basis.
- `security_ticker_extensions` permits a short price continuation only for an
  already-held, still-listed security through the next rebalance. It never
  restores universe membership or factor eligibility.
- Extension imports require exact prior identity/provider anchors, complete U.S.
  sessions, action-safe rows, HTTPS sources, a 45-day maximum, and a canonical
  payload hash. `aios validate` continuously re-hashes the stored rows.
- `examples/sp500_liquidation_price_extensions_2026_q1.csv` is the reviewed
  MTCH/PAYC manifest. It imported two extensions and 16 price rows on
  2026-07-21; all expected sessions from 2026-03-23 through 2026-04-01 are
  present and action-complete.

Rebuild/reproduce the liquidation evidence with:

```bash
PYTHONPATH=src .venv/bin/python -m aios.cli ingest-liquidation-prices \
  examples/sp500_liquidation_price_extensions_2026_q1.csv
```

Then reproduce the full stateful artifact with:

```bash
PYTHONPATH=src .venv/bin/python -m aios.cli backtest-qv \
  --start 2025-01-01 --end 2026-07-20 \
  --universe-id sp500 --benchmark SPY --calendar SPY \
  --factor-model qv --exclude-ticker FDXF --exclude-ticker HONA \
  --commission-bps 5 --slippage-bps 5 \
  --output data/backtests/us_pit_2025_to_2026-07-20_qv.json
```

Do not present this artifact as expected performance. Taxes are zero and the
period is neither long nor an untouched holdout.

## Architecture map

| Path | Responsibility |
|---|---|
| `src/aios/storage/schema.py` | PIT and provenance schema |
| `src/aios/storage/store.py` | only supported DuckDB access layer |
| `src/aios/ingest/` | source adapters and reviewed import workflows |
| `src/aios/factors/` | Quality, Value, Momentum, Low Volatility, composites |
| `src/aios/macro/regime.py` | release/vintage-aware macro state |
| `src/aios/backtest/engine.py` | quarterly stateful PIT comparison |
| `src/aios/backtest/portfolio.py` | holdings, FIFO lots, actions, costs, curve |
| `src/aios/risk/` | deterministic portfolio risk policy |
| `src/aios/readiness.py` | fail-closed U.S. operating gate |
| `src/aios/paper.py` | checksum-protected, local-only simulation |
| `src/aios/refresh.py` | current reviewed-identity refresh orchestration |
| `src/aios/daily.py` | benchmark-first exact-session workflow and recovery |
| `src/aios/universe_rollforward.py` | free no-change certification and drift stop |
| `src/aios/operations.py` | backup verification and confirmed restore |
| `src/aios/scheduler.py` | bounded systemd-user timer lifecycle |
| `src/aios/alerts.py` | independent incidents, job lifecycle, and SQLite notification outbox |
| `src/aios/notifications.py` | channel boundary and bounded local dispatcher |
| `src/aios/dashboard.py` | decision-first, read-only Streamlit control room |
| `src/aios/dashboard_ui.py` | pure scoped-status and next-action presentation model |
| `src/aios/cli.py` | supported operator command surface |
| `tests/` | behavioral and provenance regression gates |

## Non-negotiable data rules

### Point in time

- Fundamentals: use only `period_end <= as_of_date <= decision_date`.
- Macro: select the latest vintage with `release_date <= decision_date`.
- Membership: keep `effective_start/effective_end` separate from
  `known_date/end_known_date`.
- A target must be publicly knowable at decision close and a member on the
  scheduled execution date.
- Never infer `known_date = effective_date - 7 days`.

### Identity

Issuer, listed security, market ticker, SEC CIK, and provider symbol are
different objects. Keep all five separate.

- Fundamentals route by dated issuer/CIK.
- Prices route by immutable security ID and dated provider symbol.
- Ticker changes may preserve a security; mergers may create a new one.
- `unavailable` and `blocked_wrong_security` are valid terminal review states.
- Never let old Physicians Realty `DOC` contaminate Healthpeak or pre-combination
  `SW` contaminate Smurfit Westrock.
- Never backcast a live SEC ticker-map result onto a historical issuer.

### Provenance priority

For S&P membership:

1. official S&P DJI announcement;
2. issuer announcement for same-security ticker changes;
3. exact SEC filing only as a labeled fallback;
4. secondary sources for discovery/cross-check only.

SEC full-text search is discovery, not accepted identity evidence, until exact
CIK, accession, filing timestamp, security, and URL are preserved.
Automatic roll-forward is allowed only for a no-change attestation whose exact
S&P archive response, independent component response, ticker-set hashes, and
reviewed CIK lineage all reconcile. Any possible constituent event remains
manual.

### Price/action safety

- Every reviewed price declares whether actions are complete and whether Close
  is already split-normalized.
- Completed-session yfinance rows must have finite positive OHLC and adjusted
  close values plus internally consistent highs/lows. Capture malformed
  evidence, but never promote it to `prices`.
- `Store.upsert_prices` is the independent backstop: a missing, non-finite, or
  non-positive close must fail before an existing valid row can be replaced.
- Preserve replay compatibility for legacy yfinance v1 evidence; new captures
  use the stricter v2 evidence/eligibility split.
- Yahoo historical Close is split-normalized; restore its contemporaneous basis
  before combining with PIT shares/per-share facts.
- Missing scheduled entry/exit prices are hard failures.
- Do not mark Stooq rows action-complete without separate reviewed action proof.
- Do not extend membership to solve a held-security valuation problem; use a
  reviewed conversion, cash event, or liquidation-only extension.

## Factor and portfolio contract

- QV is the default: Quality and Value must both publish.
- QVML is experimental: a 60% regime-relative Q/V core plus 25% Momentum and
  15% Low Volatility. Missing factors are not silently reweighted.
- Economic weights are explicit in `aios.factors.policy`; incomplete macro uses
  baseline 60/40 and is labeled.
- Rankings are cross-sectional research scores, not expected returns.
- Positions and FIFO lots persist across quarters; rebalance only target deltas.
- Daily valuation uses reviewed closes, dividends, and split treatment.
- Conversions retain audit evidence and basis policy.
- CLI defaults: 5 bps commission + 5 bps slippage; tax rates default to zero.
- Wash sales, tax carryforwards, filing calendars, and jurisdiction-specific
  offsets are not modeled.
- One incomplete state transition invalidates later stateful periods.

## Dashboard and paper rules

- Default UI must answer: what is ready, what is blocked, what date is covered,
  and what the user should review next.
- Use everyday labels; technical codes belong in expanders/audit artifacts.
- Keep proposals visibly separate from holdings.
- Always state: simulation only, no broker, no personal recommendation.
- Never display an incomplete backtest as a performance result.
- Paper documents use integrity checksums, not adversarial cryptographic signing.
- A proposal must be generated before its scheduled U.S. session opens.
  `paper-review` runs a read-only preflight; `paper-execute` is allowed only
  from that session's conservative 4:00 p.m. New York close until the following
  U.S. session opens, and still requires next-session evidence, revalidation,
  and explicit `--confirm-simulated`.
- `stress-review` accepts only a registered, checksum-valid simulation proposal
  under an unchanged forward trial. Its production service is shared with the
  Paper Trial panel, opens DuckDB read-only, binds exact PIT identity, price,
  row-level liquidity, revenue, and calculation-source evidence, rechecks
  trial/account/proposal/source identity before output, and stores nothing unless
  `--output` explicitly requests a write-once artifact.
- Deterministic mark shocks may report modeled post-shock weights and exit
  capacity. The volatility/correlation result is a separate statistical loss
  proxy: Euler contributions are not position returns, holdings, drawdown,
  concentration, or liquidation outcomes. All generic-limit findings are
  advisory and never approve, reject, or execute a proposal.
- Missing or mismatched evidence withholds the dependent scenario result.
  Independent calculations may remain visible only as a visibly partial report.
  The Paper Trial panel is not a fifth workflow stage and must never relabel
  proposal targets as holdings.
- `forward-freeze` fingerprints the QV, macro-regime, risk, cost/tax, calendar,
  readiness, and paper-policy source plus the reviewed operating configuration.
- The forward freeze never hashes DuckDB: new public data must advance. Every
  new proposal is registered by checksum, and drift blocks CLI simulation.
- `forward-restart --confirm-restart` is the only normal replacement path after
  genuine drift. It archives the predecessor unchanged, atomically activates a
  later baseline, refuses an unchanged trial, and does not execute a proposal.

## Safe working procedure

1. Read `git status --short`; preserve staged/user changes.
2. Make one small provenance-safe slice.
3. Run focused tests, then Ruff and the full suite.
4. Run the read-only `aios validate`; hard failures block downstream claims.
5. Run `aios preflight` first for the scoped operator answer. Use the read-only
   `aios readiness` for an exact decision date/purpose and `aios health
   --report-only` for deeper evidence. A blocked date is a candidate, not
   certified.
6. Update this file and the relevant canonical doc when contracts change.
7. Never print `.env`, secrets, or externalize private repository code.
8. DuckDB is single-process. Run DB commands sequentially and close the
   dashboard before writes. A lock collision is transient concurrency; retry
   after the other process exits.

## Immediate plan

1. Start with `aios preflight`. The 2026-07-29 full read-only review for
   `data/paper/proposals/us-qv-2026-07-27.json` passed and stopped at explicit
   human confirmation; no simulation was recorded. Use the Paper Trial stress
   panel or `stress-review` when proposal-risk evidence is needed. If timing
   later expires, create a new prospective proposal rather than forcing a
   retrospective fill. Do not restart the unchanged active forward trial.
2. Observe the next naturally triggered guarded daily workflow. Installed units
   and prior controlled/systemd runs are evidence, but they do not substitute
   for this current naturally triggered runtime observation.
3. Observe normal use of the completed institutional dashboard and keep visual
   regression screenshots with any Streamlit upgrade. Ranked-row navigation,
   failure-first System Health, four-stage Paper Trial, responsive stacking, and
   origin-aware URL state are implemented. Do not add a second frontend runtime
   before a versioned API and a real multi-user/mobile requirement exist.
4. Completed on 2026-07-29: the read-only dashboard now serves factor lookups
   from decision-scoped universe batches while keeping the frozen factor policy
   unchanged. Fresh-process 503-company QV and QVML payloads matched the scalar
   path exactly; QV improved from 25.5 to 2.9 seconds and QVML from 28.5 to 3.4
   seconds. Batch errors and malformed ticker sets fall back to the existing
   scalar fail-closed path.
5. Keep SMTP disabled until the user resumes the deferred email milestone. Do
   not release historical held messages. The active U.S. transport gate is live-proven for exact
   SEC Company Facts/Submissions/ticker-map, Treasury and Tiingo responses, plus
   labeled yfinance/FRED exports and a non-destructive restore drill. Stooq is
   explicitly unavailable in this environment and fails closed; every future
   NSE or other provider adapter must pass the same capture/replay gate before
   production use.
6. Completed on 2026-07-29: governed proposal stress-review v1, immutable
   scenario policy, exact evidence/source hashes, read-only default, optional
   write-once export, distinct deterministic/statistical semantics, fail-closed
   partial withholding, and a Paper Trial panel over the same registered-proposal
   service. The Federal Reserve 2026 supervisory calibration is an approximately
   58% hypothetical equity decline, not a forecast. Next add anomaly review
   cases, then experiment registration and versioned configuration boundaries.
   Current-holdings stress, multifactor/Monte Carlo risk, and owner-authored
   scenarios remain future work.
7. Extend pre-August-2023 announcement and delisting provenance as a separate
   long-history track.
8. Follow `INDIA_BUILD_PLAN.md`: Nifty 50 first, no NSE/BSE ingest before the
   portable schema and source/licensing gates pass.
9. Ask the user for jurisdiction, account type, broker, and final risk limits
   only before after-tax or controlled-capital certification.

The historical U.S. technical gate, exact-date daily workflow, native systemd
verification, live catch-up, and startup no-op proof are complete. Forward trial
`us-qv-forward-a0b63856954c` is drifted and retained only as invalidated history;
it has zero executions. Predecessor `us-qv-forward-8559d86b6a02` is archived
unchanged. Active trial `us-qv-forward-72c4560a442d` is policy-intact, has one
2026-07-27 proposal, and has zero executions. Any real-capital pilot still
needs at least 8–12 weeks of untouched forward observation plus the separate
controlled-capital gates.

## Core commands

```bash
.venv/bin/aios doctor
.venv/bin/aios status
.venv/bin/aios audit
.venv/bin/aios validate
.venv/bin/aios preflight
# Full governed review with no simulation write:
.venv/bin/aios preflight --review-paper
.venv/bin/aios readiness --as-of 2026-07-28 --purpose paper --report-only
.venv/bin/aios health --report-only
.venv/bin/aios refresh-us-daily
.venv/bin/aios refresh-us-current
.venv/bin/aios review-universe-current
.venv/bin/aios backup
.venv/bin/aios verify-raw-snapshots
# restore requires: aios restore BACKUP_DIR --confirm-restore
.venv/bin/aios scheduler-status
# recommended install: aios scheduler-install --confirm-install --keep-running-after-logout
.venv/bin/aios alert-test
.venv/bin/aios alerts --unresolved
.venv/bin/aios notifications
.venv/bin/aios notification-test
.venv/bin/aios email-status
# After private SMTP configuration: email-test --confirm-send, verify receipt,
# then email-enable --confirm-enable. Emergency stop: email-disable --confirm-disable.
.venv/bin/aios dashboard

.venv/bin/aios paper-status
.venv/bin/aios forward-status
# only after a future genuine drift: aios forward-restart --confirm-restart
.venv/bin/aios paper-propose
.venv/bin/aios stress-review --proposal data/paper/proposals/us-qv-2026-07-27.json
# add --output PATH only for a deliberate write-once report
.venv/bin/aios paper-review --proposal data/paper/proposals/us-qv-2026-07-27.json
.venv/bin/aios paper-mark
# paper-execute additionally requires --proposal ... --confirm-simulated
# creating a new baseline additionally requires: forward-freeze --confirm-freeze

PYTHONPATH=src .venv/bin/pytest -q
.venv/bin/ruff check src tests
```
