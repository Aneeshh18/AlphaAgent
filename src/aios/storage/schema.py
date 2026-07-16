"""Point-in-time storage schema for DuckDB.

THE CORE INVARIANT
------------------
Every fundamentals row carries an `as_of_date` = the date the data became
*knowable to the market* (the filing date, for SEC data). When we compute
factors or run a backtest "as of date X", we ONLY join to fundamentals whose
as_of_date <= X. This is what eliminates look-ahead bias — the #1 source of
fake alpha in retail quant projects. Designed out from row one.

Prices are already PIT-safe by nature: a price on day D was known on day D.
"""

from __future__ import annotations

SCHEMA_SQL = """
-- ======================================================================
-- Securities: which tickers we track.
-- ======================================================================
CREATE TABLE IF NOT EXISTS securities (
    ticker          VARCHAR PRIMARY KEY,
    cik             INTEGER,            -- SEC Central Index Key (for EDGAR)
    name            VARCHAR,
    exchange        VARCHAR,
    sector          VARCHAR,
    industry        VARCHAR,
    market_cap_bucket VARCHAR,           -- mega/large/mid/small/micro
    sic_code        VARCHAR,
    is_active       BOOLEAN DEFAULT TRUE,
    first_seen      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_updated    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ======================================================================
-- Daily EOD prices. One row per (ticker, date). PIT-safe by construction.
-- ======================================================================
CREATE TABLE IF NOT EXISTS prices (
    ticker          VARCHAR NOT NULL,
    date            DATE NOT NULL,
    open            DOUBLE,
    high            DOUBLE,
    low             DOUBLE,
    close           DOUBLE,
    adj_close       DOUBLE,             -- split- AND dividend-adjusted
    volume          BIGINT,
    dividends       DOUBLE DEFAULT 0,
    split_ratio     DOUBLE DEFAULT 1,
    source          VARCHAR DEFAULT 'yfinance',
    fetched_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (ticker, date)
);

-- ======================================================================
-- Fundamentals — THE point-in-time-critical table.
--   period_end   = the fiscal period the numbers describe (e.g. 2024-12-31)
--   as_of_date   = the date the market could FIRST know these numbers
--                  (filing date for SEC data). THIS IS THE PIT KEY.
--   Every factor/backtest query filters: WHERE as_of_date <= decision_date
-- ======================================================================
CREATE TABLE IF NOT EXISTS fundamentals (
    ticker          VARCHAR NOT NULL,
    period_end      DATE NOT NULL,      -- fiscal period being reported
    as_of_date      DATE NOT NULL,      -- PIT key: when data became knowable
    fiscal_period   VARCHAR,            -- 'FY2024','Q1_2025', etc.
    statement       VARCHAR,            -- 'income','balance','cashflow'
    metric          VARCHAR NOT NULL,   -- 'revenue','net_income', etc.
    value           DOUBLE,             -- raw value (YTD-cumulative for flow metrics)
    quarter_value   DOUBLE,             -- derived single-period value (flow metrics only)
    unit            VARCHAR DEFAULT 'USD',
    source          VARCHAR DEFAULT 'edgar',
    fetched_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (ticker, period_end, as_of_date, metric)
);

-- ======================================================================
-- Macro indicators (one row per series per observation date).
-- ======================================================================
CREATE TABLE IF NOT EXISTS macro (
    series_id       VARCHAR NOT NULL,   -- e.g. 'CPIAUCSL','DGS10','T10Y2Y'
    date            DATE NOT NULL,
    value           DOUBLE,
    unit            VARCHAR,
    source          VARCHAR DEFAULT 'fred',
    fetched_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (series_id, date)
);

-- ======================================================================
-- Ingest log: every fetch run records what it did. Auditability matters.
-- (Sequence created first — the table default references it.)
-- ======================================================================
CREATE SEQUENCE IF NOT EXISTS ingest_seq;

CREATE TABLE IF NOT EXISTS ingest_log (
    id              BIGINT PRIMARY KEY DEFAULT nextval('ingest_seq'),
    run_id          VARCHAR NOT NULL,
    source          VARCHAR NOT NULL,
    table_name      VARCHAR NOT NULL,
    rows_inserted   BIGINT,
    rows_rejected   BIGINT,
    started_at      TIMESTAMP,
    finished_at     TIMESTAMP,
    status          VARCHAR,
    error           TEXT
);
"""

# Tables we expect to exist after init. Used by the smoke test.
EXPECTED_TABLES = ("securities", "prices", "fundamentals", "macro", "ingest_log")
