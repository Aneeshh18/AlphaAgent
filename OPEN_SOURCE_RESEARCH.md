# Open-source research evaluation

Last reviewed: 2026-07-17. This file records whether externally suggested
projects actually fit AIOS. A popular repository or plausible code sample is
not evidence by itself; primary documentation, PIT behavior, licensing, and
our identity model all have to agree.

## Decision summary

| Candidate | Decision | Practical use here |
|---|---|---|
| Norgate Data Platinum/Diamond | Optional paid pilot | Effective membership, delisted prices, and an independent survivorship-bias check on Windows |
| SEC EDGAR full-text search | Discovery/corroboration only | Find candidate issuer filings, then verify the exact accession and earlier announcement evidence |
| SEC submissions `filings.files` | Adopted | Follow SEC-named older shards when the current recent block does not bracket the certified window |
| SEC submissions `formerNames` | Reject as ticker history | These are issuer-name changes, not a dated ticker/security mapping |
| SEC bulk ZIP archives | Optional local reader implemented | Reuse one official nightly Company Facts archive across a large reviewed batch without weakening CIK validation |
| Tiingo EOD | Optional adapter implemented | Explicit token-header provider for reviewed symbols; configuration alone never certifies coverage |
| Wayback/Wikipedia snapshots | Secondary cross-check only | A capture timestamp is not the page's publication timestamp and cannot become `known_date` |
| `effective_date - 7 days` | Reject | An inferred offset is not a public timestamp and cannot become `known_date` |
| S&P DJI Index Announcements RSS | Monitor later | Forward announcement discovery, followed by document parsing and archival |
| `rheitner/S-P-500-Additions-and-Removals` | Reject | GitHub API returned 404; the advertised CSV could not be verified |
| OpenBB Platform | Evaluate as an optional adapter | SEC PIT cross-check and possibly normalized secondary ingestion |
| Zipline-Reloaded | Cross-check later | Independent event/execution-engine comparison after data coverage is trustworthy |
| VectorBT | Optional cost/gross-return cross-check | Fast fee/slippage simulation; not a replacement for tax-lot accounting |
| Alphalens-Reloaded | Best later addition | Factor IC, quantile spread, turnover, and decay diagnostics after breadth improves |
| dbt-duckdb | Defer | Useful when transformation DAG complexity justifies another runtime/toolchain |
| Partitioned Parquet | Defer pending measurement | Current local tables are too small to justify a second storage layout |

No third-party package dependency was added from this review. Direct Tiingo
HTTP support, SEC submissions-shard traversal, and a standard-library reader
for selected CIKs inside the official Company Facts ZIP sit behind the existing
identity/provenance contracts. Replacing working, tested code merely to use a
framework would increase migration risk without fixing the current constraint:
survivorship-safe data coverage.

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

## SEC identity metadata, history shards, and bulk archives

The official [SEC EDGAR API documentation](https://www.sec.gov/search-filings/edgar-application-programming-interfaces)
confirms three distinct capabilities that must not be conflated:

- the top-level submissions metadata includes current/former **company names**
  and current ticker/exchange metadata;
- `filings.files` names additional JSON files containing older submission
  history for large filers; and
- nightly bulk submissions and Company Facts ZIP archives are available.

The history-shard capability directly fixed a real false rejection: Citi's
current `recent` block did not reach 2023-08-01, while the SEC-named older shard
did. The batch builder now validates shard filenames, fetches only enough of
them to prove the missing boundary, fingerprints the combined evidence, and
retains each shard URL in the review CSV.

`formerNames` is not a former-ticker table. Using an issuer name as a market
symbol would join the wrong identity domain and is covered by a rejection test.
Likewise, punctuation normalization is intentionally limited to one exact
market-dot/SEC-hyphen transform, such as BF.B↔BF-B; it is never fuzzy matching.

The Submissions endpoint is keyed by a ten-digit CIK, so it cannot by itself
solve an unknown ticker→CIK lookup. A live check made the lifecycle limitation
concrete: Berkshire and Brown-Forman currently expose `BRK-B` and `BF-B`, while
post-acquisition ANSYS exposes no ticker. Current ticker arrays corroborate a
candidate identity; they are not dated ticker-owner histories.

The bulk ZIP is a scale optimization, not a correctness shortcut. For a large
reviewed batch, `aios ingest-reference-batch --companyfacts-zip PATH` opens one
local official archive and reads only the members named by the manifest's
reviewed CIKs. It refuses a missing/duplicate member, invalid JSON, absent
`facts`, or an embedded CIK mismatch before that payload can reach storage.
Submissions metadata is still fetched separately because Company Facts does not
contain the same issuer/exchange metadata. Small or freshness-sensitive runs
should keep the default real-time per-CIK API path; the archive is republished
nightly and is intentionally not downloaded automatically.

## Tiingo and archived-web suggestions

Tiingo's official [EOD documentation](https://www.tiingo.com/documentation/end-of-day)
and [symbol notes](https://www.tiingo.com/documentation/appendix/symbology)
make it a useful optional source, including some delisted symbols. Its own
documentation also warns that not every old or recycled symbol can be resolved
without ambiguity. AIOS therefore treats Tiingo exactly like other providers:
one explicit symbol, security ID, status, and half-open date range. The token
is sent in an authorization header and never in the URL. A configured token is
not evidence of coverage: returned rows must still pass the complete bounded
date, uniqueness, positive-close, and security-relabel checks. That standard
was met for ANSS, CTRA, and DFS: each returned 358 sessions for the certified
window, so Exception Batch 02 is the first versioned Tiingo-backed manifest.

Archived Wikipedia constituent pages can help discover discrepancies, but
they remain community-authored snapshots. More importantly, a Wayback capture
timestamp proves when the archive fetched a page—not when S&P published an
announcement or when the market first knew it. It cannot populate
`known_date`. Use archived pages only to generate candidates that are then
verified against an S&P release, issuer release, or explicitly labeled filing
fallback.

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
immutable data lake. The current database has about 199k prices and 289k
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
