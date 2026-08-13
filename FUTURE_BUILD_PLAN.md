# AIOS Future Build Plan

This document is the execution roadmap after the audited U.S. research beta. It
turns future ideas into ordered engineering gates so a later developer or AI
agent can tell what belongs now, what depends on earlier work, and what must not
be represented as complete.

`ARCHITECTURE.md` remains the design authority. This file controls sequencing
and acceptance. A feature is complete only when its exit gate passes; a screen
or schema alone is not completion.

[`PRODUCT_COMPLETION_GATES.md`](./PRODUCT_COMPLETION_GATES.md) defines the
mode-specific claim boundary for supervised research, recurring paper,
unattended local operations, hosted multi-user access, and controlled capital.

## Current baseline

AIOS currently supports supervised U.S. research, a fail-closed readiness gate,
a local paper portfolio, point-in-time factor research, a stateful engineering
backtest, checksum-verified backups, a recoverable benchmark-first exact-date
daily workflow, installed local scheduler timers, and a durable independent
local incident/job ledger with systemd failure/recovery capture.
The channel-neutral notification outbox, local no-network delivery proof,
selected fail-closed SMTP adapter, governed proposal stress-review v1, and the
first governed anomaly-review rule are implemented. The anomaly v1 slice
compares accepted SEC fundamentals coverage with exact reviewed membership and
source evidence, then writes only deduplicated cases to the independent
operations ledger when explicitly recorded. SMTP activation is deferred at the
user's request; when resumed it still requires private configuration and an
owner-confirmed receipt test. AIOS does not otherwise send external alerts,
preserve every original provider response, ingest Indian-market data, connect
to a broker, or make personal buy/sell decisions.

The live 2026-07-27 operations migration to schema v4 preserved every existing
incident/message record and passed SQLite integrity and foreign-key checks.
The additive schema-v6 migration is now complete: it retained that contract and
currently holds two immutable anomaly scans, three deduplicated cases, and six
append-only case events. The optional SMTP timer files are installed and
native-verified but remain disabled; the three core data/backup timers remain
enabled.

The current 2026-08-08 live research baseline reaches the 2026-08-07 decision
close: readiness is `READY`, validation has zero failures and four warnings,
and coverage is 503 members with 503/503 PIT filings and reviewed prices.
The official EA deletion and FERG addition was activated through one
content-addressed, backup-bound transaction. The immutable receipt binds the
official event, reviewed reference manifests, before/after membership hashes,
FERG price evidence and the simulation-only boundary. The activation itself
did not manufacture fundamentals; a later governed refresh accepted 323 rows
and withheld 24 ambiguous storage keys.

Backup `backups/aios-20260808T085804Z` verifies 8,536 files and 775,651,621
bytes and passed a disposable restore drill over 7,977 raw payloads and 11,601
parsed replays. The current release candidate
passes 1,075 tests, repository-wide Ruff, bytecode compilation, diff whitespace checks,
byte-identical wheel builds, exact source-to-wheel verification, and
clean-install smoke. Every later candidate must regenerate this evidence.

Company Facts v3 is not part of that certified baseline. **Re-verified live
against production on 2026-08-12**, superseding the stale 394/503 figure
below: `companyfacts-v3-plan --as-of 2026-08-10` now reports **500/500**
issuers with any accepted evidence pass every lineage check —
`_current_lineage_matches` verifies `ingest_run_id`, `source_snapshot_id`,
`source_rowset_sha256`, and per-row `source_row_sha256` all agree with the
stored evidence, row for row. The remaining 3 S&P 500 members (FDXF, HONA,
XOM) have no accepted Company Facts evidence at all yet — a pre-existing
reviewed gap unrelated to v3. **The source-lineaged migration requirement is
already satisfied; it was not a remaining blocker, the earlier count was
stale.**

The v2-to-v3 delta is large — 26,356 rows added, 41,794 removed, 326,467
changed out of ~1.17M, across **all 500** eligible issuers, not a few
outliers. This is not a bug: `_select_metric_storage_rows` in `edgar.py`
explains it directly. v2 runs with `preserve_legacy_winner=True`, meaning
when a filing has genuinely conflicting economics for the same
(period_end, as_of_date, metric) key, v2 silently keeps the *first* row
arbitrarily to avoid a duplicate storage key. v3 runs with
`preserve_legacy_winner=False` and withholds the entire conflicting key
instead of guessing — the same fail-closed philosophy this codebase applies
everywhere else. v3 also adds `taxonomy_aware=True` XBRL-taxonomy-aware
extraction. The size of the delta is the correctness fix becoming visible,
not evidence of a defect.

**Built 2026-08-12** as `src/aios/companyfacts_v3_activation.py`, mirroring
`universe_change_activation.py`'s proven pattern exactly:
`prepare_companyfacts_v3_activation()` verifies an explicit, human-chosen
issuer scope is fully eligible, takes a fresh verified backup, and publishes
an immutable content-addressed plan; `activate_companyfacts_v3()` re-verifies
CAS against freshly recomputed live evidence (rejecting if anything drifted
since review), proves the exact transaction on a disposable restore of that
backup before touching anything live, then commits once in one DuckDB
transaction and writes an append-only receipt to the new
`companyfacts_v3_activations` table. Mutation reuses the frozen `Store`'s own
`upsert_fundamentals` (which already versions into `fundamental_versions`)
for added/changed keys, and tombstones (`is_deleted=TRUE`) a key v3
correctly withholds that v2 had silently kept — the "future shrinking
replacement path" `ARCHITECTURE.md` flagged as needing explicit
confirmation, backup, and compare-and-set evidence. One
`fundamental_evidence_generations` row pins the resulting `version_sequence`
boundary per activation. CLI-wired as `companyfacts-v3-prepare` and
`companyfacts-v3-activate`. No provider fetch anywhere in this path — v3 is
a re-parse of the same already-captured, already-verified payload bytes v2
used.

Verified correct end-to-end against a real scratch copy of the production
database (one real issuer, 68 added / 720 changed / 35 removed, matching the
plan's predicted counts exactly) and via 6 unit tests covering the happy
path, confirm/actor/backup/CAS rejections, and an ineligible-issuer refusal.

**Run to completion against the real production universe, 2026-08-12,
user-directed.** All 500 eligible issuers migrated across 11 activation
batches (5 + 9×50 + 45): `companyfacts-v3-plan` now reports 0 eligible / 500
ineligible (all on v3). Batch 10 (the final 45) caught a real bug before it
touched anything live — three tickers (BG, XOM, BLK) legitimately have two
different issuer_ids each (a CIK-successor reincorporation event), and
`fundamentals`' primary key is `(ticker, period_end, as_of_date, metric)`,
not issuer-scoped. The activation's post-write verification query was
ticker-only-scoped, so it picked up an unrelated issuer's legacy rows as
noise and correctly refused inside the disposable-restore-first step, before
`activate_companyfacts_v3` ever opened the live database. Fixed: an explicit
pre-write collision guard (refuse if any touched key already belongs to a
different issuer_id) plus issuer_id-scoped delete/verification queries.
Re-verified via a scratch-copy repro that no actual key collision existed
for BG/XOM/BLK (their old/new CIK filing dates don't overlap) — this was a
false-negative in the safety check, not evidence of real corruption risk,
but the added guard is now real protection either way. Full 1282-test suite,
ruff, and frozen-bundle re-checked clean after the fix and again after the
final batch. The metric-level (not just count-level) candidate-diff report
remains the one soft gap for future migrations of this kind — the per-issuer
delta counts already in the plan are what CAS and disposable-proof rely on
for safety, so this is a reviewability nicety, not a safety gap.
The `companyfacts-v3-plan --as-of YYYY-MM-DD` surface remains read-only and
performs no provider fetch or mutation itself; activation is the separate
`companyfacts-v3-prepare`/`companyfacts-v3-activate` path above.

The expired 2026-07-27 proposal and predecessor trial
`us-qv-forward-72c4560a442d` are archived byte-identically with a no-fill
disposition. Active trial `us-qv-forward-ea4fc2788c4d` began prospectively from
the 2026-08-07 close, has one registered proposal for the 2026-08-10 close and
has zero executions. The account remains $100,000 simulated cash with zero
holdings and no broker connection. Daily, filings and backup services all have
successful terminal runs, the daily producer receipt certifies the exact
August 7 session, and the operating ledger has no blocker. FDXF, HONA and XOM
remain explicit reviewed gaps with score withholding preserved.

## Priority decision

| Capability | Decision | Why | Dependency / exit gate |
|---|---|---|---|
| Decision-first dashboard foundation | **Now — implemented** | Makes the local product operable without DuckDB or systemd knowledge | `Today` keeps Research, Paper Trial and Operations scope separate; every displayed fact reconciles to readiness, scheduler, ingest, backup, incident or forward-policy evidence |
| Institutional dashboard visual system | **Now — implemented** | A research control room must be fast to scan and hard to misread at desktop and phone widths | Light evidence canvas, navy shell, one CTA hierarchy, semantic status colors, symmetric grids, four-stage paper governance, selectable ranked rows, origin-aware URL state, rendered desktop/mobile QA |
| Canonical operator preflight | **Now — implemented** | Independent capabilities and one next action prevent a green research gate from hiding paper, operations, or real-capital blockers | Versioned checksum JSON, strict registered-proposal resolution, immutable DuckDB/SQLite reads, timing-only and full-review modes, repeatable capability requirements, no generated state-changing command |
| Release wheel contract verifier | **Now — implemented; each candidate must still pass** | A build file is not release evidence when package bytes or metadata can drift from reviewed source | Exact source/member/entry-point comparison; `METADATA` Python, dependency, and extra parity; pure-Python `WHEEL`; complete SHA-256/size-checked `RECORD`; new temporary-environment install and smoke proof |
| Cooperative cross-store mutation lease | **Now — implemented for supported CLI workflows** | Backup-covered DuckDB, raw, paper, proposal, and forward mutations must not race | Non-blocking process/thread lease covers refresh, ingest, import, repair, cleanup, backup/restore, paper, and forward CLI boundaries and fails visibly; direct-library callers remain outside the lease |
| Backup-first local schema upgrade | **Now — implemented; live run pending lease availability** | Opening a new `Store` must not silently migrate DuckDB or the incident ledger before a recovery point exists | One confirmed command owns the lease, checkpoints without application migration, verifies a complete backup, rehearses on exact copies, hashes every old DuckDB/SQLite relation, applies each migration, and emits a checksum-chained phase journal; exact-attempt recovery is implemented, while defective-code rollback still requires the release-pinned producer |
| Pre-swap semantic restore gate | **Now — implemented** | Manifest hashes alone cannot prove that a backup can safely become live state | Stage the complete candidate first; require application-version parity, openable/zero-hard-failure DuckDB, valid paper/forward envelopes and active cross-references, full raw replay, and no immutable merge conflict before safety backup or live publication; later publication failures use paper rollback/safety backup while raw additions and the stale incident remain forward-only |
| Governed artifact publication boundary | **Now — implemented** | A caller-selected output path must never overwrite an account, database, immutable input, backup, or source file | Resolve before work; project artifacts stay under `data/` but outside governed state; refuse symlink/hard-link aliases; atomically publish single files and reviewed batch names without replacement; proposals remain confined to their validated namespace |
| Stale backtest artifacts are not comparable | **Documented 2026-08-12; archive or re-run before citing** | An engine revision silently moved the same window's result by 23 points | `qv_sp500_pit_2023-08_2024-12.json` reports +74.2% and `qv_sp500_pit_schema_v4_2023-08_2024-12.json` reports +51.1% for a **byte-identical config** (same window, `top_n`, costs, taxes, universe, 5 completed periods). Root-caused: quality scores are identical for every shared name, but **value scores changed**, and the four names that dropped out (AVGO, NVDA, ORLY, LRCX) had implausible value percentiles in the old run — AVGO scored 85.6 and ORLY 98.2 as *cheap* in April 2024 while trading at premium multiples. The old value factor was defective and accidentally bought the AI trade inside a Quality+Value portfolio; ~23 points of the old "outperformance" came from that bug. **Treat every pre-`schema_v4` backtest artifact as void.** Registered experiments still carry no engine identity, so nothing detects this automatically |
| Position-weight cap is inert at defaults | **Open — decide intent, low effort** | A risk limit that can never bind gives false assurance | `PortfolioRiskPolicy.maximum_position_weight = 0.1` with `minimum_positions = 10` and an equal-weighted `top_n = 10` puts every position at exactly 1/10 = 0.10, precisely on the cap, so it cannot trigger in the default configuration — confirmed in real artifacts where every audited position reports `target_weight = 0.1`. Either lower the cap, raise `top_n`, or state explicitly that it exists only to bound non-default configurations. The sector cap (0.25) does still bind at 3+ names in one sector and is unaffected |
| Paper account identity collision | **Open — next intentional policy version; do not hotfix** | Every account `paper-init` creates carries the same hardcoded `account_id`, so the cross-account proposal guard cannot fire | `paper.py:178` hardcodes `"account_id": "us-qv-supervised-sandbox"`, so the check at `paper.py:640` (`proposal.account_id != account.account_id`) is a no-op between any two locally-created accounts: a proposal built for one sandbox can be executed against another and pass validation. Also causes spurious `unregistered proposal exists` drift on a second trial, because `_unregistered_proposal_issues` filters by `account_id`. Derive the ID from the account file identity instead. **`paper.py` is inside the frozen bundle — fixing it drifts every active trial, so it must land in a deliberate policy version alongside a planned restart, never as a standalone hotfix.** Until then, run exactly one paper account/trial |
| Final forward-library persistence CAS | **Done 2026-08-12** | A cooperative CLI lease is not a universal transaction for arbitrary direct-library callers | `register_forward_proposal`/`replace_drifted_forward_trial` now hold `paper.py`'s cross-process file lock (exposed as public `document_write_lock`) for their full read-modify-write, plus a `before_replace` compare-and-set check mirroring `paper.py`'s own `_require_document_unchanged` pattern exactly — no longer a blind `os.replace`. Both trials drifted as a result (frozen file changed); restart is a separate, not-yet-taken step |
| Expired-proposal rollover lifecycle | **Now — governed v4 activation live-proven** | Retrospective fills remain forbidden, while expired cycles need a safe successor | Stable plan, explicit confirmation, verified backup, fixed locks, fresh gates, final CAS, byte-identical archives, atomic swap and checksum-chained recovery receipt all passed in a naturally prospective window |
| Research Fast Path v1 | **Now — implemented** | A 29–33 second cold screen was the main daily research bottleneck | Dashboard-only decision-scoped batch facade, exact QV/QVML scalar parity, 12-query bound, read-only/temp-relation cleanup tests, serialized cold builds, no persistent score cache |
| Fundamental evidence generations v1 | **Now — row-version and factor-read contract implemented; paper activation deferred** | Filing-date PIT alone cannot prevent a fact accepted later from changing an earlier decision | Version the resolved post-merge projection, tombstone governed deletions, reject duplicate keys, and fail when latest history cannot reconstruct current rows; next pin reference routing, prices, membership, macro and policy, then introduce a complete boundary only in a new prospective paper schema |
| Recoverable exact-date U.S. daily workflow | **Now — implemented** | Separate timer stamps did not prove a full run survived logout, and benchmark-last ordering could leave identity windows one session behind | SPY first, universe second, members/macro third, exact-date readiness last; durable job lifecycle, restart, startup catch-up, linger, live 503-member proof |
| Local incident ledger and systemd failure capture | **Now — implemented** | Scheduled and application failures must survive analytical DB lock/open failures | Deduplicated open/repeat/acknowledge/resolve/reopen lifecycle, immutable schema-checked CLI inspection, safe structured service evidence, dashboard history, backup archive |
| Immutable raw-data snapshots | **Now — active U.S. transport gate implemented** | Provider history can change; later anomaly work needs exact ingest evidence | Active SEC, yfinance, FRED, Treasury and reviewed Tiingo paths capture and replay honestly; backup/restore drill passes; every future adapter needs the same gate |
| Company Facts v3 governed activation | **Done — full 500-issuer production migration complete 2026-08-12** | Higher structural score computability cannot override stale or unreviewed inputs | `companyfacts-v3-plan` reviews without mutating; `companyfacts-v3-prepare`/`companyfacts-v3-activate` back up, CAS-verify, prove disposable rollback, then atomically upsert/tombstone into `fundamentals` with append-only `fundamental_versions` history. All 500 eligible issuers migrated across 11 batches; a real shared-ticker (CIK-successor) verification-scoping bug was caught by the disposable-proof step before touching production, then fixed and re-verified |
| Channel-neutral notification outbox | **Now — implemented** | Channels are replaceable transports, not the source of incident truth | Atomic incident/message writes, stable idempotency, exclusive leases, bounded retries, dead letters, safe attempt audit, local no-network proof, backup/restore coverage |
| SMTP email delivery adapter | **Implemented; live activation deferred** | The user selected email but deferred setup; destination and credentials still cannot be inferred | Private SMTP config, one exact-route test, owner-confirmed receipt, explicit enable; historical held messages remain quarantined |
| Governed proposal stress review v1 | **Now — implemented** | Proposal risk should be challenged before adding prediction complexity | CLI and Paper Trial use one registered-proposal CAS service; exact PIT evidence and immutable sources bind every result; mark shocks and the statistical proxy stay separate; missing evidence fails closed |
| Data-quality anomaly review cases | **Now — SEC coverage v1 plus 6 rule families implemented and CLI-wired** | Suspect changes must be reviewed, never silently repaired | Preview is non-persistent; explicit record creates source-bound deduplicated cases; acknowledgement and disposition are audited; missing evidence withholds; `anomaly-scan --rule/--all-rules/--factor-model` live since the bundle narrowed |
| Research experiment registry | **Now — implemented and CLI-wired** | Prevents accidental cherry-picking and untraceable results | Every run records code/data/policy identity, assumptions, exclusions, metrics, artifacts, and exploratory/frozen/holdout status; `backtest-qv --register-experiment`, `list-experiments`, `compare-experiments` |
| Role-based configuration boundaries | **Domains 2-4 built (`policy_domains.py`); India ingest still gated** | Operator actions, research policy, account assumptions, secrets, and data have different change controls | Named policy versions are immutable; policy change starts a new forward trial; secrets are never exported |
| India adapter foundation | **I1 (market contracts) and I2 (source licensing) complete; blocked on data, not schema** | Market dimensions cannot be bolted onto U.S.-implicit rows safely | Market, exchange, currency, calendar, settlement, action source, benchmark, security type, ISIN, and provider-symbol intervals are first-class and parity-tested; see `INDIA_SOURCE_MATRIX.md` for the open licensing gate on prices and constituent history |

### Release artifact acceptance

Every release candidate must run
`.venv/bin/python scripts/verify_release_wheel.py DIST_WHEEL`. Acceptance
requires exact package contents and bytes, matching `METADATA`
name/version/Python/dependency/extra contracts, the expected console entry point
and pure-Python `WHEEL`, and complete `RECORD` coverage with correct SHA-256
hashes and sizes. The default path also installs the candidate with `--no-deps`
in a new temporary virtual environment and smoke-tests imports, bundled
resources, and `aios --help`. Having this verifier implemented does not certify
an old or not-yet-built wheel.

## Phase 1 — Reconstructable ingestion and incidents

### 1A. Immutable raw snapshots

Use a content-addressed layout:

```text
data/raw/{provider}/{dataset}/{YYYY-MM-DD}/{sha256}.json.gz
```

The extension may vary for a genuinely non-JSON response, but compression must
not change the hash of the original uncompressed bytes. Walk every directory
from a held project-root descriptor with no-follow semantics, write and fsync a
same-directory temporary file, then publish it with an atomic no-replace hard
link. Never overwrite an existing content address.

Store at minimum:

- snapshot ID, provider, dataset, request time, response time, HTTP status and
  content type;
- a secret-free request fingerprint and exact response SHA-256;
- relative snapshot path, byte counts, adapter name/version and parser version;
- parsed row count and canonical parsed-row SHA-256;
- parser rejection count and canonical structured rejection codes where the
  parser can withhold source rows;
- the ingest run ID and the snapshot's role in that ingest.

One ingest can consume multiple payloads, so link snapshots to `ingest_log`
through a relationship table instead of adding one nullable snapshot column.
Never store API keys, authorization headers, cookies, or signed query tokens.

Some libraries return only a DataFrame rather than the original HTTP body. Such
an adapter must either expose the true response at the transport boundary or
label its artifact `normalized_provider_export`; it must not claim exact vendor
reconstruction. Yahoo needs this explicit treatment.

Acceptance tests:

1. identical payloads deduplicate by content hash;
2. changed provider bytes produce a new immutable snapshot;
3. parsing the snapshot reproduces the stored parsed-row hash;
4. deleting or modifying a snapshot fails validation;
5. backup/restore preserves database links and raw files;
6. a failed parse still retains the response and failure metadata;
7. traversal, symlink swaps, hardlinks, FIFOs and parser downgrades fail closed;
8. network reads, stored bytes and decompression have explicit size limits; and
9. a legacy rejection-evidence migration replays exact historical bytes before
   it can certify the new schema.

Implemented foundation on 2026-07-22:

- atomic gzip storage under the content-addressed path;
- separate deduplicated payload and per-fetch observation records;
- adapter/parser versions, parsed-row hash and optional ingest-run links;
- `aios verify-raw-snapshots` tamper detection;
- backup inclusion and forward-only merge on restore.
- descriptor-relative, no-overwrite publication and bounded, nonblocking reads;
- bounded streaming at the shared HTTP boundary before response buffering;
- exact Company Facts rejection replay, including replay-backfilled historical
  evidence with no mutable timestamp exemption;
- an opt-in shared HTTP capture boundary with secret-query redaction that keeps
  malformed response bytes before JSON parsing fails;
- exact-response capture for reviewed SEC Company Facts and Submissions issuer
  ingests, plus the SEC ticker-map route used by the convenience ingest;
- exact-response capture and strict replay for the year-bounded official U.S.
  Treasury CSV fallback/cross-check;
- exact-response capture and strict replay for the SEC company-ticker map and
  reviewed Tiingo EOD route;
- canonical `normalized_provider_export` capture for production yfinance
  ingests. The export retains the provider-returned price/action fields and
  request window, links to the ingest run, records the parsed-row hash, and is
  replayed by `aios verify-raw-snapshots`;
- the first live yfinance proof: a bounded reviewed AAPL ingest stored three
  parsed rows in a 792-byte normalized export, linked it to the successful
  three-row ingest, and reproduced parsed hash
  `517ebc8074c0290bcc2b9bd642b50690843b3b933a63469989b9e77b4d2abf7d`;
- canonical normalized FRED vintage exports with secret-free request
  fingerprints, release-date-aware replay, and ingest links. The first live GDP
  proof stored 318 parsed vintages in a 22,127-byte export and reproduced hash
  `2c808a74f9ed571ff0d5c670dcecec1be80a1efbfd0bf187733f614e6a159f9b`; and
- a replay-aware exact SEC lifecycle: Company Facts and Submissions are first
  retained as capture-only evidence, then atomically promoted only after a
  successful identity-safe parse. Missing, multiply linked, partial, or
  conflicting evidence fails closed;
- a live reviewed AAPL issuer proof: two linked SEC payloads, 3,913,076 original
  bytes, 4,102 Company Facts rows plus one Submissions row, with both parsed
  hashes reproduced from the exact compressed bytes; and
- a July 24 full-universe recovery that expanded live coverage to 505 yfinance
  exports and, after the July 25 macro proof, 23 FRED exports;
- live exact-response proofs for 423 Treasury yield rows, 10,429 SEC ticker-map
  rows, and four reviewed Tiingo price sessions. Treasury and FRED exactly
  matched on their latest common DGS2/DGS10/DGS30 observation; and
- a non-destructive restore drill that restored a verified 1,542-file backup
  through the real confirmed-restore path in disposable storage, opened and
  validated DuckDB, checked all 1,535 payloads, replayed 535 parsed artifacts,
  and proved the live files were untouched.
- a pre-swap semantic restore gate that stages the complete candidate and
  requires exact application compatibility, an openable DuckDB with zero hard
  data-quality failures, valid checksum/schema envelopes for every paper and
  active/archived forward-trial document, consistent active cross-references,
  complete raw replay, and conflict-free immutable merge before creating the
  live safety backup or changing governed live state.

The active U.S. transport gate is complete. Stooq currently returns a
JavaScript verification page and remains explicitly unavailable; its response
is retained as capture-only evidence when the route runs under an ingest but
cannot be promoted to parsed price data. Historical GitHub/S&P
captures created before replay support remain byte-verifiable legacy evidence.
Every new production transport—including future NSE adapters—must add and
live-prove its own registered replay parser before use.

### 1B. Incident ledger and notification outbox

Implemented locally:

- permission-restricted SQLite `incidents` and append-only `incident_events`;
- stable deduplication, severity escalation, acknowledgement, resolution and reopening;
- systemd `OnFailure=` capture and post-success recovery for every managed service;
- bounded, allow-listed systemd result properties with no raw journal/environment capture;
- application incidents for strict health/readiness, refresh degradation, backup failure,
  scheduler runtime uncertainty and forward-policy drift;
- durable `running`, `success`, `failed`, and `interrupted` job lifecycle
  records independent of DuckDB, with live-owner overlap protection;
- CLI test/list/show/acknowledge/resolve commands and source-backed System Health history;
- checksum-manifest backup archive. Analytical restore deliberately does not roll the
  live incident ledger backward;
- schema-v4 `notification_outbox` with immutable message content, stable
  idempotency keys, incident-event linkage, immutable route activation and exact
  recovery dependency, exact state counts, exclusive five-minute leases, five
  bounded pre-send attempts, deterministic backoff and dead letters;
- append-only `notification_deliveries` with explicit success, temporary,
  permanent, ambiguous and abandoned outcomes. Ambiguous or lease-expired
  outcomes stop for review instead of retrying. Only allow-listed provider
  metadata is retained; raw responses and secrets are refused;
- atomic incident transition plus message creation for open, escalation,
  reopening, and recovery. Ordinary repeats do not produce message noise;
- migration with no historical-message backfill and a persistent local-only
  policy for diagnostic incidents;
- read-only dashboard/CLI inspection and a deterministic no-network channel
  that live-proves enqueue, claim, attempt and completion; and
- verified backup plus non-destructive restore-drill coverage for the new
  tables and attempt history;
- a one-recipient TLS-only SMTP adapter, deterministic Message-ID, safe error
  taxonomy, exact-config receipt gate, reconfiguration quarantine, and a
  separately activated optional timer.

Still pending for live email:

- private SMTP host/account/token/sender/recipient configuration;
- one real test accepted and visibly received by the owner, followed by explicit
  enablement. Historical held messages are never released.

Initial incident rules:

- refresh or timer failure;
- readiness transition from ready to blocked;
- stale prices, macro, filings or membership;
- backup verification failure;
- database checksum or forward-policy drift;
- unexpected factor-coverage drop.

Use bounded retry with backoff, deduplicate repeated symptoms, and send a
recovery notification only through an explicitly enabled channel policy.
Dashboard state and incidents must remain available when all external channels
are down.

Channel order: use the implemented local test path first, then the selected SMTP
adapter, then optional mobile push. Secrets stay in environment
or deployment secret storage. No channel credential belongs in DuckDB, a raw
snapshot, an experiment artifact, or Git.

## Phase 2 — Deterministic portfolio stress testing

**Proposal Stress Review v1 is implemented.** `aios stress-review --proposal
...` accepts only a registered, checksum-valid simulation proposal under an
unchanged forward trial. The production service shared by the CLI and Paper
Trial panel checks governance before opening DuckDB, uses a read-only
connection, and repeats the trial, account, proposal, and calculation-source
compare-and-swap checks before returning or publishing. The scenario policy is
also reloaded after calculation and at the final boundary, so concurrent bundle
changes fail closed. No artifact is stored by default; `--output PATH` is an
explicit write-once export.

Scenarios are named, versioned input policies rather than editable UI sliders
with no audit trail. The v1 bundle contains:

- 2008-style broad equity drawdown;
- the Federal Reserve 2026 supervisory severely adverse calibration: an
  [approximately 58% hypothetical equity-price
  decline](https://www.federalreserve.gov/publications/2026-stress-test-scenarios.htm),
  explicitly not a forecast;
- 2020 liquidity shock;
- 2022 inflation/rate shock;
- sector-specific crashes;
- volatility doubling and correlation convergence;
- the five largest proposal targets falling 20%, 30% and 40%;
- price- and revenue-evidence withholding demonstrations.

For each deterministic mark-shock result, the report includes:

- proposal-target and modeled proposal-portfolio loss;
- sector contribution and modeled post-shock concentration comparisons;
- modeled exit capacity and liquidity-horizon comparisons;
- assumptions, calibration basis, and an illustrative recovery path explicitly
  marked as unused in the loss calculation; and
- proposal/evidence hashes, scenario version, source-file bundle, and Git commit.

Exact PIT security identity, action-safe price history, the row-level liquidity
window, and release-aware revenue evidence are independently bound to each
target. Missing or mismatched evidence withholds its dependent numerical result;
unaffected calculations may appear only in a report visibly marked partial.

The volatility/correlation scenario is reported separately as a statistical
loss proxy with Euler risk contributions. It does not manufacture position
returns, stressed holdings, drawdown, concentration, or liquidation outcomes.
Generic sandbox-limit comparisons are human-readable advisory findings, never
automatic proposal approvals or rejections.

Historical labels describe calibration, not a claim that the next crisis will
behave identically. All scenario calculations are deterministic given identical
validated inputs, but the statistical proxy is not a deterministic market path
or price forecast.

Still future:

- owner-approved risk limits in place of generic sandbox references;
- a separate stress workflow for current simulated holdings after fills exist;
- multifactor and Monte Carlo risk models with independently validated numerical
  and provenance contracts; and
- owner-authored scenario policies, accepted only through immutable versioning
  and the same fail-closed evidence boundary.

## Phase 3 — Data review and research governance

### 3A. Anomaly review cases

Implemented v1:

- compare the exact reviewed U.S. membership and active issuer identities with
  accepted point-in-time SEC fundamentals for one decision date, deduplicating
  findings at issuer grain;
- prove covered rows through complete run/snapshot/row-set/row lineage or,
  for legacy rows only, exact raw replay plus equality with the stored
  decision-date issuer row set; never infer or backfill legacy lineage;
- accept SEC warning coverage only when positive rows were stored and the
  rejected rows were exclusively later than their filing date; zero-row and
  every other warning remain missing;
- bind each finding to its ingest run, raw snapshot, payload checksum, parser
  provenance, rule version, old/new value, severity, confidence, and suggested
  checks;
- withhold the complete scan when required source evidence is missing,
  conflicting, or tampered;
- keep `--preview` non-persistent, maintenance-lease-free, and lock-file-free,
  and make `--record` write immutable scans, append lifecycle events, and
  reconcile fingerprint-deduplicated current cases only within the independent
  operations ledger;
- let the first `--record` on schema v4 perform the supported additive
  schema-v6 migration, creating empty anomaly tables without historical
  backfill while read-only anomaly views fail closed before migration; a
  v5-to-v6 upgrade preserves existing anomaly records and deterministically
  fills only their missing `event_sequence` from prior append order;
- apply SEC source-boundary policy v2: derive the boundary as the maximum
  receipt time of only the exact snapshots consumed for accepted coverage and
  selected missing-issuer warnings, never from unrelated global ingest
  activity; permit exactly one audited legacy implicit-global-v1 to consumed-v2
  transition to use an earlier corrected boundary only when the rule bundle,
  scope, v1/v2 signatures, consumed-snapshot proof, and zero-write safety
  contract match, while blocking every same-policy regression;
- expose the queue and latest scan through read-only CLI/dashboard paths; and
- recommend acknowledgement first while permitting direct disposition when a
  named owner, note, and current evidence hash are supplied so stale reviews
  fail visibly.

The v1 scan never updates DuckDB, repairs a value, advances readiness, changes
paper/proposal/trial state, or contacts a broker.

Remaining rule families must detect, but never silently correct:

- ~~sudden share-count changes and impossible valuation jumps~~ — **built** as
  `share_count_jump@1.0.0`, scope `us-equity-shares:<universe>`;
- ~~duplicate or conflicting filings~~ — **built** as
  `conflicting_filings@1.0.0`, scope `us-equity-filings:<universe>`;
- ~~abnormal price gaps and split/dividend mismatches~~ — **built** as
  `price_action_mismatch@1.0.0`, scope `us-equity-prices:<universe>`;
- ~~changed ticker/provider mappings~~ — **built** as `mapping_drift@1.0.0`,
  scope `us-equity-mappings:<universe>`;
- ~~factor percentile jumps~~ — **built** as `factor_percentile_jump@1.0.0`,
  scope `us-equity-factors:<universe>`;
- ~~coverage deterioration versus the previous comparable run~~ — **built** as
  `coverage_deterioration@1.0.0`, scope `us-equity-coverage:<universe>`.

Each new family writes into its own ledger scope, so its monotonic
source-boundary advances independently of the SEC coverage rule and of every
other family. The built rules refuse to emit a partial scan: incomplete
corporate-action evidence, absent retained fetch evidence, an out-of-range
universe, or a baseline from another universe or a later date withholds the
whole scan rather than reporting a subset. The one deliberate exception is a
stored non-positive `shares_out`, which is a finding rather than a reason to
withhold — raising on it withheld all 503 members over 80 historical rows and
hid the very defect the rule exists to surface. No rule writes to any table.

`factor_percentile_jump` is the one rule with no stored history to read: composite
scores are computed on demand from point-in-time evidence, and no factor-score
table exists. `measure_universe_factor_percentiles()` therefore returns the
snapshot and the caller keeps it to feed the next scan's `baseline`, the same
contract `coverage_deterioration` uses. The 0-100 score maps stay deliberately
out of the scan evidence — two 503-entry mappings would not fit the 64 KiB
limit — so evidence carries a `factor_set_sha256` plus counts. Only members
scored on both sides are compared: an added or newly withheld member is a
coverage question, not a factor jump. A QV baseline is refused against a QVML
scan, because a definition change is not a move.

Because `fundamentals` is issuer-level while membership is security-level, the
issuer-keyed rules group members by `issuer_id` before scanning. The live
S&P 500 holds three dual-class issuers (Alphabet, Fox, News Corp) whose two
listed securities share one issuer, and the ledger rejects any scan that
repeats a fingerprint. For the same reason a case subject carries the knowable
`as_of_date` alongside the fiscal period: one period restated on several dates
is several distinct review cases.
`anomalies.run_detectors()` runs a selected subset and returns one independent
scan per rule; `coverage_deterioration` consumes the coverage payload of the
previous complete scan for its scope as its baseline. **CLI wiring is now
live**: the same-day `forward-restart` narrowed the frozen policy bundle from
31 files to the 13 that actually define trial-relevant behavior, and neither
`cli.py` nor `alerts.py` is in that list. `anomaly-scan` now accepts
`--rule` (repeatable), `--all-rules`, and `--factor-model`; dashboard display
required no changes, since it already renders anomaly cases generically by
scope. Preview mode does not open the operations ledger; only `--record`
does.

Each detection creates or updates a review case containing old/new values,
source snapshots, rule version and suggested operator checks. Resolution must be
explicitly `accepted`, `source_corrected`, `mapping_corrected`, `false_positive`
or `deferred`, with an audit note. Source and mapping corrections require a
complete same-scope scan recorded after the finding, with a distinct
source-boundary hash, a non-earlier source-boundary time, the exact rule
executed, the fingerprint absent, and clearance bound to a source-provenanced
accepted SEC ingest with positive verified rows. Deferral needs a future review
time and remains unresolved. Case reads and mutations replay the immutable
ordered lifecycle and verify the mutable projection. No disposition authorizes
a data edit or bypasses a readiness gate. This local integrity check trusts the
released AIOS code and OS/filesystem access controls; it is not cryptographic
attestation against an actor able to replace both code and evidence.

The one-time v1-to-v2 boundary transition changes only scan-recording
compatibility. It does not relax correction clearance: a verification scan
used for `source_corrected` or `mapping_corrected` remains subject to the
non-earlier source-boundary requirement above.

### 3B. Experiment registry

Register every backtest and factor experiment with:

- experiment/run ID and purpose (`exploratory`, `frozen`, or `holdout`);
- Git commit and dirty-worktree fingerprint;
- database snapshot hash and raw-snapshot set/manifests;
- market profile, factor policy and risk policy versions;
- parameters, exclusions, costs and tax assumptions;
- metrics and artifact paths;
- parent experiment and comparison reason.

Frozen and holdout artifacts are append-only. The UI may compare compatible
runs but must not silently choose the best result.

**Built** as `src/aios/experiments.py`. `register_experiment()` binds every run
to `git_fingerprint()` (exact commit SHA, dirty flag, changed *paths* only —
never diff content, so an uncommitted secret cannot leak into a research
artifact), `database_snapshot_sha256()` (streaming hash of the exact DuckDB
file), `retained_evidence_coverage()` (per-dataset row count and newest
`received_at`, canonically hashed — binds to the archive's shape without
duplicating what `verify-raw-snapshots` already covers), the backtest
config/metrics, and an optional parent/comparison-reason pair. `frozen` and
`holdout` purposes refuse a dirty worktree outright; `exploratory` does not.
Every registration is write-once via `publish_text_write_once` under
`data/experiments/<experiment_id>.json`, so append-only is structural rather
than convention, and `load_experiment()` re-verifies a `document_sha256` on
every read. **CLI-wired**: `backtest-qv --output PATH --register-experiment
--experiment-purpose ... --experiment-notes ...`, plus `list-experiments` and
`compare-experiments ID ID...`, once the same `forward-restart` narrowed the
frozen bundle and freed `cli.py`. No dashboard exposure yet.

## Phase 4 — Configuration boundaries and India foundation

The detailed, source-reviewed execution sequence is in
[`INDIA_BUILD_PLAN.md`](./INDIA_BUILD_PLAN.md). That plan starts with Nifty 50,
uses NSE as the primary venue and BSE as a first-class identity/cross-check
venue, and contains an explicit free-versus-paid historical-data gate.

Separate five configuration domains:

1. operator configuration: scheduler, retention, notification routing;
2. research policy: factor definitions, rebalance and risk constraints;
3. market profile: sources, sessions, benchmark and corporate actions;
4. account policy: capital, costs, taxes and settlement assumptions;
5. secrets: credentials and tokens, stored outside versioned policy artifacts.

Research, risk, market and account policies receive immutable names and
versions. A material change creates a new forward trial instead of modifying an
existing freeze.

**Domains 2-4 built** as `src/aios/policy_domains.py`. Operator configuration
(domain 1) already lives in the frozen `config.py`; secrets (domain 5) already
live outside versioned artifacts in `.env`. What was missing was an explicit
identity for the other three: they existed only as unversioned Python
constants in `factors/policy.py`, `risk/policy.py`, and `backtest/costs.py`,
visible only as a diff in the frozen-bundle file hash, never addressable by
name.

`current_research_policy()`, `current_market_profile()`, and
`current_account_policy()` each take a caller-assigned `name`/`version` and
return a content-addressed snapshot built by importing the real values from
those three frozen modules — nothing hand-duplicated, so drift in the source
changes the computed hash automatically rather than silently diverging from a
copy. `policy_snapshot()` combines all three into one document plus a combined
hash. `market_profile`'s fields (benchmark `SPY`, universe `sp500`, source
names, session-close rule) are hand-declared rather than introspected, because
the equivalent values in `market_calendar.py` are private module attributes;
every value is a convention already repeated verbatim across this codebase's
CLI defaults and `agent.md`'s own text, not invented for this module.

`experiments.register_experiment()` now embeds a policy snapshot in every
registered document by default (overridable via `policy=`), and
`compare_experiments()` surfaces `research_policy_name`/`_version` and a
`comparable_policy` flag. As of 2026-08-11 this is CLI-wired too:
`backtest-qv --output PATH --register-experiment`, `aios list-experiments`,
`aios compare-experiments ID ID...` — the frozen-bundle boundary that blocked
this was narrowed by the same-day `forward-restart`.

Before the first Indian row is ingested, make these dimensions first-class:

- market and exchange;
- trading currency;
- trading calendar/time zone and session boundaries;
- settlement convention;
- corporate-action source;
- benchmark;
- security type and ISIN;
- date-bounded provider symbols.

Then add Indian adapters in small reviewed slices: identity and symbols,
historical membership, prices/actions, filings/fundamentals, benchmarks,
calendar/settlement, macro, fees/taxes, and finally U.S.-versus-India contract
parity tests. Do not fork the factor, portfolio or risk core merely to add India.

## Dashboard product rules

The supplied dashboard POC is a useful information-architecture reference, not
a data source or implementation specification.

Use now:

- a `Today` view with separately scoped Research, Paper Trial, and Operations
  status, followed by one highest-priority safe action;
- compact status cards and a segmented research-view switch informed by
  CopyUI examples plus Carbon, Cloudscape, and Atlassian dashboard conventions,
  implemented with native Streamlit rather than a second frontend dependency;
- URL-backed workspace, research date, scoring method, research surface, and
  selected company so one link preserves the state needed to reproduce a bug;
- a dedicated `System Health` workspace;
- coverage charts, source freshness, scheduler outcomes and next human action;
- source-backed local incident history and unresolved counts;
- exact local outbox, SMTP route, configuration-match, and optional-timer state;
- full reviewed company name together with the market symbol;
- progressive disclosure: plain language first, technical evidence on demand.
- one light institutional visual system with a navy shell, blue interaction,
  semantic status colors, symmetric CTA/card geometry, and responsive stacking;
- a selectable ranked row that opens the corresponding Company Detail view;
- a fixed four-stage Paper Trial path that keeps proposal, forward policy,
  timing review, and local record visibly distinct.

Completed performance gate on 2026-07-29:

- the cold Research path now serves the existing scalar factor contract through
  decision-scoped universe batch reads. Dated identity routing, issuer
  ownership, restatements, provider gaps, source priority, ambiguity checks,
  and scalar fail-closed fallback remain explicit;
- fresh-process 503-company checks matched every serialized QV and QVML row
  exactly. QV improved from 29.0 to 2.8 seconds and QVML from 32.5 to 3.3
  seconds, while Store query calls fell from 4,278 to 12;
- the optimization does not persist scores or evidence and leaves the active
  forward trial's frozen factor-policy files unchanged. Peak working sets were
  about 1.10/1.19 GiB, so cold builds are serialized.

Next UI gates:

- keep the Research fast path behind scalar-equivalence, bounded-query,
  temporary-relation cleanup, read-only, peak-memory, and serialized-cold-build
  regression gates;
- split the remaining Streamlit monolith by view only after the pure status
  model and rendered characterization tests protect current behavior.
- repeat desktop and phone visual QA when Streamlit is upgraded; the UI now
  relies on tested 1.58 widget and keyed-container contracts.

Do not add React, Tailwind, animation packages, or a separate frontend service
merely for visual polish. Reconsider that boundary only after a versioned
application API plus multi-user authentication, roles, mobile workflows, or
real-time interaction becomes a real product requirement.

Use only after the underlying feature exists:

- external delivery receipts and channel-specific controls;
- current-holdings and advanced-risk panels beyond the implemented proposal
  stress panel;
- experiment comparisons and exports;
- event calendars;
- sector allocation and top-holdings graphics after simulated holdings exist.

Do not copy:

- invented 503/503 coverage, returns, holdings, risk metrics or alerts;
- a green `READY` label that bypasses fail-closed checks;
- a chart that mixes backtest results with a current paper account;
- dense navigation entries with no supported workflow behind them.

## Explicitly deferred

Do not implement broker integration, live orders, autonomous policy changes,
additional factor-weight tuning, or performance marketing while establishing
the replacement U.S. forward trial. Controlled-capital discussion remains gated
by at least 8–12 weeks of untouched monitoring plus naturally triggered operations,
external alerts, raw-data versioning, broker reconciliation, and user-approved
jurisdiction/account/risk policies.

## Definition of product-ready

“Ready to use” must always name the use case:

- **Supervised research:** readiness and validation pass for the exact decision
  date; already available for the current U.S. reference market.
- **Local paper monitoring:** the scheduled close is reviewed and simulation is
  explicitly confirmed; already supported, with no broker connection.
- **Operational beta:** the active U.S. raw-snapshot and restore-testing gates
  pass; external incident delivery and sufficient untouched naturally triggered
  timer evidence remain.
- **India research beta:** every India foundation field and adapter parity gate
  passes for a bounded reviewed universe.
- **Controlled capital:** separate future approval; never implied by a
  backtest, dashboard, or elapsed calendar time alone.
