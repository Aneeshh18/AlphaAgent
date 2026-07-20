from __future__ import annotations

import json
from datetime import date, timedelta
from zipfile import ZipFile

import pytest
from typer.testing import CliRunner

from aios.cli import app
from aios.ingest import reference_batch
from aios.ingest.reference_batch import (
    build_stable_reference_batch,
    build_stable_reference_window_batch,
    ingest_reviewed_reference_batch,
    load_batch_tickers,
    load_batch_windows,
    write_reference_batch,
)
from aios.ingest.reference_identity import (
    load_issuer_cik_csv,
    load_provider_symbol_csv,
    load_security_issuer_csv,
)
from aios.storage.store import Store

EVIDENCE = "https://www.sec.gov/Archives/edgar/data/1/example.htm"
SECURITY_ID = "aios:bounded:demo:tst"


def _setup_stable_security(store: Store) -> None:
    _setup_named_security(store, ticker="TST", security_id=SECURITY_ID)


def _setup_named_security(
    store: Store,
    *,
    ticker: str,
    security_id: str,
    start: str = "2024-01-01",
    end: str = "2024-01-11",
) -> None:
    membership = {
        "universe_id": "demo",
        "ticker": ticker,
        "effective_start": start,
        "effective_end": end,
        "known_date": "2023-12-15",
        "source": EVIDENCE,
    }
    store.upsert_universe_membership([membership])
    store.upsert_security_identities(
        [
            {
                **membership,
                "security_id": security_id,
                "identity_status": "bounded_ticker",
            }
        ]
    )


def _sec_records() -> dict[str, list[dict]]:
    return {"TST": [{"ticker": "TST", "title": "Test Corp", "cik": 1}]}


def _submissions(_cik: int) -> dict:
    return {
        "cik": "1",
        "name": "Test Corporation",
        "tickers": ["TST"],
        "filings": {"recent": {"filingDate": ["2023-12-01", "2024-01-31"]}},
    }


def _prices(_provider: str, symbol: str, start: str, _end: str) -> list[dict]:
    first = date.fromisoformat(start)
    return [
        {
            "ticker": symbol,
            "date": (first + timedelta(days=offset)).isoformat(),
            "close": 100.0 + offset,
            "adj_close": 100.0 + offset,
            "volume": 1_000 + offset,
            "source": "yfinance",
        }
        for offset in range(1, 9)
    ]


def test_stable_reference_batch_builds_importable_manifests(tmp_path):
    ticker_path = tmp_path / "tickers.txt"
    ticker_path.write_text("# stable batch\ntst\n", encoding="utf-8")
    assert load_batch_tickers(ticker_path) == ["TST"]

    store = Store(tmp_path / "build.duckdb")
    try:
        _setup_stable_security(store)
        result = build_stable_reference_batch(
            ["TST"],
            universe_id="demo",
            start="2024-01-01",
            end="2024-01-11",
            verified_date="2024-01-11",
            store=store,
            sec_records=_sec_records(),
            submissions_fetcher=_submissions,
            price_fetcher=_prices,
        )
        assert result["accepted"] == 1
        assert result["rejected"] == 0
        assert result["review_rows"][0]["provider_rows"] == 8
        assert len(result["review_rows"][0]["price_payload_sha256"]) == 64

        paths = write_reference_batch(
            result,
            output_dir=tmp_path / "output",
            batch_name="demo_batch",
        )
        issuers, ciks = load_issuer_cik_csv(paths["issuer_ciks"])
        owners = load_security_issuer_csv(paths["security_issuers"])
        providers = load_provider_symbol_csv(paths["provider_symbols"])
        assert issuers[0]["issuer_id"] == "aios:issuer:sec:0000000001"
        assert ciks[0]["cik"] == "0000000001"
        assert owners[0]["security_id"] == SECURITY_ID
        assert providers[0]["mapping_status"] == "verified"
    finally:
        store.close()


def test_stable_reference_batch_rejects_ambiguous_sec_and_thin_prices(tmp_path):
    store = Store(tmp_path / "reject.duckdb")
    try:
        _setup_stable_security(store)
        ambiguous = build_stable_reference_batch(
            ["TST"],
            universe_id="demo",
            start="2024-01-01",
            end="2024-01-11",
            verified_date="2024-01-11",
            store=store,
            sec_records={"TST": _sec_records()["TST"] * 2},
            submissions_fetcher=_submissions,
            price_fetcher=_prices,
        )
        assert ambiguous["accepted"] == 0
        assert "2 records" in ambiguous["review_rows"][0]["reason"]

        late_identity = _submissions(1)
        late_identity["filings"]["recent"]["filingDate"] = [
            "2024-01-05",
            "2024-01-31",
        ]
        no_historical_continuity = build_stable_reference_batch(
            ["TST"],
            universe_id="demo",
            start="2024-01-01",
            end="2024-01-11",
            verified_date="2024-01-11",
            store=store,
            sec_records=_sec_records(),
            submissions_fetcher=lambda _cik: late_identity,
            price_fetcher=_prices,
        )
        assert no_historical_continuity["accepted"] == 0
        rejection = no_historical_continuity["review_rows"][0]["reason"]
        assert "does not reach" in rejection

        thin = build_stable_reference_batch(
            ["TST"],
            universe_id="demo",
            start="2024-01-01",
            end="2024-01-11",
            verified_date="2024-01-11",
            store=store,
            sec_records=_sec_records(),
            submissions_fetcher=_submissions,
            price_fetcher=lambda *_args: _prices("yfinance", "TST", "2024-01-01", "")[:6],
        )
        assert thin["accepted"] == 0
        assert "incomplete" in thin["review_rows"][0]["reason"]
    finally:
        store.close()


def test_stable_reference_batch_follows_sec_history_shard(tmp_path):
    store = Store(tmp_path / "history-shard.duckdb")
    fetched: list[str] = []
    try:
        _setup_stable_security(store)
        submissions = _submissions(1)
        submissions["filings"] = {
            "recent": {"filingDate": ["2024-01-05", "2024-01-31"]},
            "files": [
                {
                    "name": "CIK0000000001-submissions-001.json",
                    "filingFrom": "2023-01-01",
                    "filingTo": "2024-01-03",
                }
            ],
        }

        def fetch_history(name: str) -> dict:
            fetched.append(name)
            return {"filingDate": ["2023-12-01", "2024-01-03"]}

        result = build_stable_reference_batch(
            ["TST"],
            universe_id="demo",
            start="2024-01-01",
            end="2024-01-11",
            verified_date="2024-01-11",
            store=store,
            sec_records=_sec_records(),
            submissions_fetcher=lambda _cik: submissions,
            submission_file_fetcher=fetch_history,
            price_fetcher=_prices,
        )

        assert result["accepted"] == 1
        assert fetched == ["CIK0000000001-submissions-001.json"]
        review = result["review_rows"][0]
        assert review["sec_history_sources"].endswith("CIK0000000001-submissions-001.json")
    finally:
        store.close()


def test_stable_reference_batch_accepts_only_exact_dot_hyphen_notation(tmp_path):
    store = Store(tmp_path / "share-class.duckdb")
    seen_symbols: list[str] = []
    try:
        _setup_named_security(
            store,
            ticker="TST.B",
            security_id="aios:bounded:demo:tst.b",
        )

        def share_class_submissions(_cik: int) -> dict:
            return {
                "cik": "2",
                "name": "Test Share Class Corporation",
                "tickers": ["TST-B", "TST-A"],
                "filings": {"recent": {"filingDate": ["2023-12-01", "2024-01-31"]}},
            }

        def share_class_prices(provider: str, symbol: str, start: str, end: str):
            seen_symbols.append(symbol)
            return _prices(provider, symbol, start, end)

        result = build_stable_reference_batch(
            ["TST.B"],
            universe_id="demo",
            start="2024-01-01",
            end="2024-01-11",
            verified_date="2024-01-11",
            store=store,
            sec_records={"TST-B": [{"ticker": "TST-B", "title": "Test Corp", "cik": 2}]},
            submissions_fetcher=share_class_submissions,
            price_fetcher=share_class_prices,
        )

        assert result["accepted"] == 1
        assert seen_symbols == ["TST-B"]
        assert result["issuer_rows"][0]["canonical_ticker"] == "TST-B"
        assert result["review_rows"][0]["sec_ticker"] == "TST-B"
        assert result["provider_rows"][0]["provider_symbol"] == "TST-B"
    finally:
        store.close()


def test_stable_reference_batch_preserves_sec_primary_ticker_order(tmp_path):
    store = Store(tmp_path / "primary-ticker-order.duckdb")
    try:
        _setup_stable_security(store)
        submissions = _submissions(1)
        submissions["tickers"] = ["TST", "AAA"]

        result = build_stable_reference_batch(
            ["TST"],
            universe_id="demo",
            start="2024-01-01",
            end="2024-01-11",
            verified_date="2024-01-11",
            store=store,
            sec_records=_sec_records(),
            submissions_fetcher=lambda _cik: submissions,
            price_fetcher=_prices,
        )

        assert result["accepted"] == 1
        assert result["issuer_rows"][0]["canonical_ticker"] == "TST"
    finally:
        store.close()


def test_stable_reference_batch_does_not_treat_former_names_as_tickers(tmp_path):
    store = Store(tmp_path / "former-names.duckdb")
    try:
        _setup_stable_security(store)
        submissions = _submissions(1)
        submissions["tickers"] = []
        submissions["formerNames"] = [{"name": "TST", "from": "2020-01-01", "to": "2024-01-01"}]
        result = build_stable_reference_batch(
            ["TST"],
            universe_id="demo",
            start="2024-01-01",
            end="2024-01-11",
            verified_date="2024-01-11",
            store=store,
            sec_records=_sec_records(),
            submissions_fetcher=lambda _cik: submissions,
            price_fetcher=_prices,
        )

        assert result["accepted"] == 0
        assert "absent" in result["review_rows"][0]["reason"]
    finally:
        store.close()


def test_all_rejected_batch_writes_review_without_refetching_sec(tmp_path):
    def unexpected_submissions(_cik: int) -> dict:
        raise AssertionError("empty injected SEC records must not trigger submissions")

    store = Store(tmp_path / "all_rejected.duckdb")
    try:
        _setup_stable_security(store)
        result = build_stable_reference_batch(
            ["TST"],
            universe_id="demo",
            start="2024-01-01",
            end="2024-01-11",
            verified_date="2024-01-11",
            store=store,
            sec_records={},
            submissions_fetcher=unexpected_submissions,
            price_fetcher=_prices,
        )
        assert result["accepted"] == 0
        assert "0 records" in result["review_rows"][0]["reason"]

        paths = write_reference_batch(
            result,
            output_dir=tmp_path / "review_only",
            batch_name="all_rejected",
        )
        assert set(paths) == {"review"}
        assert "TST" in paths["review"].read_text(encoding="utf-8")
    finally:
        store.close()


def test_ingest_reviewed_batch_imports_then_runs_identity_ingests(monkeypatch, tmp_path):
    store = Store(tmp_path / "ingest.duckdb")
    try:
        _setup_stable_security(store)
        result = build_stable_reference_batch(
            ["TST"],
            universe_id="demo",
            start="2024-01-01",
            end="2024-01-11",
            verified_date="2024-01-11",
            store=store,
            sec_records=_sec_records(),
            submissions_fetcher=_submissions,
            price_fetcher=_prices,
        )
        paths = write_reference_batch(
            result,
            output_dir=tmp_path / "output",
            batch_name="demo_batch",
        )
        archive_path = tmp_path / "companyfacts.zip"
        with ZipFile(archive_path, "w") as archive:
            archive.writestr(
                "CIK0000000001.json",
                json.dumps(
                    {
                        "cik": 1,
                        "entityName": "Test Corporation",
                        "facts": {},
                    }
                ),
            )

        captured_payloads = []

        def fake_ingest_issuer(_issuer_id, *, store, facts_payload):
            assert store is not None
            captured_payloads.append(facts_payload)
            return 12

        monkeypatch.setattr("aios.ingest.edgar.ingest_issuer", fake_ingest_issuer)
        monkeypatch.setattr(
            reference_batch,
            "ingest_security_prices",
            lambda *_a, **_k: 8,
        )
        summary = ingest_reviewed_reference_batch(
            paths["issuer_ciks"],
            paths["security_issuers"],
            paths["provider_symbols"],
            start="2024-01-01",
            end="2024-01-11",
            store=store,
            companyfacts_zip_path=archive_path,
        )
        assert summary["fundamental_rows"] == 12
        assert summary["price_rows"] == 8
        assert summary["failures"] == []
        assert summary["companyfacts_source"] == str(archive_path)
        assert captured_payloads[0]["cik"] == 1
        assert store.issuer_id_for_security(SECURITY_ID, "2024-01-05") == (
            "aios:issuer:sec:0000000001"
        )
    finally:
        store.close()


def test_bulk_archive_is_validated_before_reference_import(tmp_path):
    store = Store(tmp_path / "preflight.duckdb")
    try:
        _setup_stable_security(store)
        result = build_stable_reference_batch(
            ["TST"],
            universe_id="demo",
            start="2024-01-01",
            end="2024-01-11",
            verified_date="2024-01-11",
            store=store,
            sec_records=_sec_records(),
            submissions_fetcher=_submissions,
            price_fetcher=_prices,
        )
        paths = write_reference_batch(
            result,
            output_dir=tmp_path / "preflight-output",
            batch_name="preflight_batch",
        )
        archive_path = tmp_path / "empty-companyfacts.zip"
        with ZipFile(archive_path, "w"):
            pass

        with pytest.raises(ValueError, match="no member for CIK 0000000001"):
            ingest_reviewed_reference_batch(
                paths["issuer_ciks"],
                paths["security_issuers"],
                paths["provider_symbols"],
                start="2024-01-01",
                end="2024-01-11",
                store=store,
                companyfacts_zip_path=archive_path,
            )
        assert store.issuer_reference("aios:issuer:sec:0000000001") is None
    finally:
        store.close()


def test_batch_window_csv_loads_comments_blank_lines_and_mixed_windows(tmp_path):
    path = tmp_path / "windows.csv"
    path.write_text(
        "# Per-ticker certified windows\n"
        "\n"
        "ticker,start,end\n"
        "tst,2024-01-01,2024-01-11\n"
        "  # A whole-line comment is safe between records\n"
        "alt,2024-01-15,2024-01-25\n",
        encoding="utf-8",
    )

    assert load_batch_windows(path) == [
        {
            "ticker": "TST",
            "start": date(2024, 1, 1),
            "end": date(2024, 1, 11),
        },
        {
            "ticker": "ALT",
            "start": date(2024, 1, 15),
            "end": date(2024, 1, 25),
        },
    ]


@pytest.mark.parametrize(
    ("contents", "message"),
    [
        ("ticker,start\nTST,2024-01-01\n", "header must be exactly"),
        (
            "ticker,start,end\nBAD TICKER,2024-01-01,2024-01-11\n",
            "invalid ticker",
        ),
        (
            "ticker,start,end\nTST,not-a-date,2024-01-11\n",
            "invalid start",
        ),
        (
            "ticker,start,end\nTST,2024-01-11,2024-01-01\n",
            "end must follow start",
        ),
        (
            "ticker,start,end\nTST,2024-01-01,2024-01-11\ntst,2024-01-01,2024-01-11\n",
            "duplicate ticker/window",
        ),
        (
            "ticker,start,end\nTST,2024-01-01,2024-01-11\nTST,2024-01-02,2024-01-12\n",
            "conflicting window",
        ),
    ],
)
def test_batch_window_csv_rejects_invalid_duplicate_and_conflicting_rows(
    tmp_path,
    contents,
    message,
):
    path = tmp_path / "invalid-windows.csv"
    path.write_text(contents, encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        load_batch_windows(path)


def test_reference_window_batch_caches_sec_map_and_merges_deterministically(
    monkeypatch,
    tmp_path,
):
    store = Store(tmp_path / "mixed-windows.duckdb")
    sec_fetches = 0
    records = {
        "ALT": [{"ticker": "ALT", "title": "Alternate Corp", "cik": 2}],
        "TST": _sec_records()["TST"],
    }

    def fetch_records():
        nonlocal sec_fetches
        sec_fetches += 1
        return records

    def submissions(cik: int) -> dict:
        ticker = {1: "TST", 2: "ALT"}[cik]
        return {
            "cik": str(cik),
            "name": f"{ticker} Corporation",
            "tickers": [ticker],
            "filings": {"recent": {"filingDate": ["2023-12-01", "2024-02-01"]}},
        }

    windows = [
        {"ticker": "TST", "start": "2024-01-01", "end": "2024-01-11"},
        {"ticker": "ALT", "start": "2024-01-15", "end": "2024-01-25"},
    ]
    monkeypatch.setattr(reference_batch, "fetch_sec_ticker_records", fetch_records)
    try:
        _setup_stable_security(store)
        _setup_named_security(
            store,
            ticker="ALT",
            security_id="aios:bounded:demo:alt",
            start="2024-01-15",
            end="2024-01-25",
        )
        forward = build_stable_reference_window_batch(
            windows,
            universe_id="demo",
            verified_date="2024-02-01",
            store=store,
            submissions_fetcher=submissions,
            price_fetcher=_prices,
        )
        assert sec_fetches == 1

        reverse = build_stable_reference_window_batch(
            reversed(windows),
            universe_id="demo",
            verified_date="2024-02-01",
            store=store,
            submissions_fetcher=submissions,
            price_fetcher=_prices,
        )
        assert sec_fetches == 2
        assert reverse == forward
        assert [row["ticker"] for row in forward["review_rows"]] == ["ALT", "TST"]
        assert {
            row["security_id"]: (row["effective_start"], row["effective_end"])
            for row in forward["owner_rows"]
        } == {
            "aios:bounded:demo:alt": (date(2024, 1, 15), date(2024, 1, 25)),
            SECURITY_ID: (date(2024, 1, 1), date(2024, 1, 11)),
        }
    finally:
        store.close()


def test_reference_window_batch_rejects_cross_result_manifest_conflicts(
    monkeypatch,
):
    def conflicting_result(tickers, **kwargs):
        ticker = next(iter(tickers))
        issuer_id = f"aios:issuer:sec:{1 if ticker == 'AAA' else 2:010d}"
        start = kwargs["start"]
        end = kwargs["end"]
        return {
            "issuer_rows": [
                {
                    "issuer_id": issuer_id,
                    "canonical_name": f"{ticker} Corp",
                    "canonical_ticker": ticker,
                    "cik": issuer_id.rsplit(":", 1)[1],
                    "effective_start": start,
                    "effective_end": end,
                    "verified_date": date(2024, 1, 11),
                    "source": EVIDENCE,
                }
            ],
            "owner_rows": [
                {
                    "security_id": SECURITY_ID,
                    "issuer_id": issuer_id,
                    "effective_start": start,
                    "effective_end": end,
                    "verified_date": date(2024, 1, 11),
                    "source": EVIDENCE,
                }
            ],
            "provider_rows": [
                {
                    "provider": "yfinance",
                    "provider_symbol": ticker,
                    "security_id": SECURITY_ID,
                    "data_start": start,
                    "data_end": end,
                    "mapping_status": "verified",
                    "verified_date": date(2024, 1, 11),
                    "source": EVIDENCE,
                }
            ],
            "review_rows": [{"ticker": ticker, "review_status": "accepted"}],
            "accepted": 1,
            "rejected": 0,
        }

    monkeypatch.setattr(
        reference_batch,
        "build_stable_reference_batch",
        conflicting_result,
    )
    windows = [
        {"ticker": "AAA", "start": "2024-01-01", "end": "2024-01-11"},
        {"ticker": "BBB", "start": "2024-01-01", "end": "2024-01-11"},
    ]

    with pytest.raises(ValueError, match="conflicting security issuer rows"):
        build_stable_reference_window_batch(
            windows,
            universe_id="demo",
            sec_records={},
        )


def test_reference_window_batch_preserves_each_rejection_and_accepted_manifests(
    tmp_path,
):
    store = Store(tmp_path / "mixed-rejection.duckdb")
    try:
        _setup_stable_security(store)
        _setup_named_security(
            store,
            ticker="BAD",
            security_id="aios:bounded:demo:bad",
            start="2024-01-15",
            end="2024-01-25",
        )
        result = build_stable_reference_window_batch(
            [
                {"ticker": "TST", "start": "2024-01-01", "end": "2024-01-11"},
                {"ticker": "BAD", "start": "2024-01-15", "end": "2024-01-25"},
            ],
            universe_id="demo",
            verified_date="2024-02-01",
            store=store,
            sec_records=_sec_records(),
            submissions_fetcher=_submissions,
            price_fetcher=_prices,
        )

        assert result["accepted"] == 1
        assert result["rejected"] == 1
        assert [row["ticker"] for row in result["review_rows"]] == ["BAD", "TST"]
        assert "0 records" in result["review_rows"][0]["reason"]
        assert [row["security_id"] for row in result["owner_rows"]] == [SECURITY_ID]

        paths = write_reference_batch(
            result,
            output_dir=tmp_path / "mixed-output",
            batch_name="mixed_windows",
        )
        assert set(paths) == {
            "issuer_ciks",
            "security_issuers",
            "provider_symbols",
            "review",
        }
        review_csv = paths["review"].read_text(encoding="utf-8")
        assert "BAD" in review_csv
        assert "TST" in review_csv
    finally:
        store.close()


def test_reference_window_cli_writes_manifests_and_exits_nonzero_on_rejection(
    monkeypatch,
    tmp_path,
):
    windows_file = tmp_path / "cli-windows.csv"
    windows_file.write_text(
        "ticker,start,end\nTST,2024-01-01,2024-01-11\nBAD,2024-01-15,2024-01-25\n",
        encoding="utf-8",
    )
    checked_on = date(2024, 2, 1)
    fake_result = {
        "issuer_rows": [
            {
                "issuer_id": "aios:issuer:sec:0000000001",
                "canonical_name": "Test Corporation",
                "canonical_ticker": "TST",
                "cik": "0000000001",
                "effective_start": date(2024, 1, 1),
                "effective_end": date(2024, 1, 11),
                "verified_date": checked_on,
                "source": EVIDENCE,
            }
        ],
        "owner_rows": [
            {
                "security_id": SECURITY_ID,
                "issuer_id": "aios:issuer:sec:0000000001",
                "effective_start": date(2024, 1, 1),
                "effective_end": date(2024, 1, 11),
                "verified_date": checked_on,
                "source": EVIDENCE,
            }
        ],
        "provider_rows": [
            {
                "provider": "yfinance",
                "provider_symbol": "TST",
                "security_id": SECURITY_ID,
                "data_start": date(2024, 1, 1),
                "data_end": date(2024, 1, 11),
                "mapping_status": "verified",
                "verified_date": checked_on,
                "source": "https://query1.finance.yahoo.com/v8/finance/chart/TST",
            }
        ],
        "review_rows": [
            {
                "ticker": "BAD",
                "provider": "yfinance",
                "review_status": "rejected",
                "reason": "manual review required",
                "verified_date": checked_on,
            },
            {
                "ticker": "TST",
                "provider": "yfinance",
                "review_status": "accepted",
                "reason": "",
                "verified_date": checked_on,
            },
        ],
        "accepted": 1,
        "rejected": 1,
    }
    captured_windows = []

    def fake_builder(windows, **_kwargs):
        captured_windows.extend(windows)
        return fake_result

    monkeypatch.setattr(
        reference_batch,
        "build_stable_reference_window_batch",
        fake_builder,
    )
    output_dir = tmp_path / "cli-output"
    result = CliRunner().invoke(
        app,
        [
            "build-reference-window-batch",
            str(windows_file),
            "--batch-name",
            "cli_windows",
            "--output-dir",
            str(output_dir),
            "--verified-date",
            "2024-02-01",
        ],
    )

    assert result.exit_code == 1, result.output
    assert "1 accepted, 1 rejected" in result.output
    assert {row["ticker"] for row in captured_windows} == {"BAD", "TST"}
    assert {path.name for path in output_dir.iterdir()} == {
        "cli_windows_issuer_ciks.csv",
        "cli_windows_security_issuers.csv",
        "cli_windows_provider_symbols.csv",
        "cli_windows_review.csv",
    }
