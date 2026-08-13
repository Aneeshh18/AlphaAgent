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
    actions_complete BOOLEAN DEFAULT FALSE, -- provider response included actions
    close_split_adjusted BOOLEAN,        -- whether close already reflects splits
    split_normalization_factor DOUBLE,   -- restores contemporaneous price basis
    split_normalization_through DATE,    -- last date scanned for later splits
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
    ingest_run_id   VARCHAR,             -- successful source ingest (NULL = legacy)
    source_snapshot_id VARCHAR,           -- exact Company Facts response
    source_rowset_sha256 VARCHAR,         -- canonical parser output hash
    source_row_sha256 VARCHAR,            -- canonical provider-row hash
    source_fact_locator VARCHAR,           -- canonical taxonomy/concept/accession locator
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

-- Append-only system-time history for governed factor decisions. ``fundamentals``
-- remains the current projection; a named evidence generation resolves each
-- economic key to the latest version at or before its captured sequence.
CREATE SEQUENCE IF NOT EXISTS fundamental_version_seq START 1;
CREATE TABLE IF NOT EXISTS fundamental_versions (
    version_sequence BIGINT PRIMARY KEY DEFAULT nextval('fundamental_version_seq'),
    ticker          VARCHAR NOT NULL,
    issuer_id       VARCHAR,
    security_id     VARCHAR,
    ingest_run_id   VARCHAR,
    source_snapshot_id VARCHAR,
    source_rowset_sha256 VARCHAR,
    source_row_sha256 VARCHAR,
    source_fact_locator VARCHAR,
    period_end      DATE NOT NULL,
    as_of_date      DATE NOT NULL,
    fiscal_period   VARCHAR,
    statement       VARCHAR,
    metric          VARCHAR NOT NULL,
    value           DOUBLE,
    quarter_value   DOUBLE,
    unit            VARCHAR,
    source          VARCHAR,
    recorded_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    is_deleted      BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE TABLE IF NOT EXISTS fundamental_evidence_generations (
    generation_id  VARCHAR PRIMARY KEY,
    version_sequence BIGINT NOT NULL,
    purpose        VARCHAR NOT NULL,
    decision_date  DATE,
    captured_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Invalid source rows are moved here instead of being destroyed. They are
-- excluded from every PIT query but remain available for provenance review.
CREATE TABLE IF NOT EXISTS fundamentals_quarantine (
    ticker          VARCHAR NOT NULL,
    issuer_id       VARCHAR,
    security_id     VARCHAR,
    ingest_run_id   VARCHAR,
    source_snapshot_id VARCHAR,
    source_rowset_sha256 VARCHAR,
    source_row_sha256 VARCHAR,
    source_fact_locator VARCHAR,
    period_end      DATE NOT NULL,
    as_of_date      DATE NOT NULL,
    fiscal_period   VARCHAR,
    statement       VARCHAR,
    metric          VARCHAR NOT NULL,
    value           DOUBLE,
    quarter_value   DOUBLE,
    unit            VARCHAR,
    source          VARCHAR,
    fetched_at      TIMESTAMP,
    quarantine_reason VARCHAR NOT NULL,
    quarantined_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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
    subject_type    VARCHAR,
    subject_id      VARCHAR,
    rows_inserted   BIGINT,
    rows_rejected   BIGINT,
    started_at      TIMESTAMP,
    finished_at     TIMESTAMP,
    status          VARCHAR,
    error           TEXT,
    rejection_codes VARCHAR,             -- canonical machine-readable JSON codes
    CHECK (
        (subject_type IS NULL AND subject_id IS NULL)
        OR (subject_type IS NOT NULL AND subject_id IS NOT NULL)
    )
);

-- Immutable provider evidence is split into content-addressed payloads and
-- fetch observations. Identical bytes share one file, while every request
-- retains its own timestamps, adapter/parser versions, and ingest link.
CREATE TABLE IF NOT EXISTS raw_payloads (
    payload_sha256  VARCHAR PRIMARY KEY,
    relative_path  VARCHAR NOT NULL UNIQUE,
    original_bytes BIGINT NOT NULL,
    stored_bytes   BIGINT NOT NULL,
    compression    VARCHAR NOT NULL,
    created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS raw_snapshots (
    snapshot_id        VARCHAR PRIMARY KEY,
    provider           VARCHAR NOT NULL,
    dataset            VARCHAR NOT NULL,
    artifact_kind      VARCHAR NOT NULL,
    requested_at       TIMESTAMP NOT NULL,
    received_at        TIMESTAMP NOT NULL,
    http_status        INTEGER,
    content_type       VARCHAR,
    request_fingerprint VARCHAR NOT NULL,
    payload_sha256     VARCHAR NOT NULL,
    adapter_name       VARCHAR NOT NULL,
    adapter_version    VARCHAR NOT NULL,
    parser_version     VARCHAR NOT NULL,
    parsed_row_count   BIGINT,
    parsed_rows_sha256 VARCHAR,
    parsed_rows_rejected BIGINT,
    parsed_rejection_codes VARCHAR,
    created_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS ingest_raw_snapshots (
    run_id          VARCHAR NOT NULL,
    snapshot_id     VARCHAR NOT NULL,
    role            VARCHAR NOT NULL,
    linked_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (run_id, snapshot_id, role)
);

-- A no-change universe roll-forward is an explicit, immutable certification,
-- never an inferred extension. Exact provider responses remain in raw_snapshots;
-- this row records what was compared and whether any reference windows moved.
CREATE TABLE IF NOT EXISTS universe_coverage_attestations (
    attestation_id             VARCHAR PRIMARY KEY,
    run_id                     VARCHAR NOT NULL UNIQUE,
    universe_id                VARCHAR NOT NULL,
    prior_coverage_through     DATE NOT NULL,
    requested_coverage_through DATE NOT NULL,
    checked_at                 TIMESTAMP NOT NULL,
    completed_new_york_date    DATE NOT NULL,
    status                     VARCHAR NOT NULL CHECK (
        status IN ('accepted_no_change', 'blocked_review_required')
    ),
    official_source_url        VARCHAR NOT NULL,
    component_source_url       VARCHAR NOT NULL,
    official_release_count     BIGINT NOT NULL,
    relevant_release_count     BIGINT NOT NULL,
    reviewed_member_count      BIGINT NOT NULL,
    component_count            BIGINT NOT NULL,
    reviewed_member_set_sha256 VARCHAR NOT NULL,
    component_set_sha256       VARCHAR NOT NULL,
    identity_match_count       BIGINT NOT NULL,
    identity_mismatch_count    BIGINT NOT NULL,
    candidate_releases_json    TEXT NOT NULL,
    mismatch_detail_json       TEXT NOT NULL,
    membership_rows_extended   BIGINT NOT NULL,
    security_rows_extended     BIGINT NOT NULL,
    owner_rows_extended        BIGINT NOT NULL,
    cik_rows_extended          BIGINT NOT NULL,
    provider_rows_extended     BIGINT NOT NULL,
    detail                     TEXT NOT NULL,
    created_at                 TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Accepted constituent changes are append-only, content-addressed
-- transactions.  The authoritative receipt lives beside the canonical rows
-- that it changed so a database backup/restore cannot separate the two.
CREATE TABLE IF NOT EXISTS universe_constituent_change_activations (
    activation_id              VARCHAR PRIMARY KEY,
    event_id                   VARCHAR NOT NULL UNIQUE,
    plan_sha256                VARCHAR NOT NULL UNIQUE,
    activation_payload_sha256  VARCHAR NOT NULL UNIQUE,
    activation_run_id          VARCHAR NOT NULL UNIQUE,
    fundamental_run_id         VARCHAR NOT NULL UNIQUE,
    price_run_id               VARCHAR NOT NULL UNIQUE,
    source_attestation_id      VARCHAR NOT NULL,
    schema_version             INTEGER NOT NULL CHECK (schema_version = 1),
    universe_id                VARCHAR NOT NULL,
    announcement_date          DATE NOT NULL,
    effective_date             DATE NOT NULL,
    prior_coverage_through     DATE NOT NULL,
    target_coverage_through    DATE NOT NULL,
    official_detail_snapshot_id VARCHAR NOT NULL,
    component_snapshot_id      VARCHAR NOT NULL,
    before_member_set_sha256   VARCHAR NOT NULL,
    after_member_set_sha256    VARCHAR NOT NULL,
    before_state_sha256        VARCHAR NOT NULL,
    after_state_sha256         VARCHAR NOT NULL,
    change_rows_sha256         VARCHAR NOT NULL,
    activation_payload_json    TEXT NOT NULL,
    backup_manifest_sha256     VARCHAR NOT NULL,
    actor                      VARCHAR NOT NULL,
    policy_version             VARCHAR NOT NULL,
    counts_json                TEXT NOT NULL,
    activated_at               TIMESTAMP NOT NULL,
    status                     VARCHAR NOT NULL CHECK (status = 'accepted'),
    created_at                 TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    CHECK (announcement_date <= effective_date),
    CHECK (prior_coverage_through < effective_date),
    CHECK (effective_date <= target_coverage_through),
    CHECK (regexp_full_match(plan_sha256, '^[0-9a-f]{{64}}$')),
    CHECK (regexp_full_match(activation_payload_sha256, '^[0-9a-f]{{64}}$')),
    CHECK (regexp_full_match(before_member_set_sha256, '^[0-9a-f]{{64}}$')),
    CHECK (regexp_full_match(after_member_set_sha256, '^[0-9a-f]{{64}}$')),
    CHECK (regexp_full_match(before_state_sha256, '^[0-9a-f]{{64}}$')),
    CHECK (regexp_full_match(after_state_sha256, '^[0-9a-f]{{64}}$')),
    CHECK (regexp_full_match(change_rows_sha256, '^[0-9a-f]{{64}}$')),
    CHECK (regexp_full_match(backup_manifest_sha256, '^[0-9a-f]{{64}}$')),
    CHECK (length(trim(actor)) > 0),
    CHECK (length(trim(policy_version)) > 0)
);

-- Governed Company Facts v3 parser activation. Append-only, one row per
-- accepted batch. The mutation itself lands in `fundamentals` (upsert path)
-- and `fundamental_versions` (append-only, `is_deleted` tombstones a key v3
-- withholds that v2 silently kept); this table is the operational receipt
-- binding the reviewed plan, verified backup, and disposable-rollback proof
-- to what was actually committed.
CREATE TABLE IF NOT EXISTS companyfacts_v3_activations (
    activation_id              VARCHAR PRIMARY KEY,
    activation_plan_sha256     VARCHAR NOT NULL UNIQUE,
    review_plan_sha256         VARCHAR NOT NULL,
    activation_run_id          VARCHAR NOT NULL UNIQUE,
    schema_version              INTEGER NOT NULL CHECK (schema_version = 1),
    as_of                       DATE NOT NULL,
    issuer_ids_json             TEXT NOT NULL,
    source_parser_version       VARCHAR NOT NULL,
    target_parser_version       VARCHAR NOT NULL,
    activation_payload_json     TEXT NOT NULL,
    backup_manifest_sha256      VARCHAR NOT NULL,
    actor                       VARCHAR NOT NULL,
    policy_version              VARCHAR NOT NULL,
    counts_json                 TEXT NOT NULL,
    generation_id                VARCHAR NOT NULL,
    version_sequence_boundary    BIGINT NOT NULL,
    activated_at                TIMESTAMP NOT NULL,
    status                      VARCHAR NOT NULL CHECK (status = 'accepted'),
    created_at                  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    CHECK (regexp_full_match(activation_plan_sha256, '^[0-9a-f]{{64}}$')),
    CHECK (regexp_full_match(review_plan_sha256, '^[0-9a-f]{{64}}$')),
    CHECK (regexp_full_match(backup_manifest_sha256, '^[0-9a-f]{{64}}$')),
    CHECK (length(trim(actor)) > 0),
    CHECK (length(trim(policy_version)) > 0)
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

-- Reviewed share-for-share events that replace one listed security with
-- another during a holding period.  These are deliberately separate from
-- ticker changes: a conversion changes the immutable security identity and
-- must carry explicit public-date, ratio, basis, and source evidence.
CREATE TABLE IF NOT EXISTS security_conversions (
    source_security_id VARCHAR NOT NULL,
    target_security_id VARCHAR NOT NULL,
    effective_date     DATE NOT NULL,
    known_date         DATE NOT NULL,
    share_ratio        DOUBLE NOT NULL,
    basis_policy       VARCHAR NOT NULL,
    review_status      VARCHAR NOT NULL,
    verified_date      DATE NOT NULL,
    source             VARCHAR NOT NULL, -- completion/date/ratio evidence
    basis_source       VARCHAR NOT NULL, -- carry-over basis/holding-period evidence
    fetched_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (source_security_id)
);

-- Short, reviewed ticker/provider continuations used only to liquidate a
-- position after its index-membership interval ends. They never make the
-- security factor-eligible and cannot be used to backdate membership.
CREATE TABLE IF NOT EXISTS security_ticker_extensions (
    provenance_id   VARCHAR PRIMARY KEY,
    universe_id     VARCHAR NOT NULL,
    security_id     VARCHAR NOT NULL,
    ticker          VARCHAR NOT NULL,
    provider        VARCHAR NOT NULL,
    provider_symbol VARCHAR NOT NULL,
    data_start      DATE NOT NULL,
    data_end        DATE NOT NULL,
    verified_date   DATE NOT NULL,
    identity_source VARCHAR NOT NULL,
    provider_source VARCHAR NOT NULL,
    payload_sha256  VARCHAR NOT NULL,
    purpose         VARCHAR NOT NULL,
    review_policy   VARCHAR NOT NULL,
    fetched_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ======================================================================
-- Reviewed factor-price warm-up history.
--   These rows intentionally do not carry a market ticker. A provider may
--   expose history from before our certified universe/ticker interval, and
--   backdating that ticker would create false identity evidence. Instead, an
--   immutable security_id is anchored to an already reviewed provider series
--   by an overlap comparison whose hashes are retained here.
-- ======================================================================
CREATE TABLE IF NOT EXISTS factor_price_provenance (
    provenance_id   VARCHAR PRIMARY KEY,
    universe_id     VARCHAR NOT NULL,
    security_id     VARCHAR NOT NULL,
    provider        VARCHAR NOT NULL,
    provider_symbol VARCHAR NOT NULL,
    data_start      DATE NOT NULL,
    data_end        DATE NOT NULL,       -- half-open; reviewed mapping anchor
    overlap_start   DATE NOT NULL,
    overlap_end     DATE NOT NULL,
    verified_date   DATE NOT NULL,
    source          VARCHAR NOT NULL,
    payload_sha256  VARCHAR NOT NULL,
    overlap_sha256  VARCHAR NOT NULL,
    review_policy   VARCHAR NOT NULL,
    fetched_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS factor_prices (
    security_id     VARCHAR NOT NULL,
    date            DATE NOT NULL,
    provider        VARCHAR NOT NULL,
    provider_symbol VARCHAR NOT NULL,
    close           DOUBLE NOT NULL,
    adj_close       DOUBLE,
    dividends       DOUBLE NOT NULL DEFAULT 0,
    split_ratio     DOUBLE NOT NULL DEFAULT 1,
    actions_complete BOOLEAN NOT NULL,
    close_split_adjusted BOOLEAN NOT NULL,
    split_normalization_factor DOUBLE NOT NULL,
    split_normalization_through DATE,
    provenance_id   VARCHAR NOT NULL,
    fetched_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (security_id, date)
);

-- ======================================================================
-- Historical investable-universe membership.
--   effective_* = when membership applies to the portfolio universe.
--   known_date  = when the membership start became publicly knowable.
--   end_known_date = when a finite membership end became publicly knowable.
--   Effective and knowledge dates stay separate to prevent membership look-ahead.
-- ======================================================================
CREATE TABLE IF NOT EXISTS universe_membership (
    universe_id     VARCHAR NOT NULL,
    ticker          VARCHAR NOT NULL,
    security_id     VARCHAR,
    effective_start DATE NOT NULL,
    effective_end   DATE,
    known_date      DATE NOT NULL,
    end_known_date  DATE,
    source          VARCHAR NOT NULL,
    fetched_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (universe_id, ticker, effective_start)
);

-- ======================================================================
-- First-class market contracts (INDIA_BUILD_PLAN.md phase I1).
--   Every existing U.S. row stays exactly as it was: these are new, empty
--   tables, not a rewrite of security_master/prices/fundamentals. A market
--   is a country+currency+calendar identity ('us_equity', 'in_equity'), a
--   venue is one exchange within it (NSE, BSE, NYSE), a market_profile binds
--   one universe to its benchmark/filing/action sources, and
--   security_listings is the venue-level fact a stable security_id already
--   carries — one more identity layer, never a replacement for it. No
--   shared factor/PIT/portfolio code may branch on market_id, currency, or
--   venue_id: a synthetic non-U.S. fixture must pass the identical contracts
--   the U.S. reference data does. See tests/test_market_contracts.py.
-- ======================================================================
CREATE TABLE IF NOT EXISTS markets (
    market_id           VARCHAR PRIMARY KEY,   -- e.g. 'us_equity', 'in_equity'
    country             VARCHAR NOT NULL,      -- ISO 3166-1 alpha-2, e.g. 'US', 'IN'
    base_currency       VARCHAR NOT NULL,      -- ISO 4217, e.g. 'USD', 'INR'
    timezone            VARCHAR NOT NULL,      -- IANA name, e.g. 'America/New_York'
    default_venue_id    VARCHAR NOT NULL,
    source              VARCHAR NOT NULL,
    fetched_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS venues (
    venue_id            VARCHAR PRIMARY KEY,   -- e.g. 'xnys', 'xnse', 'xbom'
    market_id           VARCHAR NOT NULL,
    mic                 VARCHAR,               -- ISO 10383 Market Identifier Code
    name                VARCHAR NOT NULL,
    source              VARCHAR NOT NULL,
    fetched_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS market_profiles (
    market_profile_id   VARCHAR PRIMARY KEY,   -- e.g. 'us_equity_sp500_reference'
    market_id           VARCHAR NOT NULL,
    primary_venue_id    VARCHAR NOT NULL,
    universe_id         VARCHAR NOT NULL,      -- e.g. 'sp500', 'nifty50'
    benchmark_id        VARCHAR,               -- nullable until a benchmark is reviewed
    filing_source       VARCHAR NOT NULL,      -- e.g. 'sec-edgar', 'nse-xbrl'
    action_source       VARCHAR NOT NULL,      -- e.g. 'yfinance', 'nse-corporate-actions'
    source               VARCHAR NOT NULL,
    fetched_at           TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Complements provider_symbol_history (which bounds one data PROVIDER's
-- returned rows to a security) with the venue-LISTING fact itself: this
-- security traded under this symbol/series/ISIN on this exchange during this
-- interval, independent of which data provider is later used to fetch it.
CREATE TABLE IF NOT EXISTS security_listings (
    security_id         VARCHAR NOT NULL,
    venue_id             VARCHAR NOT NULL,
    symbol               VARCHAR NOT NULL,
    series               VARCHAR,              -- e.g. NSE 'EQ'; null where not applicable
    isin                 VARCHAR,              -- ISO 6166; nullable until reviewed
    security_type         VARCHAR NOT NULL DEFAULT 'common_stock',
    currency             VARCHAR NOT NULL,
    listed_start         DATE NOT NULL,
    listed_end           DATE,
    known_at             DATE NOT NULL,        -- when this listing fact was publicly knowable
    source               VARCHAR NOT NULL,
    fetched_at           TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (security_id, venue_id, listed_start)
);

CREATE TABLE IF NOT EXISTS trading_sessions (
    venue_id             VARCHAR NOT NULL,
    session_date         DATE NOT NULL,
    session_type         VARCHAR NOT NULL,     -- 'normal','early_close','special','closed'
    open_time             TIME,
    close_time             TIME,
    notes                 VARCHAR,
    source               VARCHAR NOT NULL,
    fetched_at           TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (venue_id, session_date)
);

CREATE TABLE IF NOT EXISTS settlement_policies (
    venue_id             VARCHAR NOT NULL,
    effective_start     DATE NOT NULL,
    effective_end         DATE,
    settlement_cycle     VARCHAR NOT NULL,     -- e.g. 'T+1', 'T+0_optional'
    source               VARCHAR NOT NULL,
    fetched_at           TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (venue_id, effective_start)
);

CREATE TABLE IF NOT EXISTS benchmarks (
    benchmark_id         VARCHAR PRIMARY KEY,  -- e.g. 'spy_price', 'nifty50_tri'
    market_id             VARCHAR NOT NULL,
    benchmark_kind         VARCHAR NOT NULL,   -- 'price_index','total_return_index'
    provider             VARCHAR NOT NULL,
    provider_symbol     VARCHAR NOT NULL,
    data_start             DATE,
    data_end             DATE,
    source               VARCHAR NOT NULL,
    fetched_at           TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""

# Tables we expect to exist after init. Used by the smoke test.
EXPECTED_TABLES = (
    "securities",
    "prices",
    "fundamentals",
    "fundamental_versions",
    "fundamental_evidence_generations",
    "macro",
    "ingest_log",
    "raw_payloads",
    "raw_snapshots",
    "ingest_raw_snapshots",
    "universe_coverage_attestations",
    "universe_constituent_change_activations",
    "companyfacts_v3_activations",
    "schema_migrations",
    "security_master",
    "security_identity_assignments",
    "issuer_master",
    "issuer_cik_history",
    "security_issuer_assignments",
    "provider_symbol_history",
    "security_conversions",
    "security_ticker_extensions",
    "factor_price_provenance",
    "factor_prices",
    "universe_membership",
    "markets",
    "venues",
    "market_profiles",
    "security_listings",
    "trading_sessions",
    "settlement_policies",
    "benchmarks",
)
