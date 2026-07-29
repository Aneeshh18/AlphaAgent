# AI Investment OS — beginner guide

## What this project does

This project collects public financial information about companies, stores it
locally, calculates transparent investment metrics, and ranks a chosen group of
stocks. Think of it as a small research assistant with a database and formulas.

It currently answers questions such as:

- Which tracked companies have stronger profitability and cash generation?
- Which companies look cheaper relative to the rest of the tracked universe?
- What numbers were actually available on a historical date?

It does not buy or sell anything. It is research software, not financial advice.

## Which market is this for?

The finished product is intended mainly for Indian stocks. The current data is
the carefully reviewed U.S. reference build used to prove that dates,
identities, scores, costs, and backtests behave correctly. NSE/BSE data is not
loaded yet and the current rankings must not be described as Indian-market
coverage.

India will be added through market and data-source adapters, not by copying the
whole system. The formulas, risk checks, portfolio accounting, and reports stay
shared. Exchange calendars, identifiers, filings, prices, index membership,
taxes, benchmarks, and brokers remain market-specific and reviewed.

You should not need to understand DuckDB or Streamlit for normal use. DuckDB is
the internal data file and Streamlit draws the screen. Use `aios dashboard`,
`aios health`, `aios refresh-us-daily`, and the backup/restore commands. On a
Linux computer, optional AIOS-managed timers provide simple start, stop, status,
refresh, and backup automation. Hosted or multi-user deployment is not ready.

## The big picture

```text
Public sources
  SEC filings     price history      macro indicators
       \              |                    /
        └──────────── ingest adapters ────┘
                         |
                    DuckDB database
                         |
                Quality + Value formulas
                         |
                 ranking / dashboard
```

The main reason for the database is reproducibility: we want to know both what
a number describes and when the number became public. This matters especially
for macro data, because inflation and GDP history can be revised.

## The most important concept: “as of”

Suppose a company reports results for December 2024, but files the report in
February 2025. A February 2024 backtest must not see that result. The code stores
both dates:

- `period_end`: the accounting period being described.
- `as_of_date`: the filing date, when the market could first know it.

Every historical calculation must use `as_of_date <= decision_date`. This is
called point-in-time correctness and prevents look-ahead bias.

Macro data has the same protection, with slightly different names:

- `macro.date` is the date the economic observation describes.
- `macro.release_date` is when that particular vintage became public.

If GDP for 2024 is revised in 2025, a 2024 decision can use the original
vintage but not the 2025 revision. The macro queries choose the latest vintage
whose release date is on or before the decision date. Older rows from before
this protection was added are marked `legacy_unversioned` and are not used by
the regime layer.

Historical universe membership has four dates/ideas:

- `effective_start`: when a ticker begins belonging to the investable universe.
- `effective_end`: the first date when it no longer belongs; blank means open-ended.
- `known_date`: when the interval's start was publicly knowable.
- `end_known_date`: when a finite interval's end was publicly knowable.

The backtester chooses a target that was knowable at decision close and is
effective on the scheduled execution date. This matters when an announced
addition or removal becomes effective on the next session: the trade uses the
new execution-date universe without leaking an announcement that was not yet
public.

## Where the code lives

- `src/aios/ingest/`: downloads and normalizes outside data.
- `src/aios/storage/`: creates and queries the DuckDB file.
- `src/aios/factors/`: turns stored data into metrics and scores.
- `src/aios/cli.py`: the command-line entrypoint.
- `src/aios/dashboard.py`: the optional local visual dashboard.
- `tests/`: small checks that protect the important behavior.
- `data/aios.duckdb`: local database; it is generated data, not source code.

## What gets calculated

### Quality

Quality is a 0–100 universe-relative score made from available percentiles of:

- ROIC: after-tax operating profit compared with invested capital.
- FCF margin: free cash flow compared with revenue.
- Gross margin: gross profit compared with revenue.
- Piotroski F-Score: nine profitability, leverage, liquidity, and efficiency
  checks when enough history exists.

Banks and other financial companies do not behave like ordinary manufacturers,
so standard ROIC is not a useful measure for them. That limitation is exposed in
the output rather than hidden.

### Value

Value ranks lower positive valuation multiples as “cheaper”:

- P/E
- EV/EBITDA
- Price/Free Cash Flow
- EV/Sales
- Price/Book

The current code derives EBITDA from operating income plus depreciation. It does
not use net income as an EBITDA substitute. The old incorrectly labeled rows
have now been removed after a database backup.

### Composite

The composite combines the two scores using the macro regime known on the
decision date:

```text
QV score = regime_quality_weight × Quality + regime_value_weight × Value
```

The initial, deliberately modest policy is:

| Regime | Quality | Value |
|---|---:|---:|
| Goldilocks | 60% | 40% |
| Reflation | 45% | 55% |
| Stagflation | 65% | 35% |
| Deflationary | 55% | 45% |
| Risk-off | 70% | 30% |
| Unknown or incomplete macro data | 60% | 40% |

These remain research hypotheses. They have passed short point-in-time
engineering tests but have not established future investment performance. If
macro evidence is missing or not PIT-ready, the system uses the baseline 60/40
blend and labels the row so the fallback is visible.

Each result has a letter grade and a list of missing inputs; a grade is not a
guarantee or a buy signal. The current policy requires at least two Quality inputs and two Value
multiples, and requires both factor scores before publishing QV. Incomplete
rows remain visible as `N/A` rather than being silently promoted.

## First-time setup

From the project root:

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev,dashboard]"
cp .env.example .env
```

Open `.env` and set a real SEC User-Agent email. Add a FRED API key if macro
data is wanted. The no-key official Treasury adapter is an independently
working fallback/cross-check for 2-, 10-, and 30-year yields. It uses a
year-bounded CSV, keeps the exact response, and refuses malformed data.
`TIINGO_API_KEY` is optional; leave it empty unless you have your own Tiingo
token and explicitly review that provider.

Check the installation:

```bash
PYTHONPATH=src .venv/bin/python -m aios.cli doctor
PYTHONPATH=src .venv/bin/python -m aios.cli status
PYTHONPATH=src .venv/bin/python -m aios.cli audit
```

## Common workflows

Ingest one company:

```bash
PYTHONPATH=src .venv/bin/python -m aios.cli ingest-ticker AAPL
```

Ingest the starter universe:

```bash
PYTHONPATH=src .venv/bin/python -m aios.cli ingest-batch data/universe_20.txt
```

Ingest macro series:

```bash
PYTHONPATH=src .venv/bin/python -m aios.cli ingest-macro
```

The first release-aware run can be large. The code automatically splits FRED
histories below its per-request vintage limit and retries truncated network
responses. Later runs refresh only a 31-day release-date overlap. Successful
series are preserved if another series fails, while the command still returns
an error so the failed source cannot be missed.

View the point-in-time macro regime for a date:

```bash
PYTHONPATH=src .venv/bin/python -m aios.cli macro-regime --as-of 2025-12-31
```

The regime command shows the label, component states, metric values, and the
release dates used. If required data is missing or still unversioned, it says
`unknown` instead of guessing. This safeguard is required before any
backtesting.

Build and import a historical universe before running a serious backtest. The
repository includes a small audited event manifest for 2023-08-01 through
2024-12-31. Download the pinned `sp500_ticker_start_end.csv` reference into
`data/`, then run:

```bash
PYTHONPATH=src .venv/bin/python -m aios.cli build-universe-membership \
  --baseline-spans data/sp500_ticker_start_end.csv \
  --events examples/sp500_events_verified_2023-08_to_2024-12.csv \
  --output data/sp500_membership_2023-08_to_2024-12.csv \
  --start 2023-08-01 --end 2024-12-31 \
  --baseline-source \
  https://github.com/fja05680/sp500/blob/4aeb5f6046dea43063f9c7be72dfdf16e96d2821/sp500_ticker_start_end.csv
```

The builder refuses missing event edges, false replacements, future known
dates, duplicate actions, and source disagreements. It deliberately ends every
open interval on 2025-01-01 so the bounded data cannot be used outside its
certified window. The generated membership CSV includes separate provenance
for both interval edges. Every finite interval must have `end_known_date`:

```text
universe_id,ticker,effective_start,effective_end,known_date,end_known_date,source
sp500,BBWI,2023-08-01,2024-10-01,2023-08-01,2024-09-24,<reviewed sources>
```

The shortened source cell above is only a readability example; use the
builder's complete generated row, not this excerpt. Import it with:

```bash
PYTHONPATH=src .venv/bin/python -m aios.cli import-universe \
  data/sp500_membership_2023-08_to_2024-12.csv
PYTHONPATH=src .venv/bin/python -m aios.cli validate
```

Close Streamlit before importing because DuckDB allows only one writing
process. The import refuses missing known dates, invalid intervals, and
overlaps. See `SP500_DATA_PROVENANCE.md` before extending or using the data.

A ticker can change while the underlying security continues, and an old ticker
can later be reused by an unrelated company. Build and import stable internal
security IDs before validation or backtesting:

```bash
PYTHONPATH=src .venv/bin/python -m aios.cli build-security-identities \
  --membership data/sp500_membership_2023-08_to_2024-12.csv \
  --transitions \
  examples/sp500_security_transitions_verified_2023-08_to_2024-12.csv \
  --output data/sp500_security_identities_2023-08_to_2024-12.csv \
  --universe-id sp500

PYTHONPATH=src .venv/bin/python -m aios.cli import-security-identities \
  data/sp500_security_identities_2023-08_to_2024-12.csv
PYTHONPATH=src .venv/bin/python -m aios.cli validate
```

The current file links four verified ticker transitions and keeps replacements
separate. Most IDs are labeled `bounded_ticker`: they are safe only inside this
certified window and are not globally authoritative IDs.

Historical ingestion needs three different identities:

- a **security ID** follows one listed share across a verified ticker change;
- an **issuer ID and dated CIK** identify the legal SEC filer that owns it; and
- a **provider symbol interval** says which yfinance, optional Tiingo, or Stooq
  symbol is safe for that security on each date.

They cannot be collapsed into one ticker. For example, `DOC` before 2024-03-04
belongs to Physicians Realty price history, not Healthpeak, and `SW` before
2024-07-08 is not Smurfit Westrock. Import the reviewed reference manifests:

```bash
PYTHONPATH=src .venv/bin/python -m aios.cli import-reference-identities \
  --issuer-ciks examples/sp500_issuer_cik_history_verified.csv \
  --security-issuers examples/sp500_security_issuer_assignments_verified.csv \
  --provider-symbols examples/sp500_provider_symbol_history_verified.csv
PYTHONPATH=src .venv/bin/python -m aios.cli validate
```

Then ingest through the immutable identity, not an old ticker:

```bash
PYTHONPATH=src .venv/bin/python -m aios.cli ingest-issuer \
  aios:issuer:cencora
PYTHONPATH=src .venv/bin/python -m aios.cli ingest-security-prices \
  aios:security:cencora-common --provider yfinance \
  --start 2023-08-01 --end 2025-01-01
```

The controlled first batch contains six issuers, six CIK intervals, six
security-owner intervals, and eight provider intervals. A mapping marked
`unavailable` or `blocked_wrong_security` is a deliberate stop, not permission
to guess another ticker.

For ordinary companies whose ticker and security are unchanged across the
whole certified window, use the conservative reviewed-batch workflow:

```bash
PYTHONPATH=src .venv/bin/python -m aios.cli build-reference-batch \
  examples/sp500_reference_batch_02_tickers.txt \
  --batch-name sp500_reference_batch_02 \
  --output-dir /tmp/aios_reference_batch_02 \
  --start 2023-08-01 --end 2025-01-01 \
  --verified-date 2026-07-17
```

This checks the certified membership interval, SEC's current ticker map, the
issuer's SEC submissions identity and filing dates on both sides of the
certified window, and the complete provider date range. It writes three import
manifests when at least one candidate passes and always writes a
`sp500_reference_batch_02_review.csv` file. Every rejected row stays in that
review file with a reason; never move it into an accepted manifest by hand
without new evidence. Source-payload and price-sample SHA-256 fingerprints make
the snapshot auditable and expose later provider revisions.

Batch 02 has already been reviewed and versioned under `examples/`. Import and
ingest it with:

```bash
PYTHONPATH=src .venv/bin/python -m aios.cli ingest-reference-batch \
  --issuer-ciks examples/sp500_reference_batch_02_issuer_ciks.csv \
  --security-issuers examples/sp500_reference_batch_02_security_issuers.csv \
  --provider-symbols examples/sp500_reference_batch_02_provider_symbols.csv \
  --start 2023-08-01 --end 2025-01-01
PYTHONPATH=src .venv/bin/python -m aios.cli validate
```

For a large reviewed batch, you can reuse the SEC's nightly Company Facts ZIP
instead of making one Company Facts request per issuer. This is optional and
does not help with ticker identity, prices, or announcement dates:

```bash
mkdir -p data/sec
curl --fail --location \
  https://www.sec.gov/Archives/edgar/daily-index/xbrl/companyfacts.zip \
  --output data/sec/companyfacts.zip

PYTHONPATH=src .venv/bin/python -m aios.cli ingest-reference-batch \
  --issuer-ciks examples/sp500_reference_batch_02_issuer_ciks.csv \
  --security-issuers examples/sp500_reference_batch_02_security_issuers.csv \
  --provider-symbols examples/sp500_reference_batch_02_provider_symbols.csv \
  --companyfacts-zip data/sec/companyfacts.zip \
  --start 2023-08-01 --end 2025-01-01
```

The download is large and the archive is refreshed nightly, so keep the normal
command for small or freshness-sensitive runs. AIOS does not download it
automatically. It reads only the reviewed CIK files and rejects missing,
duplicate, malformed, or wrong-CIK members. The whole `data/` directory is
gitignored, so the archive cannot accidentally become part of the repository.

This second batch accepted 25 of 25 candidates. Together with the controlled
corporate-action batch, it brought the local reference layer to 31 issuers, 31
CIK intervals, 31 security-owner intervals, and 33 provider intervals. The
automatic path is intentionally limited to unchanged full-window securities;
ticker changes, mergers, retired names, share classes, and unavailable provider
history belong in separately reviewed exception batches.

Batch 03 demonstrates the rejection path:

```bash
PYTHONPATH=src .venv/bin/python -m aios.cli build-reference-batch \
  examples/sp500_reference_batch_03_tickers.txt \
  --batch-name sp500_reference_batch_03 \
  --output-dir /tmp/aios_reference_batch_03 \
  --start 2023-08-01 --end 2025-01-01 \
  --verified-date 2026-07-17

PYTHONPATH=src .venv/bin/python -m aios.cli ingest-reference-batch \
  --issuer-ciks examples/sp500_reference_batch_03_issuer_ciks.csv \
  --security-issuers examples/sp500_reference_batch_03_security_issuers.csv \
  --provider-symbols examples/sp500_reference_batch_03_provider_symbols.csv \
  --start 2023-08-01 --end 2025-01-01
```

The build writes all artifacts and exits non-zero because ANSS was rejected:
its retired ticker is absent from the SEC current ticker map after acquisition.
That is an expected review signal, not permission to guess a CIK. The accepted
manifests contain only the other 24 names. After their successful ingest, the
reference layer contains 55 issuers, 55 CIK intervals, 55 owner intervals, and
57 provider intervals.

Batch 04 continues the same fail-closed workflow:

```bash
PYTHONPATH=src .venv/bin/python -m aios.cli build-reference-batch \
  examples/sp500_reference_batch_04_tickers.txt \
  --batch-name sp500_reference_batch_04 \
  --output-dir /tmp/aios_reference_batch_04 \
  --start 2023-08-01 --end 2025-01-01 \
  --verified-date 2026-07-17

PYTHONPATH=src .venv/bin/python -m aios.cli ingest-reference-batch \
  --issuer-ciks examples/sp500_reference_batch_04_issuer_ciks.csv \
  --security-issuers examples/sp500_reference_batch_04_security_issuers.csv \
  --provider-symbols examples/sp500_reference_batch_04_provider_symbols.csv \
  --start 2023-08-01 --end 2025-01-01
```

The builder accepted 19 of 25 candidates. It rejected BF.B, BK, and BRK.B
because their exact historical ticker was absent from SEC's current ticker map,
and rejected BG, BLK, and C because the SEC submissions history returned by the
strict automatic path did not reach the certified window start. The command
therefore exits non-zero after writing the review and accepted manifests. Do
not treat that expected signal as an ingest failure: inspect the review CSV,
then ingest only the 19 accepted rows with the second command. The reference
layer contained 74 issuers, 74 CIK intervals, 74 owner intervals, and 76
provider intervals at that checkpoint.

The rejected names were then handled in a separate manual exception manifest,
because each needed different evidence. BF.B/BRK.B use only the exact SEC
dot-to-hyphen notation; Citi follows an official older submissions shard; BG
and BLK use dated successor-issuer intervals from official 8-Ks; and BK uses its
historical 10-K plus the issuer's later BK→BNY announcement. ANSS has an
official issuer/CIK and fundamentals, but Yahoo and Stooq returned zero prices,
so it remained visibly `unavailable` at this checkpoint.

```bash
PYTHONPATH=src .venv/bin/python -m aios.cli ingest-reference-batch \
  --issuer-ciks examples/sp500_reference_exception_batch_01_issuer_ciks.csv \
  --security-issuers examples/sp500_reference_exception_batch_01_security_issuers.csv \
  --provider-symbols examples/sp500_reference_exception_batch_01_provider_symbols.csv \
  --start 2023-08-01 --end 2025-01-01
```

Batch 05 then accepted all 25 reviewed candidates:

```bash
PYTHONPATH=src .venv/bin/python -m aios.cli build-reference-batch \
  examples/sp500_reference_batch_05_tickers.txt \
  --batch-name sp500_reference_batch_05 \
  --output-dir /tmp/aios_reference_batch_05 \
  --start 2023-08-01 --end 2025-01-01 \
  --verified-date 2026-07-17

PYTHONPATH=src .venv/bin/python -m aios.cli ingest-reference-batch \
  --issuer-ciks examples/sp500_reference_batch_05_issuer_ciks.csv \
  --security-issuers examples/sp500_reference_batch_05_security_issuers.csv \
  --provider-symbols examples/sp500_reference_batch_05_provider_symbols.csv \
  --start 2023-08-01 --end 2025-01-01
```

Batch 06 then reviewed the next 25 candidates from COO through DGX. The strict
builder accepted 23 and kept CTRA and DFS in the review CSV because later
mergers removed both retired tickers from SEC's current map:

```bash
PYTHONPATH=src .venv/bin/python -m aios.cli build-reference-batch \
  examples/sp500_reference_batch_06_tickers.txt \
  --batch-name sp500_reference_batch_06 \
  --output-dir /tmp/aios_reference_batch_06 \
  --start 2023-08-01 --end 2025-01-01 \
  --verified-date 2026-07-17

PYTHONPATH=src .venv/bin/python -m aios.cli ingest-reference-batch \
  --issuer-ciks examples/sp500_reference_batch_06_issuer_ciks.csv \
  --security-issuers examples/sp500_reference_batch_06_security_issuers.csv \
  --provider-symbols examples/sp500_reference_batch_06_provider_symbols.csv \
  --start 2023-08-01 --end 2025-01-01
```

Exception Batch 02 uses official 2023 10-Ks and later merger/delisting filings
to resolve CTRA and DFS, and adds explicit Tiingo mappings for those names plus
ANSS. Each provider response has 358 sessions and passes the same bounded QA:

```bash
PYTHONPATH=src .venv/bin/python -m aios.cli ingest-reference-batch \
  --issuer-ciks examples/sp500_reference_exception_batch_02_issuer_ciks.csv \
  --security-issuers examples/sp500_reference_exception_batch_02_security_issuers.csv \
  --provider-symbols examples/sp500_reference_exception_batch_02_provider_symbols.csv \
  --start 2023-08-01 --end 2025-01-01
```

Reviewed Batches 07–20 and Exception Batches 03–10 continued the same
fail-closed process. They include dual-class issuer deduplication,
retired-symbol Tiingo histories, same-security ticker relabels, and a repaired
set of seven transient Yahoo throttles. The live reference layer now has 528
issuer/CIK intervals, 531 owner intervals, and 534 provider intervals. Every
one of the 533 bounded membership-assignment spans has a reviewed owner.

Window Batch 01 handles the 53 constituents whose membership covers only part
of the certified window. Its builder reads exact `ticker,start,end` rows,
validates each interval independently, retains every rejection, and refuses
conflicting merges. Forty-three spans passed automatically; Exception Batch 09
certified six retired-current-map cases from primary SEC evidence plus Tiingo,
and Exception Batch 10 certified the dated CDAY→DAY relabel as one security.
PEAK and WRK remain explicit provider-unavailable terminals rather than being
filled with a successor or inferred alias.

```bash
PYTHONPATH=src .venv/bin/python -m aios.cli build-reference-window-batch \
  --windows examples/sp500_reference_window_batch_01_windows.csv \
  --issuer-ciks /tmp/window_issuer_ciks.csv \
  --security-issuers /tmp/window_security_issuers.csv \
  --provider-symbols /tmp/window_provider_symbols.csv \
  --review /tmp/window_review.csv
```

Both builders follow SEC-named older filing shards, preserve SEC's
primary-ticker order, and accept only an exact dot/hyphen share-class notation
transform. They never interpret SEC `formerNames` as ticker history. SEC
Submissions is queried by CIK and its current ticker list can disappear after a
delisting, so it corroborates known identity candidates rather than discovering
every historical ticker owner.

Check how many historical members actually have usable local inputs:

```bash
PYTHONPATH=src .venv/bin/python -m aios.cli universe-coverage \
  --universe-id sp500 --as-of 2023-09-29 \
  --missing-output data/sp500_missing_inputs_2023-09-29.txt
```

This currently reports 501 members with both prices and PIT fundamentals out of
503 on that date; the comparable 2024-09-30 result is 503/504. The exact gaps
are PEAK and WRK provider histories on the first date and AMTM fundamentals on
the second. AMTM's first public Company Facts availability is 2024-12-17, so a
2024-09-30 fact would be future leakage. Keep these exclusions visible rather
than backdating data or substituting successor securities.

Validate the initial QV regime-weight policy:

```bash
# Repair/verify explicit benchmark actions for the exact audit window.
PYTHONPATH=src .venv/bin/python -m aios.cli refresh-price-actions \
  --start 2023-08-01 --end 2025-01-01 --ticker SPY

# Build resumable security-level history for Momentum/Low Volatility.
# The first pass exits non-zero when any identity needs explicit review.
PYTHONPATH=src .venv/bin/python -m aios.cli build-factor-price-warmup

# Read data/factor_price_warmup/factor_price_warmup_review.csv first. Once each
# rejection is confirmed as a real blocked/short-history exclusion:
PYTHONPATH=src .venv/bin/python -m aios.cli build-factor-price-warmup \
  --allow-rejections
PYTHONPATH=src .venv/bin/python -m aios.cli ingest-factor-price-warmup

PYTHONPATH=src .venv/bin/python -m aios.cli backtest-qv \
  --start 2023-08-01 --end 2024-12-31 --top-n 10 \
  --factor-model qv \
  --universe-id sp500 --benchmark SPY --calendar SPY \
  --commission-bps 5 --slippage-bps 5 \
  --exclude-ticker PEAK --exclude-ticker WRK --exclude-ticker AMTM \
  --explain-ticker PEAK --explain-ticker WRK --explain-ticker AMTM \
  --output data/backtests/qv_sp500_pit_schema_v4_2023-08_2024-12.json

# Repeat the exact experiment with all four factors.
PYTHONPATH=src .venv/bin/python -m aios.cli backtest-qv \
  --start 2023-08-01 --end 2024-12-31 --top-n 10 \
  --factor-model qvml \
  --universe-id sp500 --benchmark SPY --calendar SPY \
  --commission-bps 5 --slippage-bps 5 \
  --exclude-ticker PEAK --exclude-ticker WRK --exclude-ticker AMTM \
  --explain-ticker PEAK --explain-ticker WRK --explain-ticker AMTM \
  --output data/backtests/qvml_sp500_pit_schema_v4_2023-08_2024-12.json
```

This compares the regime-aware ranking with fixed 60/40 after explicit
commission, slippage, fixed-fee, and supplied tax assumptions. Positions and
FIFO lots persist, prior holdings remain invested until the next-session close,
and only equal-weight deltas trade. SPY is a persistent zero-friction book using
the same provider-basis/dividend convention and daily sessions. Split ratios
are applied only when a provider close is not already split-normalized. Tax rates
default to zero because tax law depends on jurisdiction; pass rates only after
deciding what tax model applies. The output is research evidence, not a promise
of future returns.

The commands keep the real 503–504 execution-date members in the denominator
while marking PEAK, WRK, and pre-filing AMTM as explicit policy exclusions.
Membership must be known at decision close and effective at the scheduled
entry. SPY defines every decision, entry, exit, and benchmark date. `--output`
writes a local, gitignored JSON audit containing every member's eligibility
reason, selected scores, macro evidence, database hash, and Git state.

After Batch 02, the command was also run without the 22 `--ticker` options. It
enumerated all 525 tickers that appear anywhere in the certified membership
history, while only 32–33 names passed factor gates in each quarter. That run
returned 52.57% net for the regime policy, 54.27% for fixed 60/40, and 41.49%
for SPY. It tests full membership enumeration and honest exclusions; because
most members still lack complete inputs, it is not a full-universe investment
result and must not be used to claim alpha.

After Batch 03, the same unfiltered audit had 47–48 factor-eligible names per
quarter. Regime-aware returned 70.81% net, fixed 60/40 returned 70.85%, and SPY
returned 41.49%. The two policies were effectively tied, and 429–430 historical
members still lacked one or both inputs. The higher return is therefore a
change in the partial sample, not proof of improved strategy performance.

After Batch 04, factor eligibility became 59, 59, 61, 61, and 60 names across
the same five decision dates. Regime-aware returned 64.88% net, fixed 60/40
returned 64.43%, and SPY returned 41.49%. The lower result than Batch 03 is
another consequence of changing a still-incomplete sample: 410–411 dated
members still lack one or both audited inputs. It is an engineering checkpoint,
not evidence for or against the investment policy.

After Batch 05 plus Exception Batch 01, factor eligibility became 77, 76, 78,
78, and 77 names. Regime-aware returned 62.65% net (64.20% gross), fixed 60/40
returned 64.14% net (65.71% gross), and SPY remained 41.49%. The policies chose
different top-10 sets in three periods. This is still a changing partial sample:
385 and 384 dated members lack one or both complete inputs at the two coverage
checkpoints, and factor publication gates narrow eligibility further. Use the
run to verify PIT, denominator, cost, and benchmark mechanics—not to claim
regime alpha.

After all reviewed batches and the corrected TTM/financial-factor logic, the
2026-07-18 interval-v1 audit published 291, 293, 350, 308, and 301 eligible companies out
of 503, 503, 503, 503, and 504 members. Raw price/fundamental coverage was much
higher—501, 500, 502, 502, and 503—because a company can have source data yet
still lack enough standardized metrics for both Quality and Value. All five
strategy periods and all five SPY periods completed on the same dates.
Regime-aware QV returned 74.24% net, fixed 60/40 returned 76.55%, and stitched
interval SPY returned 41.49%. That artifact is retained only as the
forced-liquidation regression baseline.

The provider-basis schema-v3 rerun on 2026-07-20 preserved the same membership,
coverage, eligibility, regimes, and selections. All three books have 316 daily
observations from 2023-09-29 through 2024-12-31 and no stale marks. Regime-aware
returned 74.25% net (75.14% gross), fixed 60/40 returned 76.73% net (77.68%
gross), and persistent SPY returned 39.47%. Daily annualized volatility was
17.62%, 17.29%, and 12.46%; max drawdown was -9.87%, -8.25%, and -8.41%.
Delta-only strategy turnover was 5.16x and 5.36x, with $678.13 and $707.48 in
modeled costs.

Why schema v3? Older yfinance calls did not explicitly request dividends and
splits. After those fields were restored, a diagnostic briefly showed an
impossible 764.69% return because Yahoo's historical Close is already
split-normalized and the engine applied the split a second time. That run was
rejected. Every price row now declares its split basis, the engine refuses
unknown/mixed paths, and split ratios are applied only to truly unnormalized
closes.

That schema-v3 performance result is **superseded**, not certified. Yahoo
also rewrites old `Close` values onto today's split basis, while SEC shares and
earnings remain on the basis known when filed. The old Value calculation could
therefore pair a post-split-normalized historical price with pre-split shares.
The database now stores a later-split restoration factor and Value uses the
contemporaneous price basis. Fresh schema-v4 QV and QVML backtests were required
before any newer strategy return could be evaluated.

The warm-up command solves a separate issue: a one-year market factor needs
prices before the bounded index window. It does not pretend today's ticker
existed then. Accepted rows live under the immutable security ID and must match
the reviewed provider series at the boundary. `Adj Close` is not an identity
gate because future dividends can legitimately rewrite it; raw close,
dividends, splits, and split basis are checked instead. The corrected v3 review
accepted 520 of 528 identities and atomically imported 122,466 rows. AMTM,
GEHC, GEV, KVUE, SOLV, and VLTO genuinely lacked 210 pre-anchor sessions; SW
and the Healthpeak/DOC predecessor were blocked by reviewed identity history.
Yahoo's daily split scan-through date is provenance metadata, not an economic
equality field, while every cache reuse rechecks the hashed economic overlap.

Both matched schema-v4 runs then completed all five periods and 316 daily
observations from one database snapshot:

| Policy | Net cumulative | Gross cumulative | Annualized | Max drawdown | Costs | Turnover |
|---|---:|---:|---:|---:|---:|---:|
| Regime-aware QV | 51.12% | 51.92% | 38.90% | -8.35% | $625.80 | 5.33x |
| Fixed 60/40 QV | 50.98% | 51.72% | 38.79% | -8.01% | $573.47 | 4.97x |
| Regime-aware QVML | 24.97% | 25.80% | 19.41% | -11.08% | $737.24 | 6.68x |
| Fixed QVML | 34.96% | 35.69% | 26.94% | -7.95% | $615.88 | 5.52x |
| Persistent SPY | 39.47% | 39.47% | 30.31% | -8.41% | $0.00 | 0.00x |

QVML market-factor coverage was 499, 498, 498, 496, and 497 names; strict
four-factor eligibility was 291, 293, 347, 307, and 300. QVML did not improve
the result in this short window. That is useful negative engineering evidence,
not permission to tune its weights to these five in-sample decisions.

Those returns are **not a trading claim**. The window is short and in-sample,
tax rates were zero, and jurisdiction-specific tax behavior is not certified.
The result proves that the data, eligibility, exclusion, ranking, calendar,
stateful execution, daily risk, and benchmark contracts work together. It does
not prove that the regime tilt beats 60/40.

For a deliberately survivorship-biased smoke test when membership data is not
loaded, use the explicit escape hatch and label the result accordingly:

```bash
PYTHONPATH=src .venv/bin/python -m aios.cli backtest-qv \
  --start 2015-01-01 --end 2026-06-30 --top-n 10 \
  --allow-current-universe
```

If this database was migrated from the old macro schema, run this once after a
successful full release-aware refresh:

```bash
PYTHONPATH=src .venv/bin/python -m aios.cli cleanup-legacy-macro
```

This command refuses to delete anything unless every legacy observation has a
release-aware replacement. The original migrated table remains as an audit
copy, and a durable migration marker prevents the active copies from returning
when the database is reopened.

If `aios validate` reports `fundamentals_period_end_after_as_of_date`, preserve
the impossible source rows in quarantine before continuing:

```bash
PYTHONPATH=src .venv/bin/python -m aios.cli quarantine-invalid-fundamentals
```

This does not erase the evidence. It moves every invalid row intact to
`fundamentals_quarantine`; new EDGAR extraction and inserts reject the same
impossible chronology automatically.

Open the dashboard:

```bash
.venv/bin/aios dashboard
```

For ordinary use, you do not need to open DuckDB or run Streamlit yourself.
These are the five main checks:

```bash
.venv/bin/aios doctor
.venv/bin/aios validate
.venv/bin/aios readiness --report-only
.venv/bin/aios health --report-only
.venv/bin/aios preflight
```

The first four answer whether the installation and research evidence are
healthy. `preflight` combines the usable scopes into one read-only operator
answer and prints exactly one safest next action. It keeps supervised research,
proposal creation, stress review, paper recording, unattended operations, and
real-capital execution separate, so one green scope cannot hide another blocked
scope.

Create a timestamped backup of the database and local paper state:

```bash
.venv/bin/aios backup
```

The command checkpoints the database, writes checksums for every copied file,
and excludes `.env`, logs, caches, and backtest artifacts. It tells you the
new folder path. Verify that folder later with:

```bash
.venv/bin/aios verify-backup backups/aios-YYYYMMDDTHHMMSSZ
```

You can safely prove that a backup is actually recoverable without touching
your live system:

```bash
.venv/bin/aios restore-drill backups/aios-YYYYMMDDTHHMMSSZ
```

The drill restores into a temporary project, opens and validates the recovered
database, verifies every raw file, replays every supported provider artifact,
and deletes the temporary copy. It does not replace the live database, paper
account, incident history, or raw archive.

The dashboard may remain open for normal backups because it now releases its
short read-only database connection after each cached load. Close it before a
restore because restore replaces the database file. Restore first verifies
every checksum, automatically backs up the current database and paper state,
and then replaces the snapshot only after an explicit confirmation:

```bash
.venv/bin/aios restore backups/aios-YYYYMMDDTHHMMSSZ --confirm-restore
```

If restored data has any hard validation failure, the command returns an error
and prints the automatic pre-restore backup path. Never replace DuckDB files by
hand.

Refresh current U.S. data with:

```bash
.venv/bin/aios refresh-us-daily
.venv/bin/aios health
```

The dashboard can remain open. Its reads are short-lived, and an interactive
command waits briefly if one happens to overlap. Daily, filing, backup, and
scheduled health jobs first share one 30-minute queue, so two startup catch-up
writers cannot race; DuckDB then waits up to five minutes for a brief dashboard
read. The daily command first updates SPY, then checks the official
announcement archive and independent current component list, then extends an
unchanged dated universe, updates all reviewed member prices and macro releases,
and finally proves that every broad readiness gate reaches the same U.S. close.
If any stage fails, the newer decision date stays blocked. Weekly SEC filing
refreshes remain separate because they are much slower and do not need to run
every day.

The lower-level `review-universe-current` command handles the separate
membership clock. It saves
exact copies of the public S&P Global press archive and a free independent
component list, compares every symbol and reviewed CIK lineage, and looks for
an unreviewed constituent-change announcement. If there is no change, it moves
membership, security, issuer/CIK, and provider-symbol dates together in one
database transaction. If anything disagrees, it moves nothing and tells you
that a human event review is needed.

This separation prevents a confusing but important mistake: a newly downloaded
price is allowed to update the value of an existing simulated portfolio, but it
cannot become the date of a new stock-selection decision until the investable
company list is certified for that date too.

On Linux, you can enable the same work automatically. One complete daily
workflow runs at 02:00 New York time after every U.S. weekday. In India that is
Tuesday through Saturday—about 11:30 during U.S. summer time and 12:30 during
U.S. winter time—so Friday's U.S. close is not missed. Filings run Saturday at
09:00 local time and a verified backup runs Sunday at 09:00 local time:

```bash
.venv/bin/aios scheduler-install --confirm-install --keep-running-after-logout
.venv/bin/aios scheduler-status

# Use these when needed
.venv/bin/aios scheduler-pause
.venv/bin/aios scheduler-resume
.venv/bin/aios scheduler-remove --confirm-remove

# Test and inspect local failure history
.venv/bin/aios alert-test
.venv/bin/aios alerts --unresolved

# Inspect and test durable alert messages
.venv/bin/aios notifications
.venv/bin/aios notification-test

# Email is optional and remains off during scheduler installation
.venv/bin/aios email-status
```

Installation is deliberately explicit and idempotent: rerunning
the full `scheduler-install` command updates only AIOS-managed timer files.
`--keep-running-after-logout` uses Linux's free built-in user-service setting:
updates continue after the desktop logs out while the computer remains on. If
the computer was off, or a process disappeared unexpectedly, a second
idempotent check runs three minutes after the user scheduler starts. A durable
job record lets the dashboard distinguish running, completed, failed, and
interrupted workflows.
The dashboard releases DuckDB between cached reads. If a short read overlaps a
scheduled write, the scheduled service waits instead of failing immediately.
The shared queue also covers weekly filings and backups. A genuine conflict
lasting beyond the bounded queue still fails visibly; the status command shows
whether the last service passed and when it runs next.

The status check will not wait forever if Linux's desktop scheduler interface
is temporarily unavailable. After five seconds it shows which managed timer
files are installed/enabled and labels the live state **not verified**. Run the
same command again from your normal logged-in terminal; do not interpret
“not verified” as either a passed or failed scheduled job.

Every scheduled service has a local failure recorder. If a refresh, strict
health check, or backup exits unsuccessfully, AIOS records a structured incident
outside DuckDB. A later successful run resolves the incident without deleting
its history. To inspect one entry or mark it reviewed:

```bash
.venv/bin/aios alerts --unresolved
.venv/bin/aios alert-show INCIDENT_REF
.venv/bin/aios alert-ack INCIDENT_REF
# Use only after the underlying problem is genuinely fixed:
.venv/bin/aios alert-resolve INCIDENT_REF
```

`alert-test` opens and resolves a harmless incident. It never creates an alert
message. `notification-test` separately proves the complete local message
lifecycle without using the internet. It creates one harmless audit row marked
**Sent**, but that means sent to the built-in local test receiver—not email,
Slack, a phone, or a broker.

The notification screen uses five plain-language states:

- **Held locally:** safely saved while external delivery is switched off.
- **Waiting to retry:** eligible for a configured sender after a temporary
  failure.
- **Sending now:** leased briefly to one sender so two workers cannot claim it.
- **Sent:** the configured receiver confirmed completion.
- **Needs review:** five pre-send temporary attempts were exhausted, the
  failure is permanent, or the provider may have accepted a message before the
  connection broke. Uncertain outcomes are not retried automatically because
  that could send a duplicate.

Old incidents are intentionally not turned into new messages during an upgrade,
so enabling email cannot suddenly send historical failures. Email also has its
own optional timer: the three data/backup timers do not activate it.

To use email, first add the `SMTP_*` / `ALERT_EMAIL_*` settings shown in
`.env.example` to your private `.env`. Use an app-specific password or SMTP
token, never a normal mailbox password, and never paste the password into chat
or commit it. Then follow this order:

```bash
.venv/bin/aios email-status
.venv/bin/aios email-test --confirm-send
# Check that the test actually arrived in the intended mailbox.
.venv/bin/aios email-enable --confirm-enable

# At any time:
.venv/bin/aios email-disable --confirm-disable
```

The test sends exactly one message and does not enable future alerts. Enabling
requires a successful test for the same host, account, sender, and recipient.
Changing those fields later makes delivery fail closed until the new route is
tested and explicitly enabled. Existing held messages remain local forever;
only incident changes created under the new activation can be sent.

If today's index membership has not been certified yet, the refresh may collect
data for the newest reviewed member list up to seven days old. The dashboard
continues to show the newest safe decision date instead of producing five
misleading follow-on warnings from one missing membership date. This keeps data
moving but does **not** claim that the older list is today's index or permit a
new portfolio decision for today.

“Today” means the market's date. India is ahead of New York: the U.S. session
dated July 24 normally closes after midnight on July 25 IST. Before that close,
July 23 is the newest possible complete U.S. daily bar. The dashboard compares
stored prices with the latest completed U.S. session and says either **Up to
date** or **awaiting the next automatic refresh** instead of calling safe
previous-session data stale. The price adapters enforce the same New York-close
boundary, so even an early manual run cannot save a still-moving daily bar.

A newly reviewed company can exist before its first SEC Company Facts file. It
stays visible as “company filings pending” and is tried again each week. If a
company that previously had valid filings suddenly returns nothing, the refresh
still stops and asks for investigation.

The dashboard opens on **Overview**, anchored to the latest broadly covered
reviewed market date rather than blindly using the computer date. The
Investment Command Center answers whether **Research** is usable, where the
**Paper Trial** stands, and whether **Operations** needs attention. Ready
research cannot hide a failed scheduled workflow or critical incident. One
**Priority Action** identifies the highest-priority safe next screen. Research
readiness, paper progress, proposal targets, and current incidents stay visible;
only raw commands, hashes, complete gate history, and other technical evidence
use disclosure controls.

**Research** keeps the date, scoring model, and company search visible in the
page toolbar. It opens on the ranked list and uses one visible switch for the
ranked list, opportunity map, or missing-data coverage; only the selected
surface renders. The page, date, model, research surface, selected company, and
search are stored in the browser URL, so the exact screen can be shared when
reporting a problem. **Company Detail** is opened from a Research row instead
of occupying global navigation. It starts with four headline measures, then
keeps business, valuation, market context, and missing evidence visible.
**Operations & System Health** puts active incidents first, then automation,
the data pipeline, safeguards, and alert delivery. **Methodology & Sources**
holds the plain-language model explanation, evidence-source boundaries, and
technical audit detail.

The dashboard uses a warm ivory canvas, white evidence surfaces, a light
navigation rail, near-black primary actions, and a restrained clay identity
accent. Green, amber, and red are reserved for ready, review, and blocked
states. Normal text is at least 14px, body copy is 16–17px, and controls are at
least 44px high. Paper Trial always shows Proposal, Forward Trial, Timing
Review, and Local Record as separate stages. Desktop grids stay aligned, while
phone-width views collapse the sidebar and stack without horizontal page
scrolling.

The first Research score calculation now reads the 503-company evidence in
identity-safe batches. On the certified July 27 snapshot, a fresh baseline QV
calculation took about 2.9 seconds instead of 25.5 seconds; the experimental
four-factor calculation took about 3.4 seconds instead of 28.5 seconds. Both
matched the older scalar calculation exactly. The batch facade and decision
cache are discarded after the calculation, so a later data refresh cannot
leave an old filing or price window hidden in memory. If a batch read is
invalid or unavailable, AIOS falls back to the slower fail-closed scalar path.

### Is it ready to use?

It is ready **today for supervised U.S. research, governed proposal stress
review, and the local paper workflow**. The active forward-policy baseline is
unchanged and its one saved proposal is separate from holdings. You can stress
that proposal now without changing the account: open **Paper Trial** to see the
Proposal Downside Review under the targets, or run the CLI command below. The
separate `paper-review` command decides whether its simulation timing window is
currently valid; if the window expired, create a new prospective proposal
instead of inventing an old fill.
You can explore rankings, inspect factor evidence, create a reviewed model
portfolio proposal, and monitor simulated holdings. It cannot place a broker
order and it cannot decide what you personally should buy or sell.

There is now a separate current-use check:

```bash
.venv/bin/aios readiness --report-only
```

With no `--as-of`, this chooses the newest reviewed U.S. decision-date
candidate from the database instead of treating today's computer date as a
market close. For one consolidated operator decision, run:

```bash
# Fast timing and capability view
.venv/bin/aios preflight

# Full governed paper review; still does not record the simulation
.venv/bin/aios preflight --review-paper

# Machine gate: non-zero exit unless both scopes are usable
.venv/bin/aios preflight --json --require research --require stress_review
```

At the 2026-07-29 live read-only checkpoint the latest reviewed decision close
is 2026-07-28. The current snapshot has 503 S&P 500 members, stable identities for
all 503, PIT filings for 500, action-safe prices for all 503, and SPY through
the same close. The latest required macro release is dated 2026-07-28.
Validation has zero hard failures and three visible warnings, and readiness is
`READY`.

The risk gate checks position and broad business-group concentration, leverage,
turnover, trading liquidity, and account drawdown. It is connected to a local,
checksum-protected paper account, but its defaults are engineering safeguards,
not your final personal risk limits.

The normal paper-simulation sequence is:

```bash
# Already created in this checkout; use only for a brand-new account
.venv/bin/aios paper-init

# Create a reviewable plan; this does not buy anything
.venv/bin/aios paper-propose
.venv/bin/aios paper-status

# Review "what if" losses and constraints without changing or storing anything
.venv/bin/aios stress-review \
  --proposal data/paper/proposals/us-qv-2026-07-27.json

# After the scheduled close and provider finalization, check without changing money
.venv/bin/aios paper-review \
  --proposal data/paper/proposals/us-qv-2026-07-27.json

# Only when paper-review says it is ready
.venv/bin/aios paper-execute \
  --proposal data/paper/proposals/us-qv-YYYY-MM-DD.json \
  --confirm-simulated

# Add later reviewed daily values without changing the portfolio
.venv/bin/aios paper-mark
```

A proposal must be created before its scheduled U.S. session opens, so an
operator cannot watch that day's market move before choosing whether to count
it. It can be recorded only after that session closes and before the next U.S.
session opens. If the window expires, the system refuses a made-up historical
fill and asks for a new prospective proposal. `paper-review` is safe to run
repeatedly: it does not change the account, send a broker order, or count a
performance observation.

`stress-review` is also safe to repeat. It first proves that the proposal belongs
to the unchanged forward trial, then reads the evidence database without writing
to it. The CLI and Paper Trial panel use the same governed service and recheck
the trial, account, proposal, and source identities before showing a result.
Each target is tied to its exact point-in-time security identity, action-safe
price history, row-level liquidity window, and release-aware revenue fact. If
one dependency is missing or changed, AIOS withholds the affected number and
labels the review partial or withheld instead of guessing.

Fixed mark-shock results are kept separate from the statistical
volatility/correlation loss proxy. The latter does **not** pretend to know future
holdings, drawdown, concentration, or liquidation. One fixed scenario uses the
[approximately 58% equity-price decline in the Federal Reserve's hypothetical
2026 severely adverse scenario](https://www.federalreserve.gov/publications/2026-stress-test-scenarios.htm)
as a transparent calibration; it is not a forecast. All displayed limits are
generic advisory references, not permission to trade, and the review cannot
approve, reject, or execute the proposal. Add `--json` for machine-readable
output. Add `--output PATH` only when you deliberately want one immutable report
file; no report file is created by default.

The untouched forward test has a separate policy lock:

```bash
# One deliberate start; this never freezes the changing market database
.venv/bin/aios forward-freeze --confirm-freeze

# Safe to run at any time
.venv/bin/aios forward-status

# Only after status reports real policy drift: create a current proposal,
# archive the old trial unchanged, and atomically activate its replacement.
.venv/bin/aios forward-restart --confirm-restart
```

The lock remembers checksums for the stock-ranking rules, macro regime, risk
limits, modeled costs/taxes, market calendar, readiness checks, and paper
workflow. It also records every later proposal. If one of those rules changes,
the dashboard says "drift detected" and simulated execution stops until the
change is reviewed and a new trial is deliberately started. New prices,
filings, macro releases, and reviewed membership are expected to keep updating
and therefore are not frozen.

The account is still $100,000 of simulated cash with zero holdings, zero
executions, and no broker connection. Predecessor
`us-qv-forward-8559d86b6a02` is archived unchanged. Active trial
`us-qv-forward-72c4560a442d` began prospectively from the 2026-07-27 close and
has one approved simulation-only proposal for the 2026-07-28 session. Do not
restart this unchanged trial or treat the proposal as an investment.

The next naturally triggered guarded daily run remains operational evidence to
observe. Installed units, earlier controlled runs, and historical scheduler
proof do not by themselves prove that current runtime event.

The historical U.S. technical gate is complete. The current refresh,
evidence-backed no-change membership review, scheduler, backup/recovery, local
dashboard smoke, and untouched-policy gate are complete in code, and the full
six-period rerun now passes. The recoverable daily workflow and prospective
replacement freeze are live-proven. A July 25 startup collision exposed and
then live-proved the permanent serialized scheduler fix; continued untouched
daily, filing, backup, and forward-trial observation remains necessary.
A real-capital pilot is a different gate and needs at least 8–12 weeks of
untouched forward observation plus broker/data reconciliation, alerts, and your
tax/risk assumptions. That elapsed evidence period cannot be sped up.

Use the dashboard as a research notebook, not as an autonomous adviser. A high
rank or attractive backtest is a question to investigate, not an instruction
to buy.

The dashboard uses everyday labels and keeps technical methodology in an
optional section. It also states clearly that current coverage is the U.S.
reference dataset and that no score is a buy, hold, or sell rating.

Run tests:

```bash
PYTHONPATH=src .venv/bin/pytest -q
```

The current regression baseline is 494 passing tests; Ruff, bytecode
compilation, and the whitespace diff check are clean.

`audit` shows whether a recent ingest succeeded, how many rows it wrote, and
the error text when it failed. Older data was loaded before this audit trail
was added, so it is normal for the first audit view to be empty.

`validate`, `readiness`, `health --report-only`, and `preflight` are read-only.
The default strict `health` command may record or resolve local operating
incidents in the separate SQLite ledger. Use `preflight` for one use-specific
snapshot of research, proposal creation, stress review, paper recording,
operations, and real-capital boundaries plus exactly one safe next action. If a
readiness gate is blocked, health calls the examined date a candidate rather
than incorrectly calling it certified. `validate` reports hard failures, such
as missing PIT dates, closing prices, or unversioned macro rows, separately from
warnings such as historical ingest failures that need inspection. A macro
vintage failure is a deliberate stop: do not backtest until it is repaired.
The `alerts`, `alert-show`, `notifications`, `notification-show`, and
`email-status` inspection commands also preserve the existing operations
ledger byte-for-byte. They refuse an uncheckpointed or outdated ledger rather
than silently initializing or migrating it.

Every supported CLI command that changes backup-covered DuckDB, raw-snapshot,
paper, proposal, or forward-trial state acquires one non-blocking project
maintenance lease. That includes refresh, ingest, import, repair, and cleanup
commands as well as backup, restore, paper, and forward workflows. If another
supported mutation is running, the second command stops visibly instead of
racing it. Generated reports and review files cannot be aimed at governed
state; ordinary single-file outputs are atomic and write-once, while mutable
factor warm-up workspaces reject symlink and hard-link aliases. The lease and
path policy are cooperative: direct Python imports and unrelated external
processes do not participate. The active forward trial is therefore
left unchanged, and deeper final compare-and-set hardening belongs in the next
intentional paper/forward policy version rather than a mid-trial retrofit.

## Data-source roles

- SEC EDGAR: primary fundamentals and filing dates.
- yfinance: primary daily prices. New captures use the replayable v2 normalized
  format. A malformed completed-session candle is kept as raw evidence but is
  rejected before storage; the database layer also prevents an invalid close
  from replacing a valid one. Older v1 captures remain replayable.
- Tiingo: optional explicit EOD provider when `TIINGO_API_KEY` is configured;
  the token is sent in a header, never in the URL.
- Stooq: optional price fallback. It currently returns a JavaScript verification
  page in this environment. During an ingest, the capture-before-parse path
  retains that failed evidence and never accepts it as prices.
- FRED: macro series when `FRED_API_KEY` is configured.
- US Treasury CSV: live no-key DGS2/DGS10/DGS30 fallback and cross-check using
  the official current-year download. A difference from FRED above five basis
  points is shown as a quality warning.

All HTTP calls share rate limiting and retry behavior in
`src/aios/ingest/http_client.py`. Do not add direct unthrottled downloads.
Repeated price ingestion re-fetches a five-day overlap to catch recent
corrections; a first-time ticker download still requests its full history.

## What is unfinished

The foundation, QV/QVML factors, release-aware macro layer, reviewed membership
and identities, action-safe current prices, costs, persistent holdings, FIFO
tax lots, daily account values, benchmark comparison, risk gates, paper monitor,
and current U.S. readiness path are working through the 2026-07-28 close.
Proposal Stress Review v1 is implemented for pending proposal targets. A
separate stress workflow for current simulated holdings, multifactor or Monte
Carlo risk models, and owner-authored scenario policies remains future work.

The 2025-to-current stateful historical rerun is complete: six of six periods,
327 date-aligned strategy and SPY observations, exact quarter-to-quarter state
continuity, and no stale strategy values. It applies the reviewed HES-to-CVX
share conversion and sells MTCH/PAYC on 2026-04-01 using liquidation-only price
extensions that do not make them index members again. Regime-aware QV returned
31.13% net, fixed QV 27.73%, and SPY 34.12%; taxes were set to zero. These are
software checks over a short historical period, not forecasts or proof that the
strategy will beat the market.

What remains is elapsed operational evidence: review the existing proposal
after the July 28 close/provider finalization and before the next U.S. open,
then observe the active trial and naturally triggered
daily/health/filing/backup cycles without retuning them. Older
announcement provenance before August 2023 remains a separate long-history
expansion.

Jurisdiction, account type, broker, tax rules, and final risk limits still
require your decision before after-tax or controlled-capital certification.
India work starts after these U.S. technical gates.
The deterministic engine needs no language model; a weaker model can handle
optional summaries, but no model output is accepted as a numeric or provenance
source.

The ordered roadmap is in [`FUTURE_BUILD_PLAN.md`](./FUTURE_BUILD_PLAN.md). The
local incident ledger and systemd failure capture are implemented. Immutable
downloads are now live for reviewed SEC Company Facts and Submissions: the
replay-aware AAPL run retained two exact payloads, linked both to its ingest,
and reproduced 4,102 Company Facts rows plus one Submissions metadata row.
yfinance now has an honest normalized-export path:
it stores the library-returned rows, links them to the ingest, hashes the parsed
prices, and replays them during verification. FRED has the equivalent
normalized-vintage path. After the July 24 full-universe recovery and July 25
provider proof, verification passes for 2,612 unique payloads and 2,111 parsed
replays. The latest verified backup is
`backups/aios-20260728T082412Z`: 2,622 files, 380,408,599 bytes, manifest
SHA-256
`cd4ce6e3c8256013483eee438b3167cf8ef12815b7ffbb04e03b3e4ea629d25b`.
It passed a disposable restore drill through the real recovery path. Stooq remains
explicitly unavailable instead of silently falling back through an HTML page;
future market adapters must add the same evidence contract. The retry-safe
notification outbox, no-network local proof, and selected SMTP adapter are
implemented; private SMTP configuration plus one owner-confirmed mailbox receipt
are the remaining transport activation steps.
Proposal Stress Review v1 is implemented. Anomaly review cases, experiment
registration, and the India schema foundation follow; the broader holdings and
advanced-risk extensions remain governed future work.

The India sequence is in [`INDIA_BUILD_PLAN.md`](./INDIA_BUILD_PLAN.md). It
starts with a bounded Nifty 50 research beta and explains the official NSE,
NSE Indices, SEBI, RBI, and MoSPI evidence gates before any Indian ranking is
allowed.

For the compact technical handoff, read [`agent.md`](./agent.md).
