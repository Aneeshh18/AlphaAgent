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
- We reject paid services (FMP, Polygon, Tiingo) for now: free covers MVP, and EDGAR is higher *quality* than any of them for fundamentals because it's the primary source.
**Cost:** $0.
**Keys needed:** None for EDGAR (User-Agent string only). Optional free SimFin account.

### 5. PRICE DATA → **yfinance (primary), Stooq (fallback)**
**Decision:** `yfinance` for EOD prices/adjustments/splits/dividends; Stooq (no-key CSV download) as fallback when yfinance breaks or rate-limits.
**Why:**
- `yfinance` is free, keyless, covers US + international, gives adjusted OHLCV + dividends + splits. It is the de-facto free choice. Its only weakness is reliability (unofficial scraping endpoint) — which is why we *must* have a fallback.
- **Stooq** offers free, no-API-key, downloadable CSV historical data — a genuine independent fallback when yfinance fails. Coverage skews US/Europe.
- Daily EOD only for MVP. Real-time/intraday is explicitly out of scope (see Decision 3).
- We write our own thin fetcher with a common interface so the fallback is automatic and the storage layer never knows which source supplied a row.
**Cost:** $0.

### 6. MACRO DATA → **FRED API (primary), BLS + US Treasury (fallback)**
**Decision:** Federal Reserve Economic Data (FRED) API is the macro backbone. US Treasury and BLS are targeted fallbacks.
**Why:**
- FRED aggregates CPI, PCE, GDP, unemployment, Fed Funds rate, all Treasury yields, yield-curve spreads, credit spreads, money supply, etc. — one free API key, one consistent interface, decades of history.
- `fredapi` Python library makes it trivial.
- Fallbacks: US Treasury daily yield curve (free CSV, no key), BLS public API (free, key-recommended) for employment/inflation detail.
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

---

## WHAT WE DELIBERATELY DO NOT BUILD (yet)

- No real-time/intraday. Daily EOD only.
- No broker API integration (no live trading). Manual entry of holdings for now.
- No multi-agent committee. Numeric models only; LLM synthesizes.
- The current UI is a local Streamlit ranking dashboard; it is read-only and
  does not place trades. A richer web product remains out of scope.
- No paid data feeds. We use free tiers exclusively until a concrete need forces a paid upgrade.

---

## STACK SUMMARY (one line each)

| Layer        | Choice                          | Cost |
|--------------|---------------------------------|------|
| Language     | Python 3.12 + `uv`              | $0   |
| Storage      | DuckDB + Parquet                | $0   |
| Scheduling   | systemd timers / cron           | $0   |
| Fundamentals | SEC EDGAR XBRL + SimFin         | $0   |
| Prices       | yfinance + Stooq                | $0   |
| Macro        | FRED + US Treasury / BLS        | $0   |
| LLM          | Claude (later) + GLM-5.2        | $0 now |

**Total recurring cost to run this system: $0.** All keys are free. All tools are free/open-source. We pay only if we later choose paid LLM tokens.
