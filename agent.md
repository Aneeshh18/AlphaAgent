# AI Investment OS — compact agent handoff

Read this file first. It contains the current operating truth and the rules an
agent must preserve. Open `ARCHITECTURE.md` for design rationale,
`SP500_DATA_PROVENANCE.md` for source history, and `BEGINNER_GUIDE.md` for the
non-technical operator guide.

## Mission

- Build a local-first, low-cost, auditable investment research system.
- Finish the U.S. S&P 500 reference implementation, then add India through
  market-specific adapters without forking the factor, risk, or portfolio core.
- Use deterministic code for dates, identities, calculations, risk, accounting,
  and validation. Language models may explain outputs but are never evidence.
- This is not a trading bot. No broker, order API, unattended execution, or
  personal buy/sell advice exists.
- Normal operation must not require DuckDB or Streamlit knowledge.

## Verified checkpoint — 2026-07-22

Current U.S. decision close: **2026-07-20**.

- `aios readiness --as-of 2026-07-20 --purpose paper --report-only`: ready.
- 503 dated S&P 500 members; 503/503 stable security identities.
- 500/503 members have PIT fundamentals; 503/503 have identity-safe prices.
- 503/503 have recent prices with reviewed dividend/split fields.
- SPY benchmark/calendar is reviewed through 2026-07-20.
- Raw SEC filing evidence reaches 2026-07-21 and required macro releases reach
  2026-07-20; the 2026-07-20 decision regime is reflation.
- Database quality: zero hard failures, three visible warnings.
- Full test suite baseline: 181 tests; Ruff must remain clean.

Current local row counts:

| Table | Rows |
|---|---:|
| securities | 561 |
| security_master | 560 |
| security_identity_assignments | 568 |
| issuer_master | 564 |
| issuer_cik_history | 1,063 |
| security_issuer_assignments | 1,070 |
| provider_symbol_history | 1,074 |
| security_conversions | 1 |
| security_ticker_extensions | 2 |
| factor_price_provenance | 543 |
| factor_prices | 131,252 |
| prices | 521,326 |
| fundamentals | 1,268,963 |
| fundamentals_quarantine | 42 |
| macro | 391,018 |
| universe_membership | 568 |

The three quality warnings are historical audit debt: old action-incomplete
price rows plus retained failed/zero-row ingest records. They do not override
the dated current-use checks. Never describe all historical rows as action-safe.

## Product readiness boundary

Ready now:

- supervised U.S. research in the CLI/dashboard;
- current fail-closed readiness checks;
- local paper proposals and paper-account monitoring;
- QV baseline and experimental QVML research rankings;
- stateful PIT engineering backtests with costs, holdings, lots, daily curves,
  corporate actions, security conversions, and SPY.
- plain-language health plus checksum-verified backup/verify/confirmed-restore
  commands; restore always creates a pre-restore safety snapshot.
- fail-visible current U.S. refresh over reviewed identities and AIOS-managed
  user timers for refresh, health, filings, and backups. All three timers were
  explicitly installed and their services passed real first runs on
  2026-07-21/22; no timer installation is ever implicit.
- Scheduler status queries are process-group bounded. If the desktop user bus
  is temporarily unavailable, report managed file enablement as unverified
  runtime evidence; never hang or claim that a timer passed/stopped.

Raw refresh may use the newest reviewed membership snapshot up to seven days
old solely to collect prices/filings for known identities. It prints that date
and never turns it into a current membership or portfolio-decision claim.
New reviewed issuers with no accepted Company Facts remain visible as
`fundamentals_pending` and are retried. An issuer that previously had accepted
facts but unexpectedly returns none is still a hard refresh failure.

Not approved:

- personalized buy/hold/sell instructions;
- broker or real-money execution;
- after-tax claims (jurisdiction and account type are unset);
- alpha/performance claims from short in-sample artifacts;
- Indian-market rankings (NSE/BSE data is not loaded);
- complete 1996-present announcement/delisting coverage.

The paper account at `data/paper/us_qv_sandbox.json` remains entirely simulated
cash: $100,000, zero holdings, zero recorded rebalances. A reviewed 2026-07-20
proposal exists separately. It cannot become a simulated holding before the
2026-07-21 close is reviewed and the operator explicitly passes
`--confirm-simulated`.

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
| `src/aios/operations.py` | backup verification and confirmed restore |
| `src/aios/scheduler.py` | bounded systemd-user timer lifecycle |
| `src/aios/dashboard.py` | plain-language, read-only Streamlit UI |
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

### Price/action safety

- Every reviewed price declares whether actions are complete and whether Close
  is already split-normalized.
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
- `paper-execute` requires next-session evidence, revalidation, and explicit
  `--confirm-simulated`.
- `forward-freeze` fingerprints the QV, macro-regime, risk, cost/tax, calendar,
  readiness, and paper-policy source plus the reviewed operating configuration.
- The forward freeze never hashes DuckDB: new public data must advance. Every
  new proposal is registered by checksum, and drift blocks CLI simulation.

## Safe working procedure

1. Read `git status --short`; preserve staged/user changes.
2. Make one small provenance-safe slice.
3. Run focused tests, then Ruff and the full suite.
4. Run `aios validate`; hard failures block downstream claims.
5. Run `aios readiness` for the exact decision date/purpose.
6. Update this file and the relevant canonical doc when contracts change.
7. Never print `.env`, secrets, or externalize private repository code.
8. DuckDB is single-process. Run DB commands sequentially and close the
   dashboard before writes. A lock collision is transient concurrency; retry
   after the other process exits.

## Immediate plan

1. Observe the first naturally triggered timer cycles and keep their warnings,
   source dates, and exit results in the operating record.
2. Keep the frozen U.S. forward-monitoring policy unchanged and record every
   proposed decision without retuning it.
3. Extend pre-August-2023 announcement and delisting provenance as a separate
   long-history track.
4. Start India market profile/adapters after the U.S. operational observations.
5. Ask the user for jurisdiction, account type, broker, and final risk limits
   only before after-tax or controlled-capital certification.

The historical U.S. technical gate and manual systemd-service verification are
complete. Forward trial `us-qv-forward-a0b63856954c` is active from the reviewed
2026-07-20 proposal and its policy bundle is unchanged. The local technical beta
now needs 1–3 naturally triggered clean cycles; elapsed time, not more compute,
is the limiting factor. Any real-capital pilot still needs at least 8–12 weeks
of untouched forward observation.

## Core commands

```bash
.venv/bin/aios doctor
.venv/bin/aios status
.venv/bin/aios audit
.venv/bin/aios validate
.venv/bin/aios readiness --as-of 2026-07-20 --purpose paper --report-only
.venv/bin/aios health
.venv/bin/aios refresh-us-current
.venv/bin/aios backup
# restore requires: aios restore BACKUP_DIR --confirm-restore
.venv/bin/aios scheduler-status
# install requires: aios scheduler-install --confirm-install
.venv/bin/aios dashboard

.venv/bin/aios paper-status
.venv/bin/aios forward-status
.venv/bin/aios paper-propose
.venv/bin/aios paper-mark
# paper-execute additionally requires --proposal ... --confirm-simulated
# creating a new baseline additionally requires: forward-freeze --confirm-freeze

PYTHONPATH=src .venv/bin/pytest -q
.venv/bin/ruff check src tests
```
