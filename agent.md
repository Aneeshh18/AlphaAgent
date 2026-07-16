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

## Current state (2026-07-15)

Implemented:

- SEC EDGAR XBRL fundamentals; yfinance prices with Stooq fallback; FRED macro
  with Treasury yield fallback.
- DuckDB schema and idempotent upserts; local data currently contains 22
  securities, ~156k prices, ~58k fundamentals, and ~98k macro rows.
- PIT-aware TTM helpers, Quality factor, Value factor, and fixed QV composite
  ranking (Quality 60% / Value 40%).
- Local Streamlit dashboard: universe ranking, ticker deep dive, methodology.
- Regression tests in `tests/test_pit_and_factors.py`.

Important data note: older rows in `data/aios.duckdb` include an `ebitda` metric
that was incorrectly sourced from net income. The current code ignores that
metric and derives EBITDA only from operating income + depreciation. Re-ingest
fundamentals after the current EDGAR extractor is used to populate clean D&A.

## Non-negotiable PIT rule

`fundamentals.period_end` says what period a number describes.  
`fundamentals.as_of_date` says when it became knowable (SEC filing date).

For a decision date `D`, read only rows with `as_of_date <= D`. Use
`Store.pit_fundamentals()` or the shared helpers in `aios.factors.common`; do not
query fundamentals directly with “latest period” logic. Prices use the latest
close where `date <= D`. Never use a filing’s period end as its availability
date.

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
| `src/aios/cli.py` | `doctor`, `ingest-macro`, `ingest-ticker`, `ingest-batch`, `status` |
| `src/aios/storage/schema.py` | DuckDB tables and PIT schema invariant |
| `src/aios/storage/store.py` | connection, upserts, PIT reads, diagnostics |
| `src/aios/ingest/http_client.py` | rate limits, SEC User-Agent, retries/backoff |
| `src/aios/ingest/edgar.py` | ticker→CIK, XBRL extraction, filing dates, metadata |
| `src/aios/ingest/prices.py` | yfinance/Stooq normalized price rows |
| `src/aios/ingest/fred.py` | FRED series and Treasury CSV rows |
| `src/aios/factors/common.py` | shared PIT metrics, TTM, percentiles, market cap |
| `src/aios/factors/quality.py` | ROIC, FCF margin, gross margin, Piotroski |
| `src/aios/factors/value.py` | P/E, EV/EBITDA, P/FCF, EV/Sales, P/B |
| `src/aios/factors/composite.py` | universe-relative QV score/rank/grade |
| `src/aios/dashboard.py` | read-only Streamlit UI |
| `tests/` | fast local regression checks |

## Factor semantics

- TTM flow metrics use EDGAR’s single-period `quarter_value`, not raw YTD
  values. TTM is annual + new quarters − matching prior-year quarters.
- Quality is a percentile blend of ROIC, FCF margin, gross margin, and
  Piotroski F. Financial institutions need a separate model; do not interpret
  standard ROIC for banks as meaningful.
- Value ranks low positive multiples as cheap. Negative/zero denominators are
  excluded rather than ranked.
- Composite renormalizes weights when one sub-factor is missing. Missing inputs
  remain visible in each row’s `missing` list.

## Safe working procedure

1. Read this file, then only the modules relevant to the task.
2. Preserve user data and existing `.env`; never reset or delete the DuckDB.
3. Add/adjust a regression test for every PIT, ingestion, or formula change.
4. Run `PYTHONPATH=src .venv/bin/pytest -q`.
5. Run the affected CLI or factor function against a temporary DB when possible.
6. Report assumptions, data limitations, and whether a re-ingest is required.

## Next intended work

1. Re-ingest fundamentals with clean D&A and add source/quality audit logging.
2. Add macro regime classification and make factor weights configurable by
   regime, with backtest-safe release dates.
3. Add momentum and low-volatility factors to form QVML.
4. Build a transaction-cost/tax-aware, PIT-safe backtest engine.
5. Add LLM JSON-packet synthesis only after deterministic outputs are audited.

## Commands

```bash
PYTHONPATH=src .venv/bin/python -m aios.cli doctor
PYTHONPATH=src .venv/bin/python -m aios.cli status
PYTHONPATH=src .venv/bin/pytest -q
.venv/bin/streamlit run src/aios/dashboard.py
```

