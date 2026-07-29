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
# One read-only, use-specific operator view and safest next command
.venv/bin/aios preflight
# Add the full governed paper review without recording a simulation
.venv/bin/aios preflight --review-paper
# Automation can require one or more independent capabilities
.venv/bin/aios preflight --json --require research --require stress_review

# 5. Pull macro + a sample ticker
.venv/bin/aios ingest-macro
.venv/bin/aios ingest-ticker AAPL
.venv/bin/aios status
.venv/bin/aios audit
.venv/bin/aios validate
.venv/bin/aios readiness --report-only
.venv/bin/aios forward-status
.venv/bin/aios health --report-only
.venv/bin/aios refresh-us-daily
.venv/bin/aios review-universe-current

# Optional: open the local research dashboard
.venv/bin/aios dashboard

# Optional: review the registered proposal under immutable stress policies
# Read-only by default; no report is stored unless --output is supplied.
.venv/bin/aios stress-review \
  --proposal data/paper/proposals/us-qv-2026-07-27.json
# The same advisory review appears in the dashboard's Paper Trial workspace.

# Optional: create and verify a timestamped database + paper-state backup
.venv/bin/aios backup
.venv/bin/aios restore-drill backups/aios-YYYYMMDDTHHMMSSZ
# Restore requires a verified backup and an explicit --confirm-restore flag

# Optional Linux automation; remains active after desktop logout while the computer is on
.venv/bin/aios scheduler-install --confirm-install --keep-running-after-logout
.venv/bin/aios scheduler-status

# Inspect or test the independent local incident ledger
.venv/bin/aios alert-test
.venv/bin/aios alerts --unresolved

# Inspect or certify the retry-safe local notification outbox
.venv/bin/aios notifications
.venv/bin/aios notification-test

# Optional free SMTP email alerts (no external email is sent by status)
.venv/bin/aios email-status
# After setting the SMTP_* and ALERT_EMAIL_* values in .env:
.venv/bin/aios email-test --confirm-send
# Confirm the test arrived before enabling future incident email:
.venv/bin/aios email-enable --confirm-enable
# Emergency/off switch:
.venv/bin/aios email-disable --confirm-disable
```

Scheduler status is bounded: if the Linux user scheduler does not answer in
five seconds, installed/enabled unit-file evidence is shown with runtime state
explicitly marked unverified.

`preflight` never refreshes data, records a proposal or simulation, transitions
an incident, or generates a broker/state-changing command. Its lightweight
default checks timing; `--review-paper` additionally performs the full governed
paper review and stops at an explicit human decision. Repeat `--require` to make
automation fail unless each named capability is available. When `readiness`
omits `--as-of`, it evaluates the newest reviewed U.S. decision-date candidate,
not the computer's wall date. `health --report-only` is also side-effect free;
the default strict `health` command may record or resolve incident transitions.

Managed services use systemd failure handlers. Crashes, non-zero refreshes,
strict health failures, and backup failures are recorded in an independent
SQLite incident ledger even when the analytical DuckDB cannot be opened. Use
`aios alert-show INCIDENT_REF`, `aios alert-ack INCIDENT_REF`, and `aios
alert-resolve INCIDENT_REF` to review its append-only lifecycle. Actionable
incident changes also create durable, channel-neutral message copies in the
same SQLite transaction. Run `aios notifications` to inspect them and
`aios notification-show NOTIFICATION_REF` for attempt history.

The reviewed SMTP adapter is implemented, but external email remains off until
the local SMTP settings are complete, one exact-configuration receipt test is
accepted, the owner confirms it arrived, and `email-enable` activates the
separate optional timer. Existing held messages are never released by enabling
or reconfiguring email. The schema-v4 migration binds every new routable message
to one immutable route activation; uncertain post-send outcomes and expired
leases stop for human review instead of risking a duplicate. `aios
notification-test` remains an explicit no-network proof and sends no email,
Slack message, broker request, or mobile notification.

Incident, notification, and email-status inspection commands open the existing
operations ledger through an immutable, schema-checked reader. They fail closed
on an uncheckpointed WAL or schema mismatch instead of initializing, migrating,
or otherwise changing the ledger just to display status.

If the `aios` executable points at an older checkout, reinstall the project or
use the source-checkout form: `PYTHONPATH=src .venv/bin/python -m aios.cli status`.
An installed wheel resolves its data workspace from the current directory or
one of its parents, never from `site-packages`. When launching it elsewhere,
set `AIOS_PROJECT_ROOT` to the existing AIOS workspace. The dashboard entrypoint
also comes from the installed package while data and governed policy remain
anchored to that workspace.

## When can I use it?

You can use it **now for supervised U.S. research, governed proposal stress
review, and the local paper workflow**. The active forward-policy baseline is
unchanged and has one simulation-only proposal from the 2026-07-27 decision
close. That proposal is not a holding or execution. `stress-review` can inspect
its target sensitivities without changing the account, trial, incident ledger,
database, or broker state. `paper-review` remains the authority for the separate
simulation timing window; an expired window requires a new prospective proposal
rather than a retrospective fill.
The supported dashboard opens on **Overview**, an Investment Command Center
that separates **Research**, **Paper Trial**, and **Operations** state before
showing one safe priority action. Research readiness, paper progress, proposal
targets, and current incidents are visible without opening disclosure panels;
only raw commands, hashes, and long technical history are collapsed.
Paper Trial places the read-only Proposal Downside Review directly below those
targets. It is an advisory view of the proposed weights—not a view of holdings,
a fifth workflow stage, or an approval/execution gate.
The global navigation is intentionally limited to Overview, Research, Paper
Trial, and Operations, with Methodology & Sources as a utility. Company Detail
is a contextual drill-down from Research rather than a competing workspace.
Research date, scoring model, company search, and surface controls remain
visible in the page toolbar, and the URL preserves the workspace, date, model,
surface, and company for reproducible debugging.

The production visual system uses a warm ivory canvas, white evidence surfaces,
a light navigation rail, near-black primary actions, and a restrained clay
identity accent. Green, amber, and red remain reserved for real semantic state.
Body text is 16–17px, controls are at least 44px tall, and the same reusable
headers, action notices, metric strips, evidence panels, tables, and pipeline
steppers are used across every workspace. The sidebar collapses on narrow
screens and the layout stacks without horizontal page overflow.
The UI does not connect to a broker, issue personal buy/sell instructions, or
move money.

The dashboard's first 503-company score calculation now uses identity-safe
universe batch reads behind the existing scalar factor contract. On the
certified 2026-07-27 local checkpoint, QV completed in 2.8 seconds and QVML in
3.3 seconds on the reference workstation, versus 29.0 and 32.5 seconds through
the scalar compatibility path. Both paths were field-for-field identical and
Store query calls fell from 4,278 to 12. The facade validates the returned
ticker set, falls back to the independently fail-closed scalar path on a batch
error, and is discarded after the calculation. It is not a persistent result
cache and it does not change the active forward trial's frozen scoring-policy
files. Peak working sets were about 1.10 GiB for QV and 1.19 GiB for QVML, so
cold dashboard builds are serialized. These are local engineering
measurements, not a cross-machine latency guarantee.

The current-date gate is real rather than implied. At the 2026-07-29 live
read-only checkpoint the reviewed decision date is 2026-07-28: 503 S&P 500
members have
stable security identities,
500 have PIT company filings, all 503 have current action-safe prices, SPY is
current through the same close, and the latest required macro release is dated
2026-07-28. Validation reports zero hard failures and three visible warnings;
readiness is `READY`. Active trial `us-qv-forward-72c4560a442d` has one
registered 2026-07-27 proposal and zero executions. Predecessor
`us-qv-forward-8559d86b6a02` remains archived unchanged.

The broader 2025-to-current stateful engineering backtest now completes all six
periods. It applies the reviewed HES→CVX conversion, liquidates MTCH and PAYC
without restoring membership, and aligns 327 daily strategy/SPY observations
with no stale strategy points. Regime-aware QV returned 31.13% net, fixed QV
27.73%, and SPY 34.12% with zero taxes. This is pipeline evidence—not an alpha,
after-tax, or personal investment claim.

| Use level | Current state | Required exit condition |
|---|---|---|
| Supervised U.S. research | available now | keep `aios readiness` and `aios validate` non-failing |
| Advisory proposal stress review | available now in the CLI and Paper Trial for the registered simulation-only proposal; no artifact is stored by default | keep the forward trial unchanged and exact PIT identity, action-safe price, row-level liquidity, revenue, proposal, and source evidence sufficient |
| Local U.S. paper simulation | sandbox available; $100,000 cash, zero holdings/executions, no broker; the saved proposal is not a fill | pass the current read-only timing/evidence review; if its window expired, create a new prospective proposal |
| U.S. technical-beta completion | historical gate, recoverable daily workflow, retry-safe outbox, and deferred SMTP adapter complete | observe the active forward trial and the next naturally triggered guarded daily run; email activation remains optional and deferred |
| Controlled real-capital pilot | not approved; at least 8–12 weeks of untouched forward monitoring after freeze | broker reconciliation, alerts, price versioning, and user-approved tax/risk policy |
| India market build | next major phase after the U.S. technical gate | NSE/BSE identity, membership, filings, actions, calendars, taxes, benchmarks, and parity tests |

These are engineering estimates, not return promises. Provenance gaps and the
required elapsed forward-test period cannot be compressed by adding compute.

## Read this first

- [`agent.md`](./agent.md) is the compact context file for future AI agents.
- [`BEGINNER_GUIDE.md`](./BEGINNER_GUIDE.md) explains the project without
  assuming investing or Python experience.
- [`ARCHITECTURE.md`](./ARCHITECTURE.md) records the larger design decisions.
- [`FUTURE_BUILD_PLAN.md`](./FUTURE_BUILD_PLAN.md) orders future work and defines
  the acceptance gate for each milestone.
- [`INDIA_BUILD_PLAN.md`](./INDIA_BUILD_PLAN.md) defines the official-source,
  Nifty-50-first path from the U.S. reference build to an India research beta.
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
  with PIT SEC shares or per-share facts. New yfinance captures use the
  replayable v2 normalized export. Malformed completed-session candles remain
  immutable evidence but cannot enter `prices`; the storage layer also rejects
  an invalid close before it can overwrite a valid row. Legacy v1 captures
  remain replayable.
- **Macro:** FRED primary, with the free official US Treasury year-bounded CSV
  as an independent DGS2/DGS10/DGS30 fallback and cross-check. Both paths are
  release/PIT bounded; a divergence above five basis points is a visible
  quality warning.
- **Macro regime:** release-aware FRED vintages, PIT-safe regime snapshots, and
  an explicit `unknown` state when mandatory evidence is unavailable.
- **Reliable macro refresh:** API-limit-aware vintage chunks, transient retries,
  incremental overlap, and a hard failure signal after all sources are tried.
- **HTTP:** rate-limited client (SEC's 10 req/s, 429 backoff, retries).
- **Immutable provider evidence:** the shared HTTP boundary can atomically
  retain exact response bytes with secret-free request fingerprints and ingest
  links. Reviewed SEC Company Facts/Submissions now attach parser versions,
  canonical row counts and parsed SHA-256 values only after successful identity
  validation, and the verifier replays those exact bytes. FRED and yfinance use
  explicitly labeled normalized-library exports rather than false HTTP claims.
  Exact Treasury, Tiingo and SEC ticker-map capture/replay are also live-proven.
  Stooq currently serves a JavaScript verification page in this environment,
  so that provider remains explicitly unavailable and fails closed.
- **Factors:** PIT-aware Quality, Value, 12-minus-1 Momentum, and one-year Low
  Volatility. QV remains the backtested baseline; experimental QVML keeps the
  regime-relative Q/V tilt inside a 60% core and adds 25% Momentum plus 15% Low
  Volatility. A decision-scoped cache shares PIT fundamentals and identity-safe
  price windows, then is discarded after every decision. Interactive
  503-company reads are served by a validated universe-batch facade with scalar
  fallback; the certified factor implementation itself remains unchanged. The
  Streamlit control room anchors readiness to the latest market close that also
  has a certified investable universe; `Research` has a QV/QVML selector plus a
  segmented ranked-list, opportunity-map, and explicit missing-evidence view.
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
  reviewed U.S. decision date is 2026-07-28. `validate`, `readiness`, and
  `health` use read-only DuckDB connections. An omitted readiness date resolves
  from reviewed market evidence rather than the local calendar. When a gate is blocked, health
  labels the examined date as a candidate rather than certified. A newer price
  can value an existing
  simulated holding without silently becoming a newer portfolio-decision date.
- **Current refresh and local scheduling:** normal operation uses
  `aios refresh-us-daily`. It refreshes action-safe SPY first, reviews the
  unchanged universe through that completed session, refreshes all member
  prices and macro evidence inside the newly extended identity windows, and
  succeeds only when broad exact-date readiness reaches the same close. Its
  independent SQLite job record remains visibly `running` if the process is
  killed, allowing the next startup to recover it. `aios refresh-us-current`
  remains the lower-level selective prices, filings, SPY, and macro command and
  never auto-approves a new S&P member. `aios review-universe-current`
  separately archives the official S&P Global release archive and an
  independent current-component CSV. It
  extends unchanged membership, security, issuer/CIK, and provider-symbol
  windows together or extends none of them. A real announcement, source/parser
  failure, ticker drift, or unreviewed identity mismatch opens an incident and
  stops for review. One confirmed user-level daily timer runs the complete
  dependency chain at 02:00 New York time after every U.S. weekday, followed
  by weekly filing refreshes and checksum-verified backups;
  pause/resume/status/removal stay behind supported CLI commands. The timer
  retries ordinary failures, runs an idempotent check three minutes after the
  user manager starts, and Linux linger keeps it active after desktop logout
  while the computer is on. The dashboard uses short-lived read-only
  connections. Daily, filing, backup, and scheduled health commands share one
  30-minute cross-process queue, then DuckDB still waits up to five minutes for
  a brief reader overlap. This prevents simultaneous startup catch-up writers
  from racing while still failing visibly on a genuine long conflict. The
  generated units passed the native systemd verifier. The replacement daily
  service passed real 503-member July 23 and July 24 catch-ups plus no-download
  systemd recovery runs. The backup and filing services retain passed runs. The
  2026-07-22 current-data incident exposed isolated transient Yahoo empty
  responses. The adapter now retries those responses three times with bounded
  backoff while preserving reviewed provider and date limits.
  When today's membership is not yet reviewed, raw refresh may use the newest
  reviewed snapshot up to seven days old for collection only; it displays that
  date and does not approve a current portfolio decision. New reviewed issuers
  without Company Facts remain visible and are retried; an established issuer
  unexpectedly returning no facts still fails the refresh.
  Freshness is measured against the latest completed **U.S.** session, not the
  computer's calendar date. In India, a U.S. session dated July 24 finishes
  after midnight on July 25 IST and becomes eligible for the following refresh.
  Every EOD adapter independently rejects rows beyond the latest completed New
  York close and waits another 30 minutes for free-provider finalization, so an
  early manual run or persistent catch-up cannot store a partial daily candle.
- **Portfolio risk:** the deterministic long-only risk contract rejects missing
  sector/liquidity evidence, leverage, excessive position or sector
  concentration, excessive rebalance turnover, and breached drawdown limits.
  Conservative defaults are engineering safeguards, not final user-approved
  investment limits. They are connected to the persistent local paper workflow.
  The shared registered-proposal service behind the `stress-review` command and
  Paper Trial panel evaluates immutable, versioned proposal sensitivities after
  binding the account, trial, proposal, exact PIT identity, price, liquidity,
  revenue, scenario-policy, and calculation-source evidence. It reloads and
  rechecks those identities after calculation and before returning or publishing.
  A missing or changed dependency withholds or refuses the affected calculation
  instead of inventing a number.
  Deterministic mark shocks remain separate from the statistical
  volatility/correlation proxy; neither is a forecast, approval gate, or trade
  instruction. The current Federal Reserve calibration applies the
  [approximately 58% hypothetical equity decline in the 2026 supervisory
  severely adverse scenario](https://www.federalreserve.gov/publications/2026-stress-test-scenarios.htm);
  the Federal Reserve explicitly says that scenario is not a forecast.
- **Supervised paper workflow:** `paper-init`, `paper-propose`, `stress-review`,
  `paper-review`, `paper-execute`, `paper-mark`, and `paper-status` maintain a local
  checksum-protected account, FIFO tax lots, daily account values, proposal
  evidence, and explicit simulated confirmation. A proposal must be created
  before its scheduled U.S. session opens and can be recorded only from that
  session's close until the following U.S. open; expired retrospective fills
  are refused.
  `paper-review` runs the same timing, policy, evidence, risk, and price
  preflight without changing the account. There is deliberately no broker
  credential or order API.
  `stress-review --proposal ...` is read-only by default; `--output PATH`
  deliberately creates one write-once checksum-protected report.
  Every supported CLI writer of backup-covered DuckDB, raw-snapshot, paper,
  proposal, or forward-trial state shares one non-blocking project maintenance
  lease, including refresh, ingest, import, repair, and cleanup workflows.
  Caller-selected generated outputs are resolved before work, cannot target
  governed state or alias it through symlinks/hard links, and single-file
  reports and reviewed batch files publish atomically without replacement.
  Paper proposal output is confined to `data/paper/proposals`; explicit
  replacement validates the existing kind, account, and decision date. These
  are cooperative CLI boundaries, not protection against direct library calls
  or unrelated external writers. Final in-function compare-and-set hardening
  in the frozen paper/forward policy remains a next-trial change; the active
  trial is not rewritten to retrofit it.
- **Untouched forward-policy evidence:** `forward-freeze` records checksums for
  the factor, macro, risk, cost/tax, calendar, readiness, and paper rules plus
  the reviewed configuration. `forward-status` detects drift, every later
  proposal is registered, and drift blocks simulation while market data remains
  free to advance. `forward-restart --confirm-restart` creates a prospective
  baseline, archives a genuinely drifted predecessor without rewriting it, and
  atomically activates the replacement; it refuses to replace an unchanged
  trial and never executes the proposal.
- **Historical universe:** `universe_membership` stores effective intervals,
  start `known_date`, and independently dated `end_known_date`. A backtest
  target is membership known at the decision close and effective on the
  scheduled execution date, so announced additions/removals are not shifted
  into the wrong session. Backtests refuse the current active universe by
  default; `--allow-current-universe` is an explicitly labeled survivorship-
  biased diagnostic escape hatch. The original 60-event manifest certifies the
  bounded 2023-08-01 through 2024-12-31 window; reviewed event/reference batches
  extend the manually reviewed path through 2026-07-21. The first immutable
  no-change attestation advanced current operating coverage through 2026-07-22;
  later independently archived attestations advanced it through 2026-07-23 and
  then 2026-07-24, all without inventing an announcement date. This still does not claim a
  complete 1996-present announcement archive.
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
  the supported dashboard launcher, separate valuation/decision clocks,
  current-use readiness, and fail-closed
  portfolio risk, paper state, security conversions, and liquidation-only price
  extensions, current refresh orchestration, independent incident lifecycle,
  retry-safe notification leases/delivery audit, verified-backup archiving,
  evidence-backed no-change universe roll-forward, and managed scheduler
  safety. The current baseline is 494 passing tests with Ruff, bytecode
  compilation, and `git diff --check` clean.

## What comes next

Current U.S. membership, stable identity, action-safe prices, SPY, PIT filings,
macro evidence, risk checks, and supervised paper state now reach the reviewed
2026-07-28 decision close. The benchmark-first daily workflow, free-source
universe review, health, backup/recovery,
scheduler controls, local incident ledger, systemd failure/recovery handlers,
retry-safe notification outbox, fail-closed SMTP adapter, and local dashboard
smoke are implemented and tested. External email is still deliberately off
until local credentials and a real receipt test are supplied. The six-period
stateful rerun and its held-security evidence are complete. Three core timers are installed;
the daily service passed live 503-member July 23 and July 24 catch-ups and a
no-download systemd recovery proof, and it remains active after desktop logout while the
computer is on. Predecessor `us-qv-forward-8559d86b6a02` is archived unchanged.
Active trial `us-qv-forward-72c4560a442d` began prospectively from the
2026-07-27 close, has one registered proposal, and has zero executions. The
2026-07-29 full read-only review passed and stopped at an explicit human
decision; it produced no execution command and recorded no simulation. The
host-verified guarded daily run passed at 13:12 IST and all three user timers
are active and waiting, resolving the sandbox-only scheduler warning. One
non-critical fundamentals-coverage incident remains open for three SEC issuers
that returned no fundamental rows, so unattended operation is not currently
claimed. The next operational-evidence gate is the scheduled filings run or a
reviewed bounded retry; it must not be claimed in advance. Untouched elapsed
observation continues while immutable
evidence is extended beyond the now-live SEC Company Facts/Submissions path.
No-change days can advance
automatically; a real S&P
membership announcement still requires source review and is never
auto-approved. Final after-tax or controlled-capital claims still require
the user's jurisdiction, account, broker, tax, and risk-budget decisions.
Neither regime tilts nor QVML are validated alpha.
Proposal Stress Review v1 is complete for pending proposal targets. Stressing
recorded holdings, multifactor and Monte Carlo risk models, and owner-authored
scenario policies remain later milestones rather than implied capabilities.

Immutable snapshot storage now has content-addressed atomic files, per-fetch
metadata, ingest links, checksum verification, and backup/restore coverage.
Verification passes for 2,612 unique payloads and 2,111 parsed replays. A bounded
reviewed AAPL ingest replayed 4,102 canonical Company Facts rows plus one
Submissions metadata row directly from its two exact SEC responses. Production
yfinance and FRED ingests create explicitly labeled normalized exports—not
falsely claimed HTTP bodies—and the July 24 full-universe recovery raised their
live replay coverage to 505 price exports and 23 macro exports. The official
Treasury path replays 423 yield-curve rows through July 24 and exactly matches
FRED on the latest common DGS2/DGS10/DGS30 observation. A reviewed Tiingo
sample and the 10,429-row SEC ticker map also replay from exact bytes. The
latest verified backup, `backups/aios-20260728T082412Z`, contains 2,622 files
and 380,408,599 bytes; its manifest SHA-256 is
`cd4ce6e3c8256013483eee438b3167cf8ef12815b7ffbb04e03b3e4ea629d25b`.
It passed a disposable, non-destructive restore drill through the real restore
path. Historical byte-only reference snapshots remain
honestly labeled; new production adapters cannot pass their gate without a
registered replay parser.
