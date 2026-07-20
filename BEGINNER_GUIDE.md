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

Historical universe membership has three dates/ideas:

- `effective_start`: when a ticker begins belonging to the investable universe.
- `effective_end`: the first date when it no longer belongs; blank means open-ended.
- `known_date`: when that membership change was publicly knowable.

The backtester uses membership only when both the effective interval and
`known_date` pass the decision date. This prevents a later index constituent
list from leaking into an earlier simulation.

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

These are starting hypotheses that still need a point-in-time backtest. If
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
data is wanted. The no-key Treasury adapter is a fallback, but its automated
CSV endpoint currently returns HTTP 403 in this environment, so FRED is the
reliable active source for yield history. `TIINGO_API_KEY` is optional; leave it
empty unless you have your own Tiingo token and explicitly review that provider.

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
certified window. The generated membership CSV must
include `ticker`, `effective_start`, and `known_date`; it may also include
`universe_id`, `effective_end`, and `source`:

```text
universe_id,ticker,effective_start,effective_end,known_date,source
sp500,AAPL,2015-01-01,2024-06-24,2014-12-12,provider-export
sp500,AAPL,2024-06-24,,2024-06-14,provider-export
```

The dates above are only a format example. Import the generated file with:

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
# SPY is a benchmark, so ingest its prices once if it is not already present.
PYTHONPATH=src .venv/bin/python -m aios.cli ingest-ticker SPY --no-fundamentals

PYTHONPATH=src .venv/bin/python -m aios.cli backtest-qv \
  --start 2023-08-01 --end 2024-12-31 --top-n 10 \
  --universe-id sp500 --benchmark SPY --calendar SPY \
  --commission-bps 5 --slippage-bps 5 \
  --exclude-ticker PEAK --exclude-ticker WRK --exclude-ticker AMTM \
  --explain-ticker PEAK --explain-ticker WRK --explain-ticker AMTM \
  --output data/backtests/qv_sp500_pit_2023-08_2024-12.json
```

This compares the regime-aware ranking with fixed 60/40 after explicit
commission, slippage, fixed-fee, and supplied tax assumptions. Positions and
FIFO lots persist, prior holdings remain invested until the next-session close,
and only equal-weight deltas trade. SPY is a persistent zero-friction book using
the same raw-close/split/dividend convention and daily sessions. Tax rates
default to zero because tax law depends on jurisdiction; pass rates only after
deciding what tax model applies. The output is research evidence, not a promise
of future returns.

The final command keeps the real 503–504 members in the denominator while
marking PEAK, WRK, and pre-filing AMTM as explicit policy exclusions. It uses
SPY to define every decision, entry, exit, and benchmark date. `--output`
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

The schema-v2 stateful rerun on 2026-07-20 preserved the same membership,
coverage, eligibility, regimes, and selections. All three books have 316 daily
observations from 2023-09-29 through 2024-12-31 and no stale marks. Regime-aware
returned 70.25% net (71.13% gross), fixed 60/40 returned 72.58% net (73.51%
gross), and persistent SPY returned 37.16%. Daily annualized volatility was
17.66%, 17.31%, and 12.49%; max drawdown was -9.94%, -8.32%, and -8.41%.
Delta-only strategy turnover was 5.17x and 5.37x, roughly half the forced-round-
trip baseline, with $671.32 and $700.79 in modeled costs.

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
.venv/bin/streamlit run src/aios/dashboard.py
```

The first ranking load currently takes roughly 12–20 seconds on this checkout.
That is normal: the app is rebuilding PIT Quality and Value evidence for about
500 companies. The cache exists only for that calculation and is then thrown
away, so a later data refresh cannot leave an old filing hidden in memory.

### Is it ready to use?

It is ready **today as a supervised research beta**. You can explore rankings,
inspect factor evidence, and run the bounded historical experiment. It is not
ready to place trades automatically or justify real-money decisions by itself.

If development continues steadily from 2026-07-20, the practical targets are:

- 2–4 focused weeks for a paper-trading beta with Momentum/Low Volatility,
  risk rules, refresh monitoring, a backtest/report screen, and a holdout test;
- at least 8–12 focused weeks before considering a small controlled capital
  pilot, and only after longer PIT history, walk-forward evidence, broker/data
  reconciliation, alerts, and your tax/risk assumptions are certified; and
- 12–20+ focused weeks for broader institutional-style completeness. Historical
  delisted data may still require a licensed source, so this is not a guaranteed
  free-data completion date.

Use the dashboard as a research notebook, not as an autonomous adviser. A high
rank or attractive backtest is a question to investigate, not an instruction
to buy.

Run tests:

```bash
PYTHONPATH=src .venv/bin/pytest -q
```

`audit` shows whether a recent ingest succeeded, how many rows it wrote, and
the error text when it failed. Older data was loaded before this audit trail
was added, so it is normal for the first audit view to be empty.

`validate` is a read-only pre-flight check. It reports hard failures, such as
missing PIT dates, closing prices, or unversioned macro rows, separately from
warnings such as historical ingest failures that need inspection. A macro
vintage failure is a deliberate stop: do not backtest until it is repaired.

## Data-source roles

- SEC EDGAR: primary fundamentals and filing dates.
- yfinance: primary daily prices.
- Tiingo: optional explicit EOD provider when `TIINGO_API_KEY` is configured;
  the token is sent in a header, never in the URL.
- Stooq: price fallback when yfinance returns no data.
- FRED: macro series when `FRED_API_KEY` is configured.
- US Treasury CSV: intended no-key yield-curve fallback/cross-check; currently
  blocked by HTTP 403 for automated requests in this environment.

All HTTP calls share rate limiting and retry behavior in
`src/aios/ingest/http_client.py`. Do not add direct unthrottled downloads.
Repeated price ingestion re-fetches a five-day overlap to catch recent
corrections; a first-time ticker download still requests its full history.

## What is unfinished

The foundation, first QV factor layer, release-aware macro regime layer,
initial regime-aware weights, bounded historical membership, stable security
IDs, friction-aware PIT policy harness, and certified-window identity program
are working. The identity program is complete through stable Batch 20, Window
Batch 01, and Exception Batch 10. All 533 assignment spans have reviewed
owners; 51/53 shorter spans have safe price histories, while PEAK and WRK are
documented terminal provider gaps.

The near-complete factor-eligibility audit and stateful execution layer are now
complete. The factor query optimization is also complete: the 503-member test
fell from 42,235 database queries and 174.1 seconds to 3,874 queries and 17.4
seconds. A complete five-decision rerun finished in about 92 seconds and matched
the certified result exactly. The next step is adding Momentum and Low
Volatility, a paper-portfolio/risk layer, monitored refreshes, and an untouched
walk-forward test. Exact announcement/identity provenance must also be extended
before August 2023, and jurisdiction-specific tax assumptions still require a
user decision. LLM report synthesis comes later because it depends on these
deterministic outputs being trustworthy first.

For the compact technical handoff, read [`agent.md`](./agent.md).
