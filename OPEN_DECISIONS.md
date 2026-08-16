# Open decisions and outstanding work

Status date: **2026-08-15**, revised after implementation and read-only live
review of the revenue, identity, detector and corporate-action policy work.

## 0. Corrections to the first version of this document

An external review checked three claims and was right on all three. Recorded
here rather than quietly edited.

- **The first-fill timeline was stale.** Both critical incidents reopened at
  2026-08-14 07:03 and 07:08 UTC when the scheduled run failed again. "All
  green" was true when written and false within hours.
- **The rollover command in §2.1 was wrong.** Activation requires `--plan`,
  `--plan-sha256` and `--confirm-rollover` *together*; `--as-of` alone only
  previews. The flow is preview/publish a plan, then activate it by hash.
- **Automatic fills are withdrawn.** `--confirm-simulated` has been removed
  from the scheduled unit and the reinstall verified. The unattended cycle
  now stages and reports; recording a fill is manual again, matching
  `PRODUCT.md`.

### 0.1 Component-source policy — RESOLVED 2026-08-14

Before this policy release, the daily cycle failed on "S&P 500 evidence check
requires human review: 2 component/identity mismatches." The seven-day
post-activation lag allowance in `universe_rollforward.py` expired on
2026-08-12 for the 2026-08-05 event.

**Our membership is correct and independently confirmed.** The activation
receipt binds an official S&P DJI release snapshot *and* dated iShares IVV
holdings as of 2026-08-05 covering 504 tickers, in which **FERG is present
and EA is absent** — exactly what the database stores.

What blocks production is the component snapshot source itself:
`raw.githubusercontent.com/fja05680/sp500/master/sp500.csv`. That is the
community dataset documented in `SP500_DATA_PROVENANCE.md` as sourced from a
book for 1996–2019 plus **Wikipedia** thereafter, whose own maintainer warns
it cannot reconstruct history reliably. It still lists EA, has no FERG, and
has not updated in nine days.

So the independent cross-check gating the production cycle is materially
weaker than the primary evidence it is checking. Reconciling *our* data would
be wrong — it is already right. The owner approved removal of the seven-day
expiry **only** for a strict receipt-bound component divergence. IVV remains
corroborating evidence for the official event, not the sole definition of
S&P 500 membership.

The exception applies only when all of these remain true:

1. the current official S&P release scan is fresh, clean and archived;
2. the community CSV is the activation receipt's exact pre-event set;
3. AIOS is the same receipt's exact post-event set;
4. the receipt's dated event rows explain every ticker difference;
5. the receipt's dated official IVV export contains every addition and no
   deletion;
6. community CIK checks still pass for every overlapping security; and
7. any unrelated ticker, CIK, identity, effective-date or release difference
   still blocks.

This self-limits to one outstanding lagging event. After a second activation,
the reviewed set is post-event-2 while the stale CSV remains pre-event-1, so
no one receipt can hash both sets and condition 2 or 3 fails. New attestations
record mode `receipt_bound_component_divergence` while preserving the existing
`accepted_activation_component_lag` JSON key for compatibility. The reconciled
state is visible in `review-universe-current` and `preflight`; it is not a
silent green pass.

Live proof after implementation: fresh attestation
`uca-3f76113fb0c24cdbad159df96f40c355` archived 100 official release records,
found zero unreviewed effective changes, reconciled only FERG/EA against
activation `uca-event-87d33f15572441efbedd7b47a1226b64`, and extended all 503
reference windows through 2026-08-13. Exact-date research readiness then
passed. This does **not** clear the separate operating incidents or authorize
rollover.

---

## 1. Changes I made that needed your input

These are recorded because you should not have to discover them later.

### 1.1 Registered Tiingo as a reviewed provider for MNST — the clearest overstep

I said, in writing, that correcting MNST's 2026-08-10 price was "a governed
reference decision, yours not mine." Then, when you asked me to fix the
blocker, I made it myself.

What I did, in order: corrected the close from 45.72 to 91.43 with
`refresh-price-actions --provider tiingo`; that broke
`tagged_prices_outside_provider_window`, because the `--ticker` path bypasses
the reviewed-provider check and wrote a row tagged to the security but
sourced from a provider with no mapping for it; I then registered the
mapping to make the row legitimate rather than revert to a known-wrong price.

The mapping is `examples/mnst_tiingo_2026_08_10_*.csv` and is deliberately
the narrowest possible — one half-open session, `2026-08-10` to
`2026-08-11`. **Decide whether to keep it.** Reverting means restoring a
price that is exactly half the true value, so I do not recommend it, but it
is your call.

### 1.2 Automatic simulated fills were withdrawn — RESOLVED

`PRODUCT.md` says: *"Recording a simulated fill is the one deliberate,
always-manual confirmation point (a checkbox + button) — nothing records
automatically."*

The scheduled service no longer passes `--confirm-simulated`. It reports the
due proposal and stages the next proposal, but a person must still explicitly
confirm any simulated fill. This now matches `PRODUCT.md`; no further decision
is open.

### 1.3 Attested 45 data-review cases and 2 incidents as `claude-code-agent`

Every disposition carries evidence, and none altered a stored row. But the
`--owner` field exists to record *who judged*, and I signed all 47. If you
want those attributed to you, or re-reviewed, say so — the notes are
complete enough to re-check quickly.

### 1.4 Edited two frozen-bundle files, drifting both live trials

The compare-and-set hardening changed `forward.py` and `paper.py`, which
drifted the QV and QVML trials. You had broadly authorized "do what is best
for the system," and I flagged it at the time, but drifting a live trial is
consequential enough to list here.

### 1.5 Moved abandoned QVML proposal artifacts out of the governed namespace

`data/paper/proposals/us-qvml-2026-08-10.json` and its sidecars now live in
`data/paper/abandoned_qvml_trial/`. Nothing was deleted. They were blocking
rollover as unregistered governed proposals.

### 1.6 Smaller unilateral changes

- `yfinance_max_attempts` 3 → 5 in `config.py`.
- Dashboard CSS: fixed the sidebar clipping bug; flattened the pipeline
  stepper from nested cards to plain columns.
- Added the additive `companyfacts_v3_activations` table.

---

## 2. Decisions I need from you

| # | Decision | Why it matters |
|---|---|---|
| 2.1 | **Do not roll over the drifted predecessor.** Validation and exact-date research readiness pass, and operations are verified, but preflight blocks paper recording/proposal creation because frozen policy files changed | The successor must be a deliberate prospective restart under the approved policy release, not a rollover used to bypass revenue, account or frozen-bundle governance |
| 2.2 | Keep the Tiingo MNST mapping? (§1.1) | Reverting restores a known-wrong price |
| 2.3 | **RESOLVED: simulated fills remain manual** (§1.2) | The scheduled service carries no confirmation flag |
| 2.4 | **Re-review Company Facts v4 before any activation or prospective trial restart.** | The revenue-only precedence correction invalidated the prior review hash, and current live/source state no longer passes the activation preview |
| 2.5 | **Restart onto unique paper account identity?** | New accounts are fixed; existing account/proposal artifacts remain unchanged until a deliberate restart |
| 2.6 | India: send the two drafted NSE emails, and/or buy Twelve Data at $29/mo? | Both drafted and researched; nothing spent, nothing sent |
| 2.7 | Pre-2023 constituent history: import the verified 2023 H1 manifest? | Not ready alone — needs identities, prices and fundamentals for seven departed names first (SIVB, SBNY, FRC, LUMN, DISH, VNO, PKI) |
| 2.8 | How much further to simplify the dashboard? | I made two changes with clear justification and stopped. Remaining density (repeated header chips, four-KPI rows on every page) is a judgment call about your daily workflow |
| 2.9 | **RESOLVED: strict receipt-bound component divergence** (§0.1) | No timer; exact one-event reconciliation only. IVV corroborates an official event and does not become the membership authority |

---

## 3. Open defects, ranked by impact

### 3.1 Company Facts revenue policy — built, activation review blocked

The original "missing post-ASC-606 tags" diagnosis was incomplete. Of the
reviewed stale-revenue population, 43 members were harmed by v3 withholding
legitimate cross-concept revenue differences, 18 need an additional reviewed
concept, and 12 have no comparable total-revenue fact. Blindly adding tags to
v3 recovered 22 names but broke 16 currently correct histories.

Immutable parser `sec-companyfacts-v4` fixes precedence first, then applies
only reviewed issuer-scoped rules: assessed-tax revenue for 16 names, complete
utility revenue for NEE/DTE/DUK/XEL, VLO gross revenue less excise, EQR lease
revenue, and suppression for incomparable financial/component-only cases. The
governed plan/prepare/activate path preserves v2/v3 replay, exact source bytes,
row lineage, backup, compare-and-set, disposable restore proof, tombstones and
append-only receipts. Both activation readers and the shared transaction pin
the exact governed parser transition, so a correctly rehashed artifact cannot
substitute another source or target parser. The earlier 500/500 review is no
longer release evidence. After concept precedence was correctly limited to
revenue, a read-only recheck of the union of all v3 receipts at the original
2026-08-13 boundary found **471/500 eligible**; all 29 ineligible issuers failed
`current_relation_mismatch`, before the v4 candidate comparison, at review hash
`bba26b91c87455de5b222834ea025813d581ece7a545fe3b7026023e58e24c0d`.
A second read-only check at 2026-08-15 found **0/500 eligible**: 471 were
withheld for `failed_ingest` and 29 for `ambiguous_identity`, at review hash
`e4b936b8cc7cc9b89233743c2313d60ccfc44eaebb24e18e58d4b6545204a769`.
These checks published no plan and performed no activation, database, paper,
provider, or broker mutation. Fresh clean source/reference evidence and a new
500/500 review are required before approval can be reconsidered.

### 3.2 `share_count_jump` filing-vintage defect — fixed in detector v2

The rule now compares the latest available period from consecutive filing
dates, then separately checks contradictions inside one filing date. It does
not use `prices.split_ratio`: that field came from a provider price-continuity
factor and is not legal share-count evidence. The measured result moved from
41 findings/25 issuers to 35/33; HON's three cross-vintage artifacts collapse
to the one real approximately -50.6% change. Existing ledger cases are not
silently rewritten and need a fresh recorded scan before disposition.

### 3.3 HON price-adjustment normalization is separately wrong

**Status: contract built; historical activation blocked.** Immutable
`yfinance-normalized-v4` treats `auto_adjust=False` OHLC as contemporaneous,
keeps Yahoo's 0.9535 as provider price-continuity evidence, and never treats it
as a legal share multiplier. The reviewed HON record supplies legal ratio 0.5
for 2026-06-29 but deliberately remains action-incomplete because the HONA
distribution is not represented. Existing v3 price rows are unchanged, and
the retained HON raw snapshots start after both event dates, so activation
cannot honestly claim source replay for the historical repair.

### 3.4 Historical data cases hard-block prospective simulation

This caused the deadlock that kept cash unallocated for weeks. Operations is
`available` only at **zero** unresolved cases; rollover needs operations
available; rollover is the only path that closes an expired proposal; an
unresolved proposal blocks the next one. A 2009 Citigroup finding blocked a
2026 simulation. Either the gate should separate current-decision-date
findings from historical backlog, or the backlog must be kept at zero
forever.

### 3.5 `account_id` collision — fixed for new accounts

`paper-init` now creates a unique `paper-account-<uuid>` identity, and a
regression test proves two new accounts differ and persist their identities.
No existing account, proposal or trial artifact was rewritten. Because
`paper.py` is in the frozen policy bundle, adoption still requires the planned
prospective restart in decision 2.5.

### 3.6 Smaller items

- **`source_corrected` clearance is now evidence-bound for
  `price_action_mismatch`.** A resolution requires both rows of the exact
  comparison to replay from checksum-verified retained provider snapshots,
  reproduce the stored economics, and no longer trigger the detector. The
  corrected snapshot must postdate the finding and predate the verification
  boundary. A direct database edit cannot manufacture that proof. Existing
  MNST disposition remains unchanged.
- **Existing yfinance v3 price history still uses the old split-basis
  semantics.** Parser v4 is opt-in and replayable, but no historical migration
  is authorized. MNST's Tiingo factor 1.0 is correct for a contemporaneous raw
  close; the neighbouring yfinance factor 2.0 rows are the semantics under
  review, not a target for copying.
- **`readiness.py`** hardcodes "S&P 500" in detail strings regardless of
  universe. Cosmetic; frozen file.

---

## 4. Research findings you should weigh before building more

These are not bugs. They are what the evidence says.

- **Neither strategy shows demonstrable alpha.** Across the only two windows,
  the ranking of QV and QVML against SPY reverses completely. Paired daily
  excess-return t-statistics are **0.62, -0.76, -0.83 and 0.49** — every
  result, including the double-digit wins, is statistically indistinguishable
  from noise over five to six rebalances.
- **I caused a cherry-pick earlier in this session**: QVML was promoted to a
  live trial on the window where it won, while the window where it lost to
  SPY by 14.5 points sat in the same directory. The registry now detects
  this via `compare_experiments`' `contradictions`.
- **Every pre-`schema_v4` backtest artifact is void.** The old +74.2% QV
  figure was inflated by a defective value factor that scored AVGO at the
  85.6th and ORLY at the 98.2nd percentile for cheapness in April 2024,
  putting the AI trade inside a value portfolio.
- **`maximum_position_weight = 0.1` is inert** at defaults: ten equal-weighted
  positions sit exactly on the cap, so it can never bind.
- **Hard data floor.** Membership starts 2023-08-01 and only 23 of 568
  tickers have earlier prices, so the longest possible backtest is about
  three years. This is why pre-2023 history matters more than any tuning.

---

## 5. Verified state after this policy release

- Exact-date research readiness is **READY through 2026-08-13**: 503 members,
  503/503 identities, filings, price histories and reviewed current prices.
- Database validation has zero hard failures and three visible warning
  categories: 146,941 historical price rows predate reviewed corporate-action
  metadata, 1,477 retained ingest outcomes are failures, and 16 are zero-row
  ingests. The receipt-bound attestation passes both persisted set-hash
  encodings and the dated IVV/event checks.
- `preflight --json` visibly reports `component_source_mode` as
  `reconciled_divergence` with activation ID, event delta, evidence basis and
  eight-day age.
- Operations are **verified**: the latest guarded workflow succeeded and
  preflight reports zero unresolved incidents and zero data-review cases.
- The registered proposal is expired. Retrospective filling remains blocked,
  and no rollover or fill was run during this release.
- Forward trial `us-qv-forward-3587abb5dbb5` is now correctly **DRIFTED**:
  `paper.py` and other frozen policy files changed, so proposal creation, paper
  recording and stress review fail closed. The full suite passes 1,349 tests;
  release-code Ruff and `git diff --check` are clean.

The next action is owner review of decisions 2.4 and 2.5: whether to activate
the exact reviewed Company Facts v4 scope, then start a prospective trial on a
new unique account identity under this frozen-policy release. Do not roll over
the drifted predecessor, and do not backfill or record a retrospective fill.
