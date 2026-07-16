"""DuckDB connection + storage operations.

A thin wrapper that:
  - opens a single-process connection to the local .duckdb file
  - initializes the point-in-time schema on first run
  - provides typed insert helpers that enforce the PIT invariant
  - exposes simple query helpers for the factor/test layers

This is the ONLY module allowed to open a DuckDB connection. Everything else
goes through these functions. Keeps connection management in one place.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import duckdb
from structlog import get_logger

from aios.config import settings
from aios.storage.schema import SCHEMA_SQL

if TYPE_CHECKING:
    import pandas as pd

log = get_logger(__name__)


class Store:
    """Thin wrapper around a DuckDB connection."""

    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = db_path or _resolve(settings.duckdb_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._con = duckdb.connect(str(self.db_path))
        self._init_schema()

    def _init_schema(self) -> None:
        """Run all CREATE TABLE IF NOT EXISTS statements."""
        self._con.execute(SCHEMA_SQL)
        log.info("schema.initialized", db=str(self.db_path))

    @property
    def con(self) -> duckdb.DuckDBPyConnection:
        return self._con

    # ------------------------------------------------------------------
    # Low-level helpers
    # ------------------------------------------------------------------
    def execute(self, sql: str, params: tuple[Any, ...] | None = None) -> Any:
        if params is None:
            return self._con.execute(sql)
        return self._con.execute(sql, params)

    def query(self, sql: str, params: tuple[Any, ...] | None = None) -> list[dict]:
        """Run a SELECT and return rows as list of dicts."""
        cur = self.execute(sql, params)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row, strict=True)) for row in cur.fetchall()]

    # ------------------------------------------------------------------
    # Insert helpers (typed, idempotent, PIT-aware)
    # ------------------------------------------------------------------
    def upsert_securities(self, rows: list[dict]) -> int:
        """Insert/update securities. Returns number of rows affected."""
        if not rows:
            return 0
        self._con.register("_tmp_sec", _rows_to_arrowable(rows))
        n = self._con.execute(
            """
            INSERT OR REPLACE INTO securities
            (ticker, cik, name, exchange, sector, industry, market_cap_bucket,
             sic_code, is_active, first_seen, last_updated)
            SELECT
                ticker, cik, name, exchange, sector, industry, market_cap_bucket,
                sic_code, TRUE, now(), now() FROM _tmp_sec
            """.strip()
        ).fetchone()[0]
        self._con.unregister("_tmp_sec")
        return int(n)

    def upsert_prices(self, rows: list[dict]) -> int:
        """Upsert daily prices. Idempotent on (ticker, date)."""
        if not rows:
            return 0
        self._con.register("_tmp_px", _rows_to_arrowable(rows))
        n = self._con.execute(
            """
            INSERT OR REPLACE INTO prices
            (ticker, date, open, high, low, close, adj_close, volume,
             dividends, split_ratio, source, fetched_at)
            SELECT
                ticker, CAST(date AS DATE),
                open, high, low, close, adj_close, volume,
                COALESCE(dividends, 0), COALESCE(split_ratio, 1),
                COALESCE(source, 'yfinance'), now()
            FROM _tmp_px
            """.strip()
        ).fetchone()[0]
        self._con.unregister("_tmp_px")
        return int(n)

    def upsert_fundamentals(self, rows: list[dict]) -> int:
        """Upsert fundamentals. CRITICAL: as_of_date must be set per row.

        This is the point-in-time-critical insert. Never call with a default
        as_of_date; the caller must supply the *filing date* from the source.
        """
        if not rows:
            return 0
        # Defensive: refuse to insert fundamentals without as_of_date.
        missing = [r for r in rows if not r.get("as_of_date")]
        if missing:
            raise ValueError(
                f"upsert_fundamentals: {len(missing)} rows lack as_of_date. "
                "Point-in-time correctness requires a knowable-date per row."
            )
        self._con.register("_tmp_fund", _rows_to_arrowable(rows))
        n = self._con.execute(
            """
            INSERT OR REPLACE INTO fundamentals
            (ticker, period_end, as_of_date, fiscal_period, statement, metric,
             value, quarter_value, unit, source, fetched_at)
            SELECT
                ticker, CAST(period_end AS DATE), CAST(as_of_date AS DATE),
                fiscal_period, statement, metric, value, quarter_value,
                COALESCE(unit, 'USD'), source, now()
            FROM _tmp_fund
            """.strip()
        ).fetchone()[0]
        self._con.unregister("_tmp_fund")
        return int(n)

    def upsert_macro(self, rows: list[dict]) -> int:
        if not rows:
            return 0
        self._con.register("_tmp_macro", _rows_to_arrowable(self._coerce_macro(rows)))
        n = self._con.execute(
            """
            INSERT OR REPLACE INTO macro
            (series_id, date, value, unit, source, fetched_at)
            SELECT
                series_id, CAST(date AS DATE), value, unit,
                source, now()
            FROM _tmp_macro
            """.strip()
        ).fetchone()[0]
        self._con.unregister("_tmp_macro")
        return int(n)

    @staticmethod
    def _coerce_macro(rows: list[dict]) -> list[dict]:
        clean = []
        for r in rows:
            clean.append({
                "series_id": r["series_id"],
                "date": r.get("date"),
                "value": _to_float(r.get("value")),
                "unit": r.get("unit"),
                "source": r.get("source", "fred"),
                "fetched_at": r.get("fetched_at"),
            })
        return clean

    # ------------------------------------------------------------------
    # Point-in-time query helpers (used by factor + backtest layers)
    # ------------------------------------------------------------------
    def pit_fundamentals(
        self,
        ticker: str,
        as_of: date | str,
        metrics: list[str] | None = None,
    ) -> list[dict]:
        """Get the latest fundamentals known as of `as_of` for a ticker.

        This is THE point-in-time read. It returns the most recent filing for
        each metric whose as_of_date <= as_of. No look-ahead possible.
        """
        sql = """
            WITH ranked AS (
                SELECT *,
                       ROW_NUMBER() OVER (
                           PARTITION BY metric
                           ORDER BY as_of_date DESC, period_end DESC
                       ) AS rn
                FROM fundamentals
                WHERE ticker = ?
                  AND as_of_date <= CAST(? AS DATE)
            )
            SELECT ticker, period_end, as_of_date, fiscal_period,
                   statement, metric, value, quarter_value, unit, source
            FROM ranked
            WHERE rn = 1
        """
        params: tuple[Any, ...] = (ticker, str(as_of))
        if metrics:
            placeholders = ",".join("?" for _ in metrics)
            sql += f" AND metric IN ({placeholders})"
            params = (ticker, str(as_of), *metrics)
        return self.query(sql, params)

    def price_on(self, ticker: str, on_date: date | str) -> dict | None:
        row = self.query(
            "SELECT * FROM prices WHERE ticker=? AND date=?",
            (ticker, str(on_date)),
        )
        return row[0] if row else None

    def price_history(
        self,
        ticker: str,
        start: date | str | None = None,
        end: date | str | None = None,
    ) -> list[dict]:
        sql = "SELECT * FROM prices WHERE ticker = ?"
        params: list[Any] = [ticker]
        if start:
            sql += " AND date >= CAST(? AS DATE)"
            params.append(str(start))
        if end:
            sql += " AND date <= CAST(? AS DATE)"
            params.append(str(end))
        sql += " ORDER BY date"
        return self.query(sql, tuple(params))

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------
    def table_rowcounts(self) -> dict[str, int]:
        out = {}
        for t in ("securities", "prices", "fundamentals", "macro"):
            out[t] = self.query(f"SELECT COUNT(*) AS n FROM {t}")[0]["n"]
        return out

    def close(self) -> None:
        self._con.close()


# ----------------------------------------------------------------------
# Module helpers
# ----------------------------------------------------------------------
def _resolve(p: Path | str) -> Path:
    p = Path(p)
    return p if p.is_absolute() else settings.project_root / p


def _rows_to_arrowable(rows: list[dict]) -> pd.DataFrame:
    """Normalize row dicts into a pandas DataFrame for DuckDB registration.

    DuckDB's replacement scan accepts DataFrames natively. We coerce
    datetime/date values to ISO strings so DuckDB can CAST them to DATE/
    TIMESTAMP in the INSERT statements. None values are preserved as NaN/None.
    """
    import pandas as pd

    out: list[dict] = []
    for r in rows:
        clean: dict[str, Any] = {}
        for k, v in r.items():
            if isinstance(v, datetime):
                clean[k] = v.isoformat(sep=" ")
            elif isinstance(v, date):
                clean[k] = v.isoformat()
            else:
                clean[k] = v
        out.append(clean)
    return pd.DataFrame(out) if out else pd.DataFrame()


def _to_float(x: Any) -> float | None:
    if x is None:
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


_store: Store | None = None


def get_store() -> Store:
    global _store
    if _store is None:
        _store = Store()
    return _store


@contextmanager
def store_scope() -> Iterator[Store]:
    """Context manager that yields a fresh Store and closes it after."""
    s = Store()
    try:
        yield s
    finally:
        s.close()
