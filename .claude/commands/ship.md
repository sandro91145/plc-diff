---
description: Finish a change - check, bump the version, write it down, commit and push
argument-hint: [what changed, in one line]
---

Release the change: $ARGUMENTS

1. Run `python tests/smoke.py`. Do not continue while anything fails.
2. If users can see the change, bump `VERSION` at the top of `PLCDiff.pyw` (patch for a fix,
   minor for a new feature) and add the change to `README.md` where it affects how the app is used.
3. Update `CLAUDE.md` if any of this changed: the code map (section 4), the format notes (section 5),
   a pitfall worth keeping (section 7), how to test it (section 8), or the known limits (section 9).
4. Show me the diff (`git diff --stat` plus the important hunks) and wait for my go-ahead.
5. On my go-ahead: commit with a short plain-English message saying what a user will notice,
   then `git push -u origin <current branch>`.
6. Remind me in one line if the user guide needs a new screenshot or a Changelog row for this.
