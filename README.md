# AI Investment Operating System

In-house, minimal-cost, institutional-style investment intelligence system (Path B build).

> **Status:** Usable local research beta plus an audited PIT QV backtest layer —
> release-aware macro regime, historical-universe contract, explicit execution
> costs/taxes, persistent portfolio/tax-lot state, daily equity curves, and
> benchmark reporting. It is not yet approved for unattended or real-money
> trading. See [`ARCHITECTURE.md`](./ARCHITECTURE.md) for the locked decisions.

> **Product direction:** India-first. The current U.S. S&P 500 slice is the
> audited reference implementation used to prove the data and portfolio
> contracts before India is added through market-specific adapters. U.S.
> technical completion and current-date certification are the active scope;
> India work remains deferred until those gates pass. Local operation now uses
> supported health, refresh, scheduler, backup, restore, and dashboard commands,
> so the owner does not need to administer DuckDB or Streamlit. Hosted/multi-user
> deployment is still a separate future gate.

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
.venv/bin/aios readiness --report-only
.venv/bin/aios forward-status
.venv/bin/aios health

# Optional: open the local research dashboard
.venv/bin/aios dashboard

# Optional: create and verify a timestamped database + paper-state backup
.venv/bin/aios backup
# Restore requires a verified backup and an explicit --confirm-restore flag

# Optional Linux automation; close the dashboard before scheduled runs
.venv/bin/aios scheduler-install --confirm-install
.venv/bin/aios scheduler-status
```

Scheduler status is bounded: if the Linux user scheduler does not answer in
five seconds, installed/enabled unit-file evidence is shown with runtime state
explicitly marked unverified.

If the `aios` executable points at an older checkout, reinstall the project or
use the source-checkout form: `PYTHONPATH=src .venv/bin/python -m aios.cli status`.

## When can I use it?

You can use it **now for supervised U.S. research and local paper simulation**.
The supported dashboard explains rankings, missing evidence, current readiness,
and a checksum-protected model portfolio in plain language. It does not connect
to a broker, issue personal buy/sell instructions, or move money.

The current-date gate is real rather than implied. On 2026-07-22 the reviewed
decision date is 2026-07-20: 503 S&P 500 members have stable security identities,
500 have PIT company filings, all 503 have current action-safe prices, SPY is
current through the same close, and the latest required macro release is dated
2026-07-20. The local paper proposal remains separate from holdings and cannot
be simulated until the next reviewed market close and explicit confirmation.

The broader 2025-to-current stateful engineering backtest now completes all six
periods. It applies the reviewed HES→CVX conversion, liquidates MTCH and PAYC
without restoring membership, and aligns 327 daily strategy/SPY observations
with no stale strategy points. Regime-aware QV returned 31.13% net, fixed QV
27.73%, and SPY 34.12% with zero taxes. This is pipeline evidence—not an alpha,
after-tax, or personal investment claim.

| Use level | Current state | Required exit condition |
|---|---|---|
| Supervised U.S. research | available now | keep `aios readiness` and `aios validate` non-failing |
| Local U.S. paper simulation | available now | reviewed next-session close plus explicit simulated confirmation; still no broker |
| U.S. technical-beta completion | historical gate complete; timers installed and all three services manually passed | observe 1–3 naturally triggered refresh/health/backup cycles |
| Controlled real-capital pilot | not approved; at least 8–12 weeks of untouched forward monitoring after freeze | broker reconciliation, alerts, price versioning, and user-approved tax/risk policy |
| India market build | next major phase after the U.S. technical gate | NSE/BSE identity, membership, filings, actions, calendars, taxes, benchmarks, and parity tests |

These are engineering estimates, not return promises. Provenance gaps and the
required elapsed forward-test period cannot be compressed by adding compute.

## Read this first

- [`agent.md`](./agent.md) is the compact context file for future AI agents.
- [`BEGINNER_GUIDE.md`](./BEGINNER_GUIDE.md) explains the project without
  assuming investing or Python experience.
- [`ARCHITECTURE.md`](./ARCHITECTURE.md) records the larger design decisions.
- [`SP500_DATA_PROVENANCE.md`](./SP500_DATA_PROVENANCE.md) records the audited
  bounded and current-universe sources, conflicts, and safe-use limits.
- [`OPEN_SOURCE_RESEARCH.md`](./OPEN_SOURCE_RESEARCH.md) records which external
  projects and data shortcuts were verified, deferred, or rejected.

## What exists now (foundation + first factor layer)

- **Storage:** DuckDB with a point-in-time-correct schema (`as_of_date` on every
  fundamentals row — no look-ahead bias possible).
- **Fundamentals:** SEC EDGAR XBRL Company Facts (free, the source of truth).
- **Prices:** yfinance primary, optional user-token Tiingo, then Stooq fallback.
  Provider mappings remain explicit and date-bounded; no token is placed in a
  URL or committed to the repository. Yahoo closes are retrospectively
  split-normalized, so every reviewed row also stores the cumulative later-split
  factor needed to restore the contemporaneous price basis before combining it
  with PIT SEC shares or per-share facts.
- **Macro:** FRED primary; the free US Treasury CSV fallback is currently
  degraded by HTTP 403 for automated requests in this environment.
- **Macro regime:** release-aware FRED vintages, PIT-safe regime snapshots, and
  an explicit `unknown` state when mandatory evidence is unavailable.
- **Reliable macro refresh:** API-limit-aware vintage chunks, transient retries,
  incremental overlap, and a hard failure signal after all sources are tried.
- **HTTP:** rate-limited client (SEC's 10 req/s, 429 backoff, retries).
- **Factors:** PIT-aware Quality, Value, 12-minus-1 Momentum, and one-year Low
  Volatility. QV remains the backtested baseline; experimental QVML keeps the
  regime-relative Q/V tilt inside a 60% core and adds 25% Momentum plus 15% Low
  Volatility. A decision-scoped cache shares PIT fundamentals and identity-safe
  price windows, then is discarded after every decision. The Streamlit UI has
  a QV/QVML selector and exposes raw evidence, weights, coverage, and withheld
  inputs.
- **Backtest:** quarterly PIT policy comparison against fixed 60/40, with
  an explicit market-calendar ticker, exact shared next-session/quarter-end
  dates, stable-security price paths across ticker changes, historical
  membership protection, explicit commission/slippage/fixed-fee assumptions,
  persistent FIFO tax lots, delta-only equal-weight rebalancing, explicit
  split/dividend cash accounting, aligned daily net/gross/benchmark curves,
  hard validation gates, member-level exclusion reasons, and a reproducible
  JSON audit artifact. Reviewed share conversions preserve quantity, acquisition
  date, and tax basis across mergers. Short post-membership price extensions are
  allowed only for already-held securities through the next rebalance; they are
  hash-checked and never restore factor eligibility. Tax rates are caller-supplied
  and default to zero because tax law is jurisdiction-specific.
- **Operational readiness:** `aios readiness` reports raw source clocks
  separately from the certified research window and returns a failing exit code
  when current paper-use evidence is incomplete. The dashboard refuses dates
  outside the reviewed window instead of silently using raw rows. The current
  reviewed U.S. decision date is 2026-07-20.
- **Current refresh and local scheduling:** `aios refresh-us-current` updates
  prices, SEC filings, SPY, and release-aware macro data only through already
  reviewed identities. It records each failure and never auto-approves a new
  S&P member. Confirmed user-level timers can run weekday price/macro refreshes,
  weekly filing refreshes and checksum-verified backups; pause/resume/status/
  removal stay behind supported CLI commands. The generated units passed the
  native systemd verifier. They were explicitly installed on 2026-07-21 and
  the backup, prices/macro, and filing services all passed real first runs.
  When today's membership is not yet reviewed, raw refresh may use the newest
  reviewed snapshot up to seven days old for collection only; it displays that
  date and does not approve a current portfolio decision. New reviewed issuers
  without Company Facts remain visible and are retried; an established issuer
  unexpectedly returning no facts still fails the refresh.
- **Portfolio risk:** the deterministic long-only risk contract rejects missing
  sector/liquidity evidence, leverage, excessive position or sector
  concentration, excessive rebalance turnover, and breached drawdown limits.
  Conservative defaults are engineering safeguards, not final user-approved
  investment limits. They are connected to the persistent local paper workflow.
- **Supervised paper workflow:** `paper-init`, `paper-propose`, `paper-execute`,
  `paper-mark`, and `paper-status` maintain a local checksum-protected account,
  FIFO tax lots, daily account values, proposal evidence, and explicit simulated
  confirmation. There is deliberately no broker credential or order API.
- **Untouched forward-policy evidence:** `forward-freeze` records checksums for
  the factor, macro, risk, cost/tax, calendar, readiness, and paper rules plus
  the reviewed configuration. `forward-status` detects drift, every later
  proposal is registered, and drift blocks simulation while market data remains
  free to advance.
- **Historical universe:** `universe_membership` stores effective intervals,
  start `known_date`, and independently dated `end_known_date`. A backtest
  target is membership known at the decision close and effective on the
  scheduled execution date, so announced additions/removals are not shifted
  into the wrong session. Backtests refuse the current active universe by
  default; `--allow-current-universe` is an explicitly labeled survivorship-
  biased diagnostic escape hatch. The original 60-event manifest certifies the
  bounded 2023-08-01 through 2024-12-31 window; reviewed event/reference batches
  now extend the current operating path through 2026-07-21. This still does not
  claim a complete 1996-present announcement archive.
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
- **Reviewed factor warm-up:** `aios build-factor-price-warmup` fetches history
  before each verified provider mapping, but stores it only by immutable
  `security_id`—never by an invented historical ticker. Acceptance requires a
  complete pre-anchor window, explicit actions/split basis, and at least five
  fresh sessions matching the already reviewed provider series. Compressed
  snapshots are resumable and hash-checked by
  `aios ingest-factor-price-warmup`; blocked predecessor histories and newly
  listed securities remain explicit review rejections. The reviewed v3 batch
  accepted 520/528 identities and atomically imported 122,466 rows. The eight
  rejections are six genuine short-history listings plus blocked SW and
  Healthpeak/DOC predecessor histories.
- **Coverage audit:** `aios universe-coverage` measures price and PIT-fundamental
  availability on a historical decision date. Coverage is currently 501/503 on
  2023-09-29 and 503/504 on 2024-09-30. PEAK and WRK lack safe historical
  provider rows; AMTM had no public Company Facts until 2024-12-17. These three
  limitations stay explicit instead of being hidden behind aliases or backdating.
- **Bounded smoke result:** five PIT-gated quarters completed on the 22 locally
  covered names with explicit costs and SPY. Both QV policies selected the same
  equal-weight sets, so the run validates mechanics but not regime alpha. Exact
  assumptions and results are in `SP500_DATA_PROVENANCE.md`.
- **Superseded bounded regression artifact:** the provider-basis schema-v3 rerun enumerated
  all 525 tickers in the certified history and preserved each 503–504 member
  denominator. Raw price/fundamental coverage was 500–503 per decision; strict
  Quality+Value eligibility was 291, 293, 350, 308, and 301. PEAK, WRK, and
  pre-filing AMTM were explicitly excluded and explained. All five strategy
  periods and five persistent SPY periods used the same 316 market sessions.
  Regime-aware returned 74.25% net, fixed 60/40 returned 76.73%, and SPY
  returned 39.47%. Daily max drawdowns were 9.87%, 8.25%, and 8.41%; strategy
  turnover was 5.16x and 5.36x because unchanged lots were retained. Price
  paths declared whether provider closes were already split-normalized, so its
  portfolio-return accounting remains useful regression evidence. However,
  its Value rankings combined Yahoo's post-split-normalized historical close
  with contemporaneous pre-split SEC shares for names with later splits. Those
  selections and returns are therefore **not current certification**. The
  factor-price basis is now repaired; the replacement schema-v4 QV/QVML
  artifacts described next are the current bounded engineering evidence. Exact
  evidence and caveats are in `SP500_DATA_PROVENANCE.md`.
- **Current schema-v4 engineering audits:** both matched runs completed five
  periods and 316 daily observations using the same database hash, 5 bps
  commission plus 5 bps slippage per side, zero taxes, SPY, and explicit
  PEAK/WRK/AMTM exclusions. Regime-aware/fixed QV returned 51.12%/50.98% net;
  regime-aware/fixed QVML returned 24.97%/34.96%; SPY returned 39.47%. QVML
  market-factor coverage was 499, 498, 498, 496, and 497, while strict QVML
  eligibility was 291, 293, 347, 307, and 300. This short in-sample comparison
  is pipeline evidence and negative evidence against tuning QVML to this
  window—not validated alpha.
- **2025-current stateful QV audit:** all six periods completed with 327 aligned
  daily observations, exact capital continuity, no stale strategy points, the
  reviewed HES→CVX conversion, and reviewed MTCH/PAYC liquidation paths.
  Regime-aware/fixed QV returned 31.13%/27.73% net versus SPY at 34.12%, with
  zero taxes and 5+5 bps per-side costs. The benchmark outperformance and short
  in-sample window are explicit evidence against treating this as validated
  alpha.
- **Invalid-source quarantine:** 42 EDGAR-derived rows whose stated fiscal end
  followed their filing date were transactionally moved to
  `fundamentals_quarantine`; new extraction and storage paths reject this
  impossible chronology before it reaches PIT calculations.
- **Tests:** the regression suite covers PIT reads, macro vintage selection and
  migration, regime revision timing, TTM selection, price-provider provenance,
  issuer/security identity, conservative batch rejection, ticker-reuse cutoffs,
  EBITDA derivation, membership intervals, action provenance, provider split
  basis, separate membership start/end knowledge, execution-date universes,
  cost/tax accounting, benchmark reporting, plain-language dashboard copy, and
  the supported dashboard launcher, current-use readiness, and fail-closed
  portfolio risk, paper state, security conversions, and liquidation-only price
  extensions, current refresh orchestration, and managed scheduler safety. The
  current suite has 182 passing tests.

## What comes next

Current U.S. membership, stable identity, action-safe prices, SPY, PIT filings,
macro evidence, risk checks, and supervised paper state now reach the reviewed
2026-07-20 decision close. The current refresh, health, backup/recovery,
scheduler controls, and local dashboard smoke are implemented and tested. The
six-period stateful rerun and its held-security evidence are complete. The
timers are installed and their three services passed manual first runs. The
untouched U.S. forward-policy gate is active from the reviewed 2026-07-20
proposal; the next safe slice is to observe naturally triggered cycles. New S&P
membership announcements still require source review; automation does not
approve provenance. Final after-tax or controlled-capital claims still require
the user's jurisdiction, account, broker, tax, and risk-budget decisions.
Neither regime tilts nor QVML are validated alpha.
