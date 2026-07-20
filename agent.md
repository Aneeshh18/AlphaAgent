# AI Investment OS — agent context

This file is the short, high-signal context for an AI agent. Read it before
opening the larger architecture or source tree. The repository is a Python
3.12, local-first investment research system; it is not a trading bot.

## Mission and hard boundaries

- Build a low-cost research pipeline: public data → validated storage →
  deterministic factors → (later) backtests, reports, and portfolio tooling.
- The LLM is a future explanation/synthesis layer only. It must not replace
  numeric calculations or make an untraceable investment decision.
- No broker integration, live trading, intraday data, paid feeds, or multi-agent
  “committee” logic in the current scope.
- Preserve point-in-time (PIT) correctness in every new feature.

## Current state (2026-07-20)

Implemented:

- SEC EDGAR XBRL fundamentals; yfinance prices, optional user-token Tiingo, and
  Stooq fallback; FRED macro with Treasury yield fallback.
- DuckDB schema and idempotent upserts; local data currently contains 526
  ticker records, 529 stable security IDs, 335,796 prices (including SPY
  benchmark history), 1,225,871 active fundamentals, 42 quarantined impossible
  chronology rows, and 299,364 release-aware macro vintage rows. The 97,817
  pre-vintage rows remain only in `macro_legacy` for audit; the active table has
  zero unversioned rows.
- PIT-aware TTM helpers, Quality factor, Value factor, and regime-aware QV
  composite ranking. Initial weights are explicit in `aios.factors.policy`:
  60/40 baseline, 45/55 reflation, 65/35 stagflation, 55/45 deflationary,
  and 70/30 risk-off. Unknown or non-PIT-ready macro evidence uses baseline
  and is marked on each composite row.
- Minimal PIT-safe QV policy backtest in `aios.backtest.engine`, exposed as
  `aios backtest-qv`. It compares regime-aware versus fixed 60/40 rankings at
  quarterly decision dates, uses an explicit shared market calendar, follows
  immutable security IDs across ticker changes, records every member,
  exclusion, and selection, pairs strategy and benchmark periods, emits a
  provenance-stamped JSON audit, and refuses hard data-quality failures.
- Release-aware macro schema migration: FRED rows store observation date plus
  public vintage/release date; legacy macro rows are quarantined and excluded
  from PIT reads.
- Deterministic macro regime snapshot in `aios.macro.regime`, with CLI command
  `macro-regime` and regression coverage for revision timing.
- FRED histories are chunked by actual vintage dates below the API's 2,000-date
  limit, transient truncated responses are retried, and later runs use a
  31-day release-date overlap. One failed series no longer prevents later
  series from being attempted, but the overall command still exits non-zero.
- Local Streamlit dashboard: universe ranking, ticker deep dive, methodology.
- Regression tests cover PIT factors, macro vintages, membership,
  reference identities, conservative batch rejection, provider cutoffs,
  costs/taxes, and backtesting.
- Historical universe storage/import contract: `universe_membership` keeps
  half-open effective intervals separate from public `known_date`. A strict
  state-machine builder and 60-event manifest now certify the bounded S&P 500
  window 2023-08-01 through 2024-12-31; the generated file has 533 intervals.
  All 533 intervals were imported into the main database on 2026-07-16.
- Stable security identities: `security_master` has 529 internal IDs and
  `security_identity_assignments` maps all 533 membership intervals. Four
  source-verified transitions share IDs: ABC→COR, CDAY→DAY, FLT→CPAY, and
  Healthpeak PEAK→DOC. WRK→SW and ordinary replacements remain separate.
  Five identity integrity checks are hard validation gates.
- Reviewed reference identities: 528 issuers/CIK intervals, 531 dated
  security-owner assignments, and 534 provider-symbol intervals. Every bounded
  membership assignment now has a reviewed owner. SEC
  fundamentals route by issuer ID; prices route by security ID and are clipped
  and relabeled using provider intervals. Tiingo `DAY` safely covers both the
  dated CDAY and DAY segments of the same security. Retired WRK remains
  explicitly unavailable; pre-change PEAK has no verified provider; old DOC
  and pre-2024-07-08 SW remain explicitly blocked as wrong securities.
  Reference import is atomic, and contamination/overlap/orphan checks are hard
  validation gates.
- `aios build-reference-batch` certifies only unchanged securities spanning the
  complete bounded window. It cross-checks membership, SEC ticker-map and
  submissions identities, SEC filing continuity across the window (including
  SEC-named older shards), provider density/boundaries, unique dates, positive
  closes, and exact dated relabeling. It permits only exact dot/hyphen share-
  class notation and never treats `formerNames` as tickers; rejected candidates
  and source SHA-256 fingerprints remain in a review CSV.
  `aios ingest-reference-batch` imports accepted references atomically, then
  isolates issuer/security ingests.
- `aios build-reference-window-batch` applies the same fail-closed checks to a
  strict `ticker,start,end` CSV. It fetches the SEC ticker map once, certifies
  each independent half-open window, preserves every rejection, and refuses
  conflicting or overlapping merged manifests.
- Batch 02 accepted 25/25 candidates and added 65,836 issuer-tagged fundamental
  rows plus 8,950 security/provider-tagged price rows. Every accepted security
  has 358 unique sessions from 2023-08-01 through 2024-12-31.
- Batch 03 accepted 24/25 candidates and added 52,688 issuer-tagged fundamental
  rows plus 8,592 security/provider-tagged price rows, again with 358 unique
  sessions per accepted security. ANSS failed closed because its retired ticker
  has no SEC current-map record and remains an explicit manual exception.
- Batch 04 accepted 19/25 candidates and added 43,299 issuer-tagged fundamental
  rows plus 6,802 security/provider-tagged price rows, with 358 unique sessions
  per accepted security. Its six automatic rejections were retained for the
  separate exception review.
- Exception Batch 01 identity-resolved ANSS, BF.B, BG, BK, BLK, BRK.B, and C
  using primary evidence specific to each case. It added nine dated issuer/CIK
  and owner intervals, eight provider intervals, 15,523 fundamental rows, and
  2,148 prices. Six names have 358 sessions; ANSS has 2,111 PIT fundamentals
  but zero prices and two explicit `unavailable` provider checks.
- Large reviewed fundamental batches may use a local official SEC
  `companyfacts.zip` through `ingest-reference-batch --companyfacts-zip`. The
  reader selects exact reviewed CIK members, validates their embedded CIK, and
  never changes identity/PIT rules. Default per-CIK API reads remain best for
  small or freshness-sensitive batches.
- Batch 05 accepted 25/25, performed 59,317 fundamental and 8,950 price upserts,
  upgraded five legacy-baseline names to reviewed identities, and added 20 new
  complete-input names. SEC primary-ticker order is preserved; stale issuer
  display labels are atomically removed after a successful full refresh.
- Batch 06 accepted 23/25 and ingested 56,013 fundamental plus 8,234 price rows.
  CTRA and DFS failed the current-map-only automatic path after later mergers
  and stayed in the review CSV.
- Exception Batch 02 used official 2023 10-K identity plus merger/delisting
  evidence for CTRA/DFS and added explicit Tiingo intervals for ANSS, CTRA, and
  DFS. All three returned 358 certified sessions; the ingest refreshed 6,360
  fundamentals and wrote 1,074 prices. ANSS is no longer price-blocked.
- Batches 07–17 added 274 automatically accepted security mappings through STX. Dual-class
  FOX/FOXA and GOOG/GOOGL correctly share issuers. Exception Batches 03–05
  resolve FI, HES, HOLX, IPG, JNPR, and K using historical SEC filings plus
  complete bounded provider responses. Seven transient Yahoo empties in Batch
  11 were retried individually and each restored to 358 sessions.
- Exception Batch 06 resolves the SEC-proven MMC→MRSH same-security provider
  alias with 2,396 fundamental upserts and 358 certified price sessions.
- Batch 15 automatically accepted 24/25 candidates, adding 51,061 fundamental
  upserts and 8,592 prices. Exception Batch 07 resolves the retired historical
  PARA identity with official SEC evidence and a direct 358-session Tiingo
  window, adding 2,267 fundamental upserts and 358 prices. The cancelled old
  PARA shares remain distinct from successor PSKY shares and issuer.
- Batch 16 accepted 25/25 candidates with no exception queue, adding 57,351
  fundamental upserts and 8,950 complete 358-session price histories.
- Batch 17 accepted 25/25 candidates with no exception queue, adding 63,032
  fundamental upserts and another 8,950 complete 358-session price histories.
- Batches 18 and 19 accepted 25/25 each. Batch 20 accepted 22/24 automatically;
  Exception Batch 08 uses historical SEC filings and direct Tiingo windows for
  retired WBA and the pre-successor XOM issuer. Together they added 180,723
  fundamental and 26,492 price upserts after Batch 17.
- Window Batch 01 adjudicated all 53 shorter constituent spans: 43 passed the
  automatic path, six current-map exceptions passed primary SEC plus Tiingo
  review in Exception Batch 09, CDAY→DAY passed same-security provider relabel
  review in Exception Batch 10, and PEAK/WRK remained explicit terminal
  provider gaps after Yahoo, Tiingo, and Stooq checks. The three batches added
  92,177 fundamental and 8,946 price upserts.
- `aios universe-coverage` measures dated member-level inputs. The local result
  is 501/503 with both prices and PIT fundamentals on 2023-09-29 and 503/504 on
  2024-09-30. The exact gaps are PEAK/WRK provider history and AMTM fundamentals,
  whose first public Company Facts date is 2024-12-17; do not backdate any of
  them or call the current backtest fully investable.
- Stateful execution: `aios.backtest.portfolio` persists stable-security
  positions and FIFO lots, trades equal-weight deltas, applies splits/dividend
  cash on raw closes, and records orders plus daily net/gross equity. The old
  `simulate_period` helper remains only for interval compatibility tests.
- Explicit benchmarks are persistent zero-friction books on the same calendar,
  execution timing, raw-price/action convention, and daily sessions; missing
  evidence is never silently substituted.
- Factor execution uses a decision-scoped `FactorDataCache`. It batches all
  required PIT fundamentals per ticker/date, shares them across Quality and
  Value, and discards the scope before returning. The 2023-09-29 profile fell
  from 42,235 queries/174.1 seconds to 3,874 queries/17.4 seconds with zero score
  or selection differences; the complete five-decision result and ticker
  explanations also matched the certification exactly.
- The reproducible bounded smoke run used the 22 locally researched names,
  top 10, five quarterly comparison periods, 5 bps commission plus 5 bps
  slippage per side, zero tax rates, and SPY. Regime-aware and fixed 60/40 both
  returned 44.99% net (46.39% gross, 34.66% annualized) versus SPY at 41.49%.
  Both policies chose the same equal-weight sets in every period, so this run
  validates mechanics only and provides no evidence of regime-policy alpha.
- The post-Batch-05 unfiltered audit enumerated all 525 historical tickers and
  preserved 503–504 dated members. Eligibility was 77, 76, 78, 78, and 77.
  Regime-aware returned 62.65% net (64.20% gross; 47.64% annualized; 10.89%
  volatility; $1,317.87 costs; 10.50x turnover). Fixed 60/40 returned 64.14%
  net (65.71% gross; 48.72% annualized; 10.63% volatility; $1,343.97 costs;
  10.52x turnover). SPY returned 41.49%. This is partial-sample pipeline
  evidence only; it is not a full-universe result or regime-alpha evidence.
- The stateful 2026-07-20 bounded audit enumerates all 525 tickers and preserves
  503, 503, 503, 503, and 504 decision-date members. Raw-complete counts are
  501, 500, 502, 502, and 503; Quality+Value eligibility is 291, 293, 350, 308,
  and 301. PEAK, WRK, and pre-filing AMTM are explicit exclusions. Five paired
  strategy/SPY periods completed across 316 exact shared sessions. Regime-aware
  returned 70.25% net (71.13% gross; 52.72% annualized; 17.66% daily
  annualized volatility; -9.94% max drawdown; $671.32 costs; 5.17x turnover).
  Fixed 60/40 returned 72.58% net (73.51% gross; 54.37% annualized; 17.31%
  volatility; -8.32% drawdown; $700.79 costs; 5.37x turnover). Persistent SPY
  returned 37.16% with 12.49% volatility and -8.41% drawdown. Tax rates were
  zero. This remains a short, in-sample pipeline diagnostic—not an investable
  result or regime-alpha evidence. The 2026-07-18 forced-liquidation v1 artifact
  is retained only as a regression baseline.
- Forty-two source rows with `period_end > as_of_date` were moved intact to
  `fundamentals_quarantine`. Extraction, upsert, validation, and PIT reads now
  fail closed on this impossible chronology.
- Ingest outcomes are now recorded in `ingest_log`; `aios audit` displays them.
- Repeated price ingestion refreshes a five-day overlap instead of requesting
  full history; batch SEC ticker-map lookup is done once per batch.
- The original 22-name refresh completed. XOM later required a bounded historical
  CIK exception because the live SEC ticker map points `XOM` to a different 2026
  successor registrant; that current mapping is not backcast onto 2023–2025.
- The controlled issuer batch added 10,093 issuer-tagged fundamentals and
  1,050 identity-tagged prices. Forbidden pre-cutoff Healthpeak-DOC and
  Smurfit-Westrock-SW row counts are both zero.

Important data note: the 4,362 legacy `ebitda` rows that were incorrectly
sourced from net income have been removed after backup. The current code derives
EBITDA only from operating income + depreciation. Impossible future-period
facts are preserved only in `fundamentals_quarantine`. The existing database
predates ingest auditing, so its old rows have no audit records; only new runs
appear in `ingest_log`.

## Non-negotiable PIT rule

`fundamentals.period_end` says what period a number describes.  
`fundamentals.as_of_date` says when it became knowable (SEC filing date).

For a decision date `D`, read only rows with `as_of_date <= D`. Use
`Store.pit_fundamentals()` or the shared helpers in `aios.factors.common`; do not
query fundamentals directly with “latest period” logic. Prices use the latest
close where `date <= D`. Never use a filing’s period end as its availability
date.

Macro has two dates and follows the same rule:

- `macro.date`: the economic observation period.
- `macro.release_date`: when that vintage became public.

For macro decision date `D`, use `Store.pit_macro_history()` or
`Store.pit_macro_latest()`. These select the latest vintage for each observation
with both `release_date <= D` and `date <= D`. Rows migrated from the old schema
have `release_date IS NULL`, source `legacy_unversioned`, and are intentionally
not eligible for regime/backtest inputs. `aios validate` reports these as a
hard failure until the required re-ingest is complete. New `upsert_macro()`
calls must include `release_date`.

Historical universe membership follows the same discipline:

- `effective_start` / `effective_end`: when the constituent membership applies;
- `known_date`: when that membership became publicly knowable; and
- `source`: provenance for the membership assertion.

For decision date `D`, use `Store.universe_membership_on(universe_id, D)`. It
requires `known_date <= D` and an active half-open effective interval. Never
derive `known_date` from a later constituent list without provenance. The
strict backtest requires this table; `allow_current_universe=True` is only an
explicit survivorship-biased diagnostic mode.

`build-universe-membership` takes a baseline span file plus event-level
`Addition`/`Deletion` rows. It requires independent `known_date` and source
evidence, replays membership state, supports re-entry, reconciles reference
boundaries, and closes all rows after the certified end. The current S&P
manifest is intentionally partial history; consult `SP500_DATA_PROVENANCE.md`.
For index changes, a timestamped S&P document is canonical. An exact SEC filing
may be labeled fallback evidence, but its filing date is not automatically the
first public announcement. Never infer `known_date` as
`effective_date - N days`.

Ticker is not a durable identity. For historical universe work, every
membership row must have a `security_id` backed by
`security_identity_assignments`. `build-security-identities` only joins
explicit transition evidence and gives all other tickers a clearly labeled
bounded ID. Never merge histories merely because one ticker replaced another,
and never use CIK as if it were a security/share-class identifier.

Reviewed rows follow this chain:

```text
dated universe ticker → security_id → dated issuer_id → dated SEC CIK
                                  ↘ dated provider + provider_symbol
```

Fundamentals follow issuer identity; prices follow security identity. Provider
responses must be clipped to `provider_symbol_history`, relabeled to the dated
market ticker, and stored with both `security_id` and `provider_symbol`.
`unavailable` and `blocked_wrong_security` are terminal review states, not
signals to guess another alias. Legacy ticker reads remain only for unreviewed
names during controlled migration.

The release-aware backfill and guarded cleanup completed on 2026-07-16.
`aios validate` has no active PIT failures, and both current and historical
regime snapshots are PIT-ready. The Treasury CSV currently returns HTTP 403 to
automated requests in this environment; this is a non-blocking fallback issue
because FRED's DGS2/DGS10/DGS30 histories are complete.

The latest validation also has no universe, price, fundamental, or macro hard
failure. Four historical failed-ingest audit rows and one historical zero-row
ingest remain warnings; inspect them with `aios audit`, but do not confuse them
with current table-integrity failures.

## Runtime flow

```text
aios CLI / scheduled job
  → ingest source adapters
  → Store upsert helpers
  → data/aios.duckdb
  → PIT factor functions
  → dashboard / future backtest / future LLM report
```

The only DuckDB connection owner is `aios.storage.store.Store`. Paths are
resolved from `.env` by `aios.config.settings`; relative paths are project-root
relative. Never commit `.env` or secrets.

## File map

| Path | Responsibility |
|---|---|
| `src/aios/config.py` | pydantic settings, paths, directory creation |
| `src/aios/cli.py` | CLI commands including `ingest-macro` and `macro-regime` |
| `src/aios/storage/schema.py` | DuckDB tables and PIT schema invariant |
| `src/aios/storage/store.py` | connection, upserts, PIT reads, diagnostics |
| `src/aios/ingest/http_client.py` | rate limits, SEC User-Agent, retries/backoff |
| `src/aios/ingest/edgar.py` | ticker→CIK, XBRL extraction, filing dates, metadata |
| `src/aios/ingest/prices.py` | yfinance/Stooq normalized price rows |
| `src/aios/ingest/fred.py` | FRED series and Treasury CSV rows |
| `src/aios/ingest/universe.py` | validated historical membership CSV import |
| `src/aios/ingest/security_identity.py` | stable ID build/import and transition validation |
| `src/aios/ingest/reference_identity.py` | strict issuer/CIK/owner/provider manifest import |
| `src/aios/ingest/reference_batch.py` | conservative SEC/provider batch build, review, and ingest orchestration |
| `src/aios/macro/regime.py` | deterministic growth/inflation/curve/stress regime |
| `src/aios/factors/common.py` | shared PIT metrics, TTM, percentiles, market cap |
| `src/aios/factors/quality.py` | ROIC, FCF margin, gross margin, Piotroski |
| `src/aios/factors/value.py` | P/E, EV/EBITDA, P/FCF, EV/Sales, P/B |
| `src/aios/factors/policy.py` | coverage gates and regime weight policy |
| `src/aios/factors/composite.py` | universe-relative, regime-aware QV score/rank/grade |
| `src/aios/backtest/engine.py` | PIT quarterly QV policy comparison harness |
| `src/aios/backtest/costs.py` | cost/tax policies and interval compatibility simulator |
| `src/aios/backtest/portfolio.py` | persistent positions/lots, delta orders, corporate actions, and daily equity |
| `src/aios/dashboard.py` | read-only Streamlit UI |
| `tests/` | fast local regression checks |

External-project/source verdicts live in `OPEN_SOURCE_RESEARCH.md`. Verify that
file before adding a suggested package, scraped feed, or provenance shortcut.

## Factor semantics

- TTM flow metrics use EDGAR’s single-period `quarter_value`, not raw YTD
  values. TTM is annual + new quarters − matching prior-year quarters.
- Quality is a percentile blend of ROIC, FCF margin, gross margin, and
  Piotroski F. Financial institutions need a separate model; do not interpret
  standard ROIC for banks as meaningful.
- Value ranks low positive multiples as cheap. Negative/zero denominators are
  excluded rather than ranked.
- Composite uses the PIT-ready macro regime's configured weights only when both
  sub-factors are present. Missing inputs are visible in each row’s `missing`
  list. Current coverage policy requires at least 2 of 4 Quality components,
  at least 2 of 5 Value multiples, and both sub-factor scores before publishing
  QV. If macro evidence is unavailable, QV remains calculable with the explicit
  baseline fallback and `macro_regime_pit_unavailable` marker.

## Safe working procedure

1. Read this file, then only the modules relevant to the task.
2. Preserve user data and existing `.env`; never reset or delete the DuckDB.
3. Add/adjust a regression test for every PIT, ingestion, or formula change.
4. Run `PYTHONPATH=src .venv/bin/pytest -q`.
5. Run the affected CLI or factor function against a temporary DB when possible.
6. Report assumptions, data limitations, and whether a re-ingest is required.

The local `.venv` launchers and editable path were repaired to this checkout on
2026-07-16. `.venv/bin/aios` now resolves to `AI-Invester/src/aios`; keep the
`PYTHONPATH=src` form in automation as an additional checkout-safety guard.

## Macro regime contract

`compute_regime(as_of, store)` reads five core series through the PIT macro
helpers: real GDP growth, CPI, 10Y–2Y spread, VIX, and Baa/10Y credit spread.
It derives CPI year-over-year inflation and emits one of `goldilocks`,
`reflation`, `stagflation`, `deflationary`, `risk_off`, or `unknown`.
Mandatory evidence is growth, CPI YoY history, and at least one stress signal;
missing inputs produce `unknown` with an explicit `missing` list. The composite
reads this snapshot once per universe and applies `weights_for_regime()` only
when it is PIT-ready and dated exactly as the composite decision date. A caller
may inject a same-date `regime_snapshot` to reuse an already-audited snapshot.

## Backtest contract

`run_qv_policy_backtest(start, end, ...)` is the current validation harness. It
uses explicit market-calendar decision dates, equal-weight top-N selections,
the same scheduled next-session/quarter-end dates and daily sessions for both
policies and every benchmark, and shared PIT factor snapshots. Historical
membership is required by default; `allow_current_universe=True` is an
explicitly labeled survivorship-biased diagnostic escape hatch. Stable-security
positions and FIFO lots persist across quarters; prior holdings remain invested
until the next-session close and only target-weight deltas trade. Raw closes,
splits, and dividend cash drive both daily valuation and tax accounting. Taxes
use caller-supplied rates; wash sales, cross-bucket offsets, carryforwards, and
jurisdiction-specific filing rules are not modeled. Benchmarks use the same
persistent raw-price/action convention without strategy costs or taxes.

Library costs and tax defaults are zero for deterministic diagnostics. The CLI
defaults to 5 bps commission and 5 bps slippage per side, while tax rates stay
zero until the operator supplies a jurisdiction-appropriate model. Net and
zero-friction shadow books produce aligned daily curves; daily returns drive
annualized volatility and drawdown, while win rate remains period-based. A
failed transition is atomic and stops later stateful periods rather than
inventing a portfolio bridge.

The certified 2023-08-01 through 2024-12-31 near-complete audit uses the real
503–504 membership denominator and strict factor publication gates. Its local
JSON artifact is `data/backtests/qv_sp500_pit_2023-08_2024-12.json` (gitignored).
Do not use its short in-sample return or 100% period win rate as an
investment-performance claim. Daily drawdown is now real for the stored price
paths, but the window and strategy remain in-sample.

## Working delivery estimate (2026-07-20)

These are focused engineering estimates, not fixed calendar promises. Evidence
review and unavailable delisted histories, not CPU time, are the main risk.

| Scope | Estimated focused time | Exit condition |
|---|---:|---|
| Bounded issuer/security batch program | complete | all 533 assignments have reviewed owners; 51/53 final short spans have data and two are explicit terminal provider gaps |
| Final bounded factor-eligibility audit and policy rerun | complete | five paired periods, full member audit, exact exclusions, and local JSON provenance artifact |
| Persistent execution/tax-lot/daily-curve layer | complete | atomic delta rebalances plus 316 aligned strategy/SPY sessions in schema-v2 audit |
| Complete certified 2023-08 to 2024-12 identity window | complete | 24 hard validators at zero, versioned manifests, and 501/503 plus 503/504 dated input coverage |
| Decision-scoped factor optimization | complete | 503-name decision in 17.4 seconds and five-decision rerun in about 92 seconds with exact output parity |
| Supervised local research beta | available now | dashboard headless render: 526 tickers in 12.5 seconds, zero exceptions; CLI health checks clean |
| Paper-trading beta | 2–4 focused weeks | QVML, risk constraints, refresh monitoring, backtest/report UI, and untouched holdout |
| Controlled small-capital pilot | 8–12 focused weeks minimum | longer PIT/walk-forward evidence, broker reconciliation, alerts, price versioning, and user-approved tax/risk policy |
| Free-source 1996-present institutional-style history | 6–12+ weeks | announcement provenance plus delisted/renamed prices; some gaps may remain impossible without licensed data |
| Institutional-style product breadth | 12–20+ focused weeks | broader provenance, risk/portfolio layer, reports, monitoring, and out-of-sample validation |

A licensed survivorship-safe data source could shorten the long-history branch,
but it would require explicit user authorization and would not remove the need
for index announcement `known_date` evidence.

## Next intended work

1. Add Momentum and Low Volatility to form QVML, then freeze an untouched
   holdout and create a monitored paper-portfolio/risk workflow.
2. Expose certified backtest artifacts, data freshness, exclusions, and risk
   assumptions in the dashboard without turning ranks into trade instructions.
3. If institutional completeness is required, evaluate a user-authorized
   licensed historical-price source specifically for PEAK and WRK. Free-source
   exhaustion is documented and must not trigger an alias guess.
4. Extend official announcement and stable-identity coverage before 2023-08;
   do not extrapolate the existing bounded S&P 500 slice.
5. Settle jurisdiction, account type, broker, tax-lot, and risk-budget inputs
   with the user before certifying after-tax or controlled-capital behavior.
6. Repair/replace the automated Treasury CSV fallback while retaining FRED as
   the working primary yield source.
7. Add provider-neutral LLM JSON-packet synthesis only after deterministic
   outputs are audited. Anthropic and Z.ai keys may exist in `.env`, but are
   reserved and must never enter numeric calculations or identity decisions.
   A 2026-07-17 Sonnet review attempt reached Anthropic but was rejected for
   insufficient API credits; no model output was used as data evidence.

## Commands

```bash
PYTHONPATH=src .venv/bin/python -m aios.cli doctor
PYTHONPATH=src .venv/bin/python -m aios.cli status
PYTHONPATH=src .venv/bin/python -m aios.cli audit
PYTHONPATH=src .venv/bin/python -m aios.cli validate
PYTHONPATH=src .venv/bin/python -m aios.cli ingest-macro
PYTHONPATH=src .venv/bin/python -m aios.cli build-universe-membership --help
PYTHONPATH=src .venv/bin/python -m aios.cli import-universe data/membership.csv --universe-id sp500
PYTHONPATH=src .venv/bin/python -m aios.cli build-security-identities --help
PYTHONPATH=src .venv/bin/python -m aios.cli import-security-identities data/identities.csv
PYTHONPATH=src .venv/bin/python -m aios.cli import-reference-identities \
  --issuer-ciks examples/sp500_issuer_cik_history_verified.csv \
  --security-issuers examples/sp500_security_issuer_assignments_verified.csv \
  --provider-symbols examples/sp500_provider_symbol_history_verified.csv
PYTHONPATH=src .venv/bin/python -m aios.cli build-reference-batch \
  examples/sp500_reference_batch_05_tickers.txt \
  --batch-name sp500_reference_batch_05 \
  --output-dir /tmp/aios_reference_batch_05 \
  --start 2023-08-01 --end 2025-01-01 --verified-date 2026-07-17
PYTHONPATH=src .venv/bin/python -m aios.cli build-reference-window-batch \
  examples/sp500_reference_window_batch_01_windows.csv \
  --batch-name sp500_reference_window_batch_01 \
  --output-dir /tmp/aios_reference_window_batch_01 \
  --verified-date 2026-07-18
PYTHONPATH=src .venv/bin/python -m aios.cli ingest-reference-batch \
  --issuer-ciks examples/sp500_reference_batch_05_issuer_ciks.csv \
  --security-issuers examples/sp500_reference_batch_05_security_issuers.csv \
  --provider-symbols examples/sp500_reference_batch_05_provider_symbols.csv \
  --start 2023-08-01 --end 2025-01-01
PYTHONPATH=src .venv/bin/python -m aios.cli ingest-issuer aios:issuer:cencora
PYTHONPATH=src .venv/bin/python -m aios.cli ingest-security-prices \
  aios:security:cencora-common --provider yfinance \
  --start 2023-08-01 --end 2025-01-01
PYTHONPATH=src .venv/bin/python -m aios.cli universe-coverage --universe-id sp500 --as-of 2023-09-29
PYTHONPATH=src .venv/bin/python -m aios.cli macro-regime --as-of 2025-12-31
PYTHONPATH=src .venv/bin/python -m aios.cli backtest-qv \
  --start 2023-08-01 --end 2024-12-31 --top-n 10 \
  --universe-id sp500 --benchmark SPY --calendar SPY \
  --exclude-ticker PEAK --exclude-ticker WRK --exclude-ticker AMTM \
  --explain-ticker PEAK --explain-ticker WRK --explain-ticker AMTM \
  --output data/backtests/qv_sp500_pit_2023-08_2024-12.json
PYTHONPATH=src .venv/bin/python -m aios.cli quarantine-invalid-fundamentals
PYTHONPATH=src .venv/bin/python -m aios.cli cleanup-legacy-macro
PYTHONPATH=src .venv/bin/python -m aios.cli cleanup-legacy-ebitda
PYTHONPATH=src .venv/bin/pytest -q
.venv/bin/streamlit run src/aios/dashboard.py
```
