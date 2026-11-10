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
    env = dict(os.environ, APPDATA=str(cfg), HOME=str(cfg), USERPROFILE=str(cfg))
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
