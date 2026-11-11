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
| Drawing | LAD: blocks (`blk_contact/coil/cmp/box`), `series`/`par_block`/`split_block`, `make_trie` (shared prefixes → branches to multiple coils), `net_svg`. FBD: `fbd_boxlike`, `fbd_block`, `net_svg_fbd`. `draw_net` picks LAD/FBD by network language (`FBD`/`F_FBD` → FBD). tag names, instances and called-block names are drawn whole (`NAME_MAX` = 120; only the S7-GRAPH chain, with fixed columns, shortens them - `tmid()` / `trunc()`) |
| S7-GRAPH | `parse_graph_sequence`, `graph_summary`, `graph_links` (structure with branches resolved), `compare_graph`, `graph_svg` (layered layout: rank = longest path, column by DFS; branch bars; jumps as ↓S#), `html_graph_net` (added / removed / unchanged: the chain; changed → `html_graph_change`: the whole sequence first (After / Before switch), each change (`rect data-g="S2"`, chips) opens a panel with its notes, its changed conditions before | after and both chains at full size scrolled to it - built by `GRAPH_JS`; printing shows both chains and every panel) |
| Report | `print_report` (terminal), `build_html` (compare page: TOC, SAFETY badges, safety bar, networks before/after, "Logic as text", sign-off sheet, print CSS / "Print / save as PDF"), `build_block_html` (one block in one version, every network drawn - `/view/block`), `build_where_html` (`where_used`: every network / DB / tag table using a tag, highlighted), `build_blockhistory_html` (`block_history`: `git log --follow` of one export), `html_member_table` (DB/UDT before/after table), `compare_folders` (two folders, no Git - `/view/folders`, `--folders`); the compare page has a sticky **sidebar** (`.sidenav`, same groups, marks the file in view; on screens under 960 px the top table of contents is used instead) and `ZOOM_JS` (drawings wider than the page or taller than 720 px start zoomed out with the changes highlighted; click a place or a numbered change to see it at full size; in a before | after pair (`.side.pair`, changed LAD/FBD networks and GRAPH conditions) zooming one side zooms the other to the same spot); every changed network gets an id + `data-k` kinds (`net_with_tools`; click its title line to fold it) and a block with 3+ changed networks gets `net_toolbar` (go to a network, show only new / removed networks or parts added / removed / changed, find a tag, fold all; `NETS_JS`, printing unfolds everything); `report_groups` puts the changed files in collapsible sections per TIA folder (Program blocks, PLC tags, PLC data types… - closed when more than 8 files; the table of contents opens the right one, printing opens all) |
| Git | `git()`, `rev_ok`, `rev_desc`, `changed_files` (rename limit raised; **pure moves R100 skipped**, counted in `moves`), `read_rev` (WORK = files on disk); `blocks_at` (exports that existed in a version), `restore_block` (write one
export back into the workspace, or a copy elsewhere), `workspace_path` (where an export from an older version belongs in today's PLC folder), `export_place` (PLC, TIA folder and sub-folder of an export, for grouped lists); `NOWIN` hides console windows on Windows; `ensure_git_on_path` |
| PLCs | `plcdiff.json` (`plcs: [{name, path, was?}]`), `plc_map`, `plc_prefixes` (current + earlier folders so history survives a move; inferred for exports that used to sit directly in `vci/`), `list_plcs`, `plc_of`, `plc_paths` (adds `:(exclude)` for nested PLC folders) |
| History DB | `update_history` (incremental over `git log --first-parent`, removes rewritten commits), `rows_for_report`, `history_rows` (adds `plc`, `safety`), `write_csv` (`;` + BOM for Portuguese Excel), `build_history_html` (search/filters + charts in plain SVG/JS), `install_hook` (post-commit hook) |
| App | settings, `run()`, `find_doubled`/`merge_up` (TIA doubled-folder fix), `compute_reports`, `suggest_message`, `repo_info`, `project_summary`, `scan_for_projects`, `create_project`, `init_repo`, `pull` (rebase+autostash, clean abort on conflict), `push` (auto-pull-and-retry), `clone`, `health_check`/`build_health_html`, `pick_folder`/`pick_file` (tkinter in a subprocess), `APP_CSS`/`APP_JS`/`APP_HTML`, `run_app` (HTTP server, token in header for /api, `?t=` for /view, Host check, 60 s idle shutdown, instance file, restart) |
| Reviews | `reviews_map` / `add_review` / `sync_reviews`: one Git note per reviewed commit in `refs/notes/plcdiff-review` (JSON lines, ASCII), merged with `cat_sort_uniq` on Get latest and pushed after Push - nothing is committed for a review |
| Updates | `check_update` (GitHub latest release, `PLCDIFF_UPDATE_URL` overrides it - tests use a `file://` stand-in), `install_update` (checks the download says `VERSION = "x"` and compiles, keeps `PLCDiff.pyw.bak`, replaces atomically) |
| main | dispatch: `--app` or no args under `pythonw` → app; otherwise CLI |

HTTP endpoints: GET `/`, `/api/{ping,state,projects,suggest,revs}`, `/view/{report,history,csv,health}`;
POST `/api/{pick,pickfile,open,identity,gitpath,clone,projects/add,projects/remove,scan,fetchall,newproject,init,
remote,ignore,fixdouble,hook,commit,push,fetch,pull,resolve,plc,plcfolder,plcremove,addplc,moveplc,release,
blocks,restoreblock,review,update,explore,quit,restart}`; GET `/view/{block,blockhistory,whereused,folders}`.
`rev=WORK` means the files on disk in `/api/blocks`, `/view/block`, `/view/whereused`; `/view/report?file=` limits a
compare to some files (a block's history).


## 5. What we learned about the formats (important — verified on real TIA V20 exports)

- **SimaticML FlgNet** (LAD and FBD share it): `Parts` (Access/Part/Call with UId) and `Wires`; in each `<Wire>` the
  **first** connection is the source (`Powerrail`, `IdentCon` = operand, or `NameCon` output pin), the rest are
  destinations. LAD OR-branches close with a `Part Name="O"` (`in1..inN`); branches open by fan-out. Contacts:
  `Contact` (negated via `<Negated Name="operand"/>`), `PContact`/`NContact` (pin `bit`). Coils: `Coil`, `SCoil`,
  `RCoil`… Compare boxes (`Gt`, `Eq`…) use pins `pre`/`in1`/`in2`. FBD gates: `A`/`O` with operands wired straight
  in, negation per input pin. Calls: `<Call><CallInfo Name BlockType><Instance/><Parameter Name Section Type/>`;
  negated call inputs appear as `<Negated Name="<pin>"/>` on the Call. FBD `"A" = "B"`: an `IdentCon` wired
  straight into a coil's `in` pin (no gate) - very common (500+ coils in one real project); `_direct_op` reads it.
  The same happens in S7-GRAPH transitions / supervisions / interlocks (`TrCoil` fed straight by one tag: 47 of 713
  transitions in one real project); `cond_text` reads it too.
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
- After a compile or re-export TIA may **renumber the parts** (`UId`s) and write them in another order, with no logic
  change. `export_lines` (lines without UId/ID numbers, as a multiset) recognises that → `rep.note = "renumbered"`,
  shown as "no logic change"; other differences outside the drawing → `note = "other"` with the raw diff.


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
- Getting a block back from before a PLC folder move wrote it to the old folder, where TIA never looks →
  `workspace_path` maps it into today's folder (tested for inferred and `was` moves).
- S7-GRAPH chains with 3+ parallel branches: transition labels ran into the next column's steps → columns are
  400 px and a transition's text/highlight is capped to `COLW - STEP_W/2 - 12`.
- FBD coil / instance names above a box were centred on the box and cut off when longer than the drawing (left
  for a coil with nothing in front, right otherwise) → `fbd_boxlike` keeps the label inside and widens the drawing;
  names are shortened in the middle (`tmid`) so the end stays visible. Checked on 4,384 real networks: none overflow.
- Shortening names in the middle hid exactly the part that differs in real projects (`"FGDB_010LM…On".AddS.X` for
  device 003 and 004 looked the same) → LAD/FBD drawings show names whole (`NAME_MAX`); the drawing gets wider and
  starts zoomed out. Checked again on 4,384 real networks: no text outside its drawing.
- Data type (UDT) members showed as `None.Member`: TIA calls a UDT's only section "None" → `iface_members` leaves
  that section name out (block interfaces keep `Input.`, `Static.` …).
- FBD coils fed straight by a tag were read as `:= TRUE` and drawn with nothing in front, so a change of that tag
  was missed → `chain_of` / `fbd_tree` / `fbd_roots` use `_direct_op`. The samples have such a network now.
- GRAPH transitions with one tag wired straight into `TrCoil` showed "(no condition)" and a change of that tag was
  missed → `cond_text` uses `_direct_op`. The sample GRAPH block (`FB_WeldSequence`) has such transitions.
- A changed **order** is a real change: DB/UDT members (memory layout) → `order_diff`; networks of a block
  (execution order) → "network order changed". Only then may `export_lines` call the rest "renumbered".
- An iframe with a `srcdoc` hint ignores a later `src` → `loadFrame` removes `srcdoc` first.
- Iframe height in CSS grid (150 px default) → `grid-template-rows:minmax(0,1fr)` + `height:100%`.


## 8. How we test

- **`python tests/smoke.py` runs the whole checklist below in one go** (86 checks, ~30 s): both files compile,
  a throwaway Git repo is built from the samples, the compare finds every kind of change (tag, interface, contact,
  box pin, call parameter, negated input, added/removed network), the HTML page really contains drawings and print
  CSS, `--files` / `--work` / `--history` work, the CSV has the BOM and `;` Excel needs, and the app itself starts,
  answers `/api/ping` + `/api/state`, draws the report page, refuses an `/api` call without a token and quits.
  It never writes in a real project (temp folder, its own `APPDATA`) and never changes `PLCDiff.pyw`.
  `--keep` leaves the throwaway repo for poking at by hand; `--no-app` skips the server part.
- `make_samples.py <dir> old|new` writes realistic V20 exports (LAD with branches/TON/FB call/compare, FBD with
  gates and a negated call input, tag table, a global DB, a small S7-GRAPH sequence) for two versions; build a
  throwaway git repo with them.
- S7-GRAPH: export a real GRAPH FB, edit a copy (rename a step, change a time/action/condition/jump) and run
  `--files old.xml new.xml`.
- Team flow tested with a bare repo + two app instances (different `APPDATA`, ports): concurrent pushes, conflict,
  Keep mine / Use theirs.
- Screenshots via Playwright for checking and for the guide (paths masked to `D:\...`).
- After any change: `python tests/smoke.py`, then open the app and look at the real page.


## 9. Known limits / ideas not done yet

- Not read/drawn: hardware configuration, HMI, technology objects as diagrams (shown as text/“not understood”).
- Possible next: hardware configuration and HMI; a network install for teams without GitHub access.
- Done in 1.4.0: Blocks tab (view, per-block history), where is this tag used, update from GitHub, DB/UDT table,
  compare two folders, review sign-off, member/network order changes.
- Done in 1.1.0: **restore an old block version** — `Setup → Get an old version of a block back` writes one export
  from any commit back into the workspace for TIA's *Import from workspace* (`/api/blocks`, `/api/restoreblock`;
  refuses a block outside the selected PLC, and warns before overwriting uncommitted work).
- **Setup → Check all blocks** lists every export the app can't fully draw yet — the best source of new work.
  As of 1.0.1 it comes back clean on a real project of ~1,250 exports (mostly FBD, with F_FBD safety blocks and
  S7-GRAPH sequences), so today's gaps are missing features, not parsing. Run it again after a big TIA export.


## 10. Style

- The users are PLC programmers, not software developers: plain, short English in the UI; the app should do things
  itself (buttons) rather than ask for command lines.
- Keep it one file and standard library only. Check every change visually (screenshots of the real page).


## 11. Working agreements (for AI assistants)

- **This app is in daily use on real machines.** Change only what the task asks for. Don't refactor, rename or
  "tidy" `PLCDiff.pyw` on the side, and don't add a dependency.
- **Prove it before you say it works**: `python tests/smoke.py` (paste the result), plus a screenshot of the real
  page for anything visual. Never make a check pass by weakening the check.
- Bump `VERSION` for user-visible changes, note them in `README.md`, and keep the sections of this file true
  (code map, format notes, pitfalls, limits). The separate step-by-step user guide needs a new screenshot and a
  Changelog row whenever the UI changes.
- **This repository is public.** Machine paths, customer names, colleague names, project or device names and
  anything else site-specific stay out of it — they belong in `CLAUDE.local.md` (git-ignored, loaded automatically
  next to this file) or in the chat.
- Ready-made workflows live in `.claude/commands/`: `/check` (run the smoke test), `/block <export.xml>` (make the
  app handle an export it can't draw yet), `/ship` (check → version → docs → commit → push).
