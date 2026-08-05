# Governed Forward Rollover v4 Lifecycle Contract

Status: read-only preview and stable write-once plan are production-available.
The explicitly confirmed activation engine, append-only crash journal,
deterministic recovery, and restore validation are implemented and isolated by
a production-disabled build gate. Recovery remains available only for an exact
attempt journal created by a previously enabled build. The filename is retained
for links created while the earlier v2 design was under review.

## Outcome and invariants

The v4 preview can inspect exactly one approved, registered proposal whose
prospective simulation window expired without an execution. It creates a
stable, checksum-protected review plan outside governed paper state and remains
the default command mode. A dormant activation mode is designed to consume only
that exact content-addressed artifact after its SHA-256 is supplied and explicit
confirmation is present. The current production build refuses it before state
change. When deliberately enabled in a later reviewed build, activation archives
the predecessor unchanged and starts one later prospective cycle. It cannot
edit the old proposal, mutate the paper account, record a retrospective fill,
or create a broker order.

The existing v1 trial remains authoritative until the final active-file
compare-and-set. `src/aios/forward.py` stays byte-identical, so installing v4
does not itself create policy drift in the active trial. The successor uses the
v1-compatible forward envelope plus `rollover_lifecycle_schema_version = 2`;
therefore existing `read_forward_trial()` and `assess_forward_trial()` callers
continue to work.

## Current contracts reused

- `src/aios/paper.py:124` validates paper envelope checksums.
- `src/aios/paper.py:299` creates a proposal prospectively and rechecks the
  generation deadline before atomic persistence.
- `src/aios/paper.py:574` classifies the close-to-next-open simulation window and
  returns `expired` after the following U.S. session opens.
- `src/aios/paper.py:1107` provides document compare-and-set checks.
- `src/aios/paper.py:1115` and `1133` provide cross-process document/account
  locks.
- `src/aios/paper.py:1139` stages, fsyncs, and atomically replaces paper JSON.
- `src/aios/forward.py:225` validates the active forward envelope.
- `src/aios/forward.py:248` compares the active trial with its frozen policy and
  proposal evidence.
- `src/aios/forward.py:337` hashes an explicit policy-file set.
- `src/aios/forward.py:492` stages, fsyncs, and atomically replaces the active
  forward file.
- `src/aios/maintenance.py:144` serializes governed project mutations.
- `src/aios/operations.py:57` creates the full DuckDB, paper, raw-evidence, and
  operations snapshot.
- `src/aios/operations.py:169` verifies every backup manifest entry and checksum.
- `src/aios/operator_evidence.py:86` resolves the operator-visible proposal from
  the active registry rather than filename order.
- `src/aios/operator_preflight.py:710` owns the safe next-action contract.

`replace_drifted_forward_trial()` in `src/aios/forward.py:145` is not reused as
the rollover transaction. It moves the active predecessor before installing its
replacement and has no lifecycle plan, fresh-backup binding, project/document
lock composition, or crash receipt. That sequence is acceptable only for its
older drift-restart contract.

## State machine

1. `INELIGIBLE`
   - Any checksum/path/schema/cross-reference is invalid.
   - The account is broker-connected or not simulation-only.
   - The predecessor policy already drifted.
   - There is not exactly one unresolved registered proposal.
   - Any execution shares a proposal ID but not its exact proposal hash.
   - A governed proposal JSON is unregistered, including custom filenames.
   - The unresolved proposal is not approved, not bound to the current account,
     or not expired.
   - The successor date is not later, readiness fails, or its generation window
     is closed.

2. `PREVIEWABLE`
   - All predecessor evidence passes.
   - The later decision close passes `assess_us_readiness(..., purpose="paper")`.
   - The exact production proposal constructor can build a successor against a
     disposable path.

3. `PLANNED`
   - A canonical `forward-rollover-plan.v4` payload binds:
     - predecessor trial/proposal raw-file and payload hashes;
     - account raw-file/payload hash and canonical execution-list hash;
     - successor decision date, next session, top-N, complete normalized proposal
       intent, and generation deadline;
     - complete readiness payload and hash;
     - predecessor and successor policy files/bundle hashes;
     - exact final proposal path and no-fill disposition;
     - required fresh backup, no-account-mutation, no-backfill, and no-broker
       proofs.
   - Wall-clock inspection time, current operations availability, preflight
     timestamp/checksum, and current blockers remain in the volatile preview
     observation and are excluded from the plan hash.
   - The optional plan artifact is content-addressed and write-once at
     `data/reports/forward_rollovers/plans/<plan_sha256>.json`.
   - Publishing rechecks predecessor/account/proposal raw hashes and every
     successor policy hash. Identical repeated or racing publication is
     idempotent; different bytes, tampering, symlinks, hard links, or collisions
     fail closed.

4. `COMMITTING`
   - This phase is a dormant, test-only engine in the current build.
     `available_in_this_build` is false and production activation is refused
     before state changes until the owner constants and prospective window are
     approved.
   - `--plan`, the exact lowercase `--plan-sha256`, and
     `--confirm-rollover` must all be present. Preview-only options are rejected.
   - Lock order is the project maintenance lease followed by every governed
     document path in deterministic sorted order.
   - Account/trial/predecessor/readiness/policy and deadline are rechecked.
   - A new full backup is created and independently verified. The backup
     manifest must contain the exact raw hashes of the active trial, account, and
     predecessor proposal. Its path and manifest hash are written into both the
     transaction receipt and successor lineage.
   - The successor proposal is built into a non-authoritative stage and its
     normalized intent must equal the confirmed plan.

5. `OUTPUTS_PUBLISHED`
   - A byte-identical predecessor copy is published write-once with `O_EXCL`
     semantics and directory fsync.
   - The unique successor proposal is atomically published.
   - The predecessor remains the active trial. A process death here leaves old
     evidence authoritative.

6. `ACTIVE_SUCCESSOR`
   - The staged successor forward envelope is atomically installed last.
   - Its lifecycle lineage records the plan, predecessor, archive, no-fill
     disposition, and verified backup.
   - The active trial, successor proposal, archive, account, predecessor, and
     receipt are verified after the switch.

7. `ROLLED_BACK` or `RECOVERED`
   - An in-process failure restores the predecessor bytes with an active-file
     CAS and removes only an exact hash-matched successor.
   - A process-crash recovery command reads the checksum-protected receipt.
     If the successor is authoritative and all hashes/lineage/backup pass, it
     finalizes `verified`. If the predecessor is authoritative, it removes only
     exact plan-bound orphan/stage files and finalizes `recovered_rolled_back`.
     If the active file matches neither state, recovery fails closed.

## Successor policy bundle

The planned successor freezes the existing `FORWARD_POLICY_FILES` plus:

- `src/aios/forward_rollover.py`
- `src/aios/forward_rollover_activation.py`
- `src/aios/alerts.py`
- `src/aios/artifacts.py`
- `src/aios/cli.py`
- `src/aios/config.py`
- `src/aios/ingest/edgar.py`
- `src/aios/ingest/prices.py`
- `src/aios/maintenance.py`
- `src/aios/operations.py`
- `src/aios/operator_evidence.py`
- `src/aios/operator_preflight.py`
- `src/aios/raw_snapshots.py`
- `src/aios/rollover_journal.py`
- `src/aios/security.py`
- `src/aios/storage/schema.py`
- `src/aios/storage/store.py`

This closes the v1 gap where SEC fiscal normalization, price normalization, and
storage semantics could materially alter factor evidence without appearing as
forward-policy drift. A change to any of these files changes the v4 plan and
must be reviewed again.

## CLI and operator behavior

Read-only preview:

```bash
.venv/bin/aios forward-rollover \
  --as-of YYYY-MM-DD \
  --json
```

Optional stable review artifact:

```bash
.venv/bin/aios forward-rollover \
  --as-of YYYY-MM-DD \
  --write-plan \
  --json
```

The preview never constructs or executes activation. The optional write
publishes only under `data/reports/forward_rollovers/plans`; it never changes
`data/paper`, DuckDB, the operations ledger, fills, or broker state.

Future confirmed activation contract (documented and test-only; production
builds currently refuse this command before any state change):

```bash
.venv/bin/aios forward-rollover \
  --plan data/reports/forward_rollovers/plans/PLAN_SHA256.json \
  --plan-sha256 PLAN_SHA256 \
  --confirm-rollover
```

Confirmed reconciliation after an interrupted attempt:

```bash
.venv/bin/aios forward-rollover-recover \
  --plan data/reports/forward_rollovers/plans/PLAN_SHA256.json \
  --plan-sha256 PLAN_SHA256 \
  --confirm-recovery
```

Preflight and dashboard recommend only `forward-rollover`, which is preview by
default. They never surface confirmation flags. Activation failure messaging
does not claim that nothing changed; it directs the operator to recovery when
an attempt journal may exist.

`available_in_this_build` is currently `false`. The activation example above
is therefore a reviewed future interface, not an operator instruction for the
current release. Recovery is only for an attempt journal created by a future
enabled build; it must not be used to manufacture a successor today.

`paper-propose` continues to refuse a second proposal while one registered
proposal is unresolved. `forward-restart` remains the policy-drift lifecycle and
must not become an alias for rollover.

## Threat model

| Threat | Failure mode | Control |
|---|---|---|
| Stale preview | Account, proposal, trial, readiness, or source changes after review | Raw and payload hashes, readiness and normalized-intent hashes, repeated CAS |
| Time-of-check/time-of-use | U.S. open arrives during backup or proposal computation | Deadline before work, after backup, after proposal build, and immediately before active switch |
| Concurrent CLI mutation | Execute/propose/restore races rollover | Project lease plus account/trial/proposal locks |
| Direct library mutation | Supported CLI lease bypassed | Same document locks and CAS inside rollover core |
| Orphan proposal | Filename scan misses a custom proposal | Scan every JSON in governed proposal namespace and validate identities |
| Execution-ID collision | Same proposal ID recorded with another hash | Execution ID and payload-hash parity is mandatory |
| Backup that is old or unrelated | Restore proof does not cover transition source | Backup is created under the commit lease and its manifest entries must equal source raw hashes |
| Archive rewrite | Predecessor history is rewritten | Write-once copy, raw SHA-256 equality, fsync, and collision refusal |
| Crash between files | Proposal exists but active registry remains old | Active trial switches last; checksum receipt allows exact rollback or roll-forward verification |
| Partial/hostile path | Symlink, hardlink, or `..` redirects writes | Root containment, ancestor symlink rejection, single-link regular-file checks |
| Retrospective fill | Expired proposal is applied for convenience | Rollover never calls execution or account-write APIs; old disposition is no-fill |
| Broker action | Lifecycle accidentally reaches real capital | Account must be `simulation_only`, `broker_connected is False`; no broker API exists in the module |
| Source-policy escape | Parser changes alter scores without drift | Expanded successor bundle includes SEC, price, schema, store, backup, and rollover policy |
| Receipt tampering | Recovery follows fabricated phase | Checksum envelope plus exact plan/source/output/backup hashes; mismatch blocks |
| Malicious local owner | Owner rewrites payload and checksum together | Out of scope for local checksums; external signatures/append-only remote attestations are required for adversarial-host protection |

## Compatibility

- Existing v1 trials and archives remain byte-for-byte unchanged.
- `forward_schema_version` remains 1 for compatibility with current readers.
- v2 behavior is marked by `rollover_lifecycle_schema_version = 2` and mandatory
  `lineage`.
- Active path, proposal-record fields, paper account/proposal schemas, and
  operator evidence paths do not change.
- Restore validation accepts terminal rollover receipts and requires a unique
  verified receipt for an active rollover successor.
- A backup created before activation remains a valid exact rollback source. A
  later post-activation backup must include the terminal receipt and v2 lineage.

## Required tests

- Plan hash is deterministic across different invocation times and equivalent
  fresh preflight snapshots; volatile observations may change independently.
- Stable plan publication is content-addressed, checksum-verified, idempotent,
  and confined to its report namespace without altering governed bytes.
- Expired/approved/exactly-one-unexecuted eligibility.
- Custom-named orphan and unsafe proposal paths.
- Artifact checksum tampering, collision, symlink, hard-link, concurrent
  publication, and source/policy drift.
- Wrong plan confirmation and tampered plan.
- Account/trial/proposal/execution/readiness/policy CAS failures.
- Policy-addition acknowledgement and predecessor drift refusal.
- Fresh-backup manifest identity and backup failure.
- Deadline crossing after backup and before active switch.
- Byte-identical predecessor archive.
- Account and predecessor proposal remain byte-identical; execution count remains
  zero.
- Failure before and after active switch.
- Process-crash recovery with predecessor active and successor active.
- Symlink/hardlink/path traversal and archive collision.
- Idempotent terminal receipt behavior.
- v1 reader compatibility and v2 SEC parser-policy drift detection.
- Restore validation for verified and rolled-back receipts.
- CLI preview/confirmation/recovery separation.
- Preflight/dashboard never generate confirmation.

## Planned live activation sequence

The mutation path is implemented, but it must be used only in a deliberately
scheduled prospective window after current live gates and immutable hashes are
reviewed. Code availability is not evidence that the live transition occurred.

1. Finish candidate review and all focused/full tests in an isolated source tree.
2. Confirm the production `src/aios/forward.py`, `src/aios/paper.py`, active
   trial, account, and predecessor hashes still match the pre-change checkpoint.
3. Package and verify the v4 release; installing it must leave the current v1
   trial and `forward-status` evidence unchanged until explicit activation.
4. Resolve critical operations/data incidents. Do not let rollover relabel
   unrelated predecessor drift.
5. Select a later certified decision close while its next U.S. session has not
   opened.
6. Run preview with `--write-plan`. Independently review every hash,
   path, readiness check, target, deadline, policy addition, and no-fill
   disposition.
7. Approve and freeze the owner constants, produce a reviewed build with
   `available_in_this_build=true`, and repeat the complete release proof.
8. Run confirmed activation once. Let the command create and bind its fresh
   verified backup.
9. Verify:
   - active trial has v2 lifecycle lineage;
   - predecessor archive bytes equal the old active bytes;
   - account and predecessor proposal bytes are unchanged;
   - successor is registered and still waiting for its scheduled close;
   - `forward-status`, preflight, backup verification, and restore drill pass;
   - no execution and no broker evidence exists.
10. Observe the next naturally triggered refresh/health/backup cycle before
   considering the rollover milestone operationally complete.
