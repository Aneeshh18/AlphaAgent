# AI Investment Operating System

In-house, minimal-cost, institutional-style investment intelligence system (Path B build).

> **Status:** Usable local research beta plus an audited PIT QV backtest layer —
> release-aware macro regime, historical-universe contract, explicit execution
> costs/taxes, persistent portfolio/tax-lot state, daily equity curves, and
> benchmark reporting. It is not yet approved for unattended or real-money
> trading. See [`ARCHITECTURE.md`](./ARCHITECTURE.md) for the locked decisions.

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

## When can I use it?

You can use it **now** for local research: inspect current QV rankings, audit a
ticker, and rerun the certified bounded 2023-08 through 2024-12 experiment. The
dashboard has been headlessly rendered against the real local database with 526
tickers and no application exceptions. Do not connect it to a broker or treat
the short in-sample backtest as a trading signal.

Assuming continuous focused engineering from 2026-07-20:

| Use level | Target | Required exit condition |
|---|---:|---|
| Local research beta | available now | supervised dashboard/CLI use with explicit data and model caveats |
| Paper-trading beta | 2–4 weeks (2026-08-03 to 2026-08-17) | QVML, risk constraints, scheduled refresh monitoring, backtest/report UI, and an untouched holdout |
| Controlled small-capital pilot | 8–12 weeks minimum (2026-09-14 to 2026-10-12) | longer PIT history, walk-forward evidence, broker reconciliation, alerting, price-version policy, and user-approved tax/risk assumptions |
| Institutional-style completeness | 12–20+ weeks | broader provenance and delisted histories; free sources may not make every gap solvable |

These are focused-work estimates, not return promises or fixed delivery
guarantees. The controlled-capital gate can move later if historical evidence
cannot be sourced safely.

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
- **Prices:** yfinance primary, optional user-token Tiingo, then Stooq fallback.
  Provider mappings remain explicit and date-bounded; no token is placed in a
  URL or committed to the repository.
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
  weights. A decision-scoped cache shares one PIT-deduplicated fundamental
  snapshot across both factors and is discarded after every decision. The local
  Streamlit dashboard exposes the evidence and weights.
- **Backtest:** quarterly PIT policy comparison against fixed 60/40, with
  an explicit market-calendar ticker, exact shared next-session/quarter-end
  dates, stable-security price paths across ticker changes, historical
  membership protection, explicit commission/slippage/fixed-fee assumptions,
  persistent FIFO tax lots, delta-only equal-weight rebalancing, explicit
  split/dividend cash accounting, aligned daily net/gross/benchmark curves,
  hard validation gates, member-level exclusion reasons, and a reproducible
  JSON audit artifact. Tax rates are caller-supplied and default to zero because
  tax law is jurisdiction-specific.
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
- **Issuer/provider identities:** 528 reviewed issuers now have historical SEC
  CIK assignments across 531 dated security-owner intervals, while 534 provider-symbol
  intervals explicitly mark verified, unavailable, or wrong-security aliases.
  Fundamentals route by issuer ID and prices by security ID. This prevents old
  Physicians Realty `DOC` history from contaminating Healthpeak and prevents
  pre-combination `SW` history from contaminating Smurfit Westrock.
- **Reviewed batch tooling:** `aios build-reference-batch` certifies only
  unchanged full-window securities whose SEC ticker map, SEC submissions
  identity and filing range, membership interval, and provider history agree.
  It follows official SEC history shards and accepts only exact dot/hyphen
  share-class notation normalization; former issuer names are never tickers.
  It writes accepted manifests when any pass plus an explicit rejection review
  with source fingerprints.
  `aios ingest-reference-batch` then imports references atomically and ingests
  each accepted issuer/security independently so one provider failure remains
  visible instead of rolling back unrelated evidence. Large reviewed batches
  may optionally reuse a local official `companyfacts.zip`; exact member and
  embedded-CIK checks preserve the same identity contract.
  `aios build-reference-window-batch` performs the same strict certification
  for independent half-open `ticker,start,end` windows and merges only
  non-conflicting accepted rows while retaining every rejection.
- **Coverage audit:** `aios universe-coverage` measures price and PIT-fundamental
  availability on a historical decision date. Coverage is currently 501/503 on
  2023-09-29 and 503/504 on 2024-09-30. PEAK and WRK lack safe historical
  provider rows; AMTM had no public Company Facts until 2024-12-17. These three
  limitations stay explicit instead of being hidden behind aliases or backdating.
- **Bounded smoke result:** five PIT-gated quarters completed on the 22 locally
  covered names with explicit costs and SPY. Both QV policies selected the same
  equal-weight sets, so the run validates mechanics but not regime alpha. Exact
  assumptions and results are in `SP500_DATA_PROVENANCE.md`.
- **Near-complete bounded audit:** the stateful 2026-07-20 rerun enumerated all 525
  tickers in the certified history and preserved each 503–504 member
  denominator. Raw price/fundamental coverage was 500–503 per decision; strict
  Quality+Value eligibility was 291, 293, 350, 308, and 301. PEAK, WRK, and
  pre-filing AMTM were explicitly excluded and explained. All five strategy
  periods and five persistent SPY periods used the same 316 market sessions.
  Regime-aware returned 70.25% net, fixed 60/40 returned 72.58%, and SPY
  returned 37.16%. Daily max drawdowns were 9.94%, 8.32%, and 8.41%; strategy
  turnover fell to 5.17x and 5.37x because unchanged lots were retained. This
  remains a short, in-sample selection-policy diagnostic—not an investable
  performance claim or proof of regime alpha. Exact assumptions and caveats are
  in `SP500_DATA_PROVENANCE.md`.
- **Invalid-source quarantine:** 42 EDGAR-derived rows whose stated fiscal end
  followed their filing date were transactionally moved to
  `fundamentals_quarantine`; new extraction and storage paths reject this
  impossible chronology before it reaches PIT calculations.
- **Tests:** the regression suite covers PIT reads, macro vintage selection and
  migration, regime revision timing, TTM selection, price-provider provenance,
  issuer/security identity, conservative batch rejection, ticker-reuse cutoffs,
  EBITDA derivation, membership intervals, cost/tax accounting, and benchmark
  reporting.

## What comes next

The bounded identity program, near-complete factor-eligibility audit, and
stateful execution layer are complete for 2023-08 through 2024-12. The
decision-scoped factor optimization is also complete: a 503-member profile fell
from 42,235 queries/174.1 seconds to 3,874 queries/17.4 seconds, and the complete
five-decision rerun fell from the observed 62 minutes to about 92 seconds with
an exactly identical result and ticker-explanation payload. Next, add Momentum
and Low Volatility to form QVML, expose audited backtest/report controls in the
UI, and create an untouched walk-forward/paper-trading track. In parallel,
extend official announcement and identity provenance before August 2023 and
settle jurisdiction-specific tax/risk assumptions. The initial regime tilts are
still not validated alpha.
