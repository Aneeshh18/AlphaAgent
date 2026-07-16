# AI Investment Operating System

In-house, minimal-cost, institutional-style investment intelligence system (Path B build).

> **Status:** Foundation phase — data ingestion + point-in-time storage.
> See [`ARCHITECTURE.md`](./ARCHITECTURE.md) for the locked decisions.

## Quick start

```bash
# 1. Create a virtual environment
python3 -m venv .venv

# 2. Install the CLI, tests, and dashboard dependencies
.venv/bin/pip install -e ".[dev,dashboard]"

# 3. Configure secrets
cp .env.example .env
#   → edit .env: set SEC_USER_AGENT (your email) and FRED_API_KEY

# 4. Verify everything works
.venv/bin/aios doctor

# 5. Pull macro + a sample ticker
.venv/bin/aios ingest-macro
.venv/bin/aios ingest-ticker AAPL
.venv/bin/aios status

# Optional: launch the local ranking dashboard
.venv/bin/streamlit run src/aios/dashboard.py
```

If the `aios` executable points at an older checkout, reinstall the project or
use the source-checkout form: `PYTHONPATH=src .venv/bin/python -m aios.cli status`.

## Read this first

- [`agent.md`](./agent.md) is the compact context file for future AI agents.
- [`BEGINNER_GUIDE.md`](./BEGINNER_GUIDE.md) explains the project without
  assuming investing or Python experience.
- [`ARCHITECTURE.md`](./ARCHITECTURE.md) records the larger design decisions.

## What exists now (foundation + first factor layer)

- **Storage:** DuckDB with a point-in-time-correct schema (`as_of_date` on every
  fundamentals row — no look-ahead bias possible).
- **Fundamentals:** SEC EDGAR XBRL Company Facts (free, the source of truth).
- **Prices:** yfinance primary, Stooq fallback (both free, no key).
- **Macro:** FRED primary, US Treasury CSV fallback (free).
- **HTTP:** rate-limited client (SEC's 10 req/s, 429 backoff, retries).
- **Factors:** PIT-aware Quality and Value snapshots plus a 60/40 composite
  ranking; the local Streamlit dashboard exposes the ranking and methodology.
- **Tests:** regression coverage for PIT reads, TTM selection, price-provider
  provenance, and EBITDA derivation.

## What comes next

Next order: clean re-ingestion with D&A, macro regime overlay, momentum/low
volatility factors, backtest engine, LLM synthesis, then portfolio tooling.
