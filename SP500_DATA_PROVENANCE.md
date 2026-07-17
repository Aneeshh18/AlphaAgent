# S&P 500 point-in-time universe provenance

## Approved coverage

The repository now has a verified event manifest for the bounded window
**2023-08-01 through 2024-12-31**. It is not a complete 1996-present or
2020-present announcement archive and must not be described that way.

The build has two independent inputs:

1. `data/sp500_ticker_start_end.csv` is a local copy of
   `fja05680/sp500@4aeb5f6046dea43063f9c7be72dfdf16e96d2821`. It supplies the
   2023-08-01 baseline snapshot and an independent list of expected interval
   boundaries. Bulk data under `data/` is intentionally gitignored.
2. `examples/sp500_events_verified_2023-08_to_2024-12.csv` contains 60 audited
   add/delete identifier edges. S&P Global press releases are used for index
   decisions. Issuer announcements are used for same-security ticker changes
   such as ABC→COR, CDAY→DAY, and FLT→CPAY.

The generated local file is
`data/sp500_membership_2023-08_to_2024-12.csv`. It contains 533 bounded
membership intervals. There are 503 members on the baseline date and every
still-active interval ends on 2025-01-01, so a query outside the certified
window returns no members instead of silently extrapolating history.

## Quality checks and findings

- Required dates and provenance are non-null and ISO-formatted.
- `known_date <= effective_date` for every event.
- Event actions are restricted to `Addition` and `Deletion`.
- Index announcements must use an HTTPS `press.spglobal.com` URL; issuer
  ticker-change evidence must also be HTTPS and be labeled
  `issuer_announcement`.
- Composite event keys are unique.
- Every post-baseline event identity reconciles with the independent reference
  spans; missing, extra, inactive-deletion, active-addition, overlap, and
  re-entry errors fail the build.
- A temporary DuckDB import accepted all 533 intervals. Membership counts were
  503 on normal dates, briefly 504/505 during officially staggered spin-offs,
  and zero after 2024-12-31.

Three effective-date conflicts were found in the free reference file. The
official release dates are retained:

| Ticker | Reference date | Official date | Explanation |
|---|---:|---:|---|
| DXC | 2023-10-02 | 2023-10-03 | Veralto entered one day before DXC moved out. |
| VFC | 2024-04-01 | 2024-04-03 | Solventum entered before V.F. moved out. |
| XRAY | 2024-04-02 | 2024-04-03 | GE Vernova entered before Dentsply Sirona moved out. |

These are high-confidence source conflicts, not duplicate rows. The builder
allows a maximum three-day identity match, reports the conflict, and uses the
official event date.

## Why the supplied candidate CSV was not imported

The candidate was incomplete and included material false replacements:

- KVUE replaced AAP, not JNJ.
- UBER, JBL, and BLDR replaced SEE, ALK, and SEDG; EL was not removed.
- SMCI and DECK replaced WHR and ZION; SMCI did not leave in September 2024.
- CRWD, KKR, and GDDY replaced RHI, CMA, and ILMN; BURL was not the deletion.
- PLTR, DELL, and ERIE replaced AAL, ETSY, and BIO; DOW was not removed.
- The December 2024 set also included LII replacing CTLT.

Rows with `effective_start=1900-01-01` and a 2023/2024 `known_date` are invalid:
the removal announcement date cannot serve as the public date for the whole
earlier membership spell. `9999-12-31` is also unnecessary; this project uses
null for genuinely open intervals and an explicit next-day end for bounded
certification.

An SEC 8-K filing date is also not automatically the index `known_date`: it
proves when that filing was public, not that it was the first market notice.
EDGAR full-text search may produce review candidates, but accepted fallback
evidence must retain exact CIK, accession, acceptance timestamp, matched
security, and filing URL. Never manufacture provenance with
`effective_date - 7 days` or any other fixed offset. See
`OPEN_SOURCE_RESEARCH.md` for the full source evaluation.

## Reproduce the build

```bash
PYTHONPATH=src .venv/bin/python -m aios.cli build-universe-membership \
  --baseline-spans data/sp500_ticker_start_end.csv \
  --events examples/sp500_events_verified_2023-08_to_2024-12.csv \
  --output data/sp500_membership_2023-08_to_2024-12.csv \
  --start 2023-08-01 --end 2024-12-31 \
  --baseline-source \
  https://github.com/fja05680/sp500/blob/4aeb5f6046dea43063f9c7be72dfdf16e96d2821/sp500_ticker_start_end.csv

PYTHONPATH=src .venv/bin/python -m aios.cli import-universe \
  data/sp500_membership_2023-08_to_2024-12.csv
PYTHONPATH=src .venv/bin/python -m aios.cli build-security-identities \
  --membership data/sp500_membership_2023-08_to_2024-12.csv \
  --transitions \
  examples/sp500_security_transitions_verified_2023-08_to_2024-12.csv \
  --output data/sp500_security_identities_2023-08_to_2024-12.csv
PYTHONPATH=src .venv/bin/python -m aios.cli import-security-identities \
  data/sp500_security_identities_2023-08_to_2024-12.csv
PYTHONPATH=src .venv/bin/python -m aios.cli import-reference-identities \
  --issuer-ciks examples/sp500_issuer_cik_history_verified.csv \
  --security-issuers examples/sp500_security_issuer_assignments_verified.csv \
  --provider-symbols examples/sp500_provider_symbol_history_verified.csv
PYTHONPATH=src .venv/bin/python -m aios.cli validate
```

Close the Streamlit dashboard before the import because DuckDB permits one
writing process. Do not run `--universe-id sp500` before 2023-08-01 or after
2024-12-31 with this bounded dataset.

## Stable identity findings

The identity build produces 533 interval assignments and 529 internal security
IDs. Eight intervals belong to four evidence-backed transitions:

| Old ticker | New ticker | Classification | Treatment |
|---|---|---|---|
| ABC | COR | Company name and ticker change | Same security ID |
| CDAY | DAY | Company name and ticker change; CUSIP unchanged | Same security ID |
| FLT | CPAY | Company name and ticker change | Same security ID |
| PEAK | DOC | Healthpeak survived the merger and changed ticker | Same Healthpeak security ID |

This does not mean every issuer event preserves identity. The DOC ticker before
2024-03-04 belonged to Physicians Realty Trust; those old-DOC prices must not
be treated as Healthpeak prices. WRK and SW also have different IDs because
WestRock shares were converted into shares of the new Smurfit Westrock parent
plus cash. All other identities are marked `bounded_ticker` pending stronger
issuer, share-class, and provider-symbol provenance.

The main database has zero missing IDs, orphans, membership/identity
mismatches, future-known identity rows, or overlapping ticker assignments.
After the first identity-aware ingest, the member-level coverage audit found
24/503 names with both prices and PIT fundamentals on 2023-09-29 and 26/504 on
2024-09-30. This remains the next hard data constraint.

### Completed controlled identity expansion

The controlled corporate-action batch is complete. It adds six issuer/CIK
intervals, six dated security-owner intervals, and eight yfinance symbol
intervals across Cencora, Dayforce, Healthpeak, Corpay, WestRock, and Smurfit
Westrock. The storage and factor layers now keep issuer, security, market
ticker, and provider symbol separate.

The reviewed provider results are intentionally asymmetric:

- Cencora and Corpay have continuous provider histories relabeled to ABC/COR
  and FLT/CPAY according to date;
- Healthpeak `DOC` begins on 2024-03-04, with all earlier `DOC` rows blocked as
  the wrong security;
- Smurfit Westrock `SW` begins on 2024-07-08, with all earlier `SW` rows blocked
  as the wrong security; and
- DAY and retired WRK history are marked unavailable rather than silently
  replaced with another symbol/provider.

The importer is atomic and the validator checks reference orphans, interval
overlaps, provider-symbol reuse, tagged rows outside provenance, wrong dated
ticker labels, and identity contamination. This pattern must be repeated in
reviewed batches instead of launching one unaudited gap-list ingest.

## Verified local import and smoke run

On 2026-07-16, the generated CSV was imported into the main local database:

- 533 membership intervals were upserted;
- every active fundamental, price, macro, and universe integrity check passed;
- four older failed-ingest audit records and one older zero-row ingest remain
  warnings visible through `aios audit`; and
- the identity-aware batch brought the local database to 28 securities,
  165,034 price rows, and 65,288 fundamental rows. It added 2,950 Cencora,
  1,173 Dayforce, 2,281 Healthpeak, 2,431 Corpay, 987 WestRock, and 271 Smurfit
  Westrock issuer-tagged fundamental rows.

The new verified price rows include 358 Cencora rows, 358 Corpay rows, 210
post-merger Healthpeak rows, and 124 post-combination Smurfit Westrock rows.
Validation found zero pre-2024-03-04 old-DOC rows tagged to Healthpeak and zero
pre-2024-07-08 SW rows tagged to Smurfit Westrock.

The bounded policy regression remains the original 22-company slice so its
result is directly comparable after the identity migration:

```bash
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

| Policy | Periods | Net cumulative | Gross cumulative | Annualized | Volatility | Modeled costs | Turnover |
|---|---:|---:|---:|---:|---:|---:|---:|
| Regime-aware QV | 5 | 44.99% | 46.39% | 34.66% | 10.07% | $1,266.04 | 10.39x |
| Fixed 60/40 QV | 5 | 44.99% | 46.39% | 34.66% | 10.07% | $1,266.04 | 10.39x |
| SPY adjusted close | 5 | 41.49% | 41.49% | 32.04% | 7.66% | $0.00 | N/A |

Tax rates were deliberately zero because no investor jurisdiction was supplied.
Only 11-12 names passed factor coverage at each decision. The two policies
selected the same equal-weight top-10 set in all five periods, even where rank
order differed, so their returns are identical. The result validates PIT
membership gating, release-aware regimes, execution costs, and benchmark
plumbing; it does **not** validate the regime tilts or an investable S&P 500
strategy. Drawdown and win rate currently use quarterly observations, not a
daily portfolio equity curve.

## Remaining limitations

- Exact announcement provenance before August 2023 is not complete.
- The historical reference is community-maintained and its own README warns
  that early rows may be incomplete. Official events override it only inside
  the certified window.
- Ticker changes are identifier transitions, not true index additions. A
  bounded stable security ID exists for every interval. Issuer/CIK and provider
  histories are reviewed for only the first six issuers and must be expanded;
  share-class identity still needs stronger global identifiers for long
  histories.
- The local factor database covers only 24 members on 2023-09-29 and 26 on
  2024-09-30, not all 500+ historical members. A run can test software
  behavior, but it is not yet an investable S&P 500 constituent backtest.
- Free Yahoo/Stooq history is not reliable enough for every delisted or renamed
  security. A licensed survivorship-bias-free price source remains necessary
  for institutional-grade long-history results. Norgate is a credible paid
  candidate, but its Python integration is Windows-only and it does not provide
  announcement dates; no paid dependency or subscription has been added.

## Primary evidence examples

- [Kenvue / Advance Auto Parts](https://press.spglobal.com/2023-08-21-Kenvue-Set-to-Join-S-P-500-Advance-Auto-Parts-to-Join-S-P-SmallCap-600)
- [Uber / Jabil / Builders FirstSource](https://press.spglobal.com/2023-12-01-Uber-Technologies%2C-Jabil-and-Builders-FirstSource-Set-to-Join-S-P-500-Others-to-Join-S-P-MidCap-400-and-S-P-SmallCap-600)
- [Super Micro / Deckers](https://press.spglobal.com/2024-03-01-Super-Micro-Computer-and-Deckers-Outdoor-Set-to-Join-S-P-500-Others-to-Join-S-P-100%2C-S-P-MidCap-400-and-S-P-SmallCap-600)
- [KKR / CrowdStrike / GoDaddy](https://press.spglobal.com/2024-06-07-KKR%2C-CrowdStrike-Holdings-and-GoDaddy-Set-to-Join-S-P-500-Others-to-Join-S-P-MidCap-400-and-S-P-SmallCap-600)
- [Palantir / Dell / Erie](https://press.spglobal.com/2024-09-06-Palantir-Technologies%2C-Dell-Technologies%2C-and-Erie-Indemnity-Set-to-Join-S-P-500-Others-to-Join-S-P-MidCap-400-and-S-P-SmallCap-600)
- [Apollo / Workday](https://press.spglobal.com/2024-12-06-Apollo-Global-Management-and-Workday-Set-to-Join-S-P-500-Others-to-Join-S-P-MidCap-400-and-S-P-SmallCap-600)
- [Lennox / Catalent](https://press.spglobal.com/2024-12-18-Lennox-International-Set-to-Join-S-P-500-and-BILL-Holdings-to-Join-S-P-MidCap-400)
