# PLC Diff — notes for developers and AI assistants

Read this before changing the code. The whole app is `PLCDiff.pyw` (one file, Python standard library only);
`tests/make_samples.py` generates test exports. User-facing docs are in `README.md`.

## 1. What it is, in one paragraph

PLC Diff shows what changed in **TIA Portal V20** PLC programs between two versions: changed
networks are **drawn** (LAD as ladder, FBD as FBD, S7-GRAPH as a step chain, SCL as a text diff) with added /
removed / changed elements in green / red / yellow. Versions come from **Git**: TIA's **Version Control Interface
(VCI)** exports blocks as XML (SimaticML) into a folder inside a Git repo; PLC Diff reads two Git versions of those
files and compares them. Around that it has a change-history database with charts, multi-PLC and multi-person
support, a multi-project home screen, safety (F-block) features, releases and printable reports. It is a **local
web app**: a Python stdlib HTTP server on 127.0.0.1 that opens in the browser. **No third-party packages** — only
Python 3.8+ stdlib and the `git` command.


## 2. Where things live (Windows)

| What | Where |
| --- | --- |
| The app | any folder with `PLCDiff.pyw` + `Start PLC Diff.bat` (starts it with `pythonw`, no console) |
| Python | the .bat tries `%LOCALAPPDATA%\Programs\Python\Python313\pythonw.exe`, then `pythonw` on PATH |
| Settings per PC | `%APPDATA%\plc-diff\settings.json` (projects list, last project, selected PLC per repo, git path) |
| Running copy info | `%APPDATA%\plc-diff\running.json` (port + token of the running instance) |
| TIA exports (VCI) | inside the project repo, one folder per PLC: `vci\<PLC>\Program blocks\...` |
| History DB per repo | `<repo>\.plcdiff\changes.db` (SQLite, git-ignored, rebuilt from Git any time) |
| PLC folder choices | `<repo>\plcdiff.json` (committed, shared by the team) |
| Archives | `<repo>\releases\<PLC>\*.zap20` (Git LFS) |

Real projects can be mostly FBD with F_FBD safety blocks and S7-GRAPH sequences, and VCI exports can be nested
very deep — keep paths and drawings robust for that.


## 3. Running and releasing

- Start: double-click `Start PLC Diff.bat`, or `python PLCDiff.pyw --app`. CLI: `python PLCDiff.pyw [old] [new]`,
  `--files a.xml b.xml`, `--history`, `--text`, `--help`.
- A running app keeps its old code. The app shows a "A newer version … Restart now" bar when its file changed, and a
  fresh start stops an older running copy (`/api/quit` with the token from `running.json`).
- Bump `VERSION` (top of the file) for user-visible changes and note them in the README if they change usage.


## 4. Code map (single file, ~4,600 lines, in this order)

| Section | What's in it |
| --- | --- |
| XML helpers | `load_xml` (strips namespaces), `kid/kids/ktext`, `ml_text` (MultilingualText), `access_text` (operand text from `<Access>`/`<Instance>`, quoting rules like TIA: `"DB".member`, `#local`, arrays, slices, addresses) |
| LAD/FBD model — `Net` | `parse_flg(FlgNet)`: parts, wires → `pin_src` (power-flow source per input pin), `pin_val` (operand per pin), `power_out`; `_main_input`; `chain_of()` turns the wire graph into series lists with parallel groups (`{"t":"par"}`) by factoring common prefixes of OR inputs; `term_chains` (one per terminal coil/box); `expr_text` / `statements` (logic as text, e.g. `"Q" := ("A" OR "B") AND NOT "C"`); `elements` (keys for diffing); `box_info` (pins of calls/instructions); `fbd_tree` (expression trees for FBD drawing) |
| Documents — `Doc`, `parse_doc` | FC/FB/OB → networks (FlgNet / StructuredText / StatementList / **Graph**), DBs, UDTs, tag tables; interface members; `detect_safety` |
| Diffing | `compare_nets` (element multiset diff, pairs operand changes, box pin changes → `pins_old/new`), `align_nets` (SequenceMatcher over network fingerprints + title/similarity pairing), `diff_file` → `FileReport` |
| Drawing | LAD: blocks (`blk_contact/coil/cmp/box`), `series`/`par_block`/`split_block`, `make_trie` (shared prefixes → branches to multiple coils), `net_svg`. FBD: `fbd_boxlike`, `fbd_block`, `net_svg_fbd`. `draw_net` picks LAD/FBD by network language (`FBD`/`F_FBD` → FBD). `tmid()` shortens long tag names in the middle so the end stays visible |
| S7-GRAPH | `parse_graph_sequence`, `graph_summary`, `graph_links` (structure with branches resolved), `compare_graph`, `graph_svg` (layered layout: rank = longest path, column by DFS; branch bars; jumps as ↓S#), `html_graph_net` (chain before/after + changed conditions drawn as FBD) |
| Report | `print_report` (terminal), `build_html` (compare page: TOC, SAFETY badges, safety bar, networks before/after, "Logic as text", sign-off sheet, print CSS / "Print / save as PDF") |
| Git | `git()`, `rev_ok`, `rev_desc`, `changed_files` (rename limit raised; **pure moves R100 skipped**, counted in `moves`), `read_rev` (WORK = files on disk); `NOWIN` hides console windows on Windows; `ensure_git_on_path` |
| PLCs | `plcdiff.json` (`plcs: [{name, path, was?}]`), `plc_map`, `plc_prefixes` (current + earlier folders so history survives a move; inferred for exports that used to sit directly in `vci/`), `list_plcs`, `plc_of`, `plc_paths` (adds `:(exclude)` for nested PLC folders) |
| History DB | `update_history` (incremental over `git log --first-parent`, removes rewritten commits), `rows_for_report`, `history_rows` (adds `plc`, `safety`), `write_csv` (`;` + BOM for Portuguese Excel), `build_history_html` (search/filters + charts in plain SVG/JS), `install_hook` (post-commit hook) |
| App | settings, `run()`, `find_doubled`/`merge_up` (TIA doubled-folder fix), `compute_reports`, `suggest_message`, `repo_info`, `project_summary`, `scan_for_projects`, `create_project`, `init_repo`, `pull` (rebase+autostash, clean abort on conflict), `push` (auto-pull-and-retry), `clone`, `health_check`/`build_health_html`, `pick_folder`/`pick_file` (tkinter in a subprocess), `APP_CSS`/`APP_JS`/`APP_HTML`, `run_app` (HTTP server, token in header for /api, `?t=` for /view, Host check, 60 s idle shutdown, instance file, restart) |
| main | dispatch: `--app` or no args under `pythonw` → app; otherwise CLI |

HTTP endpoints: GET `/`, `/api/{ping,state,projects,suggest,revs}`, `/view/{report,history,csv,health}`;
POST `/api/{pick,pickfile,open,identity,gitpath,clone,projects/add,projects/remove,scan,fetchall,newproject,init,
remote,ignore,fixdouble,hook,commit,push,fetch,pull,resolve,plc,plcfolder,plcremove,addplc,moveplc,release,
explore,quit,restart}`.


## 5. What we learned about the formats (important — verified on real TIA V20 exports)

- **SimaticML FlgNet** (LAD and FBD share it): `Parts` (Access/Part/Call with UId) and `Wires`; in each `<Wire>` the
  **first** connection is the source (`Powerrail`, `IdentCon` = operand, or `NameCon` output pin), the rest are
  destinations. LAD OR-branches close with a `Part Name="O"` (`in1..inN`); branches open by fan-out. Contacts:
  `Contact` (negated via `<Negated Name="operand"/>`), `PContact`/`NContact` (pin `bit`). Coils: `Coil`, `SCoil`,
  `RCoil`… Compare boxes (`Gt`, `Eq`…) use pins `pre`/`in1`/`in2`. FBD gates: `A`/`O` with operands wired straight
  in, negation per input pin. Calls: `<Call><CallInfo Name BlockType><Instance/><Parameter Name Section Type/>`;
  negated call inputs appear as `<Negated Name="<pin>"/>` on the Call.
- **S7-GRAPH** (FB, language GRAPH): `NetworkSource/Graph` with `PreOperations/PermanentOperation*` (FlgNet),
  `Sequence` → `Steps/Step(Number, Init, Name, MaximumStepTime, WarningTime)` with `Actions/Action(Qualifier,
  Event?)` of `Token`s, `Supervisions/Supervision` and `Interlocks/Interlock` (FlgNet ending in `SvCoil`/`IlCoil`);
  `Transitions/Transition(Number, Name, IsMissing)` with FlgNet ending in `TrCoil`; `Branches/Branch(Number,
  Type=AltBegin|AltEnd|SimBegin|SimEnd, Cardinality)`; `Connections/Connection(NodeFrom, NodeTo, LinkType=Direct|
  Jump)` with `StepRef`/`TransitionRef`/`BranchRef(In|Out)`. Titles/comments here use `<MultiLanguageText Lang=..>`
  (not MultilingualTextItem). TIA adds a static interface member per step/transition (`G7_StepPlus_V4`…): these are
  filtered out of interface diffs.
- **Safety**: F-blocks have language `F_LAD`/`F_FBD`, F-DBs `F_DB` (detected by `F_` prefix or a `*Failsafe*` = true attribute).
- **VCI export layout quirks** (all seen in practice): the workspace root holds a `.vci` folder; exports used to land
  directly in `vci\Program blocks` (no PLC level); a workspace path set one level too deep produced
  `Program blocks\Program blocks`; dragging a device can add extra levels like `vci\<project>\<PLC>\...`.
  That's why PLC folders are configurable (`Setup → PLCs → Change folder…`) and the doubled-folder fix exists.
- Exports are UTF-8 (sometimes BOM); Git may convert line endings — compare ignores CRLF-only differences.


## 6. Decisions and why

- **One file, stdlib only** — users are often on company PCs; nothing to pip install.
- **Browser UI from a local server** (not Tk/Qt) — nicer drawings, same SVG used in reports/PDF.
- **One Git repo per project, one folder per PLC**, one TIA VCI workspace per PLC. Commits are per selected PLC,
  so colleagues' exports never get mixed in. Rule for teams: never two people on the same PLC at once.
- **GitHub optional** — everything is local; a shared network folder (`git init --bare //SERVER/...`) or GitLab/Azure
  works as the shared remote.
- **History DB is a cache** (git-ignored, rebuilt from Git); `plcdiff.json` is shared (committed).
- **Safety**: committing F-block changes requires an explicit confirmation tick; safety reports have a sign-off sheet.
- The **TIA project itself stays outside the repo** (only VCI exports + archives inside).


## 7. Pitfalls already solved (don't reintroduce)

- `pythonw` has no stdout/stderr → replaced with devnull; any `print` is safe.
- Git not on PATH for double-clicked apps → `ensure_git_on_path()` checks `Program Files\Git`, LOCALAPPDATA, and a
  user-pasted path saved in settings (`/api/gitpath`).
- Console windows flashing on every git call → `creationflags=CREATE_NO_WINDOW` (`NOWIN`).
- Charts shifted a day in Europe time zones → dates handled in UTC in JS.
- A commit that moves 1,000+ files made Compare hang → rename limit raised, pure moves skipped, page explains it.
- Compare across a folder move lost history for a single PLC → `plc_prefixes` / `was`.
- Iframe height in CSS grid (150 px default) → `grid-template-rows:minmax(0,1fr)` + `height:100%`.


## 8. How we test

- `make_samples.py <dir> old|new` writes realistic V20 exports (LAD with branches/TON/FB call/compare, FBD with
  gates and a negated call input, tag table) for two versions; build a throwaway git repo with them.
- S7-GRAPH: export a real GRAPH FB, edit a copy (rename a step, change a time/action/condition/jump) and run
  `--files old.xml new.xml`.
- Team flow tested with a bare repo + two app instances (different `APPDATA`, ports): concurrent pushes, conflict,
  Keep mine / Use theirs.
- Screenshots via Playwright for checking and for the guide (paths masked to `D:\...`).
- After any change: `python -m py_compile`, run a CLI compare, open the app, look at the page.


## 9. Known limits / ideas not done yet

- Not read/drawn: hardware configuration, HMI, technology objects as diagrams (shown as text/“not understood”).
- Data blocks: start-value changes are listed but not as a nice table.
- Possible next: "Compare two folders" without Git; restore an old block version (write it back for *Import from
  workspace*); "where is this tag used" search; review sign-off per commit; shared network install with auto-update.
- **Setup → Check all blocks** lists every export the app can't fully draw yet — the best source of new work.


## 10. Style

- The users are PLC programmers, not software developers: plain, short English in the UI; the app should do things
  itself (buttons) rather than ask for command lines.
- Keep it one file and standard library only. Check every change visually (screenshots of the real page).
