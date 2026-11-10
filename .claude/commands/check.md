---
description: Run the PLC Diff smoke test and report what passed or broke
allowed-tools: Bash(python tests/smoke.py:*), Bash(python3 tests/smoke.py:*), Read, Grep
---

Run the full check:

```!
python3 tests/smoke.py 2>&1 || python tests/smoke.py 2>&1
```

Then:

1. If every check passed, say so in one line with the number of checks — nothing else.
2. If anything failed, find out why before changing code: read the failing area of `PLCDiff.pyw`,
   re-run only that part by hand (`--files`, `--text`, `--out page.html`) on the throwaway repo
   (`python tests/smoke.py --keep` leaves it in place and prints the path), and fix the real cause.
   Never make a check pass by weakening the check itself.
3. Report in plain English: what broke, which block or network, and what you changed.
