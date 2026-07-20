"""Rate-limited HTTP client shared by all fetchers.

DESIGN
------
Every external data source goes through this single client so we get ONE
place to enforce:
  - a global + per-host request rate (SEC fair-access = 10 req/s, we stay under)
  - the mandatory User-Agent header (SEC requires identification)
  - exponential backoff on transient failures (429, 5xx, network)
  - bounded retries with jitter

This is the backbone of polite, reliable free-tier scraping. Without it, yfinance
breaks and EDGAR 429s you within minutes. Keep it strict.
"""

from __future__ import annotations

import random
import time
from typing import Any

import httpx
from structlog import get_logger
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from aios.config import settings

log = get_logger(__name__)


class RateLimiter:
    """Simple token-bucket-ish sleep-based rate limiter, per host.

    Ensures we never exceed `max_rps` requests per second to a given host.
    Not thread-safe by design — we run single-threaded ingest. If we later go
    async/threaded, swap this for an asyncio.Semaphore + time-based gate.
    """

    def __init__(self, max_rps: float) -> None:
        self.min_interval = 1.0 / max_rps if max_rps > 0 else 0.0
        self._last: dict[str, float] = {}

    def wait(self, host: str) -> None:
        if self.min_interval <= 0:
            return
        last = self._last.get(host, 0.0)
        elapsed = time.monotonic() - last
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
        self._last[host] = time.monotonic()


class HttpClient:
    """Thin wrapper over httpx.Client with rate limiting + retries.

    Usage:
        client = HttpClient()
        data = client.get_json("https://data.sec.gov/...", headers={...})
    """

    # Per-host rate limits. SEC is strict; others we keep polite.
    _HOST_RPS = {
        "data.sec.gov": float(settings.edgar_max_rps),
        "www.sec.gov": float(settings.edgar_max_rps),
        "query1.finance.yahoo.com": 2.0,
        "query2.finance.yahoo.com": 2.0,
        "stooq.com": 2.0,
        "api.tiingo.com": 1.0,
        "fred.stlouisfed.org": 4.0,
        "api.stlouisfed.org": 4.0,
    }
    _DEFAULT_RPS = 2.0

    def __init__(self, timeout: float = 30.0) -> None:
        self._client = httpx.Client(
            timeout=timeout,
            headers={"User-Agent": settings.sec_user_agent},
            follow_redirects=True,
        )
        self._limiters: dict[str, RateLimiter] = {}

    def _limiter_for(self, url: str) -> RateLimiter:
        try:
            host = httpx.URL(url).host
        except Exception:
            host = ""
        if host not in self._limiters:
            rps = self._HOST_RPS.get(host, self._DEFAULT_RPS)
            self._limiters[host] = RateLimiter(rps)
        return self._limiters[host]

    def close(self) -> None:
        self._client.close()

    # ------------------------------------------------------------------
    # Retried, rate-limited fetch
    # ------------------------------------------------------------------
    @retry(
        retry=retry_if_exception_type((httpx.HTTPError, httpx.TimeoutException)),
        stop=stop_after_attempt(4),
        wait=wait_exponential_jitter(initial=1.0, max=30.0),
        reraise=True,
    )
    def _get(self, url: str, headers: dict[str, str] | None = None) -> httpx.Response:
        self._limiter_for(url).wait(httpx.URL(url).host)
        h = {"User-Agent": settings.sec_user_agent}
        if headers:
            h.update(headers)
        resp = self._client.get(url, headers=h)
        # Handle 429 explicitly: sleep with jitter, then force a retry by
        # raising a transient error that tenacity catches.
        if resp.status_code == 429:
            sleep_for = random.uniform(2.0, 5.0)
            log.warning("http.429_throttle", url=url, sleep=sleep_for)
            time.sleep(sleep_for)
            raise httpx.HTTPStatusError(
                "429 Too Many Requests", request=resp.request, response=resp
            )
        # 5xx → retry
        if resp.status_code >= 500:
            raise httpx.HTTPStatusError(
                f"{resp.status_code} server error", request=resp.request, response=resp
            )
        resp.raise_for_status()
        return resp

    def get_json(self, url: str, headers: dict[str, str] | None = None) -> Any:
        resp = self._get(url, headers=headers)
        return resp.json()

    def get_text(self, url: str, headers: dict[str, str] | None = None) -> str:
        return self._get(url, headers=headers).text

    def get_bytes(self, url: str, headers: dict[str, str] | None = None) -> bytes:
        return self._get(url, headers=headers).content


# Module-level singleton; import and use. Lazy-init so tests can swap it.
_client: HttpClient | None = None


def get_http() -> HttpClient:
    global _client
    if _client is None:
        _client = HttpClient()
    return _client
