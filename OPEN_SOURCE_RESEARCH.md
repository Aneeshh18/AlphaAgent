# Open-source research evaluation

Last reviewed: 2026-07-16. This file records whether externally suggested
projects actually fit AIOS. A popular repository or plausible code sample is
not evidence by itself; primary documentation, PIT behavior, licensing, and
our identity model all have to agree.

## Decision summary

| Candidate | Decision | Practical use here |
|---|---|---|
| Norgate Data Platinum/Diamond | Optional paid pilot | Effective membership, delisted prices, and an independent survivorship-bias check on Windows |
| SEC EDGAR full-text search | Discovery/corroboration only | Find candidate issuer filings, then verify the exact accession and earlier announcement evidence |
| `effective_date - 7 days` | Reject | An inferred offset is not a public timestamp and cannot become `known_date` |
| S&P DJI Index Announcements RSS | Monitor later | Forward announcement discovery, followed by document parsing and archival |
| `rheitner/S-P-500-Additions-and-Removals` | Reject | GitHub API returned 404; the advertised CSV could not be verified |
| OpenBB Platform | Evaluate as an optional adapter | SEC PIT cross-check and possibly normalized secondary ingestion |
| Zipline-Reloaded | Cross-check later | Independent event/execution-engine comparison after data coverage is trustworthy |
| VectorBT | Optional cost/gross-return cross-check | Fast fee/slippage simulation; not a replacement for tax-lot accounting |
| Alphalens-Reloaded | Best later addition | Factor IC, quantile spread, turnover, and decay diagnostics after breadth improves |
| dbt-duckdb | Defer | Useful when transformation DAG complexity justifies another runtime/toolchain |
| Partitioned Parquet | Defer pending measurement | Current local tables are too small to justify a second storage layout |

No dependency was added from this review. Replacing working, tested code merely
to use a framework would increase migration risk without fixing the current
constraint: survivorship-safe data coverage.

## Norgate Data

Norgate is the strongest paid candidate reviewed so far for historical US
membership and delisted daily prices. Its official
[package table](https://norgatedata.com/stockmarketpackages.php) includes both
features at Platinum and Diamond level. On 2026-07-16 the listed annual US
prices were USD 630 and USD 787.50 respectively, approximately USD 52.50 and
USD 65.63 per month. The suggested USD 90–150 monthly estimate was therefore
too high for these base packages.

It is not a drop-in replacement for AIOS:

- Norgate's [FAQ](https://norgatedata.com/data-package-faq.php) says it does
  not provide raw constituent, addition, or removal lists. Its plugins answer
  whether a security was a member on a given date.
- The official [accessibility table](https://norgatedata.com/accessibility.php)
  labels both Python and Zipline integrations as Windows-only. AIOS currently
  runs on Linux.
- Membership truth identifies the effective investable universe, but does not
  supply the separately sourced public announcement timestamp required for
  `known_date`.
- Its latest fundamentals are not historical PIT fundamentals; SEC filing
  vintages remain necessary.

Verdict: retain the free-source architecture now. If the user later authorizes
paid data and provides a Windows runner, run a bounded Platinum trial as a
read-only comparison/export adapter. Compare all 533 certified intervals,
delisted prices, corporate actions, and ticker transitions before considering
it authoritative. A subscription would reduce effective-membership and price
plumbing, but would not remove the S&P announcement archive or AIOS identity
tables.

## SEC full-text search and announcement dates

The official [EDGAR full-text search](https://www.sec.gov/edgar/search/index.html)
is useful for finding phrases in filings since 2001. It is not a canonical S&P
announcement feed. The SEC's
[developer page](https://www.sec.gov/about/developer-resources) documents
REST APIs for company submissions and extracted XBRL data; the suggested
`efts.sec.gov/LATEST/search-index` route is the search application's internal
endpoint, not a documented stable data contract there.

A live request with the proposed phrase and date range returned HTTP 200 and 69
hits on 2026-07-16, so it can currently support discovery. It does not produce
69 normalized additions. The first five hits already contained two separate
FactSet filings, and display values such as
`UNIVERSAL HEALTH SERVICES INC (UHS) (CIK ...)`. Splitting that string at the
first space yields `UNIVERSAL`, not ticker `UHS`. The response does expose
`adsh`, which should be retained as the exact accession identifier.

More importantly, an issuer's 8-K filing date proves when that filing became
public. It does not prove that it was the market's first notice of the index
change. The S&P release or an earlier issuer release may precede it, some
issuers may not file the exact phrase or an 8-K at all, and the phrase can
appear in unrelated or retrospective text. The sample also parses the first
word of `display_names` as a ticker and links to a generic CIK search page;
both lose the exact security/accession identity needed for audit.

AIOS may eventually use EDGAR full-text search to generate review candidates,
but an accepted row must retain exact CIK, accession, filing acceptance
timestamp, matched security, quoted context hash, and direct filing URL. Such
evidence is classified as `issuer_filing_fallback` and must be compared with
S&P and issuer announcements. It cannot silently overwrite canonical
`known_date`.

The fixed seven-calendar-day shortcut is prohibited. S&P's current
[U.S. Indices Methodology](https://www.spglobal.com/spdji/en/documents/methodologies/methodology-sp-us-indices.pdf)
says additions and deletions normally receive at least three business days'
notice and that less notice may be given at the Index Committee's discretion.
Changes are made as needed, so a guessed seven-calendar-day date has no
publication artifact. AIOS source priority is:

1. timestamped S&P announcement/document for an index decision;
2. timestamped issuer release for a same-security ticker event;
3. an exact SEC filing only as explicitly labeled fallback evidence; and
4. never an offset inferred from `effective_date`.

## S&P announcement sources

S&P DJI's official [RSS directory](https://www.spglobal.com/spdji/en/rss/)
does list an **Index Announcements** channel. That makes it useful for future
monitoring. The supplied direct URL,
`/rss/indices-news-announcements.xml`, returned HTTP 403 on 2026-07-16, and the
current directory links to a protected `rss-details` route. Therefore it is
not yet a reliable unattended ingest endpoint in this environment.

Even when accessible, an RSS item is discovery evidence, not the complete
membership event. AIOS must archive the linked S&P document and parse both its
publication timestamp and effective date. The effective interval and
`known_date` must never be collapsed into one field.

The suggested GitHub repository
[`rheitner/S-P-500-Additions-and-Removals`](https://github.com/rheitner/S-P-500-Additions-and-Removals)
and raw `sp500_changes.csv` were not discoverable, and GitHub's repository API
returned 404. Do not add this source unless a valid repository, immutable
commit, license, and actual schema are supplied and reviewed.

## OpenBB Platform

This suggestion contains a genuinely useful update. OpenBB's current official
[SEC income-statement reference](https://docs.openbb.co/odp/python/reference/equity/fundamental/income)
exposes `pit_mode`, `filing_date`, `accepted_date`, and CIK. In SEC mode,
`pit_mode=True` preserves the filing vintage rather than silently replacing it
with later comparative restatements.

OpenBB still should not replace the current ingestion layer now:

- our CIK history resolves an issuer before a provider call; a current ticker
  lookup alone cannot safely handle retired/reused symbols;
- our SEC parser stores concept-level filing vintages and derives single-
  quarter values needed by TTM logic;
- our price adapter enforces provider-symbol cutoffs before storage; a generic
  `historical(symbol=...)` call does not know that old DOC belongs to another
  security; and
- provider defaults and normalized fields can change, so provenance must still
  retain the exact provider and raw identity.

Recommended future spike: install OpenBB in an isolated branch, pin
`provider="sec"` and `pit_mode=True`, compare 20 varied issuers against our SEC
rows, and reconcile filing date, accepted timestamp, units, restatements, and
quarter values. Adopt it only if the comparison lowers maintenance without
losing any PIT or identity field.

## Zipline-Reloaded

Zipline is a credible independent event-driven simulator. It can help test
trading calendars, order timing, commissions, and slippage. However,
`data.current()` being simulation-clock-aware does not automatically make a
custom fundamentals database PIT-correct. Zipline's own release notes describe
custom Pipeline data loaders; the loader still has to present only values
whose filing availability precedes the simulation time.

Moving now would require a custom bundle, asset database, corporate-action
data, and fundamentals loader, while discarding a backtest whose current
mechanics already have focused tests. Revisit Zipline as an independent
cross-engine verification after full input coverage, not as a shortcut around
the identity/PIT work.

## VectorBT

The official [Portfolio API](https://vectorbt.dev/api/portfolio/base/) supports
percentage fees, fixed fees, slippage, order records, trades, positions, and
cash sharing. It is useful for fast parameter sweeps or an independent check of
gross returns and basic execution costs.

The claim that open-source VectorBT natively tracks FIFO short/long-term tax
lots is unsupported: its official Portfolio documentation has no tax model.
AIOS's tax-lot code therefore remains necessary. Passing Zipline executions
into VectorBT would also create two portfolio state machines and reconciliation
work, not remove it.

## Alphalens-Reloaded

[Alphalens-Reloaded](https://github.com/stefan-jansen/alphalens-reloaded) is the
strongest proposed addition. It directly provides returns, Information
Coefficient, turnover, grouped, and factor tear-sheet analysis. This fills a
real future gap: testing whether Quality/Value signals predict forward returns
before trusting a portfolio backtest.

Do not run it on the current 26-name/short-window coverage and call the charts
institutional evidence. Add it after the certified universe has materially
broader PIT factor and forward-price coverage, with sector grouping and
survivorship-safe inputs supplied by AIOS.

## dbt-duckdb and Parquet

The official [`dbt-duckdb`](https://github.com/duckdb/dbt-duckdb) project is a
good SQL/Python transformation DAG tool. It does not replace source fetching,
ingest audit records, schema migrations, PIT source validation, or runtime
identity resolution. At the current scale, introducing dbt would duplicate a
small, tested Python orchestration layer.

Likewise, partitioned Parquet can help at tens of millions of rows or for an
immutable data lake. The current database has about 165k prices and 65k
fundamental rows, so DuckDB is not the bottleneck. Benchmark first; add Parquet
only when measured query/backup/portability needs justify dual storage.

## Adoption rule

For any future candidate:

1. verify the repository/API and license from a primary source;
2. identify the exact failure or workload it solves;
3. prove that release dates, issuer/security IDs, and provider intervals survive;
4. compare output against deterministic fixtures and the existing engine;
5. add it behind an adapter before considering replacement; and
6. remove old code only after parity, migration, and rollback tests pass.
