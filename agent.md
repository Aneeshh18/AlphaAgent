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

## Current state (2026-07-16)

Implemented:

- SEC EDGAR XBRL fundamentals; yfinance prices with Stooq fallback; FRED macro
  with Treasury yield fallback.
- DuckDB schema and idempotent upserts; local data currently contains 28
  securities, 165,034 prices (including SPY benchmark history), 65,288
  fundamentals, and 299,364 release-aware macro
  vintage rows. The 97,817 pre-vintage rows remain only in `macro_legacy` for
  audit; the active table has zero unversioned rows.
- PIT-aware TTM helpers, Quality factor, Value factor, and regime-aware QV
  composite ranking. Initial weights are explicit in `aios.factors.policy`:
  60/40 baseline, 45/55 reflation, 65/35 stagflation, 55/45 deflationary,
  and 70/30 risk-off. Unknown or non-PIT-ready macro evidence uses baseline
  and is marked on each composite row.
- Minimal PIT-safe QV policy backtest in `aios.backtest.engine`, exposed as
  `aios backtest-qv`. It compares regime-aware versus fixed 60/40 rankings at
  quarterly decision dates, enters next session, uses adjusted closes, and
  refuses hard data-quality failures. The first 2015–2026 diagnostic completed
  45 periods but did not validate the tilts: 29.81% annualized regime-aware
  versus 30.79% baseline, before costs/taxes and with current-universe
  survivorship bias.
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
- Fifty regression tests cover PIT factors, macro vintages, membership,
  reference identities, provider cutoffs, costs/taxes, and backtesting.
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
- Reviewed reference identities: six issuers, six CIK intervals, six dated
  security-owner assignments, and eight yfinance symbol intervals. SEC
  fundamentals route by issuer ID; prices route by security ID and are clipped
  and relabeled using provider intervals. DAY/WRK are explicitly unavailable;
  old DOC and pre-2024-07-08 SW are explicitly blocked as wrong securities.
  Reference import is atomic, and contamination/overlap/orphan checks are hard
  validation gates.
- `aios universe-coverage` measures dated member-level inputs. The local result
  is 24/503 with both prices and PIT fundamentals on 2023-09-29 and 26/504 on
  2024-09-30; do not call the current backtest full-universe.
- Friction-aware backtest: `aios.backtest.costs` charges commission, slippage,
  fixed fees, and supplied taxes; split-aware lots and dividend cash are
  accounted for, with gross/net/cost/tax/turnover metrics.
- Explicit benchmark reporting uses adjusted-close total returns and never
  silently substitutes a missing benchmark.
- The reproducible bounded smoke run used the 22 locally researched names,
  top 10, five quarterly comparison periods, 5 bps commission plus 5 bps
  slippage per side, zero tax rates, and SPY. Regime-aware and fixed 60/40 both
  returned 44.99% net (46.39% gross, 34.66% annualized) versus SPY at 41.49%.
  Both policies chose the same equal-weight sets in every period, so this run
  validates mechanics only and provides no evidence of regime-policy alpha.
- Ingest outcomes are now recorded in `ingest_log`; `aios audit` displays them.
- Repeated price ingestion refreshes a five-day overlap instead of requesting
  full history; batch SEC ticker-map lookup is done once per batch.
- The original 22-name refresh completed; XOM required an explicit SEC CIK override
  because the SEC ticker file pointed it at a subsidiary with no US-GAAP facts.
- The controlled issuer batch added 10,093 issuer-tagged fundamentals and
  1,050 identity-tagged prices. Forbidden pre-cutoff Healthpeak-DOC and
  Smurfit-Westrock-SW row counts are both zero.

Important data note: the 4,362 legacy `ebitda` rows that were incorrectly
sourced from net income have been removed after backup. The current code derives
EBITDA only from operating income + depreciation. The existing database
predates ingest auditing, so its old rows have no audit records; only new runs
appear in `ingest_log`.
The last reproducible policy regression intentionally retains its original
22-ticker input slice. Newly covered identity names have not been used to
reinterpret that result. Missing inputs are surfaced and must be resolved or
intentionally excluded before any broader backtest.

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
| `src/aios/macro/regime.py` | deterministic growth/inflation/curve/stress regime |
| `src/aios/factors/common.py` | shared PIT metrics, TTM, percentiles, market cap |
| `src/aios/factors/quality.py` | ROIC, FCF margin, gross margin, Piotroski |
| `src/aios/factors/value.py` | P/E, EV/EBITDA, P/FCF, EV/Sales, P/B |
| `src/aios/factors/policy.py` | coverage gates and regime weight policy |
| `src/aios/factors/composite.py` | universe-relative, regime-aware QV score/rank/grade |
| `src/aios/backtest/engine.py` | PIT quarterly QV policy comparison harness |
| `src/aios/backtest/costs.py` | execution costs, tax lots, and benchmarks |
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
uses quarterly price-calendar decision dates, equal-weight top-N selections,
common next-session entries/exits, and shared PIT factor snapshots. Historical
membership is required by default; `allow_current_universe=True` is an
explicitly labeled survivorship-biased diagnostic escape hatch. Costs are
charged on entry/exit notional. Taxes use split-aware realized lots and
dividend cash with caller-supplied rates; wash sales, carryforwards, and
jurisdiction-specific filing rules are not modeled. Benchmarks are explicit
tickers and use adjusted-close total returns.

Library costs and tax defaults are zero for deterministic diagnostics. The CLI
defaults to 5 bps commission and 5 bps slippage per side, while tax rates stay
zero until the operator supplies a jurisdiction-appropriate model.

The certified 2023-08-01 through 2024-12-31 regression is intentionally the
original 22-name data slice, not a 500-name strategy test. It completed five quarters with only
11-12 factor-eligible names per decision and identical top-10 membership under
both policies. Do not use its return, zero quarter-end drawdown, or 100% period
win rate as an investment-performance claim.

## Next intended work

1. Extend official announcement and stable-identity coverage before 2023-08;
   do not extrapolate the existing bounded S&P 500 slice.
2. Expand reviewed issuer/CIK/security/provider manifests, PIT fundamentals,
   and survivorship-safe prices from 24/26 dated coverage to every member needed
   by the certified window. Do not bulk-ingest unresolved aliases.
3. Evaluate a user-authorized Norgate Platinum Windows pilot only if free
   delisted-price/membership coverage remains the bottleneck; it does not solve
   announcement `known_date`.
4. Add momentum and low-volatility factors to form QVML after breadth improves.
5. Repair/replace the automated Treasury CSV fallback while retaining FRED as
   the working primary yield source.
6. Add provider-neutral LLM JSON-packet synthesis only after deterministic
   outputs are audited. Anthropic and Z.ai keys may exist in `.env`, but are
   reserved and must never enter numeric calculations or identity decisions.

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
PYTHONPATH=src .venv/bin/python -m aios.cli ingest-issuer aios:issuer:cencora
PYTHONPATH=src .venv/bin/python -m aios.cli ingest-security-prices \
  aios:security:cencora-common --provider yfinance \
  --start 2023-08-01 --end 2025-01-01
PYTHONPATH=src .venv/bin/python -m aios.cli universe-coverage --universe-id sp500 --as-of 2023-09-29
PYTHONPATH=src .venv/bin/python -m aios.cli macro-regime --as-of 2025-12-31
PYTHONPATH=src .venv/bin/python -m aios.cli backtest-qv --start 2023-08-01 --end 2024-12-31 --universe-id sp500 --benchmark SPY
PYTHONPATH=src .venv/bin/python -m aios.cli cleanup-legacy-macro
PYTHONPATH=src .venv/bin/python -m aios.cli cleanup-legacy-ebitda
PYTHONPATH=src .venv/bin/pytest -q
.venv/bin/streamlit run src/aios/dashboard.py
```
