# AIOS India Build Plan

This is the gated implementation plan for making India the primary AIOS market
without weakening the point-in-time, identity, provenance, risk, or operating
contracts proven in the U.S. reference build.

Status date: **2026-07-22**. `ARCHITECTURE.md` remains the design authority and
`FUTURE_BUILD_PLAN.md` controls cross-project sequencing. No Indian security is
eligible for ranking until every gate in Phases I0-I5 passes for its exact date.

## Product decision

Start with a **Nifty 50, NSE-primary, supervised research beta**. Do not begin
with every NSE/BSE listing. Fifty liquid securities are enough to prove dual
identity, exchange timestamps, Indian filings, corporate actions, INR costs,
calendar/settlement, membership provenance, and parity with the U.S. engine.

The first India release will:

- rank a bounded, dated Nifty 50 universe for research;
- show full company name, NSE symbol, ISIN, evidence dates, and missing data;
- use the Nifty 50 Total Return Index as the preferred performance benchmark;
- support local paper simulation only after India readiness passes;
- remain broker-disconnected and make no personal buy/sell recommendation.

Nifty 100/500 and BSE-primary instruments come only after Nifty 50 parity. BSE
is still a first-class identity and cross-check venue from the beginning; it is
not treated as an interchangeable ticker suffix.

## What can proceed while the U.S. trial runs

The 8-12 week untouched U.S. forward observation is a controlled-capital gate,
not a reason to leave engineering idle. India schema and adapters may start
after the U.S. operational-beta contracts pass, while the frozen U.S. policy
continues unchanged in parallel.

The remaining U.S. dependency chain is:

1. connect production provider transports to the immutable snapshot foundation;
2. retry-safe notification outbox and one external delivery channel;
3. deterministic stress testing and anomaly review cases;
4. experiment registry and versioned configuration boundaries;
5. clean natural timer cycles plus a verified backup/restore drill.

## Official-source feasibility findings

| Need | Primary official source | Finding and plan |
|---|---|---|
| Current security master and EOD reports | [NSE All Reports](https://www.nseindia.com/all-reports) | Daily UDiFF bhavcopy and MII security files are suitable for current ingestion, schema-drift tests, and symbol/ISIN checks. |
| Current Nifty 50 constituents and method | [NSE Indices Nifty 50](https://www.niftyindices.com/indices/equity/broad-based-indices/nifty--50) | Use the official constituent file and methodology; never scrape a finance blog for the live universe. |
| Membership `known_at` and effective dates | [NSE Indices press releases](https://www.niftyindices.com/press-release) | Releases contain publication dates and stated effective dates. Parse and retain the PDF plus hash; trade eligibility starts only under the configured next-session policy. |
| Reconstitution schedule | [NSE Indices rebalancing calendar](https://niftyindices.com/resources/index-rebalancing-schedule) | Nifty 50 is normally reviewed semi-annually, but event-driven releases still need monitoring. The calendar is not a substitute for each release. |
| Historical constituents | [NSE Indices data subscription](https://www.niftyindices.com/offerings/data-subscription) | Official historical constituent data is subscription-based. First try a bounded release-PDF reconstruction plus verified baseline; stop for a paid-data decision if coverage cannot be certified. |
| Prices | [NSE All Reports](https://www.nseindia.com/all-reports) and [NSE historical-data terms](https://www.nseindia.com/static/market-data/eod-historical-data-subscription) | Current bhavcopy is available; official bulk historical EOD is paid. A free beta may use reviewed Yahoo `.NS` intervals only as a provider, cross-checked against official current closes and stored as normalized exports—not as exact NSE history. |
| Financial statements | [NSE financial results](https://www.nseindia.com/companies-listing/corporate-filings-financial-results) and [NSE XBRL information](https://www.nseindia.com/static/companies-listing/xbrl-information) | Exchange broadcast timestamps provide the PIT availability boundary. Build taxonomy-versioned parsers for consolidated Ind AS first; retain original XBRL/PDF payloads. |
| Announcements | [NSE corporate announcements](https://www.nseindia.com/companies-listing/corporate-filings-announcements?tabIndex=equity) | Preserve received and dissemination timestamps, attachment hash, subject, and symbol/ISIN identity. |
| Corporate actions | [NSE corporate actions](https://www.nseindia.com/companies-listing/corporate-filings-actions) | Use announced ex/record dates and exact action terms. Never infer a split or bonus solely from a price jump. |
| Calendar and sessions | [NSE market timings and holidays](https://www.nseindia.com/resources/exchange-communication-holidays) | Version annual trading calendars and special sessions such as Muhurat trading. Trading and settlement holidays are different concepts. |
| Settlement | [SEBI T+1 circular](https://www.sebi.gov.in/legal/circulars/sep-2021/introduction-of-t-1-rolling-settlement-on-an-optional-basis_52462.html) and [SEBI optional T+0 expansion](https://www.sebi.gov.in/legal/circulars/dec-2024/enhancement-in-the-scope-of-optional-t-0-rolling-settlement-cycle-in-addition-to-the-existing-t-1-settlement-cycle-in-equity-cash-markets_89443.html) | Model a dated settlement policy. Do not hard-code one global `T+1` constant because optional T+0 exists alongside it. |
| Benchmark | [NSE Indices total-return methodology](https://www.niftyindices.com/resources/index-concepts/total-return-index) | Prefer Nifty 50 TRI for return comparisons; a price-only index omits dividends. |
| Macro | [RBI DBIE](https://dbieold.rbi.org.in/DBIE/) and [MoSPI release calendar](https://mospi.gov.in/sites/default/files/Advance_Release_Calendar.pdf) | Add release-aware RBI/MoSPI series with real publication dates. Do not copy latest revised values backward. |
| Fees and levies | [NSE investor levies](https://www.nseindia.com/static/invest/first-time-investor-sebi-turnover-fees-stt-other-levies) | Version STT, stamp duty, SEBI/exchange charges, GST treatment, and effective dates. Brokerage remains account-specific. |
| Capital-gains tax | [Income Tax Department](https://www.incometax.gov.in/iec/foportal/help/all-topics/e-filing-services/file-itr-2-online) | Tax assumptions require the user's residency, account/entity type, holding purpose, and tax year. The engine must not claim certified after-tax returns from generic defaults. |

Official websites may impose access, licensing, or automation conditions even
when a file is visible in a browser. Phase I2 includes a written source/terms
review and a small live fetch probe before any bulk download.

## Target architecture

### I0 - Finish the portable U.S. operating contracts

Exit gate:

- raw payload/version evidence survives backup and restore;
- one external failure and recovery alert is delivered idempotently;
- stress scenarios and anomaly cases are deterministic and reviewable;
- experiments and policy versions are immutable;
- 1-3 natural scheduler cycles pass after the latest operational change.

The U.S. forward policy remains frozen. These operational additions must not
retune factors or overwrite the existing forward trial.

### I1 - Add first-class market contracts

**Started 2026-08-11.** Seven new, purely additive tables exist in the live
schema: `markets`, `venues`, `market_profiles`, `security_listings`,
`trading_sessions`, `settlement_policies`, `benchmarks`. Applied to the real
production database (not just a fixture) — verified against real row counts
before/after, zero existing rows touched. A `us_equity` market/venue/
market_profile/benchmark row and 569 `security_listings` rows now exist,
backfilled from the already-verified `universe_membership` history; the
backfill script is idempotent (checked by running it twice).

The exit gate's core claim — shared factor/identity/PIT/readiness code needs
no market-specific branching — is now proven, not assumed:
`tests/test_market_contracts.py` builds one synthetic ISIN-identified,
INR-denominated security on a synthetic NSE-shaped venue and runs it through
the *exact* `compute_composite`, `security_id_for_ticker`,
`universe_identity_labels`, `universe_data_coverage`, and
`assess_us_readiness` the U.S. reference data uses — no `.NS`, no `NSE`, no
currency branch anywhere in those call paths. All three tests pass. One real,
if cosmetic, finding surfaced along the way: `readiness.py`'s success/failure
detail strings hardcode the words "S&P 500" regardless of `rules.universe_id`
— the actual numeric checks are correctly parameterized, only the English
description text is wrong. `readiness.py` is frozen; this is a fix for the
next policy version, not a blocker.

Adding these tables also broke three existing backup/restore compatibility
tests — a real regression, not a false alarm. `operations.py`'s
`_LEGACY_020_COLUMN_CONTRACTS` pins exact column/constraint hashes for two
historical "0.2" schema snapshots that test fixtures reconstruct by taking
*today's* live schema and removing one specific table; adding tables changes
what "today's live schema minus that table" hashes to, so a third pinned
entry was required (not a redesign — this dict is meant to grow every time
schema.py changes, exactly as the existing two-entry history shows). Fixed,
verified, documented in the dict itself so the next schema change knows the
pattern to follow.

**I1 substantially complete (2026-08-11).** `src/aios/markets.py` is the
validated registration/read layer over those tables, because raw SQL let a
caller insert an invalid timezone, a fabricated currency, or a listing whose
`known_at` postdated the interval it claimed to cover. It enforces:

- ISO 3166-1 alpha-2 country, ISO 4217 currency, IANA timezone (validated
  through `zoneinfo`, not a regex), ISO 10383 MIC shape;
- **ISO 6166 ISIN check-digit validation** — a transposed ISIN is otherwise a
  silent identity error that routes data to the wrong company, which is
  precisely the failure class this system exists to prevent. Verified against
  real ISINs (Apple `US0378331005`, Reliance `INE002A01018`, Vodafone
  `GB0002634946`) and confirmed to reject single-digit corruptions of each;
- half-open `[listed_start, listed_end)` intervals, and a refusal when
  `known_at` postdates `listed_end`;
- referential checks: a venue must belong to a registered market, a listing
  to a registered venue *and* an existing `security_master` security.

`active_listing()` takes `as_of` and `known_as_of` **separately**, mirroring
`universe_membership`'s `effective_*` / `known_date` split. Collapsing them
would reintroduce look-ahead bias through the identity layer. Pinned by
`tests/test_markets.py` (17 tests), including the case that matters: a
listing whose interval covers March but whose `known_at` is August is
correctly invisible to a March decision and visible to a September one.

Real India identities are now registered in the live database — `in_equity`
(IN / INR / Asia/Kolkata), venue `xnse` (MIC XNSE, National Stock Exchange of
India Limited) and venue `xbom` (MIC XBOM, BSE Limited). These are verifiable
ISO/registry facts, not market data; no Indian price, fundamental, or
membership row exists yet and none may until I2's source gate passes.

Still deferred from I1, deliberately: `trading_sessions` and
`settlement_policies` remain empty. Populating them requires the real NSE
holiday calendar and the dated T+1/optional-T+0 settlement rules, which are
I2/I4 inputs — inventing placeholder sessions now would be fabricated
evidence. Neither blocks I2.


Add versioned entities rather than scattered string columns:

- `markets`: country, base currency, timezone and default calendar;
- `venues`: NSE/BSE, MIC where available, session and settlement profiles;
- `market_profiles`: benchmark, universe, filing/action sources and policy IDs;
- `security_listings`: stable security ID, venue, symbol, series, ISIN,
  security type, currency, listed/delisted interval and `known_at` evidence;
- `provider_symbol_history`: retain current dated mapping and add venue/market;
- `trading_sessions`: normal, early, special and closed sessions;
- `settlement_policies`: dated cycle and holiday calendar;
- `benchmarks`: price/TRI identity and provider intervals.

Migration is additive. Existing U.S. rows receive an explicit `us_equity`
market profile through a tested migration; factor outputs and the frozen U.S.
policy hash must not change.

Exit gate: every existing U.S. integration test passes through the generic
market interface, and a synthetic India fixture passes the same identity,
calendar, PIT, factor, portfolio, and readiness contracts without `.NS`, INR,
or NSE logic inside shared factor code.

### I2 - Run a source and licensing spike

**Executed 2026-08-11. Findings in [`INDIA_SOURCE_MATRIX.md`](./INDIA_SOURCE_MATRIX.md)
— read that before any India ingest work.**

Headline result: **the free-scraping path is closed, on NSE's own stated
terms, on two independent grounds.** Both clauses were retrieved and
independently confirmed from the canonical Terms of Use page:

- Clause 4 prohibits "any systematic or automated data collection activities
  (including scraping, data mining, data extraction and data harvesting)"
  — no personal-use or research exemption exists;
- Clause 3 prohibits using the data "for any gaming, virtual trading or
  simulation activities under any circumstances whatsoever" — which
  describes this project's backtest and paper-trading simulation directly,
  and would bar the use even for lawfully obtained data.

`robots.txt` is permissive and contradicts Clause 4. That contradiction is
real, but a crawler convention is not a licence grant; the Terms bind.

Every free scraper library and Kaggle dataset examined (`jugaad-data`,
`nsepy`, `eod2`, `nsetools`) sources from nseindia.com and therefore inherits
this defect — a permissive *code* licence never conveys rights to the *data*.
This is the same test that eliminated yfinance for U.S. commercial use, and
NSE's prohibition is stricter than Yahoo's.

The constructive half: licensed access is genuinely affordable. NSE's own
tariff has a Students/Researchers tier (₹18,000/yr indicative, 2020 document)
and prices internal non-redistributed use at a quarter of display use; NSE's
data policy explicitly contemplates fee waivers for "Researchers, Students
etc." Among third parties, **Twelve Data ($29/mo) has the only licence whose
grant is "internal business purposes" rather than "personal, non-commercial"**
— the distinction that determines whether the licence survives the project
growing beyond a hobby. Tiingo, despite already being integrated for U.S.
data, covers no Indian equities at all.

**Blocking open item:** dated historical Nifty 50 constituents remain
unresolved. Without survivorship-safe membership, no honest India backtest is
possible — this is the gate, not the price of prices.


For each source, capture exact response bytes or label a library-only artifact
as `normalized_provider_export`. Record access method, terms URL, request rate,
retention permission, historical depth, schema, timestamps, and failure mode.

Explicit decision gate:

- **Free bounded beta:** official current files + official release archive +
  reviewed free provider intervals are sufficient for a clearly dated window;
- **Paid historical track:** request prices for official NSE EOD and NSE Indices
  constituent history if free evidence cannot certify the target window;
- **Stop:** do not build a survivorship-biased backtest from today's Nifty 50.

Exit gate: a checked-in source matrix and immutable sample snapshots reproduce
their parsed-row hashes; no secret, cookie, or signed URL is stored.

### I3 - Build the India identity and universe layer

1. Import NSE/BSE security masters keyed primarily by ISIN plus dated listing.
2. Keep issuer identity separate from each listed security and venue listing.
3. Import one reviewed Nifty 50 baseline.
4. Parse official NSE Indices releases into add/remove events with publication
   timestamp, effective date, action, symbol, company, source URL and hash.
5. Reconcile events to the baseline and fail on gaps, duplicate active ISINs,
   ticker reuse, unannounced membership changes, or announcement/effective-date
   inversion.

Exit gate: `membership_on(date, known_at=date)` is demonstrably survivorship-
safe for the bounded window and every member resolves to one active security,
listing, ISIN, issuer, and reviewed provider interval.

### I4 - Add prices, actions, benchmark and calendar

- ingest official current UDiFF bhavcopy and security master snapshots;
- implement reviewed Yahoo `.NS` mappings only where the source matrix permits;
- reconcile current close, split, bonus, rights and dividend evidence;
- ingest Nifty 50 TRI through a reviewed benchmark adapter;
- version NSE sessions, holidays, special sessions and settlement calendars;
- add stale/missing-price and action-mismatch review cases.

Exit gate: every current member and benchmark has complete action-safe prices
through one exact reviewed close; no current-day partial session is certified.

### I5 - Add PIT fundamentals and macro

- parse consolidated Ind AS XBRL first, then standalone and sector-specific
  bank/NBFC/insurance taxonomies;
- store period end separately from exchange received/dissemination timestamps;
- quarantine duplicate, amended, unit/scale-conflicting, or taxonomy-unknown facts;
- implement Indian TTM semantics with tests for non-March fiscal years;
- ingest RBI/MoSPI series with release/vintage dates and revision history.

Exit gate: each ranked value was publicly knowable at its decision timestamp,
coverage loss is visible, and revised macro/filing data cannot leak backward.

### I6 - Add India costs, taxes, risk and research parity

Version separately:

- exchange/clearing/SEBI charges, STT, stamp duty and GST;
- brokerage and slippage assumptions;
- settlement and cash-availability rules;
- user-approved capital-gains and account policy;
- India sector, liquidity, concentration and price-limit controls.

Run the same experiment registry, deterministic stresses, stateful holdings,
tax lots, daily equity curve, benchmark alignment, and holdout rules used by the
U.S. reference implementation.

Exit gate: Nifty 50 readiness, validation, backtest, paper proposal and System
Control all pass under the India market profile with no U.S.-specific defaults.

### I7 - Operate the India research beta

- schedule current prices/actions, filings, macro, membership releases,
  validation, backup and notification delivery;
- observe 1-3 natural cycles before calling the beta operational;
- begin a new frozen India forward trial; never reuse the U.S. trial ID;
- expand to Nifty 100/500 only as separately reviewed experiments.

Controlled capital remains a later, explicit approval. Broker integration and
orders are not part of this plan.

## Estimated path

These are focused engineering ranges, not delivery promises:

| Work | Estimate | Main uncertainty |
|---|---:|---|
| Remaining U.S. operational beta | 2-4 weeks plus natural cycles | raw transport capture and selected alert channel |
| India market schema and parity migration | 1-2 weeks | U.S. hard-coded assumptions discovered by tests |
| Official-source/licensing spike | 1 week | NSE/NSE Indices access and retention terms |
| Nifty 50 identity and PIT membership | 2-4 weeks | historical baseline/release completeness |
| Prices/actions/calendar/benchmark | 2-3 weeks | historical price licensing and action reconciliation |
| XBRL fundamentals and macro | 3-6 weeks | taxonomy variants and amended filings |
| Costs, stress, backtest, readiness and paper beta | 2-4 weeks | user tax/account decisions and data gaps |

A credible Nifty 50 supervised research beta is therefore approximately
**10-16 focused engineering weeks after the U.S. operational dependencies**, or
**14-24 weeks** if official historical data must be reconstructed manually or a
provider/licensing issue forces redesign. The untouched U.S. observation can
run concurrently and is not shortened by these estimates.

## Decisions needed from the owner

Not needed to start I0-I3:

- external alert channel: email or Slack;
- initial India account type and tax residency;
- intended broker only for fee-statement modeling, not integration;
- whether a paid official historical dataset is acceptable if the free bounded
  evidence fails the source gate;
- final capital, turnover, concentration and drawdown limits.

Recommended defaults until those decisions are required: Nifty 50, NSE primary,
BSE cross-check, INR, supervised research only, zero broker connectivity, and
no after-tax performance claim.
