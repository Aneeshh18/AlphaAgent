# Future policy contracts

These tests describe intended behavior that would alter files frozen by the
currently active forward trial. They are preserved for the successor policy
release but are not part of the current release test collection.

Activation requires:

1. byte-identical archival of the predecessor trial and its expired proposal;
2. a reviewed successor policy bundle that explicitly includes every helper;
3. a fresh prospective proposal with no retrospective fill; and
4. moving the contract back to a `test_*.py` filename and making it green.

The factor-withholding contract can be inspected explicitly with:

```bash
.venv/bin/pytest -q \
  tests/future_policy_contracts/factor_withholding_diagnostics.py
```
