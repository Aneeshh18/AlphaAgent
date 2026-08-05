# AIOS Product Completion Gates

This document defines what AIOS may be claimed ready for. It separates five
operating modes because a passing research gate does not imply that paper
recording, unattended operation, hosted access, or real-capital execution is
safe.

`ARCHITECTURE.md` remains the design authority and `FUTURE_BUILD_PLAN.md`
controls implementation order. This file is the claim and exit-gate ledger.

## Status language

- **Supported** — code and current evidence satisfy the stated use.
- **Implemented; evidence required** — the feature exists, but live or release
  proof is missing or stale.
- **Blocked** — a known condition prevents the use now.
- **Code gap** — the required implementation does not exist.
- **External decision** — owner, provider, licensing, security, or legal input
  is required and must not be inferred.
- **Time-bound** — proof must accumulate naturally; tests or backfills cannot
  replace elapsed observation.

A feature is not complete because a screen, schema, test double, or successful
backtest exists. Evidence must be exact for the claimed operating mode and
decision date.

## Current evidence snapshot — 2026-07-30

This is a dated snapshot, not a permanent certification. Refresh it with
`aios preflight`, exact-date `aios readiness`, `aios validate`,
`aios forward-status`, `aios scheduler-status`, unresolved incident/case views,
and release verification before making a new claim.

- Supervised research and registered-proposal stress review are available.
- The certified U.S. decision close is 2026-07-30: 503 reviewed members,
  503/503 stable identities and reviewed prices, and 500/503 PIT filing
  coverage.
- Quality + Value has 302 complete scores; 201 names are withheld rather than
  estimated.
- SEC Company Facts v3 remains a blocked candidate, not an active data policy.
  Its reviewed replay processes 500 payloads, emits 1,119,730 rows, rejects 42
  future-period rows, withholds 17,860 unsupported-context rows and 26,152
  ambiguous storage keys, and emits zero duplicate database keys. A disposable
  candidate makes 394/503 QV scores structurally computable, but that count is
  before freshness and lineage gates and is not a safe-use or certified-score
  claim. All 1,270,623 live fundamental rows and all rows in the disposable
  candidate remain unlineaged, so zero issuers are currently eligible for
  governed v3 activation. The active v2 replay path and current certified score
  state remain unchanged; activation is blocked. The read-only
  `companyfacts-v3-plan --as-of YYYY-MM-DD` command now classifies exact,
  already-captured v2 evidence and may publish a content-addressed review
  artifact, but it performs no provider fetch or state mutation and exposes no
  activation path.
- Validation has zero hard failures and three warning families.
- The paper account has $100,000 simulated cash, zero holdings, zero
  executions, and no broker connection.
- The active forward policy is unchanged, but its one registered proposal is
  expired. Proposal creation and retrospective paper recording are blocked.
- The hardened rollover lifecycle now separates a stable
  `forward-rollover-plan.v4` payload from volatile preflight/operations
  observations. `--write-plan` publishes only a content-addressed,
  checksum-verified report after source/policy rechecks. Explicit activation
  consumes only that artifact and adds a fresh verified backup, fixed-order
  locks, final gates/CAS, one atomic active-trial swap, append-only phase
  evidence, confirmed recovery, and restore reconciliation. The mechanism is
  implemented, but live activation is currently gate-blocked and must remain
  so unless current operations, readiness, and deadline checks are all clean
  in a naturally prospective window.
- One natural daily cycle completed successfully on the active host on
  2026-07-30. A current normal-session report-only check verified all three
  user timers enabled and waiting with successful latest results, scheduled
  next runs, and linger enabled. Unattended operation remains blocked by one
  acknowledged XOM SEC evidence case and the fundamentals-refresh warning.
- A fresh 2026-07-30 backup is checksum-valid: 4,262 files, 397,657,272 bytes,
  manifest SHA-256
  `a3081e23103997e3f94b0f9066cbc8ed6c446b1b9958fb75b8dad73ed5afabce`.
- That exact backup passed the production disposable restore drill: 4,252 raw
  payloads, 4,424 parsed replays, and zero hard validation failures.
- The last certified release checkpoint passed the full suite,
  repository-wide Ruff, bytecode compilation, reproducible wheel builds, exact
  source-to-wheel verification, and clean-install smoke. Those results are
  historical checkpoint evidence only; every release candidate must regenerate
  the complete proof from its stabilized source.
- The hosted read-model candidate remains isolated and excluded after
  adversarial review. No hosted-service claim is valid.
- SMTP is incomplete and off. No external incident-delivery claim is valid.
- Real-capital execution is disabled; no broker credential, live account, or
  order route exists.

## 1. Supervised U.S. research

**Current claim:** supported for local, supervised research on an exact date
that passes readiness and validation. It is not personalized advice, an alpha
claim, or permission to trade.

| Gate | State | Current evidence | Exit condition |
|---|---|---|---|
| Exact-date readiness | Supported | 2026-07-30 passes historical-research and paper readiness | Re-run for every claimed decision date |
| PIT universe and identity | Supported | 503 members; 503/503 stable security identities | Every member remains date-bound to reviewed security, issuer, CIK, and provider intervals |
| Prices, actions, benchmark, and calendar | Supported for current research | 503/503 reviewed prices; SPY and the U.S. calendar reach the certified close | Current action-safe coverage remains sufficient; historical warnings stay excluded from unsupported claims |
| PIT filings and macro | Supported with visible gaps | 500/503 filing coverage; release-dated macro is available | Missing evidence remains withheld and source-bound |
| Research scores | Supported with withholding | The active state has 302/503 complete QV scores and withholds 201; a disposable v3 candidate makes 394/503 structurally computable only before freshness and lineage gates | Keep the candidate count out of safe-use claims; never fill missing scores with estimates; require freshness-qualified, source-lineaged governed replay before promotion |
| Raw provenance and replay | Partially supported | Active U.S. transports have immutable, checksum-verified evidence, but all 1,270,623 live fundamental rows remain unlineaged | Migrate every promoted v3 issuer to explicit source-row lineage and prove exact replay without inferring lineage for legacy rows |
| Fundamental system-time generations | Row-version and factor-read contract implemented; paper activation deferred | Post-merge rows and deletion tombstones are append-only; latest-version reconstruction is a hard data-quality invariant; named IDs pin scalar and batch fundamental rows and fail closed on unknown IDs | Pin reference identity routing, prices, membership, macro and policy under one complete generation; bind it into a new prospective proposal schema without modifying the active frozen trial |
| Data-quality review | Partially supported | FDXF/HONA are resolved as explicit zero-row gaps with withholding preserved; XOM remains acknowledged pending predecessor-successor evidence | Preserve explicit gaps; complete reviewed XOM lineage and add a first-class expected-new-issuer review state |
| Company Facts v3 selection | Parser and read-only planner implemented; activation unavailable | The 500-payload replay emits 1,119,730 rows, rejects 42 future-period rows, withholds 17,860 unsupported-context rows and 26,152 ambiguous storage keys, and emits zero duplicate keys; `companyfacts-v3-plan --as-of ...` rechecks exact local v2 evidence without fetch or mutation | Zero issuers are currently eligible: add source-lineaged governed migration, explicit freshness gates, candidate-DB diff, restore proof, and a separately governed activation contract before promotion |
| Experiment governance | Code gap | Backtests are engineering evidence, not registered experiments | Append-only exploratory, frozen, and holdout registry binds code, data, policy, assumptions, and artifacts |
| Long-history event coverage | Evidence required | Complete pre-August-2023 announcement and delisting provenance is not claimed | Extend source-reviewed history independently of current-use certification |
| Release and recovery | Implemented; evidence required per candidate | Wheel verification exists and the latest full backup verifies | Full tests, lint, compilation, stable source hashes, wheel verification, and a current disposable restore drill all pass |
| Local schema upgrade | Backup-first workflow and exact-attempt recovery implemented; live execution pending | A new release can add DuckDB and incident-ledger contracts, but opening a writer must not become the first recovery point | Run only after the mutation lease is free; require exact backup verification, disposable rehearsal parity, unchanged old-row evidence, checksum-chained phase receipts and forward recovery; retain release-pinned code for any exact defective-migration rollback |

Research completion does not require inventing a score for every company. It
requires every published or withheld result to be reproducible, source-bound,
and honestly labeled.

## 2. Recurring supervised paper simulation

**Current claim:** read-only paper status and stress review are supported. A new
proposal or paper fill is not currently available because the registered
proposal expired.

| Gate | State | Current evidence | Exit condition |
|---|---|---|---|
| Simulation-only account | Supported | Checksum-protected account; no broker; $100,000 cash | Account, proposal, and trial envelopes remain valid and owner-only |
| Prospective proposal generation | Supported in code | Proposal must be frozen before its scheduled U.S. open | Recheck deadline and evidence at the final write boundary |
| Risk and liquidity review | Supported with generic policy | Long-only, concentration, exposure, turnover, drawdown, sector, and ADV checks exist | Owner-approved immutable risk limits replace generic sandbox references |
| Registered-proposal stress review | Supported | Exact proposal, policy, PIT evidence, and final CAS are required | Preserve fail-closed withholding and read-only default |
| Explicit close-window recording | Supported in code | Confirmation and close-to-next-open timing are enforced | Complete one naturally valid proposal-to-fill cycle without retrospective action |
| Forward policy integrity | Supported | Active trial reports unchanged | Never rewrite its frozen policy sources in place |
| Expired-proposal lifecycle | Governed v4 lifecycle implemented; live activation remains gate-dependent | Preview binds the exact predecessor/account/readiness/blueprint/policy/path boundary while volatile time and operations evidence stay outside its hash; activation requires the persisted plan, exact SHA, explicit confirmation, fresh gates, verified backup, locks, final CAS, atomic cutover and durable recovery | Prove the command in a planned naturally prospective window; preserve the active trial until every live gate passes |
| Final forward-library CAS | Code gap | Cooperative CLI lease does not cover arbitrary library callers | Add final account/trial/proposal identity checks in a new policy version |
| Recurring cycle evidence | Evidence required | Zero paper executions and zero holdings | Observe proposal, review, explicit simulation, mark, backup, and recovery in normal use |
| Current-holdings stress | Code gap | Stress review covers the pending proposal only | Add a separate evidence-bound workflow after valid simulated holdings exist |
| Account, tax, and final risk policy | External decision | Jurisdiction and account type are unset; taxes remain zero | Owner supplies jurisdiction, account type, costs, tax assumptions, and risk budget before after-tax claims |

No deadline justifies rushing a policy activation. If a prospective generation
window is missed, use a later certified close.

## 3. Unattended local operations

**Current claim:** not ready. Automation code exists, but current operating
evidence has unresolved blockers. “Unattended” applies to data refresh,
monitoring, and backup—not autonomous portfolio approval or execution.

| Gate | State | Current evidence | Exit condition |
|---|---|---|---|
| Recoverable daily workflow | Certified through August 3 | A complete benchmark-first cycle certified the exact close; a later already-current run bound the successful job receipt and fresh readiness report to the open incident generation | Observe the next naturally scheduled session and retain the same producer-verified evidence without operator intervention |
| Scheduler runtime | Installed; current runtime blocked | Unit files are enabled but the user bus was unavailable to report waiting times; daily, filings, and backup service incidents remain open after the failed cycle | Reload/reinstall the verified units, confirm linger and live timer state, then observe successful natural runs |
| Incident and job ledger | Supported | Durable open/repeat/acknowledge/resolve history exists | Every actionable failure remains visible and source-bound |
| Current operating queue | Blocked | The acknowledged XOM SEC case, pending August 5 FERG/EA event import, current filing/backup/systemd failures, and legacy unproven resolutions remain visible; the former daily-cycle failure now has producer-verified recovery | Complete source review/import, observe natural service recovery, and resolve or attest each remaining incident only with current-generation proof |
| Notification outbox | Supported locally | Retry, lease, dead-letter, and no-network proof exist | Preserve idempotency and immutable delivery history |
| External alert delivery | External decision / evidence required | TLS-only SMTP adapter exists; configuration and receipt proof are absent | Private config, one exact-route receipt test, owner confirmation, and explicit enablement |
| Backup and restore | Supported for the current checkpoint | The 2026-08-02 backup-first state upgrade created and reverified 4,785 files / 423,279,406 bytes with checksum-chained completion evidence | Repeat the disposable restore/replay drill for the final release candidate and after material governed-state changes |
| Cross-store concurrency | Partially supported | Supported CLI writers share a maintenance lease | Complete final library-level CAS and keep unsupported direct writers outside production |
| Broader anomaly detection | Code gap | Only SEC coverage v1 exists | Add shares, valuation, filing, price/action, mapping, factor-jump, and deterioration rules incrementally |
| Availability objective | External decision | Current deployment depends on one local machine and user service manager | Define acceptable uptime, recovery, and host-failure behavior before claiming an operational service |

## 4. Hosted multi-user beta

**Current claim:** not deployable. The local dashboard, policy engine, `Store`
boundary, audit ledger, backup primitives, and local security hardening are
reusable foundations only.

| Gate | State | Exit condition |
|---|---|---|
| Public read model | Bounded local-only contract implemented; hosted promotion enforced false | Replace Python-local canonicalization with an interoperable signed envelope, verify the signature out of band, and retain exact readiness, source identity, membership, size and TTL gates before integration |
| Versioned application API | Code gap | Stable authenticated contracts replace direct Streamlit/process coupling |
| Authentication and sessions | Code gap | Reviewed identity provider, secure session lifecycle, optional MFA, and account recovery |
| Authorization and actor audit | Code gap | RBAC and authenticated actor IDs guard every read and mutation |
| HTTPS and web security | Code gap | Managed TLS, secure cookies, CSRF protection, headers, rate limits, and abuse controls |
| Local secret and path hygiene | Partial foundation only | Owner-only runtime directories and masked configured credentials do not authenticate remote users or defeat a hostile host |
| Tenant isolation | Code gap | Tenant identity, data boundaries, quotas, audit export, and deletion are enforced and tested |
| Concurrent production storage | Code gap | Multi-user database replaces single-process DuckDB behind the `Store` boundary |
| Deployment and rollback | Code gap | Reproducible image, migrations, persistent storage, health checks, rollback, and environment promotion |
| Secrets management | Local hardening only | Managed hosted secrets with rotation replace workstation `.env`; repository and Streamlit secret files remain excluded |
| Observability and incident delivery | Code gap / external decision | Central logs, metrics, traces, uptime checks, paging route, and ownership |
| Backup and disaster recovery | Code gap | Tenant-aware backups, restore drills, retention, RPO, and RTO |
| Capacity and concurrency | Evidence required | Load tests cover research memory, serialized cold builds, API limits, and failure recovery |
| Supply-chain security | Code gap | Locked dependencies, CI gates, vulnerability scanning, SBOM, signed artifacts, and provenance |
| Repository-secret hygiene | Evidence required | Rotate any historical secrets and assess history cleanup before public distribution |
| Privacy, terms, and data licensing | External decision | Approved retention, privacy, redistribution, and provider-license boundaries |
| Independent security review | External decision / evidence required | Threat model, security review, and remediation pass before external users |

Do not expose the current unauthenticated dashboard beyond loopback. Choose the
hosting region/provider, authentication model, tenant isolation, and data
licensing before implementing this mode.

## 5. Controlled real-capital operation

**Current claim:** disabled and not approved. The research, simulation,
accounting, risk, and audit foundations do not constitute an order system.

| Gate | State | Exit condition |
|---|---|---|
| Jurisdiction, account, broker, and capital scope | External decision | Owner approves exact jurisdiction, account type, broker, capital cap, and risk budget |
| Legal and compliance operating model | External decision | Qualified review defines advisory, brokerage, disclosure, recordkeeping, and approval obligations |
| Broker connectivity | Code gap | Credential-isolated sandbox integration passes before any live credential exists |
| Order-management lifecycle | Code gap | Idempotent submit, acknowledge, reject, partial fill, cancel/replace, recovery, and immutable audit |
| Pre-trade controls and kill switch | Code gap | Hard notional, position, concentration, price, session, liquidity, and emergency-stop controls |
| Broker and custodian reconciliation | Code gap | Orders, fills, cash, positions, fees, actions, and statements reconcile independently |
| Live holdings risk | Code gap | Evidence-bound holdings stress, exposure, drawdown, liquidity, and escalation |
| Tax and settlement policy | External decision / code gap | Account-specific lots, wash-sale handling, settlement, withholding, and reporting are approved |
| Market-data rights and freshness | External decision / evidence required | Licensed data is sufficiently current for the approved execution convention |
| External monitoring and dual control | Code gap | Independent alert delivery, manual approval, role separation, and escalation are live |
| Hosted security and disaster recovery | Blocked by hosted gates | Hosted multi-user/security gates pass for the controlled environment |
| Strategy and model evidence | Code gap / time-bound | Registered experiments, holdout evidence, and prohibited-claim controls are complete |
| Untouched forward observation | Time-bound | At least 8–12 weeks under the final frozen policy, including natural operations and no retrospective fills |
| Bounded pilot | External decision / evidence required | Written pilot limits, stop conditions, reconciliation cadence, and rollback are approved and tested |

Broker implementation is not the next task. Finish recurring paper governance,
operational evidence, external alerts, configuration versioning, and untouched
forward observation first.

## Release claim gate

Every candidate—regardless of operating mode—must have:

1. a reviewed, intentionally scoped commit with no unexplained files;
2. stable pre/post source and governed-state hashes;
3. complete tests, Ruff, bytecode compilation, and diff checks;
4. an exact wheel verification and clean temporary-environment smoke;
5. current backup verification and a proportionate restore drill;
6. no unresolved critical release/security finding;
7. documentation whose claim matches live `preflight` evidence; and
8. explicit separation between code proof, live-host proof, external decisions,
   and time-bound evidence.

Passing this release gate proves packaging and reviewed behavior. It does not
approve hosted access, personal advice, autonomous execution, or real capital.
