---
description: Finish a change - check, bump the version, write it down, commit and push for review
argument-hint: [what changed, in one line]
---

Ship the change: $ARGUMENTS

1. Run the smoke test (see `/check`). Do not continue while anything fails.
2. If users can see the change, bump `VERSION` at the top of `PLCDiff.pyw` (patch for a fix,
   minor for a new feature) and add it to `README.md` where it affects how the app is used.
3. Update `CLAUDE.md` if any of this changed: the code map (section 4), the format notes (section 5),
   a pitfall worth keeping (section 7), how to test it (section 8), or the known limits (section 9).
4. Commit everything with a short plain-English message saying what a user will notice.
5. Push **to a branch**, not to `main`: `git push -u origin work/<a-few-words>`.
   `main` is only for changes I have already approved in this conversation — if I said "push to main",
   or the change is only notes or tests, push there instead. Never force-push.
6. Then report, in this order: the smoke-test result (number of checks), a screenshot of the real page
   for anything visual, `git diff --stat` against `main`, and the branch name with how to merge it.
7. Remind me in one line if the user guide needs a new screenshot or a Changelog row, and if the
   installed copy (a separate file from this repo) has to be updated to get the change.

**Why a branch and not "wait for approval":** a cloud session runs a Stop hook that fails the turn
while anything is uncommitted, untracked *or* unpushed, so holding a finished change back is not
possible there. Pushing to a branch satisfies the hook and still leaves the decision with me —
nothing lands on `main` until I merge it. In a session on my own PC there is no such hook, so ask
before pushing if you are unsure.
