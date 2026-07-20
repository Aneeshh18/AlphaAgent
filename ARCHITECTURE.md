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
- systemd timers give us logging (journalctl), retries, calendar expressions, and dependency ordering for free, with no extra dependency.
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
- **Stooq** offers free, no-API-key, downloadable CSV historical data — a genuine independent fallback when yfinance fails. Coverage skews US/Europe.
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
- Operational note (2026-07-16): Treasury's automated CSV currently returns
  HTTP 403 in this environment. FRED yield histories are complete; replacing
  or repairing that fallback remains follow-up work, not a PIT blocker.
**Cost:** $0.
**Keys needed:** Free FRED API key (one signup, takes 2 minutes).

### 7. LLM (SYNTHESIS ONLY) → **Claude API (primary), GLM-5.2 (fallback)**
**Decision:** The LLM is used *only* as the synthesis/reporting layer. It never makes the numeric decision. Claude primary, GLM-5.2 fallback.
**Why:**
- This corrects the single biggest flaw in the original prompt: the LLM cannot run Monte Carlo, compute VaR, rank 8000 stocks by factor score, or maintain state across sessions. Those are deterministic Python jobs.
- The LLM's job is narrow and high-value: take a JSON packet of factor scores + fundamentals + macro regime + news → produce the human-readable research report, explain a recommendation, narrate a portfolio review. This is what LLMs are actually good at.
- **No multi-agent "committee" theater.** If we later run multiple "agents," they will be agents running *different computations* (factor agent = numeric model, valuation agent = DCF code, news agent = NLP), and the LLM synthesizes their *outputs*. Never personas sharing one model.
- For the MVP/foundation phase (now), the LLM is not even wired in yet — we build the data pipeline first. The LLM slot is reserved but empty until the data layer is proven.
**Cost:** $0 in this phase. Later: pay-per-token, controllable.

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
2026-07-16. Treasury's daily CSV does not expose a separate release timestamp, so its fallback rows use the
observation date under the system's after-close/next-session decision
convention.

### Historical universe and membership PIT contract

`universe_membership` is the backtest universe boundary. Each row stores a
half-open effective interval `[effective_start, effective_end)`, the date that
membership became publicly knowable (`known_date`), and source provenance.
For a decision date `D`, `Store.universe_membership_on()` requires both
`known_date <= D` and an active effective interval. It is not valid to replace
`known_date` with `effective_start`: index changes are often announced before
they take effect.

`aios import-universe path.csv` is the controlled input. The importer rejects
missing known dates, invalid intervals, and unsupported columns; the storage
layer rejects overlapping intervals and records an ingest audit row.
`aios build-universe-membership` is the preceding provenance gate: it creates a
bounded baseline, replays announced additions/deletions as a state machine,
supports re-entry, reconciles every event identity against an independent span
source, and refuses missing or contradictory boundaries. Small official event
manifests live under `examples/`; bulk source/output data stays gitignored.

The currently audited S&P 500 manifest covers 2023-08-01 through 2024-12-31.
Issuer announcements identify same-security ticker transitions; S&P Global
releases identify index decisions. Three one-to-two-day conflicts in the free
reference spans are retained as explicit warnings and resolved to official
release dates. See `SP500_DATA_PROVENANCE.md`. This bounded slice is suitable
for software validation, not a full-history investable performance claim.

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
free provider is immutable or that adjusted-price vintages are archived. A
later ingest may observe provider corrections; exact price-payload versioning
remains separate future work and is required for fully reproducible vendor-data
reconstruction.

Large reviewed batches may optionally read fundamentals from a local official
SEC `companyfacts.zip` via `ingest-reference-batch --companyfacts-zip`. The ZIP
is opened once and only exact reviewed CIK members are parsed; member presence,
JSON shape, and embedded CIK must validate before ingestion. The default remains
the real-time per-CIK Company Facts API because the bulk archive is only updated
nightly. This optimization changes transport, not identity rules, PIT dates, or
the separate Submissions metadata request.

Reference import remains one atomic transaction. Data ingestion is isolated per
accepted issuer and security so a network/provider failure cannot hide or roll
back successful peers; each failure is reported and logged independently. A
successful full issuer refresh also atomically removes facts for that same
`issuer_id` under an obsolete canonical display ticker, preventing duplicate
PIT rows when SEC's multi-ticker ordering is corrected.

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

The 2023-09-29 503-member profile dropped from 42,235 DuckDB queries and 174.1
seconds to 3,874 queries and 17.4 seconds. The optimized five-decision bounded
run completed in about 92 seconds. Its full result and ticker-explanation JSON
matched the pre-optimization schema-v2 certification exactly; performance is
not allowed to change PIT eligibility, scores, selections, or accounting.

### Cost, tax, and benchmark contract

`aios.backtest.portfolio` keeps execution state separate from factor ranking;
`aios.backtest.costs.simulate_period` remains only as the interval-level
compatibility harness:

- positions and FIFO lots are keyed by stable security identity and persist
  across decision dates;
- commission and slippage are charged only on traded rebalance deltas;
- fixed fees are charged once per non-zero order and deducted from book cash;
- raw `close`, dividends, and split ratios drive both daily valuation and tax
  accounting, preventing adjusted-price/dividend double counting;
- buy costs enter lot basis and sell costs reduce proceeds; realized gains use
  FIFO holding periods, while gains/losses net within each short/long bucket
  over the run and dividends accrue tax at the supplied rate;
- net and zero-friction shadow books use the same selection schedule instead of
  approximating gross return by adding costs back; and
- benchmarks are explicit persistent zero-friction books on the same calendar,
  execution dates, raw-price/action convention, and daily sessions.

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

`aios backtest-qv` compares the regime-aware QV ranking with the same-date,
fixed 60/40 ranking. Each quarter uses only factor, macro, and historical
membership evidence known at the decision date, selects equal-weight top-N
holdings, keeps prior positions invested through the next-session close, and
then trades only the target-weight deltas. Quarter boundaries and every daily
valuation session come from one explicit market-calendar ticker. Price paths
use immutable security IDs, so a reviewed ticker change does not become a false
sale or missing delisting. Historical membership is required by default.
`--allow-current-universe` is available only as an explicitly labeled
survivorship-biased diagnostic. Each schema-v2 result records the member-list
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

The schema-v2 stateful rerun on 2026-07-20 preserved every selection and
eligibility count while carrying positions/lots and producing 316 aligned daily
observations. Regime-aware returned 70.25% net (71.13% gross), fixed 60/40
returned 72.58% net (73.51% gross), and persistent SPY returned 37.16%. Daily
max drawdowns were -9.94%, -8.32%, and -8.41%; strategy turnover fell from
10.58x/10.59x to 5.17x/5.37x. This is better execution-pipeline evidence, not
validated alpha or an investable claim: the sample remains short and in-sample,
tax rates are zero, and jurisdiction-specific tax rules are unresolved. See
`SP500_DATA_PROVENANCE.md` for exact assumptions and evidence.

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
- No broker API integration (no live trading). Manual entry of holdings for now.
- No multi-agent committee. Numeric models only; LLM synthesizes.
- The current UI is a local Streamlit ranking dashboard; it is read-only and
  does not place trades. A richer web product remains out of scope.
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
