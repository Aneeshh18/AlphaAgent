"""Decision-scoped batch facade for interactive factor research.

The certified factor engine deliberately keeps its scalar Store contract. This
facade preserves that contract while serving each scalar lookup from one
identity-safe universe batch. It is created for one dashboard calculation and
discarded immediately afterwards; it never persists factor data or changes the
frozen scoring policy used by the active forward trial.
"""

from __future__ import annotations

from typing import Any

from structlog import get_logger

from aios.storage.store import Store

log = get_logger(__name__)


class DecisionScopedFactorStore:
    """Serve ``compute_composite`` from validated decision-scoped batches.

    This facade is intentionally not a general Store replacement. Dashboard
    Research creates it for one calculation; governed workflows keep using the
    scalar Store directly.
    """

    def __init__(self, store: Store, tickers: list[str]) -> None:
        self._store = store
        self._tickers = tuple(sorted({ticker.upper() for ticker in tickers}))
        self._ticker_set = set(self._tickers)
        self._fundamentals: dict[tuple[str, tuple[str, ...]], dict[str, list[dict]]] = {}
        self._fundamental_failures: set[tuple[str, tuple[str, ...]]] = set()
        self._latest_prices: dict[str, dict[str, dict | None]] = {}
        self._latest_price_failures: set[str] = set()
        self._price_histories: dict[tuple[str, int], dict[str, list[dict]]] = {}
        self._price_history_failures: set[tuple[str, int]] = set()

    def __getattr__(self, name: str) -> Any:
        """Delegate every non-factor operation to the bounded underlying Store."""
        return getattr(self._store, name)

    def _validate_keys(self, label: str, rows: dict[str, Any]) -> None:
        actual = set(rows)
        if actual != self._ticker_set:
            missing = sorted(self._ticker_set - actual)
            unexpected = sorted(actual - self._ticker_set)
            raise ValueError(
                f"{label} returned an invalid ticker set; "
                f"missing={missing[:5]}, unexpected={unexpected[:5]}"
            )

    def pit_factor_fundamentals(
        self,
        ticker: str,
        as_of: str,
        metrics: list[str],
    ) -> list[dict]:
        """Match Store.pit_factor_fundamentals using one universe read per date."""
        normalized_ticker = ticker.upper()
        normalized_metrics = tuple(
            sorted({metric.strip() for metric in metrics if metric.strip()})
        )
        if normalized_ticker not in self._ticker_set:
            return self._store.pit_factor_fundamentals(ticker, as_of, metrics)

        key = (str(as_of), normalized_metrics)
        if key not in self._fundamentals and key not in self._fundamental_failures:
            try:
                snapshots = self._store.pit_factor_fundamentals_batch(
                    list(self._tickers),
                    as_of,
                    list(normalized_metrics),
                )
                self._validate_keys("factor fundamentals batch", snapshots)
                self._fundamentals[key] = snapshots
            except Exception as exc:
                self._fundamental_failures.add(key)
                log.warning(
                    "factor_batch.fundamental_scalar_fallback",
                    as_of=str(as_of),
                    error=str(exc),
                )

        snapshots = self._fundamentals.get(key)
        if snapshots is None:
            return self._store.pit_factor_fundamentals(ticker, as_of, metrics)
        return snapshots[normalized_ticker]

    def latest_price(self, ticker: str, as_of: str) -> dict | None:
        """Return factor-compatible latest-price evidence from one universe read."""
        normalized_ticker = ticker.upper()
        decision_date = str(as_of)
        if normalized_ticker not in self._ticker_set:
            return self._store.latest_price(ticker, as_of)

        if (
            decision_date not in self._latest_prices
            and decision_date not in self._latest_price_failures
        ):
            try:
                prices = self._store.pit_factor_latest_prices_batch(
                    list(self._tickers),
                    as_of,
                )
                self._validate_keys("factor latest-price batch", prices)
                self._latest_prices[decision_date] = prices
            except Exception as exc:
                self._latest_price_failures.add(decision_date)
                log.warning(
                    "factor_batch.latest_price_scalar_fallback",
                    as_of=decision_date,
                    error=str(exc),
                )

        prices = self._latest_prices.get(decision_date)
        if prices is None:
            return self._store.latest_price(ticker, as_of)
        return prices[normalized_ticker]

    def pit_factor_price_history(
        self,
        ticker: str,
        as_of: str,
        *,
        observations: int,
    ) -> list[dict]:
        """Match Store.pit_factor_price_history with one universe price window."""
        normalized_ticker = ticker.upper()
        key = (str(as_of), observations)
        if normalized_ticker not in self._ticker_set:
            return self._store.pit_factor_price_history(
                ticker,
                as_of,
                observations=observations,
            )

        if key not in self._price_histories and key not in self._price_history_failures:
            try:
                histories = self._store.pit_factor_price_histories_batch(
                    list(self._tickers),
                    as_of,
                    observations=observations,
                )
                self._validate_keys("factor price-history batch", histories)
                self._price_histories[key] = histories
            except Exception as exc:
                self._price_history_failures.add(key)
                log.warning(
                    "factor_batch.price_history_scalar_fallback",
                    as_of=str(as_of),
                    observations=observations,
                    error=str(exc),
                )

        histories = self._price_histories.get(key)
        if histories is None:
            return self._store.pit_factor_price_history(
                ticker,
                as_of,
                observations=observations,
            )
        return histories[normalized_ticker]
