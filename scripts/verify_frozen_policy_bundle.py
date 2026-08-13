#!/usr/bin/env python3
"""Verify that the active forward trial's frozen policy files are unchanged.

The active trial freezes a SHA-256 hash for every source file that can
materially alter factor evidence. Editing any of them marks the trial drifted
and forfeits the untouched forward-observation window, so this check is run
before and after any change set that is meant to stay outside the bundle.

Exit code 0 means every frozen file still matches. Any mismatch or missing file
exits non-zero and names the exact paths.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

TRIAL_PATH = Path("data/paper/us_qv_forward_trial.json")


def main() -> int:
    project_root = Path(__file__).resolve().parent.parent
    trial_file = project_root / TRIAL_PATH
    if not trial_file.is_file():
        print(f"FAIL: active trial file is missing: {TRIAL_PATH}")
        return 2

    document = json.loads(trial_file.read_text(encoding="utf-8"))
    payload = document["payload"]
    policy_files = payload["policy_files"]

    matched: list[str] = []
    drifted: list[tuple[str, str, str]] = []
    missing: list[str] = []

    for relative_path, expected in sorted(policy_files.items()):
        candidate = project_root / relative_path
        if not candidate.is_file():
            missing.append(relative_path)
            continue
        actual = hashlib.sha256(candidate.read_bytes()).hexdigest()
        if actual == expected:
            matched.append(relative_path)
        else:
            drifted.append((relative_path, expected, actual))

    total = len(policy_files)
    print(f"active trial: {payload['trial_id']}")
    print(f"policy bundle: {payload['policy_bundle_sha256']}")
    print(f"frozen files: {len(matched)}/{total} unchanged")

    for relative_path, expected, actual in drifted:
        print(f"  DRIFT   {relative_path}")
        print(f"          frozen  {expected}")
        print(f"          current {actual}")
    for relative_path in missing:
        print(f"  MISSING {relative_path}")

    if drifted or missing:
        print("\nRESULT: FAIL — the active forward trial would be drifted.")
        return 1

    print("\nRESULT: PASS — every frozen policy file is byte-identical.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
