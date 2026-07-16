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
a number describes and when the number became public.

## The most important concept: “as of”

Suppose a company reports results for December 2024, but files the report in
February 2025. A February 2024 backtest must not see that result. The code stores
both dates:

- `period_end`: the accounting period being described.
- `as_of_date`: the filing date, when the market could first know it.

Every historical calculation must use `as_of_date <= decision_date`. This is
called point-in-time correctness and prevents look-ahead bias.

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
not use net income as an EBITDA substitute. The checked-in database was created
before that correction, so clean EBITDA multiples require re-ingestion.

### Composite

The current composite is:

```text
QV score = 60% Quality + 40% Value
```

If one side is missing, available weights are normalized. Each result also has a
letter grade and a list of missing inputs; a grade is not a guarantee or a buy
signal.

## First-time setup

From the project root:

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev,dashboard]"
cp .env.example .env
```

Open `.env` and set a real SEC User-Agent email. Add a FRED API key if macro
data from FRED is wanted; Treasury data can work without that key.

Check the installation:

```bash
PYTHONPATH=src .venv/bin/python -m aios.cli doctor
PYTHONPATH=src .venv/bin/python -m aios.cli status
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

Open the dashboard:

```bash
.venv/bin/streamlit run src/aios/dashboard.py
```

Run tests:

```bash
PYTHONPATH=src .venv/bin/pytest -q
```

## Data-source roles

- SEC EDGAR: primary fundamentals and filing dates.
- yfinance: primary daily prices.
- Stooq: price fallback when yfinance returns no data.
- FRED: macro series when `FRED_API_KEY` is configured.
- US Treasury CSV: no-key yield-curve fallback/cross-check.

All HTTP calls share rate limiting and retry behavior in
`src/aios/ingest/http_client.py`. Do not add direct unthrottled downloads.

## What is unfinished

The foundation and first QV factor layer are working. The planned next pieces
are clean re-ingestion with D&A, a macro regime overlay, momentum and low-vol
factors, a transaction-cost-aware backtester, LLM report synthesis, and finally
portfolio tooling. These should be added in that order because later layers
depend on trustworthy earlier data.

For the compact technical handoff, read [`agent.md`](./agent.md).

