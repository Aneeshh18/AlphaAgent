"""Point-in-time storage schema for DuckDB.

THE CORE INVARIANT
------------------
Every fundamentals row carries an `as_of_date` = the date the data became
*knowable to the market* (the filing date, for SEC data). When we compute
factors or run a backtest "as of date X", we ONLY join to fundamentals whose
as_of_date <= X. This is what eliminates look-ahead bias — the #1 source of
fake alpha in retail quant projects. Designed out from row one.

Prices are already PIT-safe by nature: a price on day D was known on day D.

Macro observations are different: a historical observation can be revised and
the revision may only become known later. The macro table therefore stores
both the observation date (`date`) and the public availability/vintage date
(`release_date`). Rows without a release date are legacy data and are excluded
from PIT macro reads until re-ingested with vintage metadata.
"""

from __future__ import annotations

MACRO_TABLE_SQL = """
CREATE SEQUENCE IF NOT EXISTS macro_seq;

CREATE TABLE IF NOT EXISTS macro (
    macro_id        BIGINT PRIMARY KEY DEFAULT nextval('macro_seq'),
    series_id       VARCHAR NOT NULL,   -- e.g. 'CPIAUCSL','DGS10','T10Y2Y'
    date            DATE NOT NULL,      -- observation/economic period date
    release_date    DATE,               -- first public availability of this vintage
    value           DOUBLE,
    unit            VARCHAR,
    source          VARCHAR DEFAULT 'fred',
    fetched_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (series_id, date, release_date, source)
);
"""

SCHEMA_SQL = f"""
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
    security_id     VARCHAR,             -- immutable listed-security identity
    provider_symbol VARCHAR,             -- symbol actually queried at the provider
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
    issuer_id       VARCHAR,             -- legal/reporting entity identity
    security_id     VARCHAR,             -- optional listed-security context
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
-- Macro indicators: one row per series, observation date, and release vintage.
-- ======================================================================
{MACRO_TABLE_SQL}

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

-- Durable markers distinguish an interrupted migration from an intentional
-- post-backfill cleanup. Without this, reopening DuckDB could restore rows
-- that were deliberately removed from the active table.
CREATE TABLE IF NOT EXISTS schema_migrations (
    name            VARCHAR PRIMARY KEY,
    applied_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ======================================================================
-- Stable security identities.
--   security_id is an internal immutable identifier for one listed security.
--   It must not be inferred from the current ticker: tickers can change or be
--   reused by a different issuer. Bounded identities are deliberately labeled
--   until stronger issuer/share-class identifiers are sourced.
-- ======================================================================
CREATE TABLE IF NOT EXISTS security_master (
    security_id     VARCHAR PRIMARY KEY,
    canonical_ticker VARCHAR NOT NULL,
    security_type   VARCHAR NOT NULL DEFAULT 'common_stock',
    identity_status VARCHAR NOT NULL,
    source          VARCHAR NOT NULL,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_updated    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Every assignment mirrors one certified universe interval. Keeping the
-- evidence separately makes ticker transitions and later corrections auditable.
CREATE TABLE IF NOT EXISTS security_identity_assignments (
    universe_id     VARCHAR NOT NULL,
    ticker          VARCHAR NOT NULL,
    effective_start DATE NOT NULL,
    effective_end   DATE,
    security_id     VARCHAR NOT NULL,
    known_date      DATE NOT NULL,
    identity_status VARCHAR NOT NULL,
    source          VARCHAR NOT NULL,
    fetched_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (universe_id, ticker, effective_start)
);

-- ======================================================================
-- Issuer and provider-reference identities.
--   CIK identifies an SEC reporting entity, not a listed share class.
--   security_issuer_assignments connects those separate identity domains.
--   provider_symbol_history bounds symbols to the dates for which a provider's
--   returned history is certified to belong to the intended security.
-- ======================================================================
CREATE TABLE IF NOT EXISTS issuer_master (
    issuer_id        VARCHAR PRIMARY KEY,
    canonical_name   VARCHAR NOT NULL,
    canonical_ticker VARCHAR NOT NULL,
    source           VARCHAR NOT NULL,
    created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_updated     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS issuer_cik_history (
    issuer_id        VARCHAR NOT NULL,
    cik              VARCHAR NOT NULL,  -- normalized, zero-padded 10-digit SEC CIK
    effective_start  DATE NOT NULL,
    effective_end    DATE,
    verified_date    DATE NOT NULL,
    source           VARCHAR NOT NULL,
    fetched_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (issuer_id, effective_start)
);

CREATE TABLE IF NOT EXISTS security_issuer_assignments (
    security_id      VARCHAR NOT NULL,
    issuer_id        VARCHAR NOT NULL,
    effective_start  DATE NOT NULL,
    effective_end    DATE,
    verified_date    DATE NOT NULL,
    source           VARCHAR NOT NULL,
    fetched_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (security_id, effective_start)
);

CREATE TABLE IF NOT EXISTS provider_symbol_history (
    provider         VARCHAR NOT NULL,
    provider_symbol  VARCHAR NOT NULL,
    security_id      VARCHAR NOT NULL,
    data_start       DATE NOT NULL,
    data_end         DATE,
    mapping_status   VARCHAR NOT NULL,
    verified_date    DATE NOT NULL,
    source           VARCHAR NOT NULL,
    fetched_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (provider, security_id, data_start)
);

-- ======================================================================
-- Historical investable-universe membership.
--   effective_* = when membership applies to the portfolio universe.
--   known_date  = when that membership interval became publicly knowable.
--   The two dates are intentionally separate to prevent membership look-ahead.
-- ======================================================================
CREATE TABLE IF NOT EXISTS universe_membership (
    universe_id     VARCHAR NOT NULL,
    ticker          VARCHAR NOT NULL,
    security_id     VARCHAR,
    effective_start DATE NOT NULL,
    effective_end   DATE,
    known_date      DATE NOT NULL,
    source          VARCHAR NOT NULL,
    fetched_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (universe_id, ticker, effective_start)
);
"""

# Tables we expect to exist after init. Used by the smoke test.
EXPECTED_TABLES = (
    "securities",
    "prices",
    "fundamentals",
    "macro",
    "ingest_log",
    "schema_migrations",
    "security_master",
    "security_identity_assignments",
    "issuer_master",
    "issuer_cik_history",
    "security_issuer_assignments",
    "provider_symbol_history",
    "universe_membership",
)
