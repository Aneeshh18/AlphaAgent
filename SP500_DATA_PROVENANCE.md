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
2024-09-30. Batch 02 subsequently increased those results to 49/503 and 51/504.
Batch 03 increased them again to 73/503 and 75/504. Batch 04 increased them to
92/503 and 94/504. The reviewed exception batch raised them to 98/503 and
100/504, Batch 05 raised them to 118/503 and 120/504, Batch 06 raised them to
140/503 and 142/504, and Exception Batch 02 raised them to 143/503 and 145/504.
Batches 07–13 plus Exception Batches 03–05 then raised the live checkpoint to
311/503 and 313/504. Completed Exception Batch 06 and Batch 14 raised it again
to 336/503 and 338/504. Batch 15 plus Exception Batch 07 raised it to 359/503
and 361/504. Batch 16 raised it to 384/503 and 386/504, and Batch 17 raised it
again to 409/503 and 411/504. Batches 18–20, Window Batch 01, and Exception
Batches 08–10 completed the bounded identity review and raised current coverage
to 501/503 and 503/504. The only remaining dated gaps are PEAK/WRK historical
provider data and AMTM fundamentals before its first public Company Facts
availability on 2024-12-17.

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

### Completed stable reference batch 02

On 2026-07-17, the conservative batch builder reviewed 25 unchanged
full-window candidates: ABBV, ABT, ACN, ADBE, ADI, ADM, ADP, ADSK, AEE, AEP,
AES, AKAM, ALB, ALGN, ALLE, AMAT, AMD, AME, AMGN, AMT, ANET, AOS, APD, APH,
and AWK. All 25 were accepted; none was rejected.

Acceptance required all of the following:

- exactly one security/ticker assignment covering the full certified window;
- exactly one current SEC ticker-map record and the same CIK/ticker pair in the
  issuer's official SEC submissions payload, whose recent filing dates bracket
  the certified window;
- unique provider dates, positive closes, minimum history density, and first
  and last observations within seven calendar days of the requested edges; and
- exactly one valid dated market-ticker relabel for every provider row.

The versioned artifacts are
`examples/sp500_reference_batch_02_issuer_ciks.csv`,
`examples/sp500_reference_batch_02_security_issuers.csv`,
`examples/sp500_reference_batch_02_provider_symbols.csv`, and
`examples/sp500_reference_batch_02_review.csv`. The review file preserves one
SEC-submissions SHA-256 and one provider-sample SHA-256 per candidate so later
source drift is detectable.

The live ingest added 65,836 issuer-tagged fundamental rows and 8,950
security/provider-tagged price rows. Each accepted security has 358 unique
sessions from 2023-08-01 through 2024-12-31, with no duplicate price keys. The
database at that checkpoint contained 31 issuers, 31 CIK intervals, 31 owner
intervals, 33 provider intervals, 53 securities, 173,984 price rows, and 131,124
fundamental rows. All hard validation checks passed; the four historical
failed-ingest and one historical zero-row audit entries remained warnings
rather than being erased.

A same-day recheck under the stricter SEC filing-continuity and 95%-of-weekdays
provider-density gates again accepted 25/25. The three import manifests were
semantically identical. Several Yahoo normalized-sample hashes changed even
though every name still returned the same 358 dates; the original review hashes
are intentionally retained as the ingest-time snapshot. This observed drift is
why Yahoo is treated as a mutable free provider and why fingerprints must not be
silently refreshed. These hashes are drift detectors, not archived price
vintages; exact reconstruction of every later Yahoo revision is not claimed.

Reproduce the review in a disposable directory, inspect the review CSV, then
ingest the already-versioned accepted manifests:

```bash
PYTHONPATH=src .venv/bin/python -m aios.cli build-reference-batch \
  examples/sp500_reference_batch_02_tickers.txt \
  --batch-name sp500_reference_batch_02 \
  --output-dir /tmp/aios_reference_batch_02 \
  --start 2023-08-01 --end 2025-01-01 \
  --verified-date 2026-07-17

PYTHONPATH=src .venv/bin/python -m aios.cli ingest-reference-batch \
  --issuer-ciks examples/sp500_reference_batch_02_issuer_ciks.csv \
  --security-issuers examples/sp500_reference_batch_02_security_issuers.csv \
  --provider-symbols examples/sp500_reference_batch_02_provider_symbols.csv \
  --start 2023-08-01 --end 2025-01-01
```

### Completed stable reference batch 03

Batch 03 reviewed 25 additional unchanged full-window candidates: A, ACGL,
AFL, AIG, AIZ, AJG, ALL, AMCR, AMP, ANSS, AON, APA, APTV, ARE, ATO, AVB,
AVGO, AVY, AXON, AXP, AZO, BALL, BAX, BBY, and BDX. Twenty-four were accepted.
ANSS was rejected because the retired ticker has zero records in the SEC
current ticker map after its acquisition. No CIK, issuer, owner, provider, price,
or fundamental row was guessed for ANSS.

The accepted manifests had no existing CIK, owner, or provider-symbol
collisions. The live ingest added 52,688 issuer-tagged fundamental rows and
8,592 security/provider-tagged price rows. Each accepted security has 358 unique
sessions from 2023-08-01 through 2024-12-31; duplicate price keys and missing
provider tags are both zero. Batch fundamentals span SEC filing dates from
2009-07-28 through 2026-06-12.

After Batch 03 the database contains 55 issuers, 55 CIK intervals, 55 owner
intervals, 57 provider intervals, 77 securities, 182,576 price rows, and 183,812
fundamental rows. Every hard validator remains at zero. The same four historical
failed-ingest and one historical zero-row records remain visible warnings.

The versioned files are:

- `examples/sp500_reference_batch_03_tickers.txt`;
- `examples/sp500_reference_batch_03_issuer_ciks.csv`;
- `examples/sp500_reference_batch_03_security_issuers.csv`;
- `examples/sp500_reference_batch_03_provider_symbols.csv`; and
- `examples/sp500_reference_batch_03_review.csv`.

Reproduce the review and accepted ingest with:

```bash
PYTHONPATH=src .venv/bin/python -m aios.cli build-reference-batch \
  examples/sp500_reference_batch_03_tickers.txt \
  --batch-name sp500_reference_batch_03 \
  --output-dir /tmp/aios_reference_batch_03 \
  --start 2023-08-01 --end 2025-01-01 \
  --verified-date 2026-07-17

PYTHONPATH=src .venv/bin/python -m aios.cli ingest-reference-batch \
  --issuer-ciks examples/sp500_reference_batch_03_issuer_ciks.csv \
  --security-issuers examples/sp500_reference_batch_03_security_issuers.csv \
  --provider-symbols examples/sp500_reference_batch_03_provider_symbols.csv \
  --start 2023-08-01 --end 2025-01-01
```

The build command intentionally exits non-zero because the review contains one
rejection, even though it writes valid accepted manifests for the other 24
names. This prevents automation from overlooking ANSS.

### Completed stable reference batch 04

Batch 04 reviewed 25 more full-window candidates: BEN, BF.B, BG, BIIB, BK,
BKNG, BKR, BLK, BMY, BR, BRK.B, BRO, BSX, BWA, BXP, C, CAG, CAH, CARR, CB,
CBOE, CBRE, CCI, CCL, and CDNS. Nineteen were accepted: BEN, BIIB, BKNG, BKR,
BMY, BR, BRO, BSX, BWA, BXP, CAG, CAH, CARR, CB, CBOE, CBRE, CCI, CCL, and
CDNS.

Six candidates failed the automatic evidence contract and remain only in the
review file:

- BF.B, BK, and BRK.B had zero exact records in SEC's current ticker map; and
- BG, BLK, and C had SEC submissions filing histories that did not reach the
  certified window start under the strict automatic continuity check.

These are manual exception cases, not authorization to substitute punctuation,
a current ticker, a CIK, or a provider alias. The accepted manifests had no
existing CIK, owner, or provider-symbol collisions.

The live ingest added 43,299 issuer-tagged fundamental rows and 6,802
security/provider-tagged price rows. Every accepted security has 358 unique
sessions from 2023-08-01 through 2024-12-31. Duplicate price keys and missing
provider tags are both zero. Batch fundamentals span as-of dates from
2009-07-17 through 2026-07-15.

After Batch 04 the database contains 74 issuers, 74 CIK intervals, 74 owner
intervals, 76 provider intervals, 96 securities, 189,378 price rows, and
227,111 fundamental rows. Every hard validator remains at zero. The same four
historical failed-ingest and one historical zero-row records remain visible as
warnings. Dated input coverage is now 92/503 on 2023-09-29 and 94/504 on
2024-09-30, an exact gain of 19 at both checkpoints.

The versioned files are:

- `examples/sp500_reference_batch_04_tickers.txt`;
- `examples/sp500_reference_batch_04_issuer_ciks.csv`;
- `examples/sp500_reference_batch_04_security_issuers.csv`;
- `examples/sp500_reference_batch_04_provider_symbols.csv`; and
- `examples/sp500_reference_batch_04_review.csv`.

Reproduce the review and accepted ingest with:

```bash
PYTHONPATH=src .venv/bin/python -m aios.cli build-reference-batch \
  examples/sp500_reference_batch_04_tickers.txt \
  --batch-name sp500_reference_batch_04 \
  --output-dir /tmp/aios_reference_batch_04 \
  --start 2023-08-01 --end 2025-01-01 \
  --verified-date 2026-07-17

PYTHONPATH=src .venv/bin/python -m aios.cli ingest-reference-batch \
  --issuer-ciks examples/sp500_reference_batch_04_issuer_ciks.csv \
  --security-issuers examples/sp500_reference_batch_04_security_issuers.csv \
  --provider-symbols examples/sp500_reference_batch_04_provider_symbols.csv \
  --start 2023-08-01 --end 2025-01-01
```

The build command intentionally exits non-zero because six review rows are
rejected, while still writing valid accepted manifests for the other 19 names.
This makes partial acceptance explicit to both humans and automation.

### Completed exception reference batch 01

The separate exception review resolves the seven names rejected by the
automatic path according to the actual reason each failed:

- BF.B and BRK.B use the exact market-dot/SEC-hyphen notation pair confirmed in
  the official SEC ticker map and submissions payload;
- C follows the older submissions shard named by Citi's official current
  submissions record, rather than assuming the truncated recent list is full
  history;
- BG uses dated CIK/owner intervals at the official 2023-11-01 Bunge successor
  transaction;
- BLK uses dated CIK/owner intervals at BlackRock's official 2024-10-01 holding-
  company reorganization;
- BK uses its 2023 10-K to prove the historical ticker and BNY's official 2026
  BK→BNY announcement to certify that Yahoo's current BNY label may be relabeled
  only inside the bounded historical BK assignment; and
- ANSS uses its official 2023 10-K and CIK for issuer/fundamental identity, but
  Yahoo and Stooq both returned zero bounded rows after the acquisition. Those
  two provider mappings are `unavailable`, not guessed.

The manifest contains nine legal issuer/CIK intervals, nine dated owner
intervals, and eight provider intervals (six verified and two unavailable).
The live ingest wrote 15,523 issuer-tagged fundamental rows and 2,148 verified
price rows. Each of the six price-complete securities has 358 unique sessions.
ANSS had 2,111 PIT fundamental rows and zero prices at that checkpoint, so it
remained visibly incomplete rather than disappearing from the denominator.

After this exception batch the database had 83 issuer/CIK intervals, 83 owner
intervals, 84 provider intervals, 103 securities, 191,526 prices, and 242,634
fundamentals. Every hard validator remained at zero. Dated complete-input
coverage became 98/503 and 100/504.

The versioned evidence is:

- `examples/sp500_reference_exception_batch_01_tickers.txt`;
- `examples/sp500_reference_exception_batch_01_issuer_ciks.csv`;
- `examples/sp500_reference_exception_batch_01_security_issuers.csv`;
- `examples/sp500_reference_exception_batch_01_provider_symbols.csv`; and
- `examples/sp500_reference_exception_batch_01_review.csv`.

Ingest the reviewed exception manifests with:

```bash
PYTHONPATH=src .venv/bin/python -m aios.cli ingest-reference-batch \
  --issuer-ciks examples/sp500_reference_exception_batch_01_issuer_ciks.csv \
  --security-issuers examples/sp500_reference_exception_batch_01_security_issuers.csv \
  --provider-symbols examples/sp500_reference_exception_batch_01_provider_symbols.csv \
  --start 2023-08-01 --end 2025-01-01
```

### Completed stable reference batch 05

Batch 05 reviewed AAPL, AMZN, BA, BAC, CAT, CDW, CE, CEG, CF, CFG, CHD, CHRW,
CHTR, CI, CINF, CL, CLX, CMCSA, CME, CMG, CMI, CMS, CNC, CNP, and COF. All
25 passed the automatic contract and returned 358 unique sessions. The first
five were already covered by legacy ticker rows; this batch upgrades them to
reviewed issuer/provider provenance. The other 20 produce the exact complete-
input coverage gain at both checkpoints.

The ingest performed 59,317 fundamental upserts and 8,950 price upserts. Since
five names replaced existing legacy ticker rows, the net table growth is lower
than the operation count. At that checkpoint the database contained 108 issuers, 108 CIK
intervals, 108 owner intervals, 109 provider intervals, 123 securities,
198,686 prices, and 288,541 fundamentals. Complete dated coverage is 118/503 on
2023-09-29 and 120/504 on 2024-09-30.

The post-ingest review found one canonical-display bug: alphabetically sorting
Comcast's SEC tickers promoted the debt symbol CCZ ahead of CMCSA. The builder
now preserves SEC's published order (`CMCSA`, then `CCZ`), and a full issuer
refresh atomically removes stale labels for that same issuer. The 2,664 Comcast
facts are now labeled CMCSA; zero CCZ security/fundamental rows remain. This did
not affect the CMCSA security ID or its correctly labeled price history.

The versioned files are:

- `examples/sp500_reference_batch_05_tickers.txt`;
- `examples/sp500_reference_batch_05_issuer_ciks.csv`;
- `examples/sp500_reference_batch_05_security_issuers.csv`;
- `examples/sp500_reference_batch_05_provider_symbols.csv`; and
- `examples/sp500_reference_batch_05_review.csv`.

Reproduce the review and ingest with:

```bash
PYTHONPATH=src .venv/bin/python -m aios.cli build-reference-batch \
  examples/sp500_reference_batch_05_tickers.txt \
  --batch-name sp500_reference_batch_05 \
  --output-dir /tmp/aios_reference_batch_05 \
  --start 2023-08-01 --end 2025-01-01 \
  --verified-date 2026-07-17

PYTHONPATH=src .venv/bin/python -m aios.cli ingest-reference-batch \
  --issuer-ciks examples/sp500_reference_batch_05_issuer_ciks.csv \
  --security-issuers examples/sp500_reference_batch_05_security_issuers.csv \
  --provider-symbols examples/sp500_reference_batch_05_provider_symbols.csv \
  --start 2023-08-01 --end 2025-01-01
```

### Completed stable reference batch 06

Batch 06 reviewed COO, COP, COST, CPB, CPRT, CPT, CRL, CRM, CSCO, CSGP, CSX,
CTAS, CTRA, CTSH, CTVA, CVS, CVX, CZR, D, DAL, DD, DE, DFS, DG, and DGX. The
automatic contract accepted 23/25. Every accepted provider response contains
358 unique sessions from 2023-08-01 through 2024-12-31. CTRA and DFS remained
only in the review CSV because later mergers removed their retired symbols from
SEC's current ticker map; the builder did not guess their historical CIKs.

The accepted ingest performed 56,013 fundamental upserts and 8,234 price
upserts with no per-issuer or per-security failure. At that checkpoint the
reference layer contained 131 issuer/CIK intervals, 131 owner intervals, and
132 provider intervals. Complete dated coverage became 140/503 and 142/504.

The versioned files are:

- `examples/sp500_reference_batch_06_tickers.txt`;
- `examples/sp500_reference_batch_06_issuer_ciks.csv`;
- `examples/sp500_reference_batch_06_security_issuers.csv`;
- `examples/sp500_reference_batch_06_provider_symbols.csv`; and
- `examples/sp500_reference_batch_06_review.csv`.

### Completed exception reference batch 02

Exception Batch 02 closes three retired-symbol price gaps without changing the
automatic builder's current-map rule:

- ANSS retains its already reviewed 2023 10-K/CIK identity;
- CTRA uses Coterra's 2023 10-K for ticker/CIK identity and its 2026 merger 8-K
  to explain the later delisting; and
- DFS uses Discover's 2023 10-K for ticker/CIK identity and its 2025 merger 8-K
  to explain the later delisting.

Yahoo returned zero bounded rows for CTRA and DFS, as it previously did for
ANSS. Explicit Tiingo requests returned 358 unique sessions for each symbol,
covering 2023-08-01 through 2024-12-31. The token was sent only in an
authorization header. Each response passed the standard density, boundary,
positive-close, unique-date, and dated-security relabel checks and has a
SHA-256 fingerprint in the review CSV.

The ingest refreshed 6,360 fundamental rows and wrote 1,074 price rows. Because
ANSS fundamentals already existed and some reviewed rows replace legacy ticker
records, operation counts are not net table growth. The live database now has
133 issuers/CIK intervals, 133 owner intervals, 135 provider intervals, 147
securities, 207,636 prices, and 345,585 fundamentals. Complete dated coverage
is 143/503 on 2023-09-29 and 145/504 on 2024-09-30. Every hard validator remains
at zero; the four failed-ingest and one zero-row warnings predate these batches.

The versioned evidence is:

- `examples/sp500_reference_exception_batch_02_tickers.txt`;
- `examples/sp500_reference_exception_batch_02_issuer_ciks.csv`;
- `examples/sp500_reference_exception_batch_02_security_issuers.csv`;
- `examples/sp500_reference_exception_batch_02_provider_symbols.csv`; and
- `examples/sp500_reference_exception_batch_02_review.csv`.

Ingest it with:

```bash
PYTHONPATH=src .venv/bin/python -m aios.cli ingest-reference-batch \
  --issuer-ciks examples/sp500_reference_exception_batch_02_issuer_ciks.csv \
  --security-issuers examples/sp500_reference_exception_batch_02_security_issuers.csv \
  --provider-symbols examples/sp500_reference_exception_batch_02_provider_symbols.csv \
  --start 2023-08-01 --end 2025-01-01
```

### Reviewed expansion through bounded-window completion

Batches 07–17 continued the same fail-closed 25-name workflow. The accepted
manifests add 274 security-owner/provider mappings through STX; FOX/FOXA and
GOOG/GOOGL intentionally deduplicate to shared issuers. Exception Batch 03
resolves FI after its later FI→FISV change. Exception Batch 04 resolves the
post-window HES and HOLX delistings. Exception Batch 05 resolves IPG, JNPR, and
K after their later acquisitions. Every accepted security has 358 bounded
sessions. Batch 11's seven transient Yahoo empty responses were retried only on
their price legs and each restored to 358 rows. Exception Batch 06 resolves the
SEC-proven MMC→MRSH same-security provider alias with 2,396 fundamental upserts
and 358 prices. Batch 15 automatically accepted 24/25 candidates and performed
51,061 fundamental plus 8,592 price upserts. PARA failed the current SEC map
check after its 2025 transaction, so Exception Batch 07 uses Paramount Global's
official 2023 10-K, the official closing/delisting filings, and a direct Tiingo
PARA response. It added 2,267 fundamental and 358 price upserts. The old PARA
shares were cancelled; successor PSKY belongs to a different issuer and is not
backfilled onto PARA history. Batch 16 then accepted 25/25 candidates with no
exception queue and performed 57,351 fundamental plus 8,950 price upserts; all
25 provider responses contained the complete 358-session bounded window.
Batch 17 also accepted 25/25 without an exception queue and performed 63,032
fundamental plus 8,950 price upserts; all 25 provider responses again contained
the complete bounded window.

Batches 18 and 19 then accepted 25/25 candidates. Batch 20 accepted 22/24 on
the automatic path; Exception Batch 08 used historical SEC filings and direct
Tiingo windows for retired WBA and the pre-successor XOM issuer. Those four
batches performed 180,723 fundamental and 26,492 price upserts after Batch 17.

Window Batch 01 adjudicated every one of the 53 shorter membership spans. The
automatic path accepted 43. Exception Batch 09 certified ATVI, CMA, CTLT, MRO,
PXD, and SEE from primary SEC evidence plus direct Tiingo windows. Exception
Batch 10 certified CDAY→DAY as two dated provider labels for the same security.
Together the window batch and its exceptions performed 92,177 fundamental and
8,946 price upserts. PEAK and WRK retain reviewed owner/fundamental histories
but are terminal provider gaps: Yahoo cannot safely return the old security,
and direct Tiingo and Stooq checks produced no usable bounded history.

The final live checkpoint is 528 issuer/CIK intervals, 531 owner intervals,
534 provider intervals, 529 stable security-master records, 335,796 prices,
1,225,871 active fundamentals, and 42 quarantined source anomalies. Every one
of the 533 membership-assignment spans has an
overlapping reviewed owner. Complete dated input coverage is 501/503 on
2023-09-29 and 503/504 on 2024-09-30. The latter gap is AMTM: its first public
Company Facts availability is 2024-12-17, after the decision date. Upsert
operation counts exceed net table growth when a reviewed identity replaces a
legacy ticker-tagged copy. Direct live validation reports zero active hard-
integrity failures. The four failed-ingest and one zero-row warnings are older
audit records, not failures in the completed batches.

## Batch 01 local import and original smoke-run baseline

This section preserves the pre-Batch-02 regression baseline so later breadth
changes are not mistaken for a like-for-like return improvement.

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

### Expanded membership-denominator audit after Batch 02

After Batch 02, the same command was run without any `--ticker` filter. The
engine enumerated all 525 distinct tickers appearing in the certified
membership history and retained the real 503–504 member boundary at each
decision date. Only 32–33 names per quarter met the current Quality and Value
factor gates, so this is a **partial-coverage audit**, not a 500-stock strategy
backtest and not evidence that excluded members would have ranked below the
selected names.

| Policy | Periods | Net cumulative | Gross cumulative | Annualized | Volatility | Modeled costs | Turnover |
|---|---:|---:|---:|---:|---:|---:|---:|
| Regime-aware QV | 5 | 52.57% | 54.04% | 40.27% | 16.23% | $1,315.11 | 10.45x |
| Fixed 60/40 QV | 5 | 54.27% | 55.75% | 41.51% | 16.73% | $1,340.87 | 10.47x |
| SPY adjusted close | 5 | 41.49% | 41.49% | 32.04% | 7.66% | $0.00 | N/A |

The policies selected different holdings in three of five periods; baseline
outperformed over this short sample. Tax rates remained zero. These numbers are
useful only as deterministic pipeline evidence while roughly 90% of each
historical universe still lacks the complete local input set.

### Expanded membership-denominator audit after Batch 03

The same unfiltered audit was repeated after Batch 03. It still enumerated all
525 historical tickers and preserved each 503–504 member decision-date
denominator. Factor eligibility increased to 47, 47, 48, 48, and 47 names over
the five quarters, respectively. This remains a partial-coverage audit because
429–430 members still lack prices, PIT fundamentals, or both.

| Policy | Periods | Net cumulative | Gross cumulative | Annualized | Volatility | Modeled costs | Turnover |
|---|---:|---:|---:|---:|---:|---:|---:|
| Regime-aware QV | 5 | 70.81% | 72.44% | 53.55% | 15.93% | $1,415.93 | 10.57x |
| Fixed 60/40 QV | 5 | 70.85% | 72.48% | 53.58% | 16.03% | $1,417.99 | 10.57x |
| SPY adjusted close | 5 | 41.49% | 41.49% | 32.04% | 7.66% | $0.00 | N/A |

The two policies were effectively tied; both had one losing quarter and an
approximately 0.18% quarter-end drawdown. Tax rates remained zero. The large
change from the Batch 02 audit is caused by changing the small eligible sample,
including AVGO and other Batch 03 names, and must not be interpreted as strategy
improvement or evidence of regime alpha.

### Expanded membership-denominator audit after Batch 04

The post-Batch-04 audit again enumerated all 525 historical tickers and retained
the 503–504 dated membership denominator. Factor eligibility increased to 59,
59, 61, 61, and 60 names across the five decision dates. This is still a
partial-coverage audit: the coverage command reports 410–411 dated members
without one or both complete local inputs at the two measured dates.

| Policy | Periods | Net cumulative | Gross cumulative | Annualized | Volatility | Modeled costs | Turnover |
|---|---:|---:|---:|---:|---:|---:|---:|
| Regime-aware QV | 5 | 64.88% | 66.45% | 49.26% | 9.77% | $1,335.86 | 10.52x |
| Fixed 60/40 QV | 5 | 64.43% | 66.01% | 48.94% | 9.77% | $1,330.68 | 10.52x |
| SPY adjusted close | 5 | 41.49% | 41.49% | 32.04% | 7.66% | $0.00 | N/A |

All five quarterly observations were positive, so quarter-end drawdown was
zero in this short diagnostic. Tax rates remained zero. The return fell from
the Batch 03 checkpoint even as audited breadth increased, illustrating why
neither checkpoint is a like-for-like strategy comparison. These values verify
denominator, PIT, cost, and benchmark mechanics only; they do not validate an
investable S&P 500 policy or regime alpha.

### Expanded membership-denominator audit after Batch 05

The post-Batch-05 audit again enumerated all 525 historical tickers and kept
the real 503–504 decision-date membership denominator. Factor eligibility rose
to 77, 76, 78, 78, and 77 names across the five dates. This is still a
historical partial-coverage audit: at that checkpoint the separate member-level
check reported 385 missing one or both complete local inputs on 2023-09-29 and
384 on 2024-09-30. Current missing-input counts are two and one respectively.
Factor
eligibility is narrower than raw input coverage because the Quality and Value
publication gates can reject an otherwise price/fundamental-covered name.

| Policy | Periods | Net cumulative | Gross cumulative | Annualized | Volatility | Modeled costs | Turnover |
|---|---:|---:|---:|---:|---:|---:|---:|
| Regime-aware QV | 5 | 62.65% | 64.20% | 47.64% | 10.89% | $1,317.87 | 10.50x |
| Fixed 60/40 QV | 5 | 64.14% | 65.71% | 48.72% | 10.63% | $1,343.97 | 10.52x |
| SPY adjusted close | 5 | 41.49% | 41.49% | 32.04% | 7.66% | $0.00 | N/A |

The policies selected different top-10 sets in three of five periods, and the
fixed policy finished 1.49 percentage points ahead over this short sample. All
five quarterly observations were positive, so the quarter-end drawdown metric
was zero; tax rates were also zero. The change from Batch 04 is caused by a
different incomplete eligible sample, not a strategy improvement or regression.
This checkpoint verifies denominator, PIT, execution-cost, and benchmark
behavior only. It is not an investable S&P 500 backtest and is not evidence of
regime alpha.

## Final near-complete bounded eligibility and stateful policy audit

The forced-liquidation schema-v1 baseline was recorded on 2026-07-18. The
persistent schema-v2 certification was recorded on 2026-07-20.

This is the first rerun after the complete reviewed identity program and the
factor-correctness repair. The repair requires exact prior-year fiscal-quarter
matches for TTM roll-forwards, routes SIC financials through bank-appropriate
quality components, and normalizes Piotroski only by criteria actually
evaluated. Forty-two EDGAR-derived rows whose stated `period_end` followed the
filing `as_of_date` were moved intact to `fundamentals_quarantine`; new
extraction and upserts reject that chronology. `aios validate` then reported no
active hard failures.

The exact command was:

```bash
PYTHONPATH=src .venv/bin/python -m aios.cli backtest-qv \
  --start 2023-08-01 --end 2024-12-31 --top-n 10 \
  --universe-id sp500 --benchmark SPY --calendar SPY \
  --commission-bps 5 --slippage-bps 5 \
  --exclude-ticker PEAK --exclude-ticker WRK --exclude-ticker AMTM \
  --explain-ticker PEAK --explain-ticker WRK --explain-ticker AMTM \
  --output data/backtests/qv_sp500_pit_2023-08_2024-12.json
```

On 2026-07-20 the factor read path was then optimized without changing this
experiment. A 503-member decision profile fell from 42,235 DuckDB queries and
174.1 seconds to 3,874 queries and 17.4 seconds by sharing one
PIT-deduplicated, decision-scoped fundamental snapshot across Quality and
Value. The cache is destroyed after each decision and the next call rereads the
database, so a later filing or identity correction cannot be hidden by stale
state. The optimized five-decision command completed in about 92 seconds. Its
entire `result` object and `ticker_explanations` array matched the prior
schema-v2 artifact exactly before the canonical artifact was regenerated with
the optimized repository fingerprint.

SPY defines every quarter end, next-session rebalance close, quarter-end close,
and daily valuation session. Existing positions remain invested between the
decision close and next-session close; only equal-weight deltas trade. FIFO lots
and their acquisition dates survive quarter boundaries. Raw closes plus
explicit splits and dividend cash drive strategy and benchmark valuation. Net
books and zero-friction shadow books use the same selections; SPY is a
persistent zero-friction book under the same timing convention.

A missing scheduled entry/exit price rejects the whole atomic transition and
stops later stateful periods. A missing individual daily mark may carry forward
and is listed in `stale_tickers`; this certified run had zero stale marks across
all three 316-observation curves. Price paths follow reviewed `security_id`, so
ticker changes do not become false sales. All five strategy and SPY periods
completed.

| Decision | PIT members | Raw complete | Quality scored | Value scored | Q+V eligible | Regime | Q/V weights |
|---|---:|---:|---:|---:|---:|---|---:|
| 2023-09-29 | 503 | 501 | 375 | 343 | 291 | reflation | 45/55 |
| 2023-12-29 | 503 | 500 | 379 | 343 | 293 | goldilocks | 60/40 |
| 2024-03-28 | 503 | 502 | 445 | 385 | 350 | reflation | 45/55 |
| 2024-06-28 | 503 | 502 | 390 | 364 | 308 | reflation | 45/55 |
| 2024-09-30 | 504 | 503 | 378 | 359 | 301 | goldilocks | 60/40 |

“Raw complete” means both price history and PIT fundamentals exist. It does
not guarantee enough standardized metrics to publish both factors. Quality and
Value exclusion reasons overlap, so their missing counts must not be added as
disjoint buckets. No member had a stale decision-date price. PEAK and WRK were
explicitly excluded on the dates when they were members because their reviewed
historical price mappings are unavailable. AMTM was not yet a member on the
first four dates and was explicitly excluded on 2024-09-30 because no public
filing was known then. The JSON contains the full reason list for every member,
including BG's missing PIT fundamentals on 2023-12-29.

The 2026-07-18 interval-v1 regression baseline was:

| Policy | Paired periods | Net cumulative | Gross cumulative | Annualized | Quarterly volatility | Modeled costs | Turnover |
|---|---:|---:|---:|---:|---:|---:|---:|
| Regime-aware QV | 5 | 74.24% | 75.89% | 56.01% | 11.68% | $1,352.10 | 10.58x |
| Fixed 60/40 QV | 5 | 76.55% | 78.22% | 57.66% | 12.27% | $1,366.76 | 10.59x |
| Stitched SPY intervals | 5 | 41.49% | 41.49% | 32.04% | 7.66% | $0.00 | N/A |

The 2026-07-20 schema-v2 stateful result is the current certification:

| Policy | Paired periods | Net cumulative | Gross cumulative | Annualized | Daily annualized volatility | Max drawdown | Modeled costs | Turnover | Daily observations |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Regime-aware QV | 5 | 70.25% | 71.13% | 52.72% | 17.66% | -9.94% | $671.32 | 5.17x | 316 |
| Fixed 60/40 QV | 5 | 72.58% | 73.51% | 54.37% | 17.31% | -8.32% | $700.79 | 5.37x | 316 |
| Persistent SPY | 5 | 37.16% | 37.16% | 28.58% | 12.49% | -8.41% | $0.00 | 0.00x | 316 |

The two policies selected different top-10 sets on 2023-09-29, 2024-03-28,
and 2024-06-28; the fixed policy finished 2.32 percentage points ahead in this
short sample. Every period remained positive, so period win rate is 100%, but
the daily curves now expose the drawdowns that quarter endpoints hid. Strategy
turnover and modeled costs roughly halved because unchanged holdings/lots were
not sold and rebuilt. The SPY number changed because v2 is one continuously
held raw-price/action book; v1 stitched five adjusted-close intervals and
omitted each quarter-end-to-next-entry transition.

These values are pipeline diagnostics, not investable performance or evidence
that either policy has alpha. The window is short and in-sample; tax rates were
zero; wash sales, cross-bucket offsets, carryforwards, and filing calendars are
not modeled. The earlier batch checkpoints also used older factor logic,
changing eligible samples, and interval accounting, so they are preserved as
engineering history rather than like-for-like strategy comparisons.

The canonical local gitignored schema-v2 audit is
`data/backtests/qv_sp500_pit_2023-08_2024-12.json`. The preserved v1 baseline is
`data/backtests/qv_sp500_pit_2023-08_2024-12_interval_v1.json` with SHA-256
`44ee2921118971c318048377615d87d4873dcf189a1c7e0cfbdc806d65811aaa`.
Each audit records a unique run ID, runtime, base commit, tracked-diff SHA-256,
untracked-tree SHA-256, dirty-worktree flag, and DuckDB SHA-256
`1584cad36f0231ee14f538cc02ddc75189d497608dc61a2736e130f269dda0cd`.
The database hash is independently matched against the closed file after every
certification run.

## Remaining limitations

- The stateful accounting engine is implementation-ready for generic research,
  but after-tax performance is not certified: rates remain zero and the model
  intentionally omits jurisdiction-specific wash sales, cross-bucket offsets,
  carryforwards, and filing/payment calendars.
- Daily marks use stored EOD data. Scheduled execution and period-end prices
  fail closed; intermediate absent marks carry forward with an explicit stale
  flag. The certified window had zero stale strategy or SPY marks.
- The 500-name factor path takes roughly three minutes per decision date on the
  current local database. Query batching/caching is an optimization target, but
  it must preserve the certified PIT and identity behavior.
- Exact announcement provenance before August 2023 is not complete.
- The historical reference is community-maintained and its own README warns
  that early rows may be incomplete. Official events override it only inside
  the certified window.
- Ticker changes are identifier transitions, not true index additions. A
  bounded stable security ID exists for every interval, and all 533 assignment
  spans have reviewed owners. The 528 issuer/CIK and 534 provider intervals are
  complete for the certified window, but share-class identity still needs
  stronger global identifiers before extending deep history.
- The local factor database covers 501/503 members on 2023-09-29 and 503/504 on
  2024-09-30. PEAK and WRK lack safe historical prices; AMTM has no public PIT
  facts on the second date. These exclusions are small but still prevent calling
  the bounded result a fully investable S&P 500 backtest.
- The current-map exception queue is identity-resolved. Successful Tiingo
  histories for the reviewed retired symbols do not prove broad delisted-symbol
  coverage beyond the versioned intervals.
- Free Yahoo/Stooq history is not reliable enough for every delisted or renamed
  security. A licensed survivorship-bias-free price source remains necessary
  for institutional-grade long-history results. Norgate is a credible paid
  candidate, but its Python integration is Windows-only and it does not provide
  announcement dates; no paid dependency or subscription has been added.
- Tiingo support is implemented as an optional, explicit provider using a token
  header. A configured token does not certify symbol coverage; every bounded
  response must pass the normal provider QA. Exception Batches 02–10 contain
  the versioned Tiingo-certified historical cases used in this window.

## Primary evidence examples

- [Walgreens Boots Alliance 2023 Form 10-K](https://www.sec.gov/Archives/edgar/data/1618921/000161892123000062/wba-20230831.htm)
- [Walgreens Boots Alliance closing Form 8-K](https://www.sec.gov/Archives/edgar/data/1618921/000119312525190603/d87240d8k.htm)
- [Exxon Mobil 2023 Form 10-K](https://www.sec.gov/Archives/edgar/data/34088/000003408824000018/xom-20231231.htm)
- [Exxon Mobil successor Form 8-K](https://www.sec.gov/Archives/edgar/data/2115436/000119312526291990/d71068d8k12b.htm)
- [Activision Blizzard closing Form 8-K](https://www.sec.gov/Archives/edgar/data/718877/000110465923108985/tm2328253d1_8k.htm)
- [Dayforce CDAY-to-DAY ticker announcement](https://investors.dayforce.com/news-and-events/press-releases/press-release-details/2024/Ceridian-to-change-ticker-symbol-to-DAY-on-NYSE-and-TSX-effective-February-1/default.aspx)
- [ANSYS 2023 Form 10-K](https://www.sec.gov/Archives/edgar/data/1013462/000101346224000007/anss-20231231.htm)
- [Coterra 2023 Form 10-K](https://www.sec.gov/Archives/edgar/data/858470/000085847024000019/cog-20231231.htm)
- [Coterra merger/delisting Form 8-K](https://www.sec.gov/Archives/edgar/data/858470/000110465926057278/tm2613882d1_8k.htm)
- [Discover 2023 Form 10-K](https://www.sec.gov/Archives/edgar/data/1393612/000139361224000010/dfs-20231231.htm)
- [Discover merger/delisting Form 8-K](https://www.sec.gov/Archives/edgar/data/1393612/000119312525122107/d928199d8k.htm)
- [Bunge successor-issuer Form 8-K](https://www.sec.gov/Archives/edgar/data/1144519/000110465923113122/tm2329110d1_8k.htm)
- [Bank of New York Mellon 2023 Form 10-K](https://www.sec.gov/Archives/edgar/data/1390777/000139077724000051/bk-20231231.htm)
- [BNY BK-to-BNY ticker announcement](https://www.bny.com/corporate/global/en/about-us/newsroom/press-release/bny-announces-planned-change-of-stock-ticker-symbol-to-bny-130465.html)
- [BlackRock successor-issuer Form 8-K](https://www.sec.gov/Archives/edgar/data/1364742/000119312524229654/d856279d8k.htm)
- [Kenvue / Advance Auto Parts](https://press.spglobal.com/2023-08-21-Kenvue-Set-to-Join-S-P-500-Advance-Auto-Parts-to-Join-S-P-SmallCap-600)
- [Uber / Jabil / Builders FirstSource](https://press.spglobal.com/2023-12-01-Uber-Technologies%2C-Jabil-and-Builders-FirstSource-Set-to-Join-S-P-500-Others-to-Join-S-P-MidCap-400-and-S-P-SmallCap-600)
- [Super Micro / Deckers](https://press.spglobal.com/2024-03-01-Super-Micro-Computer-and-Deckers-Outdoor-Set-to-Join-S-P-500-Others-to-Join-S-P-100%2C-S-P-MidCap-400-and-S-P-SmallCap-600)
- [KKR / CrowdStrike / GoDaddy](https://press.spglobal.com/2024-06-07-KKR%2C-CrowdStrike-Holdings-and-GoDaddy-Set-to-Join-S-P-500-Others-to-Join-S-P-MidCap-400-and-S-P-SmallCap-600)
- [Palantir / Dell / Erie](https://press.spglobal.com/2024-09-06-Palantir-Technologies%2C-Dell-Technologies%2C-and-Erie-Indemnity-Set-to-Join-S-P-500-Others-to-Join-S-P-MidCap-400-and-S-P-SmallCap-600)
- [Apollo / Workday](https://press.spglobal.com/2024-12-06-Apollo-Global-Management-and-Workday-Set-to-Join-S-P-500-Others-to-Join-S-P-MidCap-400-and-S-P-SmallCap-600)
- [Lennox / Catalent](https://press.spglobal.com/2024-12-18-Lennox-International-Set-to-Join-S-P-500-and-BILL-Holdings-to-Join-S-P-MidCap-400)
