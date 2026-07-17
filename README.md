# AI Investment Operating System

In-house, minimal-cost, institutional-style investment intelligence system (Path B build).

> **Status:** Foundation plus an audited PIT QV backtest layer — release-aware
> macro regime, historical-universe contract, explicit execution costs/taxes,
> and benchmark reporting.
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
.venv/bin/aios audit
.venv/bin/aios validate

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
- [`SP500_DATA_PROVENANCE.md`](./SP500_DATA_PROVENANCE.md) records the audited
  bounded-universe sources, conflicts, and safe-use limits.
- [`OPEN_SOURCE_RESEARCH.md`](./OPEN_SOURCE_RESEARCH.md) records which external
  projects and data shortcuts were verified, deferred, or rejected.

## What exists now (foundation + first factor layer)

- **Storage:** DuckDB with a point-in-time-correct schema (`as_of_date` on every
  fundamentals row — no look-ahead bias possible).
- **Fundamentals:** SEC EDGAR XBRL Company Facts (free, the source of truth).
- **Prices:** yfinance primary, Stooq fallback (both free, no key).
- **Macro:** FRED primary; the free US Treasury CSV fallback is currently
  degraded by HTTP 403 for automated requests in this environment.
- **Macro regime:** release-aware FRED vintages, PIT-safe regime snapshots, and
  an explicit `unknown` state when mandatory evidence is unavailable.
- **Reliable macro refresh:** API-limit-aware vintage chunks, transient retries,
  incremental overlap, and a hard failure signal after all sources are tried.
- **HTTP:** rate-limited client (SEC's 10 req/s, 429 backoff, retries).
- **Factors:** PIT-aware Quality and Value snapshots plus a regime-aware QV
  composite ranking. The initial policy tilts Quality/Value by macro regime;
  unknown or incomplete macro evidence falls back to explicit 60/40 baseline
  weights. The local Streamlit dashboard exposes the evidence and weights.
- **Backtest:** quarterly PIT policy comparison against fixed 60/40, with
  next-session entry, common trading-date windows, historical membership
  protection, explicit commission/slippage/fixed-fee assumptions, split-aware
  tax lots, dividend tax, benchmark total returns, hard validation gates, and
  explicit skipped-period reporting. Tax rates are caller-supplied and default
  to zero because tax law is jurisdiction-specific.
- **Historical universe:** `universe_membership` stores effective intervals and
  separate `known_date` values. Backtests refuse the current active universe by
  default; `--allow-current-universe` is an explicitly labeled survivorship-
  biased diagnostic escape hatch. A 60-event source manifest now certifies the
  bounded 2023-08-01 through 2024-12-31 S&P 500 window, and all 533 generated
  intervals are loaded locally; it does not claim full historical coverage.
- **Stable identities:** 533 membership intervals link to 529 internal security
  IDs. Four source-verified ticker transitions are joined; ordinary index
  replacements and WRK→SW remain separate. Bounded ticker-derived IDs are
  labeled provisional instead of masquerading as authoritative identifiers.
- **Issuer/provider identities:** six reviewed issuers now have historical SEC
  CIK assignments and security ownership, while eight dated provider-symbol
  intervals explicitly mark verified, unavailable, or wrong-security aliases.
  Fundamentals route by issuer ID and prices by security ID. This prevents old
  Physicians Realty `DOC` history from contaminating Healthpeak and prevents
  pre-combination `SW` history from contaminating Smurfit Westrock.
- **Coverage audit:** `aios universe-coverage` measures price and PIT-fundamental
  availability on a historical decision date. Coverage is currently 24/503 on
  2023-09-29 and 26/504 on 2024-09-30, so full-universe backtesting remains
  blocked by data coverage rather than hidden behind skipped rows.
- **Bounded smoke result:** five PIT-gated quarters completed on the 22 locally
  covered names with explicit costs and SPY. Both QV policies selected the same
  equal-weight sets, so the run validates mechanics but not regime alpha. Exact
  assumptions and results are in `SP500_DATA_PROVENANCE.md`.
- **Tests:** 50 passing tests cover PIT reads, macro vintage selection and
  migration, regime revision timing, TTM selection, price-provider provenance,
  issuer/security identity, ticker-reuse cutoffs, EBITDA derivation, membership
  intervals, cost/tax accounting, and benchmark reporting.

## What comes next

Next order: extend exact announcement and stable-identity coverage before
August 2023, then expand reviewed issuer/CIK/provider mappings, PIT
fundamentals, and survivorship-safe prices to all needed members. After that,
validate jurisdiction-specific cost/tax assumptions and build momentum and
low-volatility factors. LLM synthesis and portfolio tooling follow. The
initial regime tilts are not validated alpha.
