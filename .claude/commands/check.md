---
description: Run the PLC Diff smoke test and report what passed or broke
argument-hint: [optional: --keep or --no-app]
---

Run the smoke test: `tests/smoke.py $ARGUMENTS`

**Find the Python interpreter first** — on a Windows PC Python is often not on PATH.
Try these in order and use the first that works:

1. `python3`, `python`, `py` (on PATH)
2. the Windows launcher: `/c/Users/$USERNAME/AppData/Local/Programs/Python/Launcher/py.exe`
3. the install itself: `/c/Users/$USERNAME/AppData/Local/Programs/Python/Python3*/python.exe`
   (`%LOCALAPPDATA%\Programs\Python\Python313\python.exe` — the same place `Start PLC Diff.bat` looks)

Do not expand `$LOCALAPPDATA` in a bash command: it holds a Windows path with backslashes, which
bash eats. Use the `/c/Users/...` form, or `cygpath -u "$LOCALAPPDATA"`.

Then:

1. If every check passed, say so in one line with the number of checks — nothing else.
2. If anything failed, find out why before changing code: read the failing area of `PLCDiff.pyw`,
   re-run only that part by hand (`--files`, `--text`, `--out page.html`) on the throwaway repo
   (`tests/smoke.py --keep` leaves it in place and prints the path), and fix the real cause.
   Never make a check pass by weakening the check itself.
3. Report in plain English: what broke, which block or network, and what you changed.
