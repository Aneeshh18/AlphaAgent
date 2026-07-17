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
reliable active source for yield history.

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
- a **provider symbol interval** says which Yahoo/Stooq symbol is safe for that
  security on each date.

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

Check how many historical members actually have usable local inputs:

```bash
PYTHONPATH=src .venv/bin/python -m aios.cli universe-coverage \
  --universe-id sp500 --as-of 2023-09-29 \
  --missing-output data/sp500_missing_inputs_2023-09-29.txt
```

This currently reports 24 members with both prices and PIT fundamentals out of
503 on that date; the comparable 2024-09-30 result is 26/504. The missing list
is for review, not blind bulk ingestion: renamed, delisted, merged, and
punctuation-heavy tickers need verified SEC CIK and data-provider symbol
mappings first.

Validate the initial QV regime-weight policy:

```bash
# SPY is a benchmark, so ingest its prices once if it is not already present.
PYTHONPATH=src .venv/bin/python -m aios.cli ingest-ticker SPY --no-fundamentals

PYTHONPATH=src .venv/bin/python -m aios.cli backtest-qv \
  --start 2023-08-01 --end 2024-12-31 --top-n 10 \
  --universe-id sp500 --benchmark SPY \
  --commission-bps 5 --slippage-bps 5 \
  --ticker AAPL --ticker MSFT --ticker GOOGL --ticker META --ticker NVDA \
  --ticker AMZN --ticker TSLA --ticker COST --ticker WMT --ticker MCD \
  --ticker JPM --ticker BAC --ticker V --ticker MA --ticker JNJ \
  --ticker UNH --ticker PFE --ticker BA --ticker CAT --ticker XOM \
  --ticker T --ticker VZ
```

This compares the regime-aware ranking with fixed 60/40 after explicit
commission, slippage, fixed-fee, and supplied tax assumptions. It also reports
an explicit benchmark using adjusted-close total returns. Tax rates default to
zero in the CLI because tax law depends on jurisdiction; pass rates only after
deciding what tax model applies. The output is research evidence, not a promise
of future returns.

The command retains the original 22-ticker regression slice deliberately even
though audited input coverage has since increased. The membership table still
provides the real 500-name boundary. The verified five-quarter run returned
44.99% net for both policies and 41.49% for SPY. Both policies chose the same
equal-weight holdings, so this is a software/data-pipeline test, not proof that
the regime tilts work. See
`SP500_DATA_PROVENANCE.md` for exact assumptions and limitations.

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

Open the dashboard:

```bash
.venv/bin/streamlit run src/aios/dashboard.py
```

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
IDs, first issuer/CIK/provider identity batch, and the friction-aware PIT
policy-validation harness are working. Next, extend official announcement
history before August 2023 and expand identities, fundamentals, and
survivorship-safe prices in reviewed batches until every required historical
member is covered. Momentum/low-volatility factors, LLM report synthesis, and
portfolio tooling come later because they depend on trustworthy earlier data.

For the compact technical handoff, read [`agent.md`](./agent.md).
