#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Smoke test for PLC Diff - the "after any change" checklist from CLAUDE.md, as one command.

    python tests/smoke.py              # run everything
    python tests/smoke.py --keep       # keep the throwaway repo and say where it is
    python tests/smoke.py --no-app     # skip the browser-app check (no ports used)

It never changes PLCDiff.pyw and never touches a real project: it builds a throwaway Git repo
in a temp folder from tests/make_samples.py, runs the app against it, and checks the output.
Exit code 0 = all good, 1 = something broke (the failing output is printed).

Only Python 3.8+ stdlib and the `git` command, like the app itself.
"""
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
APP = ROOT / "PLCDiff.pyw"
SAMPLES = HERE / "make_samples.py"
PY = sys.executable or "python"
NOWIN = {"creationflags": 0x08000000} if os.name == "nt" else {}

fails = []


def say(ok, name, detail=""):
    print(("  ok   " if ok else "  FAIL ") + name + (("  -> " + detail) if detail and not ok else ""))
    if not ok:
        fails.append(name + ((": " + detail) if detail else ""))


def runp(args, cwd=None, env=None, timeout=180):
    """Run a command, return (code, output). Output is stdout+stderr as text."""
    p = subprocess.run([str(a) for a in args], cwd=str(cwd) if cwd else None, env=env,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout, **NOWIN)
    return p.returncode, p.stdout.decode("utf-8", "replace")


def app(*args, cwd=None, env=None):
    return runp([PY, APP] + list(args), cwd=cwd, env=env)


def has_all(text, needles):
    return [n for n in needles if n not in text]


# --- 1. the files compile -------------------------------------------------------------------
def check_compile():
    print("Compile")
    for f in (APP, SAMPLES, Path(__file__)):
        code, out = runp([PY, "-m", "py_compile", f])
        say(code == 0, "py_compile %s" % f.name, out.strip()[-300:])


# --- 2. a throwaway project with two versions ---------------------------------------------
def make_repo(tmp):
    repo = tmp / "repo"
    repo.mkdir(parents=True)
    env = dict(os.environ, GIT_AUTHOR_NAME="Smoke", GIT_AUTHOR_EMAIL="smoke@test",
               GIT_COMMITTER_NAME="Smoke", GIT_COMMITTER_EMAIL="smoke@test")
    for args in (["init", "-q", "."], ["config", "user.name", "Smoke"], ["config", "user.email", "smoke@test"]):
        runp(["git"] + args, cwd=repo, env=env)
    def commit(msg):
        runp(["git", "add", "-A"], cwd=repo, env=env)
        return runp(["git", "commit", "-q", "-m", msg], cwd=repo, env=env)
    code, out = runp([PY, SAMPLES, repo, "old"])
    say(code == 0, "make_samples old", out.strip()[-300:])
    commit("first export")
    code, out = runp([PY, SAMPLES, repo, "new"])
    say(code == 0, "make_samples new", out.strip()[-300:])
    code, out = commit("motor start reworked, jam timer 5s")
    say(code == 0, "two commits in the throwaway repo", out.strip()[-300:])
    return repo, env


# --- 3. the compare finds every kind of change --------------------------------------------
WANT = [
    'tag added: "I_DoorClosed"',                  # tag table
    'tag removed: "Q_TestLamp"',
    "interface added: Temp.tmpFlag",              # block interface
    'added contact "I_DoorClosed"',               # LAD contact
    "T#3s → T#5s",                                # box pin (TON PT)
    'Start "M_Start" → "M_AutoStart"',            # FB call parameter
    "network removed",                            # network alignment
    "network added",
    'NOT "I_ExtFault1" → "I_ExtFault1"',          # negated call input (FBD)
    "added reset coil (R)",                       # FBD coil
    'input "I_Ready" → "I_ReadyOk"',              # FBD tag wired straight into a coil
    "variable Static.Speed: Int := 1200 → Int := 1500",   # data block start value
    "variable order changed - moved",             # data block member order (changes the layout)
]


def check_text(repo):
    print("Compare (terminal)")
    code, out = app("--text", "--no-open", "HEAD~1", "HEAD", cwd=repo)
    say(code == 0, "compare runs", out.strip()[-400:])
    say("Traceback" not in out, "no traceback", out[-400:])
    miss = has_all(out, WANT)
    say(not miss, "all %d expected changes reported" % len(WANT), "missing: " + "; ".join(miss))
    for lang, block in (("LAD", "FC_Station.xml"), ("FBD", "FC_Alarms.xml")):
        say(re.search(re.escape(block) + r".*" + lang, out) is not None,
            "%s block read as %s" % (block, lang), out[-400:])
    return out


def check_html(repo, tmp):
    print("Compare (HTML page)")
    out_html = tmp / "report.html"
    code, out = app("--no-open", "--out", out_html, "HEAD~1", "HEAD", cwd=repo)
    say(code == 0, "page written", out.strip()[-400:])
    if not out_html.exists():
        return say(False, "page exists")
    h = out_html.read_text(encoding="utf-8")
    say(len(h) > 20000, "page is a real page (%d bytes)" % len(h))
    miss = has_all(h, ["<svg", "Logic as text", "PLC Diff", "@media print"])
    say(not miss, "page has drawings, text logic and print CSS", "missing: " + "; ".join(miss))
    say(h.count("<svg") >= 6, "networks are drawn (%d svg)" % h.count("<svg"))
    say("Traceback" not in h and "not understood" not in h, "nothing left undrawn",
        "page says a block was not understood")
    groups = re.findall(r'<details class="grp"[^>]*><summary>([^<]+)<', h)
    say(groups == ["Program blocks", "PLC tags"], "changed files are grouped by TIA folder, in TIA's order", str(groups))
    first_tag = h.find('id="f', h.find('<details class="grp"', h.find('<details class="grp"') + 1))
    say(h.find("<summary>PLC tags<") < first_tag, "each file sits inside its folder's group")
    say('<table class="mt">' in h and "1200" in h and "1500" in h and ">moved<" in h,
        "data block changes are a table (start value, moved member)")
    side = h[h.find('<aside class="sidenav">'):h.find("</aside>")]
    say(side.count('data-f="f') == h.count('<article class="file') >= 3 and "<details open>" in side,
        "the sidebar lists every changed file, grouped by TIA folder")
    say("Whole drawing" in h and "zfit" in h, "big drawings get the zoom-out / zoom-in tools")
    import re as _re
    secs = _re.findall(r'<section id="f\d+n\d+" data-k="([^"]*)" class="net (\w+)', h)
    say(secs and all(k for k, _ in secs) and h.count('<section class="net ') == 0 and "No network matches" in h,
        "every changed network can be folded and filtered (%d networks)" % len(secs))
    say('<div class="ntools"' in h and 'data-k="new"' in h and 'placeholder="Find a tag in this block' in h,
        "a block with 3+ changed networks gets the go-to / show-only / find toolbar")
    say('<div class="side pair"><div class="col"><div class="cap">Before' in h,
        "a changed LAD / FBD network is drawn before | after side by side")
    say('<div class="gview">' in h and all('<div class="gchg" data-g="%s" hidden>' % k in h for k in ("S2", "S3", "T2"))
        and 'rect data-g="S2"' in h, "a changed S7-GRAPH shows the whole sequence, each change opens before | after")
    say("transition T2: condition changed" in h, "a transition whose condition is one tag wired straight in is compared")


def check_files_mode(repo, tmp):
    print("Two files, no Git (--files)")
    rel = "vci/Program blocks/Station/FC_Station.xml"
    pair = []
    for rev, name in (("HEAD~1", "old.xml"), ("HEAD", "new.xml")):
        code, out = runp(["git", "show", "%s:%s" % (rev, rel)], cwd=repo)
        p = tmp / name
        p.write_text(out, encoding="utf-8")
        pair.append(p)
    code, out = app("--files", pair[0], pair[1], "--text", "--no-open", cwd=tmp)
    say(code == 0, "--files runs", out.strip()[-400:])
    say("T#3s → T#5s" in out, "--files finds the timer change", out[-400:])


def check_work(repo):
    print("Uncommitted export (--work)")
    f = repo / "vci" / "Program blocks" / "Station" / "FC_Station.xml"
    txt = f.read_text(encoding="utf-8")
    f.write_text(txt.replace("I_Reset", "I_ResetButton"), encoding="utf-8")
    code, out = app("--text", "--no-open", "--work", cwd=repo)
    say(code == 0, "--work runs", out.strip()[-400:])
    say('"I_Reset" → "I_ResetButton"' in out, "--work sees the edit on disk", out[-400:])
    f.write_text(txt, encoding="utf-8")


def check_history(repo, tmp):
    print("Change history")
    csv = tmp / "changes.csv"
    code, out = app("--history", "--csv", csv, "--no-open", cwd=repo)
    say(code == 0, "--history runs", out.strip()[-400:])
    say((repo / ".plcdiff" / "changes.db").exists(), "database written")
    page = repo / ".plcdiff" / "history.html"
    if page.exists():
        h = page.read_text(encoding="utf-8")
        say("<svg" in h, "history page has charts")
    if not csv.exists():
        return say(False, "CSV written")
    raw = csv.read_bytes()
    say(raw.startswith(b"\xef\xbb\xbf"), "CSV has the BOM Excel needs")
    rows = raw.decode("utf-8-sig").splitlines()
    say(rows[0].count(";") >= 10, "CSV uses ; separators", rows[0][:120])
    say(len(rows) >= 8, "CSV has the changes (%d rows)" % (len(rows) - 1))


# --- 4. the app itself (local web server) --------------------------------------------------
def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def check_app(repo, tmp):
    print("App (local web server)")
    cfg = tmp / "appdata"
    (cfg / "plc-diff").mkdir(parents=True)
    # pick the throwaway project for the app, the way Setup -> Choose folder would
    (cfg / "plc-diff" / "settings.json").write_text(json.dumps(
        {"recent": [str(repo)], "projects": [str(repo)], "last": str(repo)}, indent=2), encoding="utf-8")
    env = dict(os.environ, APPDATA=str(cfg), HOME=str(cfg), USERPROFILE=str(cfg),
               PLCDIFF_UPDATE_URL=(tmp / "no-such-release.json").as_uri())     # never ask the real GitHub
    port = free_port()
    proc = subprocess.Popen([PY, str(APP), "--app", "--port", str(port), "--no-browser"],
                            cwd=str(repo), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **NOWIN)
    inst = cfg / "plc-diff" / "running.json"
    try:
        token, deadline = None, time.time() + 25
        while time.time() < deadline:
            if proc.poll() is not None:
                return say(False, "app starts", (proc.stdout.read() or b"").decode("utf-8", "replace")[-400:])
            try:
                token = json.loads(inst.read_text(encoding="utf-8"))["token"]
                break
            except Exception:
                time.sleep(0.3)
        say(bool(token), "app starts and writes running.json")
        if not token:
            return

        def get(path, with_token=True):
            req = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path),
                                         headers={"X-Token": token} if with_token else {})
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status, r.read().decode("utf-8", "replace")

        st, body = get("/api/ping")
        say(st == 200, "/api/ping answers")
        st, body = get("/api/state")
        say(st == 200 and "repo" in body, "/api/state answers", body[:200])
        st, body = get("/")
        say(st == 200 and "PLC Diff" in body, "home page loads")
        st, body = get("/view/report?t=%s&old=HEAD~1&new=HEAD" % token)
        say(st == 200 and "<svg" in body, "report page in the app draws networks", body[:200])
        st, body = get("/view/history?t=%s" % token)
        say(st == 200 and "Something went wrong" not in body, "history page in the app loads", body[:200])
        def post(p, payload):
            req = urllib.request.Request("http://127.0.0.1:%d%s" % (port, p), data=json.dumps(payload).encode(),
                                         method="POST", headers={"X-Token": token, "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, json.loads(r.read().decode("utf-8", "replace"))

        # --- get an old version of a block back
        st, j = post("/api/blocks", {"rev": "HEAD~1"})
        names = [b["name"] for b in j.get("blocks", [])]
        say(st == 200 and "FC_Station" in names, "/api/blocks lists the blocks of an older version", str(j)[:200])
        fcs = next((b for b in j.get("blocks", []) if b["name"] == "FC_Station"), {})
        say(fcs.get("group") == "Program blocks" and fcs.get("sub") == "Station",
            "/api/blocks says which TIA folder each block is in", str(fcs)[:200])
        rel = next((b["path"] for b in j.get("blocks", []) if b["name"] == "FC_Station"), "")
        st, j = post("/api/restoreblock", {"rev": "HEAD~1", "path": rel})
        say(st == 200 and j.get("ok"), "the old version is put back into the workspace", str(j)[:200])
        code, want = runp(["git", "show", "HEAD~1:" + rel], cwd=repo)
        say((repo / rel).read_text(encoding="utf-8") == want, "the file on disk is now that old version")
        code, out = app("--text", "--no-open", "--work", cwd=repo)
        say("T#5s → T#3s" in out, "the compare shows the block going back", out[-300:])
        st, j = post("/api/restoreblock", {"rev": "HEAD~1", "path": rel})
        say(st == 200 and j.get("dirty") and not j.get("ok"),
            "a restore over uncommitted work warns first", str(j)[:200])
        st, j = post("/api/restoreblock", {"rev": "HEAD~1", "path": "../outside.xml"})
        say(st == 200 and j.get("error") and not j.get("ok"), "a file outside the project is refused", str(j)[:200])
        runp(["git", "checkout", "--", rel], cwd=repo)
        # --- Blocks tab
        st, j = post("/api/blocks", {"rev": "WORK"})
        say(st == 200 and "FC_Station" in [b["name"] for b in j.get("blocks", [])], "Blocks tab lists the files on disk", str(j)[:200])
        st, body = get("/view/block?t=%s&rev=WORK&path=%s" % (token, urllib.request.quote(rel)))
        say(st == 200 and "<svg" in body, "Blocks tab draws a block from the files on disk", body[:200])
        st, body = get("/view/blockhistory?t=%s&path=%s" % (token, urllib.request.quote(rel)))
        say(st == 200 and body.count("<tr>") >= 2 and "What changed" in body and "first export" in body,
            "a block's history lists its commits with links", body[-400:])
        st, body = get("/view/report?t=%s&old=HEAD~1&new=HEAD&file=%s" % (token, urllib.request.quote(rel)))
        say(st == 200 and "FC_Station" in body and "Station1_IO" not in body, "'What changed' shows only that block", body[:200])
        st, body = get("/view/whereused?t=%s&rev=HEAD&tag=%s" % (token, urllib.request.quote('"I_DoorClosed"')))
        say(st == 200 and "FC_Station" in body and "Station1_IO" in body and "<svg" in body,
            "Where is this tag used finds the network and the tag table", body[:300])
        st, j = post("/api/review", {"commit": "HEAD", "note": "smoke"})
        say(st == 200 and j.get("ok") and [e["who"] for e in j.get("reviews", [])] == ["Smoke"],
            "Mark as reviewed signs the commit with the Git name", str(j)[:200])
        st, body = get("/view/history?t=%s" % token)
        say(st == 200 and '"who": "Smoke"' in body, "the history page shows the review", body[-300:])
        st, body = get("/view/report?t=%s&old=HEAD~1&new=HEAD" % token)
        say(st == 200 and "reviewed by Smoke" in body, "the compare page shows the review", body[:300])
        st, body = get("/view/whereused?t=%s&rev=HEAD&tag=NoSuchTagAnywhere" % token)
        say(st == 200 and "No block" in body, "Where is this tag used says when nothing uses it", body[:200])
        st, body = get("/view/block?t=%s&rev=HEAD~1&path=%s" % (token, urllib.request.quote(rel)))
        say(st == 200 and "<svg" in body and "FC_Station" in body and "badge" not in body.split("<main>")[-1][:2000],
            "View draws a whole block as it was in a version", body[:200])
        st, body = get("/view/block?t=%s&rev=HEAD~1&path=%s" % (token, urllib.request.quote("../outside.xml")))
        say(st == 200 and "not in that version" in body, "View refuses a file that is not a block of that version", body[:200])

        try:
            get("/api/state", with_token=False)
            say(False, "/api without a token is refused")
        except Exception:
            say(True, "/api without a token is refused")
        req = urllib.request.Request("http://127.0.0.1:%d/api/quit" % port, data=b"{}", method="POST",
                                     headers={"X-Token": token, "Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=10).read()
        except Exception:
            pass
        for _ in range(30):
            if proc.poll() is not None:
                break
            time.sleep(0.2)
        say(proc.poll() is not None, "app quits when asked")
    finally:
        if proc.poll() is None:
            proc.kill()


# --- 7. a PLC whose folder moved: an old version goes back into today's folder --------------
def check_moved_plc(tmp):
    print("Get a block back after the PLC folder moved")
    import importlib.machinery, importlib.util
    loader = importlib.machinery.SourceFileLoader("plcdiff_under_test", str(APP))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    pd = importlib.util.module_from_spec(spec)
    loader.exec_module(pd)
    for case, was in (("inferred", None), ("was", "vci/OldName")):
        repo = tmp / ("moved-" + case)
        repo.mkdir(parents=True)
        env = dict(os.environ, GIT_AUTHOR_NAME="Smoke", GIT_AUTHOR_EMAIL="smoke@test",
                   GIT_COMMITTER_NAME="Smoke", GIT_COMMITTER_EMAIL="smoke@test")
        g = lambda *a: runp(["git"] + list(a), cwd=repo, env=env)
        g("init", "-q", ".")
        runp([PY, SAMPLES, repo, "old"])                        # exports straight in vci/ (old TIA layout)
        if was:
            (repo / was).mkdir(parents=True)
            for d in ("Program blocks", "PLC tags"):
                shutil.move(str(repo / "vci" / d), str(repo / was / d))
        g("add", "-A"); g("commit", "-q", "-m", "old layout")
        first = g("rev-parse", "HEAD")[1].strip()
        src = repo / (was or "vci")
        (repo / "vci" / "PLC_1").mkdir(parents=True)
        for d in ("Program blocks", "PLC tags"):
            shutil.move(str(src / d), str(repo / "vci" / "PLC_1" / d))
        cfg = {"name": "PLC_1", "path": "vci/PLC_1"}
        if was:
            cfg["was"] = [was]
        (repo / "plcdiff.json").write_text(json.dumps({"plcs": [cfg]}), encoding="utf-8")
        g("add", "-A"); g("commit", "-q", "-m", "moved into PLC_1")
        root = repo.resolve()
        blocks = pd.blocks_at(root, first, pd.plc_paths("PLC_1", root))
        rel = next((p for p, n in blocks if n == "FC_Station"), "")
        say(bool(rel), "%s: the block from before the move is listed" % case, str(blocks)[:200])
        if not rel:
            continue
        dest = pd.workspace_path(root, "PLC_1", rel)
        say(dest == "vci/PLC_1/Program blocks/Station/FC_Station.xml",
            "%s: it goes back into today's PLC folder" % case, dest)
        anyplc = pd.workspace_path(root, "", rel)
        say(anyplc == dest, "%s: also with All PLCs selected" % case, anyplc)
        out = pd.restore_block(root, first, rel, dest_rel=dest)
        say(Path(out) == root / dest and not (root / rel).exists(),
            "%s: no file is written to the old folder" % case, str(out))


# --- 8. long tag names fit inside the drawing --------------------------------------------------
def check_labels_fit():
    print("Long tag names stay inside the drawing")
    import importlib.machinery, importlib.util, re as _re
    loader = importlib.machinery.SourceFileLoader("plcdiff_labels", str(APP))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    pd = importlib.util.module_from_spec(spec)
    loader.exec_module(pd)
    name = '"DB_010_Safety_Data"."010STU001_Display_secured_and_checked"'
    for case, rows in (("coil fed by a gate", [{"pin": "", "block": pd.fbd_label('"I_Start"'), "neg": False}]),
                       ("coil with nothing in front", [{"pin": "", "block": pd.B(0, 20, 10, ""), "neg": False}])):
        b = pd.fbd_boxlike("=", name, rows, [], None, gate=True)
        xs = [float(x) for x in _re.findall(r'<text[^>]*x="([-0-9.]+)"[^>]*text-anchor="start"[^>]*>(?:<title>[^<]*</title>)?[^<]*', b.svg)]
        lab = pd.tmid(name, 44)
        ok = all(x >= 0 for x in xs) and b.w >= max(xs) + pd.tw(lab)
        say(ok, "%s: the label starts inside and the drawing is wide enough" % case, "w=%.0f xs=%s" % (b.w, xs))
        say(name[-10:] in pd.tmid(name, 44), "%s: the end of a long name stays visible" % case)
    print("Data type members")
    iface = pd.ET.fromstring('<Interface><Sections><Section Name="None"><Member Name="Speed" Datatype="Int"/>'
                             '</Section></Sections></Interface>')
    keys = list(pd.iface_members(iface))
    say(keys == ["Speed"], 'a data type member is called "Speed", not "None.Speed"', str(keys))
    iface = pd.ET.fromstring('<Interface><Sections><Section Name="Input"><Member Name="Start" Datatype="Bool"/>'
                             '</Section></Sections></Interface>')
    keys = list(pd.iface_members(iface))
    say(keys == ["Input.Start"], "a block interface member keeps its section", str(keys))


# --- 9. exports TIA only rewrote ----------------------------------------------------------------
def check_rewritten(tmp):
    print("Exports TIA only rewrote")
    d = tmp / "rewritten"
    d.mkdir()
    runp([PY, SAMPLES, d, "old"])
    src = next(d.rglob("FC_Alarms.xml"))
    text = src.read_text(encoding="utf-8")
    ren = re.sub(r'UId="(\d+)"', lambda m: 'UId="%d"' % (int(m.group(1)) + 1000), text)
    (d / "renumbered.xml").write_text(ren, encoding="utf-8")
    code, out = app("--files", src, d / "renumbered.xml", "--text", "--no-open")
    say("no logic change" in out and "text changed" not in out,
        "a block TIA only renumbered says 'no logic change'", out[-300:])
    (d / "attr.xml").write_text(text.replace('<Engineering version="V20" />', '<Engineering version="V20 Upd1" />'),
                                encoding="utf-8")
    code, out = app("--files", src, d / "attr.xml", "--text", "--no-open")
    say("nothing the drawing shows changed" in out, "a change outside the drawing is still reported", out[-300:])


# --- 10. folders, updates, reviews --------------------------------------------------------------
def check_folders(tmp):
    print("Compare two folders (no Git)")
    a, b = tmp / "fold-old", tmp / "fold-new"
    runp([PY, SAMPLES, a, "old"])
    runp([PY, SAMPLES, b, "new"])
    code, out = app("--folders", a, b, "--text", "--no-open")
    say(code == 0 and 'added contact "I_DoorClosed"' in out and "variable added: Static.Limit" in out,
        "two folders are compared like two versions", out[-300:])
    code, out = app("--folders", a, a, "--no-open", "--out", tmp / "same.html")
    page = (tmp / "same.html").read_text(encoding="utf-8") if (tmp / "same.html").exists() else ""
    say("same exports" in page, "two equal folders say so", page[-300:])


def _load_app(name):
    import importlib.machinery, importlib.util
    loader = importlib.machinery.SourceFileLoader(name, str(APP))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    pd = importlib.util.module_from_spec(spec)
    loader.exec_module(pd)
    return pd


def check_update(tmp):
    print("Update from GitHub (with a local stand-in for GitHub)")
    pd = _load_app("plcdiff_update")
    d = tmp / "upd"
    d.mkdir()
    newer = APP.read_text(encoding="utf-8").replace('VERSION = "%s"' % pd.VERSION, 'VERSION = "99.0.0"', 1)
    (d / "PLCDiff.pyw").write_text(newer, encoding="utf-8")
    rel = {"tag_name": "v99.0.0", "html_url": "https://example.invalid", "body": "test",
           "assets": [{"name": "PLCDiff.pyw", "browser_download_url": (d / "PLCDiff.pyw").as_uri()}]}
    (d / "latest.json").write_text(json.dumps(rel), encoding="utf-8")
    os.environ["PLCDIFF_UPDATE_URL"] = (d / "latest.json").as_uri()
    try:
        upd = pd.check_update()
        say(bool(upd) and upd["version"] == "99.0.0", "a newer release is found", str(upd)[:200])
        installed = d / "installed" / "PLCDiff.pyw"
        installed.parent.mkdir()
        installed.write_text(APP.read_text(encoding="utf-8"), encoding="utf-8")
        pd.install_update(installed, upd)
        say('VERSION = "99.0.0"' in installed.read_text(encoding="utf-8") and
            installed.with_name("PLCDiff.pyw.bak").exists(), "the update replaces the file and keeps a backup")
        bad = dict(upd, version="99.0.1")
        before = installed.read_bytes()
        try:
            pd.install_update(installed, bad)
            say(False, "a download of the wrong version is refused")
        except ValueError:
            say(installed.read_bytes() == before, "a download of the wrong version is refused, nothing changed")
        rel["tag_name"] = "v" + pd.VERSION
        (d / "latest.json").write_text(json.dumps(rel), encoding="utf-8")
        say(pd.check_update() is None, "no update offered when already up to date")
    finally:
        os.environ.pop("PLCDIFF_UPDATE_URL", None)


def check_reviews(tmp):
    print("Review sign-off shared through Git")
    pd = _load_app("plcdiff_reviews")
    d = tmp / "team"
    d.mkdir()
    env = dict(os.environ, GIT_AUTHOR_NAME="Smoke", GIT_AUTHOR_EMAIL="smoke@test",
               GIT_COMMITTER_NAME="Smoke", GIT_COMMITTER_EMAIL="smoke@test")
    runp(["git", "init", "-q", "--bare", "origin.git"], cwd=d, env=env)
    runp(["git", "clone", "-q", "origin.git", "A"], cwd=d, env=env)
    a, b = d / "A", d / "B"
    runp([PY, SAMPLES, a, "old"])
    for args in (["add", "-A"], ["commit", "-q", "-m", "first"], ["push", "-q", "origin", "HEAD"]):
        runp(["git"] + args, cwd=a, env=env)
    runp(["git", "clone", "-q", "origin.git", "B"], cwd=d, env=env)
    head = pd.git(a, "rev-parse", "HEAD").decode().strip()
    pd.add_review(a, head, "Ana", "checked")
    pd.add_review(b, head, "Bruno", "ok")             # both review the same commit before syncing
    pd.push(a)
    pd.push(b)
    pd.pull(a)
    ra = [e["who"] for e in pd.reviews_map(a).get(head, [])]
    rb = [e["who"] for e in pd.reviews_map(b).get(head, [])]
    say(sorted(ra) == ["Ana", "Bruno"] and sorted(rb) == ["Ana", "Bruno"],
        "two people's reviews of the same commit are both kept and shared", "A=%s B=%s" % (ra, rb))


def main():
    keep = "--keep" in sys.argv
    print("PLC Diff smoke test - %s" % APP)
    if not APP.exists() or not SAMPLES.exists():
        print("  FAIL  PLCDiff.pyw / make_samples.py not found next to tests/")
        return 1
    code, out = runp(["git", "--version"])
    if code != 0:
        print("  FAIL  the `git` command is not available: %s" % out.strip())
        return 1
    tmp = Path(tempfile.mkdtemp(prefix="plcdiff-smoke-"))
    try:
        check_compile()
        repo, env = make_repo(tmp)
        check_text(repo)
        check_html(repo, tmp)
        check_files_mode(repo, tmp)
        check_work(repo)
        check_history(repo, tmp)
        check_moved_plc(tmp)
        check_labels_fit()
        check_rewritten(tmp)
        check_folders(tmp)
        check_update(tmp)
        check_reviews(tmp)
        if "--no-app" not in sys.argv:
            check_app(repo, tmp)
        else:
            print("App (local web server)\n  skipped (--no-app)")
    finally:
        if keep:
            print("\nThrowaway project kept: %s" % tmp)
        else:
            shutil.rmtree(tmp, ignore_errors=True)
    print()
    if fails:
        print("%d check(s) FAILED:" % len(fails))
        for f in fails:
            print("  - %s" % f)
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
