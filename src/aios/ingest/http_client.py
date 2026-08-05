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
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
from structlog import get_logger
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from aios.config import settings

if TYPE_CHECKING:
    from aios.storage.store import Store

log = get_logger(__name__)

_SECRET_QUERY_KEYS = {
    "api_key",
    "apikey",
    "access_token",
    "authorization",
    "key",
    "secret",
    "sig",
    "signature",
    "token",
}
_MAX_RESPONSE_BYTES = 256 * 1024 * 1024


class ResponseTooLargeError(ValueError):
    """Raised before an external response can exceed the ingest memory boundary."""


@dataclass(frozen=True)
class RawSnapshotContext:
    """Opt-in immutable evidence settings for one external GET response."""

    provider: str
    dataset: str
    store: Store
    ingest_run_id: str | None
    role: str
    adapter_name: str
    adapter_version: str
    parser_version: str
    artifact_kind: str = "exact_response"
    project_root: Path | None = None


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
            headers={
                "User-Agent": settings.sec_user_agent,
                # Provider payloads are captured and compressed locally.  Ask
                # intermediaries for identity bytes so a stale proxy cannot
                # attach a gzip/deflate header to an already-decoded body and
                # make httpx fail before the evidence boundary can inspect it.
                "Accept-Encoding": "identity",
            },
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
        # This is a transport integrity rule, not a caller preference.  The
        # exact decoded provider payload is bounded below and then compressed
        # deterministically by the immutable raw-snapshot layer.
        h["Accept-Encoding"] = "identity"
        with self._client.stream("GET", url, headers=h) as streamed:
            # Handle 429 explicitly: sleep with jitter, then force a retry by
            # raising a transient error that tenacity catches.
            if streamed.status_code == 429:
                sleep_for = random.uniform(2.0, 5.0)
                log.warning(
                    "http.429_throttle",
                    url=_secret_free_url(url),
                    sleep=sleep_for,
                )
                time.sleep(sleep_for)
                raise httpx.HTTPStatusError(
                    "429 Too Many Requests",
                    request=streamed.request,
                    response=streamed,
                )
            # 5xx → retry
            if streamed.status_code >= 500:
                raise httpx.HTTPStatusError(
                    f"{streamed.status_code} server error",
                    request=streamed.request,
                    response=streamed,
                )
            streamed.raise_for_status()
            declared_length = streamed.headers.get("content-length")
            if declared_length is not None:
                try:
                    declared_bytes = int(declared_length)
                except ValueError as exc:
                    raise ResponseTooLargeError("HTTP response Content-Length is invalid") from exc
                if not 0 <= declared_bytes <= _MAX_RESPONSE_BYTES:
                    raise ResponseTooLargeError("HTTP response exceeds the ingest byte limit")
            body = bytearray()
            for chunk in streamed.iter_bytes():
                if len(body) + len(chunk) > _MAX_RESPONSE_BYTES:
                    raise ResponseTooLargeError("HTTP response exceeds the ingest byte limit")
                body.extend(chunk)
            return httpx.Response(
                streamed.status_code,
                headers=streamed.headers,
                content=bytes(body),
                request=streamed.request,
                extensions=streamed.extensions,
            )

    def get_json(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        *,
        raw_snapshot: RawSnapshotContext | None = None,
    ) -> Any:
        requested_at = datetime.now(UTC)
        resp = self._get(url, headers=headers)
        self._capture_response(resp, url, requested_at, raw_snapshot)
        return resp.json()

    def get_text(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        *,
        raw_snapshot: RawSnapshotContext | None = None,
    ) -> str:
        requested_at = datetime.now(UTC)
        resp = self._get(url, headers=headers)
        self._capture_response(resp, url, requested_at, raw_snapshot)
        return resp.text

    def get_bytes(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        *,
        raw_snapshot: RawSnapshotContext | None = None,
    ) -> bytes:
        requested_at = datetime.now(UTC)
        resp = self._get(url, headers=headers)
        self._capture_response(resp, url, requested_at, raw_snapshot)
        return resp.content

    @staticmethod
    def _capture_response(
        response: httpx.Response,
        requested_url: str,
        requested_at: datetime,
        context: RawSnapshotContext | None,
    ) -> None:
        if context is None:
            return
        from aios.raw_snapshots import (
            canonical_request_fingerprint,
            capture_raw_snapshot,
        )

        capture_raw_snapshot(
            response.content,
            provider=context.provider,
            dataset=context.dataset,
            artifact_kind=context.artifact_kind,
            requested_at=requested_at,
            received_at=datetime.now(UTC),
            request_fingerprint=canonical_request_fingerprint(
                {"method": "GET", "url": _secret_free_url(requested_url)}
            ),
            adapter_name=context.adapter_name,
            adapter_version=context.adapter_version,
            parser_version=context.parser_version,
            http_status=response.status_code,
            content_type=response.headers.get("content-type"),
            ingest_run_id=context.ingest_run_id,
            role=context.role,
            store=context.store,
            project_root=context.project_root,
        )


def _secret_free_url(url: str) -> str:
    """Return a stable request description with secret-shaped query values removed."""
    split = urlsplit(url)
    safe_query = urlencode(
        [
            (key, "<redacted>" if key.lower() in _SECRET_QUERY_KEYS else value)
            for key, value in parse_qsl(split.query, keep_blank_values=True)
        ],
        doseq=True,
    )
    return urlunsplit((split.scheme, split.netloc, split.path, safe_query, ""))


# Module-level singleton; import and use. Lazy-init so tests can swap it.
_client: HttpClient | None = None


def get_http() -> HttpClient:
    global _client
    if _client is None:
        _client = HttpClient()
    return _client
