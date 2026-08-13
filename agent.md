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

## Verified checkpoint — 2026-08-07

Current U.S. decision close: **2026-08-07**.

- S&P Dow Jones Indices announced EA's deletion and FERG's addition effective
  before the 2026-08-05 open. The event is now live through governed activation
  receipt `uca-event-87d33f15572441efbedd7b47a1226b64`, which binds the
  official event, reviewed reference manifests, backup, exact member-set hashes
  and staged price evidence. FERG is SEC CIK `0002011641`. A later governed
  refresh accepted 323 source-bound rows and withheld 24 ambiguous storage
  keys.
- Current live preflight reports supervised research, operations and proposal
  stress review available. Paper recording is waiting for the prospective
  2026-08-10 close. Real capital remains disabled.
- The paper account remains simulation-only with $100,000 cash and zero
  holdings/executions. Expired trial `us-qv-forward-72c4560a442d` and its
  proposal are archived byte-identically with no fill. Active trial
  `us-qv-forward-ea4fc2788c4d` has one prospective proposal.
- Reference-batch endpoint review now accepts a half-open window ending today
  or tomorrow only when verified today. Historical reviews retain the stricter
  filings-on-both-sides rule.

- `aios health --report-only`: healthy for supervised research and paper
  monitoring; database integrity is 0 failures/4 warnings and the active
  forward policy is unchanged.
- 503 dated S&P 500 members; 503/503 stable security identities.
- 503/503 members have PIT fundamentals and identity-safe prices.
- Company Facts v3: **re-verified live 2026-08-12**, source lineage is not
  the blocker. `companyfacts-v3-plan --as-of 2026-08-10` shows 500/500
  issuers with accepted evidence pass every lineage check
  (`ingest_run_id`/`source_snapshot_id`/row-hash all match stored evidence);
  the earlier "394/503, zero eligible" figures were stale. FDXF, HONA, XOM
  still have no accepted evidence at all — unrelated pre-existing gap.
- The v2→v3 delta is real and large — 26,356 added, 41,794 removed, 326,467
  changed rows across all 500 eligible issuers, out of ~1.17M. Root cause
  understood, not guessed: v2 sets `preserve_legacy_winner=True` and silently
  keeps an arbitrary first row when a filing has genuinely conflicting
  economics for one storage key; v3 sets it `False` and withholds the whole
  key instead — fail-closed, same philosophy as the rest of this codebase,
  not a bug.
- **Activation write path built and verified 2026-08-12** as
  `src/aios/companyfacts_v3_activation.py`, mirroring
  `universe_change_activation.py`'s proven prepare/backup/CAS/disposable-
  proof/atomic-commit/receipt pattern. `companyfacts-v3-prepare` and
  `companyfacts-v3-activate` are CLI-wired. Mutation reuses the frozen
  `Store.upsert_fundamentals` (already versions into `fundamental_versions`)
  for added/changed keys and tombstones (`is_deleted=TRUE`) a key v3
  withholds that v2 had silently kept — the "future shrinking replacement
  path" `ARCHITECTURE.md` flagged as needing explicit confirmation, backup,
  and compare-and-set evidence. One `fundamental_evidence_generations` row
  pins the resulting `version_sequence` boundary per activation, in the new
  `companyfacts_v3_activations` receipt table. Verified against a real
  production scratch copy (one issuer, 68 added/720 changed/35 removed,
  exact match to plan prediction) and 6 unit tests.
- **Run to completion 2026-08-12, user-directed.** All 500 eligible issuers
  migrated across 11 batches; `companyfacts-v3-plan` now reports 0 eligible,
  500 ineligible (all on v3). Batch 10 caught a real bug before it touched
  production: three tickers (BG, XOM, BLK) legitimately have two different
  issuer_ids (CIK-successor reincorporation), and `fundamentals`' primary
  key is `(ticker, period_end, as_of_date, metric)`, not issuer-scoped. The
  post-write verification query was ticker-only, so it saw the other
  issuer's unrelated legacy rows as noise and correctly refused inside the
  disposable-restore-first step — production was never touched by the
  failing batch. Fixed with a pre-write cross-issuer collision guard plus
  issuer_id-scoped delete/verification queries; confirmed via repro that no
  actual key collision existed for BG/XOM/BLK in practice (a false-negative,
  not a near-miss), then re-ran clean. Full suite/ruff/frozen-bundle
  re-verified after the fix and again after the final batch.
- `aios companyfacts-v3-plan --as-of YYYY-MM-DD` now classifies already-captured
  exact v2 evidence and optionally publishes a content-addressed review plan.
  It is read-only, performs no provider fetch or governed-state mutation, and
  exposes no v3 activation path.
- 503/503 have recent prices with reviewed dividend/split fields.
- SPY benchmark/calendar and the full reviewed member set are certified through
  2026-08-07. Current source clocks for prices, filings and mandatory macro
  releases reach 2026-08-07.
- The immutable archive verifier checks 7,977 unique payloads and replays
  11,601 parsed artifacts across 12,647 fetch observations.
- A prior replay-aware AAPL SEC ingest proved that the transport can link 4,102
  canonical Company Facts rows
  (parsed SHA-256
  `4aa426f42980401992c7b6f28659d9cc5b3ecd6ebb19bdb865e0b4ff2110665b`)
  and one Submissions metadata row (parsed SHA-256
  `5b0495ce0bc6f27cc5147cc63617a6423ed53ef52b49ab1626d99c0dd39c7cde`)
  to one successful reviewed-issuer run. That transport proof is not current
  live-row lineage and does not make AAPL or any other issuer v3-eligible.
- Database quality: zero hard failures, four visible warning categories.
- The current source passes 1,075 tests, repository-wide Ruff, bytecode
  compilation and diff checks. Independent wheels are byte-identical, and the
  exact source-to-wheel plus clean-install verifier passes. Candidate SHA-256 is
  `448c9f77031bf24084636ade283f932c71163e7505535a273877dd8365745287`.
- `aios preflight --json` is read-only. It now reports operations verified and
  the paper proposal waiting for the scheduled close.
- Daily, filings and backup services all reached `Result=success` and
  `ExecMainStatus=0`. The daily job certified 503 members and the exact August 7
  session; filings attempted 499/499 issuers with no hard failure; the current
  backup passed manifest verification and a disposable restore drill. No
  operational blocker remains. FDXF, HONA, and XOM are resolved as explicit
  reviewed gaps with score withholding preserved.
- Governed anomaly-review v1 compares reviewed membership with accepted SEC
  fundamentals, fails closed on missing or conflicting source evidence, and
  makes explicit `--record` append an immutable scan and lifecycle events while
  reconciling the deduplicated current-case projection, all within the
  independent operations ledger. Preview, case listing, case detail, and the
  dashboard are read-only; no case transition repairs DuckDB or waives
  readiness.
- Every supported CLI writer of backup-covered DuckDB, raw, paper, proposal,
  and forward state shares one fail-visible project maintenance lease.
  Caller-selected generated outputs cannot target or alias governed state;
  normal files publish atomically without replacement. Read-only incident and
  notification inspection uses an immutable exact-schema SQLite connection and
  leaves the live operations ledger byte-for-byte unchanged.
- The live schema-v6 operations migration preserves the previously verified v4
  contract. At the verified `aios-20260729T192830Z` checkpoint, the ledger
  contains 12 incidents, 100 incident events, 12 job records, 26 outbox rows,
  one local-test delivery, three immutable anomaly scans, three current cases,
  and nine append-only case events. SQLite integrity is `ok`, the foreign-key
  check is empty, and no external route exists. The migration did not change
  the incident/notification rules. New
  routable messages bind to immutable
  activations, old route state is quarantined, and ambiguous/expired outcomes
  are terminal.
- Pre-v4-migration backup `backups/aios-20260727T115222Z` verified 1,542 files
  (378,754,847 bytes; manifest SHA-256
  `a038652296c4947d27ba757768802e7c6c6dea06f23b37ca6788c7620eb5fd0c`).
  `.env`, logs, caches, and backtest artifacts are excluded.
- Recovery-tested checkpoint `backups/aios-20260728T082412Z` verified 2,622
  files and passed a non-destructive restore drill. The later July 29
  schema-v6 checkpoint `backups/aios-20260729T192830Z` verifies 3,748 files
  (410,397,206 bytes; manifest SHA-256
  `5d445e301153832bd6bffd2bad7c375b9e05583806d0d47d7edf4e3ee461ee5e`).

Stabilization note for 2026-07-29: `aios preflight` is now the canonical
read-only operator entrypoint. It keeps research, proposal creation, stress
review, paper recording, unattended operations, and real capital independent,
then returns exactly one safe next action. `--review-paper` performs the full
governed read-only paper review but stops at a human decision and produces or
executes no order or state-changing command; repeatable `--require` flags
provide a machine gate.
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
| securities | 562 |
| security_master | 561 |
| security_identity_assignments | 569 |
| issuer_master | 565 |
| issuer_cik_history | 1,064 |
| security_issuer_assignments | 1,071 |
| provider_symbol_history | 1,075 |
| security_conversions | 1 |
| security_ticker_extensions | 2 |
| factor_price_provenance | 543 |
| factor_prices | 131,252 |
| prices | 528,381 |
| fundamentals | 1,279,612 |
| fundamentals_quarantine | 42 |
| macro | 915,001 |
| universe_membership | 569 |
| universe_coverage_attestations | 19 |
| raw_payloads | 7,977 |
| raw_snapshots | 12,647 |
| ingest_raw_snapshots | 12,647 |

All 1,279,612 rows in `fundamentals` are currently unlineaged at the row level;
immutable raw and ingest-run evidence remains available separately.

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

The latest verified backup is `backups/aios-20260730T092832Z` (4,262 files,
397,657,272 bytes; manifest SHA-256
`a3081e23103997e3f94b0f9066cbc8ed6c446b1b9958fb75b8dad73ed5afabce`).
It captures the governed database, both forward-trial histories,
paper/operations state, and registered immutable provider evidence at backup
time. That exact checkpoint passed the non-destructive restore drill through
the real restore path into a disposable project, opened the recovered
DuckDB, passed hard data-quality checks, verified every registered payload and
reviewed replay at that checkpoint, and left the live files untouched.

The three quality warnings are historical audit debt: old action-incomplete
price rows plus retained failed/zero-row ingest records. They do not override
the dated current-use checks. Never describe all historical rows as action-safe.

## Product readiness boundary

Ready now:

- supervised U.S. research in the CLI/dashboard;
- governed, supervised SEC fundamentals-coverage anomaly review in the
  CLI/dashboard and independent operations ledger; it is a review workflow,
  never an analytical repair or readiness override;
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
  commands. Restore stages the complete candidate first and requires matching
  application version, an openable zero-hard-failure DuckDB, valid
  account/proposal/active-or-archived-forward envelopes, consistent active
  cross-references, complete raw replay, and no immutable merge conflict before
  it creates the pre-restore safety snapshot or changes governed live state.
  The later publication phase is not a cross-filesystem transaction: paper
  rollback and the safety backup are its recovery boundary, while published
  immutable raw additions and the operations stale-evidence incident remain
  forward-only audit evidence.
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
- Company Facts v3 activation or use of its 394/503 structural count as a
  freshness-qualified or source-lineaged score claim;
- after-tax claims (jurisdiction and account type are unset);
- alpha/performance claims from short in-sample artifacts;
- Indian-market rankings (NSE/BSE data is not loaded);
- complete 1996-present announcement/delisting coverage.

The paper account at `data/paper/us_qv_sandbox.json` remains entirely simulated:
$100,000 cash, zero holdings, zero executions, and no broker connection. Active
trial `us-qv-forward-ea4fc2788c4d` began prospectively from the 2026-08-07
decision close and has one registered, approved simulation-only proposal
(`paper-2026-08-07-35266fec0591`) for the 2026-08-10 session. Predecessor
`us-qv-forward-72c4560a442d` is archived unchanged with a no-fill disposition.
Do not restart the active trial or treat the proposal as a holding.

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
| `src/aios/alerts.py` | independent incidents, jobs, anomaly cases, and SQLite notification outbox |
| `src/aios/anomalies.py` | source-bound, fail-closed data-quality detectors |
| `src/aios/canonical.py` | the single canonical JSON contract every evidence hash uses |
| `src/aios/markets.py` | validated market/venue/dated-listing identity (India phase I1) |
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
- Keep data-quality cases separate from operational incidents and analytical
  evidence. `anomaly-scan --preview` persists nothing, acquires no maintenance
  lease, and creates or updates no lock file. `--record` may change only the
  operations ledger: it writes an immutable scan, appends lifecycle events, and
  reconciles the deduplicated current-case projection. Its first run against
  schema v4 performs the supported additive schema-v6 migration with empty
  anomaly tables and no historical backfill. A v5-to-v6 upgrade retains every
  anomaly scan, case, and event and deterministically fills only a missing
  `event_sequence` from the existing append row order. It does not infer any
  historical lifecycle event. Read-only anomaly views fail closed until the
  required migration occurs.
  Coverage is proved either by complete run/snapshot/row-set/row lineage or by
  exact legacy response replay plus decision-date row-set equality; legacy
  lineage is never guessed or backfilled. A warning can establish coverage only
  when it stored positive rows and rejected exclusively rows whose `period_end`
  followed the filing date. Zero-row and other warnings remain missing.
  SEC source-boundary policy v2 sets the boundary to the maximum receipt time
  among only the exact SEC snapshots consumed for accepted coverage and
  selected missing-issuer evidence, not the newest global ingest. Exactly one
  audited legacy implicit-global-v1 to consumed-v2 transition may use an
  earlier corrected boundary when the SEC bundle/scope, exact v1/v2 signatures,
  consumed-snapshot proof, and zero-write safety contract match. V2-to-v2
  regressions remain blocked.
  Acknowledgement-first is recommended, but direct disposition is permitted;
  either transition requires an owner, note, and the current evidence SHA-256
  shown by `anomaly-show`. Stale hashes fail instead of overwriting newer review
  evidence.
  Resolution is limited to `accepted`, `source_corrected`,
  `mapping_corrected`, `false_positive`, or `deferred`; correction outcomes
  require a complete same-scope scan recorded after the finding, a distinct
  source-boundary hash, non-earlier source-boundary time, exact-rule execution,
  fingerprint absence, and clearance from a source-provenanced accepted SEC
  ingest with positive verified rows. Deferral requires a future review time.
  Every case read or mutation replays the immutable ordered lifecycle and
  verifies the mutable projection. This local structural check trusts the
  released AIOS code and OS/filesystem access controls; it is not cryptographic
  attestation against an actor who can replace both code and evidence.
  The one-time policy transition is not a correction-clearance exception:
  `source_corrected` and `mapping_corrected` verification scans still require
  a non-earlier source boundary than the finding.
- An anomaly disposition never repairs DuckDB, advances readiness, changes
  paper/proposal/trial state, creates a broker action, or substitutes for the
  underlying evidence correction and normal certification path.
- `forward-freeze` fingerprints the QV, macro-regime, risk, cost/tax, calendar,
  readiness, and paper-policy source plus the reviewed operating configuration.
- The forward freeze never hashes DuckDB: new public data must advance. Every
  new proposal is registered by checksum, and drift blocks CLI simulation.
- `forward-restart --confirm-restart` is the only normal replacement path after
  genuine drift. It archives the predecessor unchanged, atomically activates a
  later baseline, refuses an unchanged trial, and does not execute a proposal.
- `forward-rollover` defaults to the read-only path for an unchanged trial with
  one expired, unexecuted proposal. Its v4 plan hash binds exact state,
  readiness, normalized proposal intent, policy, paths, deadline, and no-fill
  requirements while volatile preflight/operations observations stay outside
  the hash. `--write-plan` explicitly publishes the content-addressed plan only
  under `data/reports/forward_rollovers/plans`. Activation requires that exact
  artifact, its exact SHA, explicit confirmation, fresh gates, verified backup,
  final CAS, and crash-recoverable journal evidence.

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
8. Keep `.env`, DuckDB, and paper state owner-only. Supported mutation commands
   enforce a private creation umask, and the unauthenticated dashboard must
   remain on a loopback address.
9. DuckDB is single-process. Run DB commands sequentially and close the
   dashboard before writes. A lock collision is transient concurrency; retry
   after the other process exits.
10. Before accepting any release candidate, run
   `.venv/bin/python scripts/verify_release_wheel.py DIST_WHEEL`. It must match
   the reviewed source contents and bytes, console entry point, `METADATA`
   name/version/Python/dependency/extra contract, pure-Python `WHEEL`, and every
   `RECORD` SHA-256/size entry, then pass its default new-temporary-environment
   install/import/resource/`aios --help` smoke. The verifier's presence does
   not certify a stale or not-yet-built artifact.

## Immediate plan

1. Start with `aios preflight`. As of 2026-08-08, supervised research is
   certified through 2026-08-07 and operations are verified. The active
   2026-08-07 proposal is prospective for the 2026-08-10 close. Wait for that
   close; never record early or retrospectively.
2. The EA-to-FERG change is atomically active, current readiness passes, and
   daily, filings and backup services have successful terminal evidence. Keep
   FERG's 24 ambiguous storage keys withheld. For every new constituent event,
   repeat the exact plan, source-hash CAS, backup, disposable
   activation/rollback, explicit confirmation and immutable-receipt workflow.
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
   58% hypothetical equity decline, not a forecast.
7. Completed on 2026-07-29: governed anomaly-review v1 for SEC fundamentals
   coverage, immutable source-bound scans, preview-versus-record separation,
   deduplicated case/event history, explicit owner/disposition controls, and
   read-only dashboard visibility. The 2026-07-27 and current certified
   2026-07-29 boundaries are recorded in operations schema v6. FDXF/HONA are
   accepted explicit gaps; XOM is also resolved without manufacturing evidence.
   The stale-proposal v4 lifecycle is live-proven through one no-fill successor
   activation. Five further rule families are now implemented as library
   detectors: `price_action_mismatch@1.0.0` (scope `us-equity-prices:*`) flags a
   close-to-close move past the review threshold on an action-complete session
   that declares no split and no dividend;
   `coverage_deterioration@1.0.0` (scope `us-equity-coverage:*`) compares
   current coverage with the previous comparable measurement and stays silent
   without a baseline; `mapping_drift@1.0.0` (scope
   `us-equity-mappings:*`) reports a security holding two overlapping verified
   windows at one provider, or one provider symbol claimed by two securities on
   the same date; `share_count_jump@1.0.0` (scope `us-equity-shares:*`) flags
   implausible `shares_out` movement between consecutive point-in-time filings;
   and `conflicting_filings@1.0.0` (scope `us-equity-filings:*`) reports one
   issuer/metric/period key resolving to more than one stored value.
   `anomalies.run_detectors()` runs a selected subset and returns
   one independent scan per rule. Each family owns its scope so its monotonic
   source-boundary advances independently; `rule_bundle_version` and the
   single-rule `executed_rules` contract are unchanged. The sixth and final
   family, `factor_percentile_jump@1.0.0` (scope `us-equity-factors:*`), is now
   built: it compares each member's 0-100 composite percentile against the
   previous comparable measurement and reports a move past the threshold as an
   inputs question, never a score adjustment. All six rule families are
   complete and, as of 2026-08-11, wired into `aios anomaly-scan --rule
   RULE_ID` (repeatable) and `--all-rules`, plus `--factor-model` for the
   factor rule. Preview mode never opens the operations ledger, matching the
   legacy single-rule command exactly, which means `coverage_deterioration`
   and `factor_percentile_jump` always preview baseline-free (their real
   baseline is only fetched under `--record`) — a deliberate, documented
   trade for zero-footprint preview, not an oversight.
   `coverage_deterioration`'s baseline comes from
   `AlertStore.latest_anomaly_scan_evidence(scope)`, a new read method (the
   ledger already carries its small `current_coverage` payload).
   `factor_percentile_jump`'s baseline cannot: a 503-name score map does not
   fit the ledger's 64 KiB evidence limit, so it has its own tiny write-once
   store, `anomalies.record_factor_percentile_baseline()` /
   `latest_factor_percentile_baseline()`, under
   `data/anomaly_baselines/factor_percentiles/<universe>-<model>/<date>.json`,
   entirely outside the operations ledger. Recorded for real against
   production on 2026-08-11: 45 review cases (5 `price_action_mismatch`, 40
   `share_count_jump`) for 2026-08-07, confirmed visible in the dashboard's
   existing Operations anomaly table with zero dashboard code changes — that
   table was already generic across rule_id, never hardcoded to the SEC rule.

   A genuine second, parallel forward paper trial for QVML now runs alongside
   the certified QV trial, live as of 2026-08-11 — real, ongoing, out-of-sample
   observation, not another backtest. `data/paper/us_qvml_sandbox.json`
   ($100,000 simulated, isolated from the QV account) and
   `data/paper/us_qvml_forward_trial.json` (trial `us-qv-forward-89ebd906b8d7`,
   registered proposal `us-qvml-2026-08-10.json`, targets CINF/HST/MPC/CF/DVA/
   EIX/CVS/EXPE/OXY/EOG). Getting here required two real fixes, both in
   non-frozen `cli.py`:
   - `paper-propose`, `paper-review`, `paper-execute`, and `paper-status` all
     hardcoded the forward-trial path to the single default
     (`aios.forward.DEFAULT_FORWARD_RELATIVE_PATH`) with no override — a
     second trial was structurally impossible to operate before this. All
     four now accept `--trial`, defaulting to the original path, so every
     existing invocation is unaffected.
   - `create_paper_proposal` (frozen `paper.py`) always reads
     `row.qv_score`/`row.qv_rank` off whatever `composite_computer` returns
     and always calls it with `include_market_factors=False`; there is no
     parameter to select QVML. `paper-propose --factor-model qvml` passes
     `_qvml_selecting_composite_computer` through that exact injection
     point: it calls the real `compute_composite(..., include_market_factors=
     True)` (ignoring the frozen caller's `False`) and copies each row's
     already-independently-ranked `qvml_score`/`qvml_rank` onto
     `qv_score`/`qv_rank` before returning — a dependency injection, not an
     edit to frozen selection logic. The one honesty gap this cannot close:
     `payload["strategy"]` is a hardcoded `"qv"` literal in frozen
     `paper.py`, so every QVML proposal also gets a write-once sidecar,
     `<proposal>.factor_model_override.json`, declaring the override
     explicitly, plus a prominent CLI warning at creation time. Read that
     sidecar, not the `strategy` field, to know what a proposal actually is.
   Watch both with `aios paper-status --account ... --trial ...` and
   `aios forward-status --trial ... --account ...`; nothing here executes a
   trade or requires real money. Real evidence about whether QVML helps
   accumulates here, one real session at a time — not from re-running the
   backtest that motivated trying it.

   Next: versioned configuration boundaries.

   Experiment registration is now built as `src/aios/experiments.py`
   (`register_experiment`, `load_experiment`, `list_experiments`). Every
   backtest/factor run binds an exact Git commit, dirty-worktree fingerprint
   (changed paths only, never diff content), a streaming SHA-256 of the exact
   DuckDB file, retained raw-evidence coverage, backtest parameters and
   metrics, and an optional parent/comparison-reason pair. `frozen` and
   `holdout` purposes refuse a dirty worktree; every purpose is write-once
   under `data/experiments/<experiment_id>.json`, so append-only is enforced
   by the filesystem primitive, not just documented. Verified against the real
   402 MB `data/aios.duckdb` and the live repo's actual git state (66 dirty
   files at verification time) into a scratch directory outside `data/`; the
   round-tripped document matched exactly and its integrity hash re-verified.
   The 2026-08-11 `forward-restart` (below) narrowed the frozen bundle to 13
   files and removed `cli.py`/`alerts.py`/`storage/store.py` from it, so this
   boundary is now lifted for the active trial: `backtest-qv --output PATH
   --register-experiment [--experiment-purpose ...] [--experiment-notes ...]`
   registers inline, `aios list-experiments` and `aios compare-experiments
   ID ID...` are real CLI commands. `compare_experiments()` still never picks
   a winner; adopting a variant is still a deliberate
   `forward-restart --confirm-restart`.

   The dashboard's Paper Trial page now has one write path: when the timing
   stage reads "Review Now," a panel offers the exact same review the CLI's
   `paper-review` performs, then — after an explicit checkbox acknowledgement
   — a button that runs the identical call chain `paper-execute` uses
   (`project_maintenance_lock`, `require_registered_forward_proposal`,
   `execute_paper_proposal(confirm_simulated=True)`), all imported from the
   frozen `aios.paper`/`aios.forward`/`aios.maintenance` modules without
   editing any of them. `src/aios/dashboard.py` is not itself in the frozen
   bundle. The dashboard remains otherwise entirely read-only; this is not
   unattended execution — the checkbox is the same deliberate human
   confirmation the CLI requires, relocated into the browser. Verified with
   six new `AppTest` cases in `tests/test_dashboard_app.py`, including a real
   simulated checkbox-check-then-click through the fake write path and the
   refused-write error path; a live headless `streamlit run` smoke test also
   passed.

   A dashboard-jargon sweep found the app already follows its own "everyday
   labels, technical codes in expanders" rule everywhere except one place: the
   sidebar footer named the database engine ("DuckDB · checksum protected").
   Fixed to "Stored on this computer · Tamper-evident". No broader dashboard
   rewrite was warranted or performed — inventing edits where the rule was
   already followed would not have made the product more usable.
   `DASHBOARD_GUIDE.md` is a new plain-language walkthrough of the app itself
   (no CLI), linked from `README.md` and `BEGINNER_GUIDE.md`.

   Phase 4's research/market/account configuration domains are built as
   `src/aios/policy_domains.py`. Each domain gets a caller-assigned
   `name`/`version` and a content hash computed by importing the real values
   from `factors/policy.py`, `risk/policy.py`, and `backtest/costs.py` — all
   three frozen — rather than duplicating them by hand, so drift in the
   source changes the hash automatically. `market_profile` is hand-declared
   (benchmark `SPY`, universe `sp500`, session-close rule) rather than
   introspected, because the equivalent `market_calendar.py` values are
   private module attributes; every value matches an existing repo-wide
   convention, none invented. `experiments.register_experiment()` embeds this
   snapshot by default, and `compare_experiments()` now flags whether two
   runs shared a named policy identity via `comparable_policy`. Item 8
   (pre-August-2023 delisting/announcement provenance back to 1996) is
   deliberately not attempted here: it requires real S&P DJI archival
   research across ~28 years, and fabricating "verified" historical events
   would corrupt the one property this system actually has — trustworthy
   evidence. It remains open, and should stay open until real primary-source
   research is done, not synthesized.

   All five were verified read-only against the live 503-member database for
   the 2026-08-07 decision date, and each resulting scan passes the ledger's
   own `_prepare_anomaly_scan` contract with all four safety counters at zero:
   prices 5 findings, shares 40, coverage/mappings/filings 0. Unit tests alone
   did not establish this — their fakes accept any dataset label, give every
   member a distinct issuer, and never exercise the ledger — so five defects
   reached the live path and are now fixed and regression-tested:
   the price rule bounded itself on dataset `prices` when the archive labels
   every price response `daily-prices`, so it withheld every scan; a stored
   non-positive `shares_out` raised instead of being reported, withholding all
   503 members over 80 historical rows; the issuer-keyed rules iterated members
   rather than issuers and repeated a fingerprint for the three dual-class
   issuers, which the ledger rejects; a case subject omitted `as_of_date`, so
   one restated period collapsed into one fingerprint; and every new rule used
   a descriptive `confidence` string when the ledger accepts only
   `low`/`medium`/`high`, which alone made all five unrecordable.
   `factor_percentile_jump` was verified the same way. There is no stored
   factor-score history, so `measure_universe_factor_percentiles()` returns the
   snapshot and the caller keeps it as the next scan's baseline, matching the
   coverage rule's contract; the 503-entry score maps stay out of scan evidence
   because two of them exceed the 64 KiB limit. Against the live database it is
   correctly silent between consecutive sessions and fires across a real gap:
   comparing 2026-07-24 with 2026-08-07 reports KLAC at -48.2 percentile points
   and, from 2026-07-31, VRTX at +25.0. Note that decision dates on or before
   roughly 2026-07-10 currently score zero members, so a baseline older than
   that yields `compared_members = 0` and the scan stays silent — confirm the
   factor-price freshness gate before reading that silence as health.
   The share rule reviews the most recent `SHARES_LOOKBACK_FILINGS = 8` filings
   per issuer. Its full retained history yields 750 findings for one date,
   which no scan can store: ledger evidence is capped at 64 KiB and `alerts.py`
   is frozen, so the cap cannot be raised. Pass a larger `lookback_filings`
   deliberately for a one-off historical sweep, and expect roughly 200 findings
   to be the practical per-scan ceiling.
   Current-holdings stress, multifactor/Monte Carlo risk, and owner-authored
   scenarios remain future work.
0. **Run exactly one paper account and one forward trial until the
   `account_id` collision is fixed.** `paper.py:178` hardcodes
   `"account_id": "us-qv-supervised-sandbox"` for every account
   `paper-init` creates, which makes the cross-account guard at
   `paper.py:640` a no-op: a proposal built for one sandbox executes
   against another without complaint. `paper.py` is frozen, so this waits
   for a deliberate policy version — see `FUTURE_BUILD_PLAN.md`. The QVML
   trial was deliberately **not** restarted on 2026-08-12 for this reason.
8. Extend pre-August-2023 announcement and delisting provenance as a separate
   long-history track. **Researched, not attempted, 2026-08-12** (see
   `SP500_DATA_PROVENANCE.md`'s new section): no source found meets this
   project's evidence bar (official S&P DJI release + independent
   cross-check) for a full 1996-present extension without an institutional
   CRSP/Compustat-class subscription this project isn't budgeted for.
   S&P DJI's own press archive only goes back to 2012 on the modern domain;
   Wikipedia's changes table is unreliable pre-2001 by its own editors'
   admission; SEC EDGAR full-text search only indexes filings from 2001. A
   smaller, real extension is achievable — 2012-forward via S&P DJI's own
   archive (one release at a time, no consolidated file), optionally
   cross-checked against 2001-forward EDGAR 8-Ks — if this is picked up
   again. Do not lower the evidence bar to force full 1996 coverage.
9. Follow `INDIA_BUILD_PLAN.md`: Nifty 50 first, no NSE/BSE ingest before the
   portable schema and source/licensing gates pass. **Phase I1 is
   substantially complete as of 2026-08-11**: seven additive market-contract
   tables exist in the live schema, `src/aios/markets.py` is the validated
   registration/read layer (ISO country/currency/timezone/MIC checks, ISIN
   check-digit validation, half-open dated listings, and an `active_listing`
   that takes `as_of` and `known_as_of` separately so a later-reviewed listing
   cannot inform an earlier decision), and real `in_equity` / `xnse` / `xbom`
   identities are registered. `tests/test_market_contracts.py` proves the
   phase's actual exit claim — a synthetic ISIN/INR/NSE-shaped security scores
   and resolves through the exact same `compute_composite`, identity and
   readiness code the U.S. build uses, with no market branching in that path.
   **Phase I2 (source/licensing review) is also complete**, written up in
   `INDIA_SOURCE_MATRIX.md`: NSE's own Terms of Use bar both the collection
   method (Clause 4, scraping) and the intended use (Clause 3, "simulation
   activities... under any circumstances") for nseindia.com data, with no
   personal/research exemption on either — a free bounded beta is **not
   available**. A paid track is: NSE's own Students/Researchers tier
   (~₹18,000/yr indicative, unconfirmed current) or Twelve Data ($29/mo, best
   licence posture found — "internal business purposes," not
   personal-non-commercial). Emails drafted to `marketdata@nse.co.in`
   (pricing/waiver) and `indices@nse.co.in` (constituent history) but not yet
   sent by the user. Separately, an official dated Nifty 50 constituent file
   (`archives.nseindia.com/content/indices/IndexInclExcl.xls`) exists and is
   PIT-correct (effective dates, not announcement dates) but only covers
   1996–2020-07-31 and inherits the same Clause 3 block; post-2020 history
   would need individual dated press releases, also gated.
   **No Indian price, fundamental, or membership row exists, and none may be
   ingested until a licensed source is actually acquired** — that decision
   (spend or wait on NSE's reply) is the user's, not yet made.
10. Ask the user for jurisdiction, account type, broker, and final risk limits
   only before after-tax or controlled-capital certification.

The U.S. exact-date daily workflow, atomic constituent activation, native
systemd services, backup/restore proof and prospective rollover are complete.
Forward trial `us-qv-forward-a0b63856954c` is drifted and retained only as
invalidated history. Expired trial `us-qv-forward-72c4560a442d` is archived
unchanged with zero executions. Active trial `us-qv-forward-ea4fc2788c4d` is
policy-intact, has one prospective proposal and zero executions. Any
real-capital pilot still needs at least 8–12 weeks of untouched forward
observation plus the separate controlled-capital gates.

## Core commands

```bash
.venv/bin/aios doctor
.venv/bin/aios status
.venv/bin/aios audit
.venv/bin/aios validate
.venv/bin/aios preflight
# Full governed read-only review; produces/executes no order or simulation:
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
.venv/bin/aios anomaly-scan --preview
# after reviewing the preview: aios anomaly-scan --record
.venv/bin/aios anomalies --unresolved
.venv/bin/aios anomaly-show CASE_REF
.venv/bin/aios anomaly-ack CASE_REF --owner OWNER --note NOTE --evidence-sha256 HASH
.venv/bin/aios anomaly-resolve CASE_REF --outcome OUTCOME --owner OWNER --note NOTE --evidence-sha256 HASH
.venv/bin/aios companyfacts-v3-plan --as-of 2026-07-29
# optional read-only review artifact; still no provider fetch or v3 activation
.venv/bin/aios companyfacts-v3-plan --as-of 2026-07-29 --write-plan --json
.venv/bin/aios notifications
.venv/bin/aios notification-test
.venv/bin/aios email-status
# After private SMTP configuration: email-test --confirm-send, verify receipt,
# then email-enable --confirm-enable. Emergency stop: email-disable --confirm-disable.
.venv/bin/aios dashboard

.venv/bin/aios paper-status
.venv/bin/aios forward-status
.venv/bin/aios forward-rollover --as-of 2026-07-29 --write-plan --json
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
