# PLC Diff

**See what changed in your Siemens TIA Portal PLC program — drawn as ladder, FBD and S7-GRAPH, not as XML.**

PLC Diff compares two versions of a TIA Portal V20 program and shows every changed network as a picture, with
added parts in green, removed in red and changed in yellow. Versions come from Git: TIA Portal's
*Version Control Interface* exports your blocks as files into a Git repository, and PLC Diff reads them.

It runs on your own PC, opens in your browser, and needs nothing but Python and Git. Your code never leaves your PC
(GitHub is optional — a shared network folder works too).

![Compare two versions](docs/images/compare.png)

| Ladder (LAD) | Function block diagram (FBD) |
| --- | --- |
| ![LAD network](docs/images/ladder.png) | ![FBD network](docs/images/fbd.png) |

## What it does

- **Compare versions** — any two commits, or your unsaved export against the last commit. LAD drawn as ladder,
  FBD as FBD, S7-GRAPH as a step chain (steps, transitions, branches, jumps, supervisions, interlocks),
  SCL/STL as a text diff. Data blocks and UDTs as a before/after table (data type, start value, moved members).
  Changed files are grouped like TIA's project tree (Program blocks, PLC tags, PLC data types, …), each group opens
  and closes, and a sidebar lists them so you can jump to any file. Big drawings (wide networks, long S7-GRAPH
  chains) start zoomed out with the changes highlighted; click a change to zoom in there.
  A block TIA only renumbered after a compile says "no logic change".
- **Compare two folders** — no Git needed: e.g. the customer's export against yours, or a backup from site.
- **Change history** — every change with date, time and author, searchable, with charts, and a download for Excel.
- **Commit and share** — one button to save ("commit") and send ("push") your changes, with a suggested message
  that lists what changed. Pull, conflicts (keep mine / use theirs) are handled for you.
- **Several PLCs, several people** — one folder per PLC in one project; each person commits only their own PLC.
- **Several projects** — a home screen with all your projects and their status.
- **Safety** — F-blocks (F_LAD / F_FBD / F_DB) are marked, need an extra confirmation before committing, and can be
  reported on their own with a sign-off sheet.
- **Blocks tab** — every block of any version, grouped like TIA's project tree. Click one to see it drawn (LAD, FBD,
  S7-GRAPH chain), even if it never changed; **History** lists every commit that changed it, with what changed.
- **Where is this tag used?** — type a tag in the Blocks tab: every network, data block and tag table that uses it,
  drawn, with the tag highlighted.
- **Review sign-off** — mark a commit as reviewed (with a remark); it is shared with the team through Git and shows
  in the history, the compare page and the printed sign-off sheet.
- **Get an old version back** — put any earlier version of a single block into the TIA workspace folder, ready for
  *Import from workspace* in TIA. Setup → **Get an old version of a block back**.
- **Releases and reports** — mark a version as a release (e.g. FAT, SAT), attach the archive, print any comparison
  or save it as PDF.
- **Updates** — PLC Diff tells you when a new version is on GitHub and updates itself with one click (Setup → Updates
  to turn this off).

![Change history](docs/images/history.png)

## Install (Windows)

1. Install **Git for Windows** — https://git-scm.com (default options are fine).
2. Install **Python 3.8 or newer** — https://python.org (tick *Add python.exe to PATH*).
3. Download `PLCDiff.pyw` and `Start PLC Diff.bat` into one folder, e.g. `D:\Tools\PLCDiff`.
4. Double-click **Start PLC Diff.bat**. The app opens in your browser.

No extra Python packages are needed. If the app can't find Git, paste the path to `git.exe` in **Setup**.

## Connect TIA Portal

1. In TIA Portal V20: *Version control interface* → add a workspace → point it to a folder inside your Git project,
   one folder per PLC, e.g. `<project>\vci\PLC_1`.
2. Drag the PLC's program blocks (and tag tables, data types) into the workspace and export.
3. In PLC Diff, open the project; under **Setup → PLCs** check that each PLC points to its folder.
4. After each change in TIA: export to the workspace, then in PLC Diff **Changes → Commit and share**.

## Command line (optional)

```
python PLCDiff.pyw --app                 # start the app
python PLCDiff.pyw HEAD~1 HEAD           # compare two commits, open the report
python PLCDiff.pyw --files old.xml new.xml
python PLCDiff.pyw --folders D:\Customer\Export D:\Projects\MyProject\vci\PLC_1
python PLCDiff.pyw --history --csv changes.csv   # history page + file for Excel
python PLCDiff.pyw --help
```

## Limits

- Reads TIA Portal V20 SimaticML exports. Hardware configuration, HMI and technology objects are not drawn.
- Tested on Windows; the code is plain Python and also runs on Linux/macOS (without TIA Portal, of course).

## For developers

Everything is in one file, `PLCDiff.pyw` (Python standard library only). `tests/make_samples.py` creates
realistic test exports. `CLAUDE.md` explains the structure, the TIA export formats and the design decisions.

## License

MIT — see [LICENSE](LICENSE). Not affiliated with Siemens. *TIA Portal*, *SIMATIC* and *S7-GRAPH* are trademarks of
Siemens AG. Use at your own risk; always check changes in TIA Portal before downloading to a machine.
