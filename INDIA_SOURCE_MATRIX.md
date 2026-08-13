# India source and licensing matrix (INDIA_BUILD_PLAN.md phase I2)

Status date: **2026-08-11**. This is the checked-in source matrix that phase
I2's exit gate requires. It records what each candidate source actually
permits, not what is technically reachable.

Evidence tiers are marked on every claim and must stay marked:

- **[VERIFIED]** — read directly from the source's own page or document, quoted.
- **[REPORTED]** — third-party/developer claim. A lead, not a fact.
- **[UNVERIFIED]** — could not determine. This is a legitimate finding, not a gap to paper over.

## The decisive finding

NSE's own Terms of Use prohibit both the collection method and the intended
use. Independently retrieved and confirmed twice, from the canonical URL
<https://www.nseindia.com/static/nse-terms-of-use>.

**Clause 4 [VERIFIED], verbatim:**

> "User is prohibited to conduct any systematic or automated data collection
> activities (including scraping, data mining, data extraction and data
> harvesting) on or in relation to our Website / Mobile Application."

**Clause 3 [VERIFIED], verbatim:**

> "User acknowledges, agrees and undertakes that it shall not, in any form and
> manner, use or make the data or Content available on the Website / Mobile
> Application, for any gaming, virtual trading or simulation activities under
> any circumstances whatsoever."

Clause 3 is the one that matters most here and it is easy to miss. Even a
lawfully obtained copy of NSE website data may not be used for "simulation
activities." This project's India plan is a backtest plus a paper-trading
simulation. That is the prohibited use, described directly. Scraping fails
this twice over: once on how the data would be collected, once on what it
would be used for.

There is **no** personal-use, non-commercial, or research exemption to
Clause 4 [VERIFIED — searched, none found]. Clause 4 as published on the
canonical page carries **no** "without our express written consent"
qualifier, though an older copy on NSE's ENIT portal does [VERIFIED both].

### robots.txt says the opposite, and does not win

<https://www.nseindia.com/robots.txt> [VERIFIED], complete contents:

```
User-agent: *
Allow: /
Disallow: /market-data-test
Sitemap: https://www.nseindia.com/sitemap.xml
```

This permits everything except one test path. It directly contradicts
Clause 4. That contradiction is real and worth knowing — but `robots.txt` is
a crawler convention, not a licence grant, and the Terms of Use is the
instrument that binds a user. Relying on the permissive signal means betting
that NSE's SEO configuration overrides NSE's legal page, against the party
that published both. Not a bet this project should make.

## Official NSE paid data — the licensed path exists and is affordable

From NSE's own tariff PDF,
<https://archives.nseindia.com/content/press/Other_product_pricing_01042020.pdf>
("MARKET DATA PRODUCT TARIFF, Effective April 1, 2020") [VERIFIED]. All
figures annual. **This document is over six years old — treat as indicative
and confirm current rates before budgeting.**

| Product | Students/Researchers | Others (commercial) |
|---|---:|---:|
| CM Historical Trade data | **₹18,000** | ₹72,000 |
| FAO Historical Trade data | ₹18,000 | ₹72,000 |
| CM Historical Order & Trade (full book) | ₹500,000 | ₹1,000,000 |

From <https://archives.nseindia.com/content/press/EOD_data.pdf> [VERIFIED],
EOD data priced by *usage type*:

| Feed | Internal use | Display on website/app |
|---|---:|---:|
| CM and F&O segment | ₹25,000 | ₹100,000 |

Two structural facts favour this project. First, a **Students/Researchers
tier exists at roughly a quarter of the commercial rate**. Second, NSE prices
*internal, non-redistributed* use at a quarter of display use — and a local
personal research system is squarely internal use.

NSE's Data Sharing & Usage Policy additionally defines [VERIFIED]:

> "Non Commercial Users means accredited academic institutions, members of
> academia and not for profit institutions/entities, Researchers, Students
> etc."

and states the NSE Data board "may also consider introducing reduced fee
arrangements or waivers for Non-Commercial Users." **Asking costs nothing:**
`marketdata@nse.co.in`, +91-22-2659-8385.

## Third-party providers

| Provider | India coverage | Fundamentals | Price | Commercial/internal use | Tier |
|---|---|---|---|---|---|
| **Twelve Data** | **Yes — XNSE confirmed** | **Yes** | **$29/mo (Grow)** | **"Internal business purposes"** | [VERIFIED] |
| Kite Connect (Zerodha) | Yes, NSE+BSE, deep history | No | ₹500/mo | Research only; no redistribution | [VERIFIED] |
| Trendlyne MCP | Yes — 3,500+ parameters | **Yes, deep** | ₹299–499/mo | "Individual use only" | [VERIFIED] |
| Angel One SmartAPI | Yes | No | **Free** | **Terms not read** | [UNVERIFIED] |
| Global Datafeeds | Yes — *authorised NSE vendor* | Yes | Not published | Exchange-authorised | [REPORTED] |
| Tiingo | **None** | — | — | — | [VERIFIED] |
| EODHD | **Appears withdrawn** | — | $19.99/mo | Personal only; $399/mo commercial | [VERIFIED] |
| Alpha Vantage | Partial/degraded (BSE only) | No | $49.99+/mo | **Personal only even when paid** | [VERIFIED] |

### Notes that change decisions

**Tiingo does not cover India at all** [VERIFIED]. Their EOD product page
lists US venues plus Shenzhen and Shanghai only. The existing $50/mo Tiingo
commercial licence this project already considered for the U.S. side does
**not** extend to Indian equities, because there is no Indian data behind it.

**EODHD appears to have withdrawn Indian coverage** between 2025-11 and now
[VERIFIED]: `eodhd.com/exchange/NSE` returns 404 today, an archived snapshot
from 2025-11-18 shows a full NSE page with 2,688 active tickers, and the
current exchange list contains zero occurrences of India, NSE, XNSE, XBOM or
Bombay. Most "best API for Indian stocks" articles still recommend it; they
are stale. Cause unconfirmed.

**Alpha Vantage is the yfinance trap again** [VERIFIED]. Its grant is
"personal, non-commercial use, unless you and Alpha Vantage have agreed
otherwise in writing" — **buying a premium plan does not itself grant
commercial rights**, since a card payment is not "agreed otherwise in
writing." Its commercial-use definition is also self-contradictory as
drafted, naming "investment analysis, research, testing" as both commercial
and "individual in nature."

**Twelve Data has the best licence posture of any provider reviewed**
[VERIFIED]. The grant is use "solely for Internal Use," defined as "use
solely for Customer's internal business purposes and not for redistribution
or external commercial purposes." That is materially different from
"personal, non-commercial": internal business use is *inside* the grant, so
growing beyond a hobby does not invalidate the licence. §2.2(c) also permits
"Derived Data that cannot be reverse-engineered to recreate the original
Data" — computed factor scores are the project's own. Caveats: the free tier
excludes India, so there is no zero-cost evaluation path; historical depth
for XNSE is unpublished [UNVERIFIED]; BSE coverage unconfirmed [UNVERIFIED].

**Kite Connect has one genuinely ambiguous clause** [VERIFIED]. It prohibits
users to "Scrape, build databases, or otherwise create permanent copies of
such content, or keep cached copies with the intent of redistributing." The
trailing qualifier grammatically attaches to the cached-copies clause;
whether it also governs "build databases" is unclear. A local research
database with zero redistribution is defensible on the narrow reading and
problematic on the broad one. **This project's whole premise is not relying
on a favourable reading of ambiguous terms** — get a written answer from
Zerodha before building on it.

## Free scraper libraries and datasets — all rejected

`jugaad-data`, `nsepy`, `eod2`, `nsetools`, and every Kaggle "NSE bhavcopy"
dataset examined ultimately obtain their data by automated collection from
nseindia.com [VERIFIED for each]. A permissive code licence (MIT, GPL,
public-domain) covers the *code* and never the *data*. `jugaad-data`'s own
licence file reads "do whatever you want with it" — about the library, not
about NSE's data, which NSE's policy states "shall at all times lie with
NSE/ NSE Data."

**The Kaggle-licence trap, stated plainly:** a CC0 or ODbL tag on a scraped
NSE dataset does not launder it. An uploader can only grant rights they hold,
and an uploader who scraped nseindia.com holds none. Treat every such dataset
as unlicensed regardless of its tag.

This is the same test that eliminated yfinance for U.S. commercial use
earlier in this project — and NSE's prohibition is *more* explicit than
Yahoo's, plus adds the simulation-use ban that Yahoo does not have.

`nsepy` is additionally abandoned and non-functional [REPORTED].

## Technical reality, independent of licensing

Even setting licensing aside, the NSE scraping path is operationally poor
[REPORTED unless noted]:

- Session/cookie handshake required; endpoints reject cold requests.
- Rate limited to roughly 3 requests/second.
- The legacy bhavcopy CSV was **discontinued 2024-07-08** and replaced by
  UDiFF, changing URL structure, column names and date formats
  simultaneously — pre- and post-cutover files are not schema-compatible.
- HTML pages on `www.nseindia.com` were unreachable from this research
  session entirely (repeated 60s timeouts) while `robots.txt` and
  `archives.nseindia.com` PDFs served instantly [VERIFIED by direct
  observation] — consistent with bot mitigation on HTML routes.

For a system built on frozen policy bundles and hash-pinned reproducibility,
an upstream that silently reshapes its schema is a structural hazard on its
own merits.

## Decision gate

`INDIA_BUILD_PLAN.md` phase I2 defines three outcomes. Against the evidence
above:

- **Free bounded beta — NOT AVAILABLE from NSE directly.** Clause 4 bars the
  collection method and Clause 3 bars the simulation use. This is not a grey
  area needing a judgement call; it is stated prohibition on both axes.
- **Paid historical track — AVAILABLE and affordable.** Either a licensed
  third-party feed (Twelve Data $29/mo is the strongest licence posture) or
  NSE's own Students/Researchers tier (₹18,000/yr indicative, plus a
  published willingness to consider researcher waivers).
- **Stop — correctly avoided.** The plan's "stop" condition is building a
  survivorship-biased backtest from today's Nifty 50. Nothing here does that.

## Open items — must close before any India ingest

1. **Historical Nifty 50 constituents — narrowed, still blocking.**
   Researched 2026-08-11. An official consolidated file exists:
   `archives.nseindia.com/content/indices/IndexInclExcl.xls` [VERIFIED —
   downloaded directly, HTTP 200, no proxy needed]. Nifty 50 sheet: 196 rows,
   `Event Date` column, 1996-09-18 to **2020-07-31**, then stops (file
   metadata + community reports agree NSE never updated it further
   [REPORTED]). Cross-checked one row (2018-04-02 entry/exit) against a
   press report confirming the index change was "effective from" that date
   [VERIFIED by cross-reference] — so `Event Date` is the **effective** date,
   not announcement date, which is what point-in-time correctness needs.
   Post-2020 changes exist only as individual dated press releases at
   `niftyindices.com/Press_Release/ind_prsDDMMYYYY.pdf`, "effective from
   [date]" convention [REPORTED — PDF text unreadable this session, both
   direct fetch and proxy fetch to niftyindices.com timed out].

   **Licensing is the actual blocker, on two different terms documents.**
   The `.xls` lives on archives.nseindia.com and inherits nseindia.com's
   Clause 3 (bars "simulation activities... under any circumstances") — same
   defect as price data. `niftyindices.com` itself has **separate, differently
   -shaped terms** [VERIFIED via proxy]: a "personal, non-commercial use
   only" restriction plus an explicit bar on "aggregate, copy or duplicate in
   any manner any of the content" — blocking bulk collection of the
   press-release PDFs even though its scraping clause (12) has a
   consent-cure path nseindia.com's does not.

   A paid path plausibly exists: `niftyindices.com/offerings/data-subscription`
   explicitly lists "index constituent data" (names, IDs, weights) as a
   product [VERIFIED via proxy], also resold via Bloomberg/Factset/
   Rimes/MSCI, but **no price is published** — contact `indices@nse.co.in`
   (distinct from `marketdata@nse.co.in`, already the price-data contact).
   No reviewed third-party provider (Twelve Data, Kite Connect) confirmed to
   carry historical constituent membership as a product; Trendlyne markets
   "Index Constituents" but current-vs-historical is unconfirmed
   [UNVERIFIED]. Free Kaggle/GitHub Nifty 50 datasets checked are all
   OHLCV price-only, none contain constituent membership [VERIFIED by
   description review].

   Next step: email `indices@nse.co.in` about constituent-history pricing
   and terms, same non-commercial-research framing as the `marketdata@nse.co.in`
   inquiry.
2. **Twelve Data** — confirm XNSE historical depth and whether BSE is covered.
3. **Angel One SmartAPI** — read the actual terms. Free access says nothing
   about permitted use; currently the cheapest candidate and entirely
   unverified on licensing.
4. **Kite Connect** — written clarification on local database persistence for
   non-redistributive research.
5. **NSE Data** — email `marketdata@nse.co.in` about the researcher tier and
   the published waiver possibility. Costs nothing.
6. **EODHD** — written confirmation of whether NSE/BSE coverage still exists.

## What this means for the build

No Indian price, fundamental, or membership row may be ingested until at
least items 1 and 2 (or an equivalent licensed source) are closed. Phase I1's
market/venue identity rows (`in_equity`, `xnse`, `xbom`) are already
registered and are ISO registry facts, not market data — they are unaffected
by this gate and remain valid.
