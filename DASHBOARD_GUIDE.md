# Using the AIOS dashboard

A plain-language walkthrough of the app itself — what each screen shows, what
to check and how often, and how the paper trial works day to day. For CLI
commands, troubleshooting, and how the calculations work, see
[`BEGINNER_GUIDE.md`](./BEGINNER_GUIDE.md). For design rationale, see
[`ARCHITECTURE.md`](./ARCHITECTURE.md).

## What this is, in one paragraph

AIOS researches U.S. stocks using public filings and prices, ranks them with a
repeatable scoring method, and tracks a **simulated** paper portfolio so you
can watch how that research would have performed — with real dates, real
prices, real costs, and zero real money. It is not a trading bot: there is no
broker connection, no order gets sent anywhere, and it never tells you
personally what to buy or sell. It only ever recommends **researching**
something further.

## Launching it

```bash
.venv/bin/aios dashboard
```

Opens at `http://localhost:8501` by default, on your machine only — nothing is
exposed to the internet. Close it before running a backup or restore; every
other command is safe to use while it's open.

## The five screens

The sidebar has five items. Read them in this order the first time.

### 1. Overview

The home screen. Answers one question: **what should I look at right now?**
Three status tiles (Research, Paper Trial, Operations) and a single "next
safe action" — always exactly one recommendation, never a list to sort
through yourself. If everything is green, there's nothing else to do here.

### 2. Research

A ranked, filterable list of the ~500 companies the system currently follows,
scored by two methods:

- **QV** (Quality + Value) — the default, reviewed method.
- **QVML** — experimental, adds price-trend and stability factors. Labeled
  experimental everywhere it appears; don't treat it as more reliable than QV.

Click any company to see the evidence behind its score — filings used, prices
used, and anything withheld and why. A missing or "N/A" score is deliberate:
the system would rather show nothing than guess.

### 3. Paper Trial

The simulated portfolio. A fixed four-step pipeline runs every cycle:

1. **Proposal** — a target portfolio gets generated from that cycle's research.
2. **Forward Trial** — confirms the proposal belongs to the current locked-in
   strategy (the "policy" hasn't changed since the trial started).
3. **Timing Review** — waits for the scheduled market close, then a read-only
   check confirms the closing prices needed to simulate a fill are available.
4. **Local Record** — the fill is recorded into the simulated account, or not
   yet ("No Fill") if step 3 isn't done.

**Recording a fill:** when step 3 says "Review Now," a **Record this
simulated fill** panel appears with the projected trades and costs. Check the
box, click the button — that's it, no terminal needed. It runs the exact same
checks the command-line `paper-execute` would; the checkbox is the one
deliberate confirmation this system always requires before touching the
account, even in simulation. Nothing records automatically without that click
— by design, so a bad research cycle can't silently compound into a worse one
unattended.

Below the pipeline: current simulated cash, holdings, and drawdown — always
starts at $100,000, always simulation-only.

### 4. Operations

Health checks for the machinery: is the daily data update running, is the
last backup good, are there any open data-quality flags. You generally don't
need to visit this unless Overview's status tile for Operations turns amber
or red.

### 5. Methodology & Sources

Reference page. Explains the scoring method and data sources in plain
English, with the current honest limitations listed at the bottom (what the
system does *not* yet support — after-tax figures, India, real trading).
Read it once; you won't need it daily.

## A normal week

- **Check Overview** once a day (or whenever you think of it). If it's all
  green, you're done.
- **Check Paper Trial** after a scheduled proposal's close has passed — that's
  when the "Record this simulated fill" panel appears.
- **Check Research** whenever you're curious about a specific company or want
  to see the current ranked list.
- **Operations and Methodology** are read-as-needed, not daily habits.

## If you want to compare two research approaches

Every backtest you run with `aios backtest-qv` can be registered with its
exact identity (code version, database snapshot, data coverage, and result) so
comparisons are never based on cherry-picked or unreproducible runs:

```python
from aios.experiments import register_experiment, compare_experiments

# after running a backtest and getting `result` (a QVBacktestResult):
register_experiment(
    result=result,
    purpose="exploratory",      # or "frozen" / "holdout" once you're comparing seriously
    artifact_path=artifact_path,
    store=store,
    db_path=db_path,
)

compare_experiments(["exp-...", "exp-..."])
```

This never picks a winner for you — it's a side-by-side comparison table. If
one variant genuinely looks better, adopting it is a deliberate step:
`aios forward-restart --confirm-restart` after independent review, exactly
like adopting any other policy change. There is no dashboard page for this
yet; it's a Python-level tool for now.

## What this system will not do

- Send an order to a broker. There is no broker connection.
- Tell you personally what to buy or sell.
- Auto-tune its own strategy based on paper-trial results. Every research
  change is human-reviewed and explicitly activated — see above.
- Work for India yet (Nifty 50 data isn't loaded).
- Produce after-tax figures (tax rates default to zero and are labeled as such
  everywhere they appear).

If something on screen contradicts this list, treat the screen as wrong and
stop — it means evidence is missing or a check failed, not that a new
capability quietly turned on.
