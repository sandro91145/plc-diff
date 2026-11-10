#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PLC Diff - see what changed in TIA Portal blocks between two Git versions,
with LAD networks drawn as ladder, FBD networks as FBD, and the changes highlighted.

Run it inside your Git repo (Git Bash or any terminal):

    python PLCDiff.pyw                 # uncommitted export vs last commit
                                                # (or, if nothing is uncommitted, last commit vs the one before)
    python PLCDiff.pyw v1.0-FAT        # a tag/commit compared with HEAD
    python PLCDiff.pyw c159251 HEAD    # any two versions
    python PLCDiff.pyw --files old.xml new.xml   # two files, no Git needed

Options:
    --all        also show the unchanged networks of changed blocks
    --text       only print the change log in the terminal, no HTML page
    --work       compare against the files on disk (uncommitted export)
    --out FILE   where to save the HTML page (default: a temp file)
    --no-open    don't open the browser
    --path DIR   only look inside this folder (default: the whole repo)
    --view V     auto (default: LAD as ladder, FBD as FBD), lad or fbd

Change history (a database of every change, with date and time):
    python PLCDiff.pyw --history           # update the database and open the history page
    python PLCDiff.pyw --history --csv changes.csv   # also save it for Excel
    python PLCDiff.pyw --install-hook      # update the history automatically after every commit

The app: double-click this file (saved as .pyw) - choose your folder, see changes, commit,
compare versions, browse the history, for all your projects. Or run:  python PLCDiff.pyw --app

Needs only Python 3.8+ - no extra packages.
"""
import argparse
import datetime
import difflib
import html
import json
import os
import sqlite3
import re
import subprocess
import sys
import tempfile
import webbrowser
import xml.etree.ElementTree as ET
from collections import OrderedDict, defaultdict
from pathlib import Path

VERSION = "1.1.1"
WORK = "<work>"          # marker for "files on disk"
TEXT_EXT = {".scl", ".stl", ".awl", ".db", ".udt", ".s7dcl", ".s7res", ".txt", ".csv", ".md"}
GATES = ("or", "and")


# =============================================================================
#  XML helpers
# =============================================================================

def load_xml(data):
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    root = ET.fromstring(data)
    for el in root.iter():
        if isinstance(el.tag, str) and "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
    return root


def kids(el, tag):
    return [c for c in el if c.tag == tag] if el is not None else []


def kid(el, tag):
    if el is None:
        return None
    for c in el:
        if c.tag == tag:
            return c
    return None


def ktext(el, tag, default=""):
    c = kid(el, tag)
    if c is None or c.text is None:
        return default
    return c.text.strip()


def natkey(s):
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", s or "")]


CULTURES = ("en-US", "en-GB", "pt-PT", "pt-BR", "de-DE")


def ml_text(owner, comp):
    """Text of a MultilingualText (Title / Comment) child of owner."""
    for mt in kids(kid(owner, "ObjectList"), "MultilingualText"):
        if mt.get("CompositionName") != comp:
            continue
        texts = OrderedDict()
        for item in mt.iter("MultilingualTextItem"):
            al = kid(item, "AttributeList")
            t = ktext(al, "Text")
            if t:
                texts[ktext(al, "Culture")] = t
        for c in CULTURES:
            if c in texts:
                return texts[c]
        return next(iter(texts.values()), "")
    return ""


SAFE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
AREAS = {"Input": "I", "Output": "Q", "Memory": "M", "DB": "DB", "DI": "DI",
         "Local": "L", "Timer": "T", "Counter": "C", "PeripheryInput": "I:P",
         "PeripheryOutput": "Q:P"}
SIZES = {"Byte": "B", "Char": "B", "SInt": "B", "USInt": "B", "Word": "W", "Int": "W",
         "UInt": "W", "DWord": "D", "DInt": "D", "UDInt": "D", "Real": "D", "Time": "D"}


def address_text(a):
    pre = AREAS.get(a.get("Area", ""), a.get("Area", "?"))
    try:
        off = int(a.get("BitOffset", ""))
    except ValueError:
        return "%" + pre + "?"
    typ = a.get("Type", "")
    if typ == "Bool":
        return "%%%s%d.%d" % (pre, off // 8, off % 8)
    return "%%%s%s%d" % (pre, SIZES.get(typ, ""), off // 8)


def access_text(acc):
    """Readable operand text for an <Access> / <Instance> element."""
    if acc is None:
        return "?"
    scope = acc.get("Scope", "")
    const = kid(acc, "Constant")
    if const is not None:
        if scope == "GlobalConstant":
            return '"%s"' % const.get("Name", "?")
        if scope == "LocalConstant":
            return "#" + const.get("Name", "?")
        return ktext(const, "ConstantValue") or const.get("Name", "?")
    sym = kid(acc, "Symbol")
    if sym is None and kid(acc, "Component") is not None:
        sym = acc
    if sym is not None:
        out = []
        for i, c in enumerate(kids(sym, "Component")):
            name = c.get("Name", "?")
            if (i == 0 and scope in ("GlobalVariable", "")) or not SAFE_NAME.match(name):
                t = '"%s"' % name
            else:
                t = name
            if c.get("AccessModifier") == "Array":
                t += "[" + ", ".join(access_text(a) for a in kids(c, "Access")) + "]"
            sl = c.get("SliceAccessModifier")
            if sl:
                t += ".%" + sl.upper()
            out.append(t)
        s = ".".join(out)
        return "#" + s if scope == "LocalVariable" else s
    addr = kid(acc, "Address")
    if addr is not None:
        return address_text(addr)
    label = kid(acc, "Label")
    if label is not None:
        return label.get("Name", "?")
    return "<%s>" % (scope or "?")


# =============================================================================
#  LAD / FBD network model
# =============================================================================

CMP_OPS = {"Eq": "==", "Ne": "<>", "Gt": ">", "Ge": ">=", "Lt": "<", "Le": "<="}
COIL_SYM = {"SCoil": "S", "RCoil": "R", "PCoil": "P", "NCoil": "N", "SRCoil": "SR"}
COIL_TYPE = {"Coil": "coil", "SCoil": "set coil (S)", "RCoil": "reset coil (R)",
             "PCoil": "P-edge coil", "NCoil": "N-edge coil"}
EDGE = {"PContact": "P", "NContact": "N"}
MAIN_PREF = ["en", "EN", "IN", "in", "pre", "S", "S1", "CU", "CD", "R", "R1", "CLK", "in1", "IN1"]


def classify(name):
    if name in ("Contact", "PContact", "NContact"):
        return "contact"
    if name.endswith("Coil"):
        return "coil"
    if name in CMP_OPS:
        return "cmp"
    if name == "Not":
        return "not"
    if name == "O":
        return "or"
    if name == "A":
        return "and"
    return "box"


class Part:
    def __init__(self, uid, name, kind):
        self.uid, self.name, self.kind = uid, name, kind
        self.negated = set()
        self.instance = ""
        self.tmpl = OrderedDict()
        self.params = []          # [(name, section)] for block calls
        self.block_type = ""
        self.is_call = False
        self.operand = ""         # main readable text
        self.extra = ""           # edge memory bit, compare type ...
        self.in1 = self.in2 = ""
        self.title = name
        self.etype = ""           # element type ("contact", "set coil (S)", "call FB" ...)
        self.key = ""             # identity for diffing
        self.ins = []             # boxes: [(pin, value, is_main)]
        self.outs = []            # boxes: [(pin, value, is_power)]
        self.pvals = OrderedDict()


class Net:
    def __init__(self, idx, title="", comment="", lang=""):
        self.idx, self.title, self.comment, self.lang = idx, title, comment, lang
        self.text = None
        self.graph = None
        self.parts = OrderedDict()
        self.pin_src, self.pin_val, self.pin_dir = {}, {}, {}
        self.pin_order = defaultdict(list)
        self.power_out = defaultdict(list)
        self.main_in = {}
        self.term_chains = []
        self.terms = []
        self.statements = []
        self.elements = []
        self._memo = {}
        self._pcs = OrderedDict()
        self._boxdone = set()

    @property
    def is_text(self):
        return self.text is not None

    def label(self):
        if self.graph is not None:
            return "GRAPH " + (self.title or "Sequence")
        return "Network %d" % self.idx + (' "%s"' % self.title if self.title else "")

    def fingerprint(self):
        if self.is_text:
            return "T|%s|%s|%s" % (self.title, self.comment, self.text)
        els = sorted("%s %s" % (e["key"], list(e["pvals"].items())) for e in self.elements)
        return "L|%s|%s|%s|%s" % (self.title, self.comment, "\n".join(self.statements), "\n".join(els))

    def set_text(self, t):
        self.text = t.replace("\r\n", "\n").strip("\n")
        self.statements = [l.rstrip() for l in self.text.split("\n")]

    # ---- parsing ---------------------------------------------------------
    def parse_flg(self, fn):
        accesses = {}
        parts_el, wires_el = kid(fn, "Parts"), kid(fn, "Wires")
        for el in (list(parts_el) if parts_el is not None else []):
            uid = el.get("UId")
            if el.tag == "Access":
                accesses[uid] = access_text(el)
            elif el.tag == "Part":
                name = el.get("Name", "?")
                p = Part(uid, name, classify(name))
                for n in kids(el, "Negated"):
                    p.negated.add(n.get("Name"))
                inst = kid(el, "Instance")
                if inst is not None:
                    p.instance = access_text(inst)
                for tv in kids(el, "TemplateValue"):
                    p.tmpl[tv.get("Name")] = (tv.text or "").strip()
                self.parts[uid] = p
            elif el.tag == "Call":
                ci = kid(el, "CallInfo")
                p = Part(uid, ci.get("Name", "?") if ci is not None else "?", "box")
                p.is_call = True
                if ci is not None:
                    p.block_type = ci.get("BlockType", "")
                    inst = kid(ci, "Instance")
                    if inst is not None:
                        p.instance = access_text(inst)
                    for prm in kids(ci, "Parameter"):
                        p.params.append((prm.get("Name"), prm.get("Section", "")))
                for n in kids(el, "Negated"):
                    p.negated.add(n.get("Name"))
                self.parts[uid] = p

        def note(uid, pin, d):
            if (uid, pin) not in self.pin_dir:
                self.pin_dir[(uid, pin)] = d
                self.pin_order[uid].append(pin)

        for w in (list(wires_el) if wires_el is not None else []):
            eps = []
            for e in w:
                if e.tag == "Powerrail":
                    eps.append(("rail",))
                elif e.tag == "IdentCon":
                    eps.append(("ident", e.get("UId")))
                elif e.tag == "NameCon":
                    eps.append(("pin", e.get("UId"), e.get("Name")))
                elif e.tag == "OpenCon":
                    eps.append(("open",))
            if not eps:
                continue
            src, dests = eps[0], eps[1:]
            if src[0] == "pin":
                note(src[1], src[2], "out")
            for d in dests:
                if d[0] == "pin":
                    k = (d[1], d[2])
                    note(d[1], d[2], "in")
                    if src[0] == "ident":
                        self.pin_val[k] = accesses.get(src[1], "?")
                    elif src[0] == "rail":
                        self.pin_src[k] = src
                    elif src[0] == "pin":
                        self.pin_src[k] = src
                        self.power_out[(src[1], src[2])].append(k)
                elif d[0] == "ident" and src[0] == "pin":
                    k = (src[1], src[2])
                    t = accesses.get(d[1], "?")
                    self.pin_val[k] = self.pin_val[k] + ", " + t if k in self.pin_val else t

        for uid, p in self.parts.items():
            self.main_in[uid] = self._main_input(p)
        for p in self.parts.values():
            self._describe(p)
        for uid in self.parts:
            self.chain_of(("pin", uid, ""))
        order = sorted(self.parts.values(), key=lambda p: natkey(p.uid))
        for p in order:
            if p.kind == "box":
                self.box_info(p)

        consumed = {k[0] for k, v in self.power_out.items() if v}
        terms = [p for p in order if p.kind not in GATES and p.uid not in consumed]
        self.term_chains = [self.chain_of(("pin", p.uid, "")) for p in terms]
        self.terms = terms
        term_ids = {p.uid for p in terms}

        for p in order:
            if p.kind == "coil" or p.uid in term_ids:
                self.statements.append(self._statement(p))

        for p in order:
            if p.kind in GATES:
                continue
            self.elements.append({"id": p.uid, "etype": p.etype, "operand": p.operand,
                                  "key": p.key, "pvals": p.pvals})
        for pid, it in self._pcs.items():
            t = "NOT input" if it["neg"] else "input"
            self.elements.append({"id": pid, "etype": t, "operand": it["text"],
                                  "key": "%s %s" % (t, it["text"]), "pvals": OrderedDict()})

    def _main_input(self, p):
        uid = p.uid
        if p.kind in ("contact", "coil", "cmp", "not"):
            for n in ("in", "pre"):
                if (uid, n) in self.pin_src:
                    return n
            return None
        if p.kind in GATES:
            return None
        power = [x for x in self.pin_order[uid] if (uid, x) in self.pin_src]
        for n in MAIN_PREF:
            if n in power:
                return n
        return power[0] if power else None

    def _describe(self, p):
        pv = lambda pin: self.pin_val.get((p.uid, pin), "")
        if p.kind == "contact":
            p.operand = pv("operand") or "???"
            edge = EDGE.get(p.name, "")
            p.extra = pv("bit")
            if edge:
                p.etype = "%s-edge contact" % edge
            else:
                p.etype = "NOT contact" if "operand" in p.negated else "contact"
            p.key = "%s %s" % (p.etype, p.operand) + (" (memory %s)" % p.extra if p.extra else "")
        elif p.kind == "coil" and p.name in GRAPH_COIL_NAMES:
            p.operand = GRAPH_COIL_NAMES[p.name]
            p.etype = "condition"
            p.key = "condition " + p.operand
        elif p.kind == "coil":
            p.operand = pv("operand") or "???"
            neg = bool(p.negated & {"operand", "out"})
            p.etype = "negated coil" if neg and p.name == "Coil" else COIL_TYPE.get(p.name, p.name)
            p.extra = pv("bit")
            p.key = "%s %s" % (p.etype, p.operand)
        elif p.kind == "cmp":
            p.in1, p.in2 = pv("in1") or "...", pv("in2") or "..."
            p.extra = p.tmpl.get("SrcType", "")
            p.operand = "%s %s %s" % (p.in1, CMP_OPS[p.name], p.in2)
            p.etype = "compare"
            p.key = "compare %s" % p.operand + (" (%s)" % p.extra if p.extra else "")
        elif p.kind == "not":
            p.operand = ""
            p.etype = "NOT (invert)"
            p.key = "NOT (invert)"
        elif p.kind == "box":
            if p.is_call:
                p.title = '"%s"' % p.name
                p.etype = "call %s" % (p.block_type or "block")
            else:
                tv = [v for k, v in p.tmpl.items() if k != "Card" and v]
                p.title = p.name + (" " + tv[0] if tv else "")
                p.etype = "%s box" % p.name
            p.operand = p.title + (" " + p.instance if p.instance else "")
            if p.is_call:
                p.key = "%s %s" % (p.etype, p.title) + (" (instance %s)" % p.instance if p.instance else "")
            else:
                p.key = p.etype + (" (instance %s)" % p.instance if p.instance else "")

    # ---- logic -----------------------------------------------------------
    def chain_of(self, src):
        """Series list of items from the power rail up to (and including) the part at src."""
        if not src or src[0] != "pin":
            return []
        uid = src[1]
        p = self.parts.get(uid)
        if p is None:
            return []
        if uid in self._memo:
            return self._memo[uid]
        self._memo[uid] = []                       # cycle guard
        if p.kind in GATES:
            branches = []
            ins = sorted([x for x in self.pin_order[uid] if self.pin_dir.get((uid, x)) == "in"], key=natkey)
            for pin in ins:
                k = (uid, pin)
                if k in self.pin_src:
                    branches.append(self.chain_of(self.pin_src[k]))
                elif k in self.pin_val:
                    pid = "%s:%s" % (uid, pin)
                    it = {"t": "pc", "id": pid, "text": self.pin_val[k], "neg": pin in p.negated}
                    self._pcs[pid] = it
                    branches.append([it])
            if p.kind == "or":
                res = factor(branches, "or" + uid)
            else:
                res = [x for b in branches for x in b]
        else:
            m = self.main_in.get(uid)
            res = (self.chain_of(self.pin_src.get((uid, m))) if m else []) + [{"t": "part", "id": uid, "p": p}]
        self._memo[uid] = res
        return res

    # ---- FBD expression trees ------------------------------------------
    def fbd_tree(self, src, seen=frozenset()):
        """Tree of gates/boxes/operands feeding the source pin (FBD view)."""
        if not src or src[0] != "pin":
            return None
        uid = src[1]
        p = self.parts.get(uid)
        if p is None:
            return None
        if uid in seen:
            return {"t": "op", "text": "(loop)", "neg": False, "id": "loop" + uid}
        seen = seen | {uid}

        def inp(pin, label=""):
            k = (uid, pin)
            neg = pin in p.negated
            if k in self.pin_src:
                return {"pin": label, "node": self.fbd_tree(self.pin_src[k], seen), "neg": neg}
            return {"pin": label, "op": self.pin_val.get(k, "..."), "neg": neg, "id": "%s:%s" % (uid, pin)}

        if p.kind in GATES:
            ins = sorted([x for x in self.pin_order[uid] if self.pin_dir.get((uid, x)) == "in"], key=natkey)
            return {"t": "gate", "kind": p.kind, "id": "g" + uid, "ins": [inp(x) for x in ins],
                    "neg_out": "out" in p.negated}
        m = self.main_in.get(uid)
        main = self.fbd_tree(self.pin_src.get((uid, m)), seen) if m else None
        if p.kind == "coil":                      # coil in the middle: passes the signal on
            return main
        if p.kind == "not":
            return {"t": "gate", "kind": "not", "id": "g" + uid, "ins": [{"pin": "", "node": main, "neg": False}],
                    "neg_out": False, "pid": uid}
        if p.kind == "contact":                   # a LAD contact inside an FBD network: AND with its operand
            ins = ([{"pin": "", "node": main, "neg": False}] if main else []) + \
                  [{"pin": "", "op": p.operand, "neg": "operand" in p.negated, "id": p.uid}]
            return {"t": "gate", "kind": "and", "id": "g" + uid, "ins": ins, "neg_out": False}
        return {"t": "box", "p": p, "id": uid, "main": main, "main_pin": m or ("en" if p.is_call else ""),
                "sec": {x: self.fbd_tree(self.pin_src[(uid, x)], seen)
                        for x, _, mm in p.ins if not mm and (uid, x) in self.pin_src}}

    def fbd_roots(self):
        roots = []
        for p in self.terms:
            if p.kind == "coil":
                m = self.main_in.get(p.uid)
                roots.append({"t": "assign", "p": p, "id": p.uid,
                              "in": self.fbd_tree(self.pin_src.get((p.uid, m))) if m else None})
            else:
                roots.append(self.fbd_tree(("pin", p.uid, "")))
        return [r for r in roots if r]

    def _pred(self, p):
        m = self.main_in.get(p.uid)
        return self.chain_of(self.pin_src.get((p.uid, m))) if m else []

    def box_info(self, p):
        if p.uid in self._boxdone:
            return
        self._boxdone.add(p.uid)
        uid = p.uid
        pins = self.pin_order[uid]
        if p.params:
            in_p = [n for n, s in p.params if s in ("Input", "InOut")]
            out_p = [n for n, s in p.params if s in ("Output", "Return")]
            for x in pins:
                if x not in in_p and x not in out_p:
                    (in_p if self.pin_dir[(uid, x)] == "in" else out_p).append(x)
        else:
            in_p = [x for x in pins if self.pin_dir[(uid, x)] == "in"]
            out_p = [x for x in pins if self.pin_dir[(uid, x)] == "out"]
        m = self.main_in.get(uid)
        if m in in_p:
            in_p.remove(m)
        ins = [(m or "", "", True)]
        for x in in_p:
            k = (uid, x)
            if k in self.pin_src:
                val = self.expr_text(self.chain_of(self.pin_src[k])) or "TRUE"
            else:
                val = self.pin_val.get(k, "...")
            if x in p.negated:
                val = "NOT " + val
            ins.append((x, val, False))
            p.pvals[x] = val
        power = [x for x in out_p if self.power_out.get((uid, x))]
        if "eno" in out_p and "eno" not in power:
            out_p.remove("eno")
        outs = [(x, "", True) for x in power]
        for x in out_p:
            if x in power:
                continue
            val = self.pin_val.get((uid, x), "...")
            outs.append((x, val, False))
            p.pvals[x] = val
        p.ins, p.outs = ins, outs

    def box_text(self, p, acc):
        self.box_info(p)
        args = []
        for pin, val, main in p.ins:
            if main:
                if acc or pin.lower() != "en":
                    args.append("%s := %s" % (pin or "EN", acc or "TRUE"))
            else:
                args.append("%s := %s" % (pin, val))
        for pin, val, pw in p.outs:
            if not pw and val and val != "...":
                args.append("%s => %s" % (pin, val))
        return "%s%s(%s)" % (p.title, " " + p.instance if p.instance else "", ", ".join(args))

    def expr_text(self, items):
        acc = ""

        def AND(a, b):
            return "%s AND %s" % (a, b) if a else b

        for it in items:
            if it["t"] == "par":
                br = []
                for b in it["br"]:
                    s = self.expr_text(b) or "TRUE"
                    br.append("(%s)" % s if " AND " in s and len(it["br"]) > 1 else s)
                acc = AND(acc, "(%s)" % " OR ".join(br))
            elif it["t"] == "pc":
                acc = AND(acc, ("NOT " if it["neg"] else "") + it["text"])
            else:
                p = it["p"]
                if p.kind == "contact":
                    e = EDGE.get(p.name)
                    if e:
                        s = "%s_EDGE(%s)" % (e, p.operand)
                    else:
                        s = ("NOT " if "operand" in p.negated else "") + p.operand
                    acc = AND(acc, s)
                elif p.kind == "cmp":
                    acc = AND(acc, p.operand)
                elif p.kind == "not":
                    acc = "NOT (%s)" % (acc or "TRUE")
                elif p.kind == "box":
                    acc = self.box_text(p, acc)
                # coils pass the signal through unchanged
        return acc

    def _statement(self, p):
        base = self.expr_text(self._pred(p))
        if p.kind == "coil":
            b = base or "TRUE"
            if p.etype == "coil":
                return "%s := %s" % (p.operand, b)
            if p.etype == "negated coil":
                return "%s := NOT (%s)" % (p.operand, b)
            if p.name == "SCoil":
                return "SET %s IF %s" % (p.operand, b)
            if p.name == "RCoil":
                return "RESET %s IF %s" % (p.operand, b)
            return "%s %s IF %s" % (p.etype, p.operand, b)
        if p.kind == "box":
            return self.box_text(p, base)
        return "%s   (not connected to an output)" % self.expr_text(self.chain_of(("pin", p.uid, "")))


def factor(branches, pid):
    if not branches:
        return []
    if len(branches) == 1:
        return branches[0]
    k = 0
    while all(len(b) > k for b in branches) and len({b[k]["id"] for b in branches}) == 1:
        k += 1
    return branches[0][:k] + [{"t": "par", "id": pid, "br": [b[k:] for b in branches]}]


# ---- SCL / STL networks as text ------------------------------------------

def tokens_text(el):
    out = []

    def walk(e):
        t = e.tag
        if t == "Access":
            if e.get("Scope") == "Call":
                for c in e:
                    walk(c)
            else:
                out.append(access_text(e))
            return
        if t == "CallInfo":
            inst = kid(e, "Instance")
            out.append(access_text(inst) if inst is not None else '"%s"' % e.get("Name", "?"))
            for c in e:
                if c.tag != "Instance":
                    walk(c)
            return
        if t == "Parameter":
            out.append(e.get("Name", ""))
            for c in e:
                walk(c)
            return
        if t == "Blank":
            out.append(" " * int(e.get("Num", "1") or 1))
            return
        if t == "NewLine":
            out.append("\n" * int(e.get("Num", "1") or 1))
            return
        if t in ("Token", "StlToken"):
            out.append(e.get("Text", ""))
            if t == "StlToken":
                out.append(" ")
            for c in e:
                walk(c)
            return
        if t == "LineComment":
            out.append("//" + "".join(x.text or "" for x in e.iter("Text")))
            return
        if t == "Comment":
            out.append("(*" + "".join(x.text or "" for x in e.iter("Text")) + "*)")
            return
        if t == "Text":
            out.append(e.text or "")
            return
        for c in e:
            walk(c)
        if t == "StlStatement":
            out.append("\n")

    walk(el)
    return "".join(out)


# =============================================================================
#  Documents (one exported XML file)
# =============================================================================

class Doc:
    def __init__(self, kind, name=""):
        self.kind = kind          # code | data | udt | tags | other
        self.name = name
        self.btype = ""
        self.number = ""
        self.lang = ""
        self.title = ""
        self.iface = OrderedDict()
        self.nets = []
        self.tags = OrderedDict()
        self.text = ""
        self.safety = False

    def heading(self):
        bits = [self.btype + (" %s" % self.number if self.number else "")] if self.btype else []
        if self.lang:
            bits.append(self.lang)
        return " · ".join(b for b in bits if b)


def detect_safety(al, lang):
    """F-blocks: language F_LAD / F_FBD / F_DB ..., or an attribute like IsFailsafe = true."""
    if (lang or "").upper().startswith("F_"):
        return True
    for c in (list(al) if al is not None else []):
        t = c.tag.lower() if isinstance(c.tag, str) else ""
        if ("failsafe" in t or t in ("isfailsafe", "issafety")) and (c.text or "").strip().lower() == "true":
            return True
    return False


def iface_members(iface):
    out = OrderedDict()
    if iface is None:
        return out

    def walk(m, sec, prefix):
        name = prefix + (m.get("Name") or "?")
        dt = m.get("Datatype", "")
        sv = ktext(m, "StartValue")
        out["%s.%s" % (sec, name)] = dt + (" := %s" % sv if sv else "")
        for sub in kids(m, "Member"):
            walk(sub, sec, name + ".")

    for sec in iface.iter("Section"):
        for m in kids(sec, "Member"):
            walk(m, sec.get("Name", "?"), "")
    return out


def parse_doc(data, path):
    try:
        root = load_xml(data)
    except ET.ParseError:
        d = Doc("other", Path(path).stem)
        d.text = data.decode("utf-8-sig", "replace")
        return d
    obj = next((c for c in root if isinstance(c.tag, str) and c.tag.startswith("SW.")), None)
    if obj is None:
        d = Doc("other", Path(path).stem)
        d.text = data.decode("utf-8-sig", "replace")
        return d
    tag = obj.tag
    al = kid(obj, "AttributeList")
    name = ktext(al, "Name") or Path(path).stem
    if tag in ("SW.Blocks.FC", "SW.Blocks.FB", "SW.Blocks.OB"):
        d = Doc("code", name)
        d.btype = tag.split(".")[-1]
        d.number = ktext(al, "Number")
        d.lang = ktext(al, "ProgrammingLanguage")
        d.title = ml_text(obj, "Title")
        d.safety = detect_safety(al, d.lang)
        d.iface = iface_members(kid(al, "Interface"))
        for i, cu in enumerate(kids(kid(obj, "ObjectList"), "SW.Blocks.CompileUnit"), 1):
            cal = kid(cu, "AttributeList")
            net = Net(len(d.nets) + 1, ml_text(cu, "Title"), ml_text(cu, "Comment"), ktext(cal, "ProgrammingLanguage") or d.lang)
            src = kid(cal, "NetworkSource")
            gr = kid(src, "Graph") if src is not None else None
            if gr is not None:                       # S7-GRAPH: permanent instructions + sequence(s)
                def perm(section, label):
                    for k, po in enumerate(kids(kid(gr, section), "PermanentOperation"), 1):
                        t = ml2(kid(po, "Title"))
                        pn = Net(len(d.nets) + 1, "%s %d%s" % (label, k, ": " + t if t else ""), ml2(kid(po, "Comment")),
                                 po.get("ProgrammingLanguage") or "FBD")
                        fn = kid(po, "FlgNet")
                        if fn is not None:
                            pn.parse_flg(fn)
                        d.nets.append(pn)
                perm("PreOperations", "Permanent pre-instruction")
                seqs = kids(gr, "Sequence")
                for k, seq in enumerate(seqs, 1):
                    gs = parse_graph_sequence(seq, k)
                    sn = Net(len(d.nets) + 1, ("Sequence %d" % k if len(seqs) > 1 else "Sequence") +
                             (": " + gs["title"] if gs["title"] else ""), ml2(kid(seq, "Comment")), "GRAPH")
                    sn.graph = gs
                    sn.statements = graph_summary(gs)
                    d.nets.append(sn)
                perm("PostOperations", "Permanent post-instruction")
                continue
            if src is not None:
                fn = kid(src, "FlgNet")
                st = kid(src, "StructuredText")
                if st is None:
                    st = kid(src, "StatementList")
                if fn is not None:
                    net.parse_flg(fn)
                elif st is not None:
                    net.set_text(tokens_text(st))
                elif len(src):
                    net.set_text(tokens_text(src))
            d.nets.append(net)
        return d
    if tag.startswith("SW.Blocks."):
        d = Doc("data", name)
        d.btype = tag.split(".")[-1]
        d.number = ktext(al, "Number")
        d.lang = ktext(al, "ProgrammingLanguage")
        d.safety = detect_safety(al, d.lang)
        d.iface = iface_members(kid(al, "Interface"))
        return d
    if tag == "SW.Types.PlcStruct":
        d = Doc("udt", name)
        d.btype = "UDT"
        d.iface = iface_members(kid(al, "Interface"))
        return d
    if tag == "SW.Tags.PlcTagTable":
        d = Doc("tags", name)
        d.btype = "Tag table"
        for t in obj.iter("SW.Tags.PlcTag"):
            tal = kid(t, "AttributeList")
            d.tags[ktext(tal, "Name")] = OrderedDict([
                ("type", ktext(tal, "DataTypeName")), ("address", ktext(tal, "LogicalAddress")),
                ("comment", ml_text(t, "Comment"))])
        for t in obj.iter("SW.Tags.PlcUserConstant"):
            tal = kid(t, "AttributeList")
            d.tags[ktext(tal, "Name")] = OrderedDict([
                ("type", ktext(tal, "DataTypeName")), ("value", ktext(tal, "Value")),
                ("comment", ml_text(t, "Comment"))])
        return d
    d = Doc("other", name)
    d.btype = tag.replace("SW.", "")
    d.text = data.decode("utf-8-sig", "replace")
    return d


# =============================================================================
#  Diffing
# =============================================================================

class NetDiff:
    def __init__(self, status, old, new):
        self.status = status       # same | changed | added | removed
        self.old, self.new = old, new
        self.bullets = []          # [(cls, text)]  cls: add | del | chg
        self.marks_old, self.marks_new = {}, {}
        self.pins_old, self.pins_new = {}, {}     # element id -> set of changed box pins
        self.details = []                         # GRAPH: [(label, NetDiff of a condition)]


class FileReport:
    def __init__(self, path, status):
        self.path, self.status = path, status
        self.old = self.new = None
        self.bullets = []
        self.nets = []
        self.textdiff = []
        self.error = ""

    @property
    def doc(self):
        return self.new or self.old

    @property
    def safety(self):
        return bool((self.new and self.new.safety) or (self.old and self.old.safety))

    def n_changes(self):
        return len(self.bullets) + sum(len(n.bullets) or 1 for n in self.nets if n.status != "same") + \
            (1 if self.textdiff and not self.bullets else 0)


def dict_diff(old, new, label):
    out = []
    for k, v in new.items():
        if k not in old:
            out.append(("add", "%s added: %s : %s" % (label, k, v)))
    for k, v in old.items():
        if k not in new:
            out.append(("del", "%s removed: %s : %s" % (label, k, v)))
    for k, v in new.items():
        if k in old and old[k] != v:
            out.append(("chg", "%s %s: %s → %s" % (label, k, old[k], v)))
    return out


def tag_diff(old, new):
    out = []
    for k, v in new.items():
        if k not in old:
            out.append(("add", 'tag added: "%s" %s %s' % (k, v.get("type", ""), v.get("address", v.get("value", "")))))
    for k, v in old.items():
        if k not in new:
            out.append(("del", 'tag removed: "%s" %s %s' % (k, v.get("type", ""), v.get("address", v.get("value", "")))))
    for k, v in new.items():
        if k in old and old[k] != v:
            ch = ["%s %s → %s" % (f, old[k].get(f) or "—", v.get(f) or "—") for f in v if old[k].get(f) != v.get(f)]
            out.append(("chg", 'tag "%s": %s' % (k, "; ".join(ch))))
    return out


def text_lines_diff(a, b, context=3):
    a = (a or "").replace("\r\n", "\n").split("\n")
    b = (b or "").replace("\r\n", "\n").split("\n")
    out = []
    for line in difflib.unified_diff(a, b, lineterm="", n=context):
        if line.startswith(("---", "+++")):
            continue
        out.append(line)
    return out


def compare_nets(o, n):
    if getattr(o, "graph", None) is not None and getattr(n, "graph", None) is not None:
        d = compare_graph(o, n)
        if o.title != n.title:
            d.bullets.insert(0, ("chg", 'title "%s" → "%s"' % (o.title or "—", n.title or "—")))
        return d
    d = NetDiff("changed", o, n)
    b = d.bullets
    if o.title != n.title:
        b.append(("chg", 'title "%s" → "%s"' % (o.title or "—", n.title or "—")))
    if o.comment != n.comment:
        b.append(("chg", "network comment changed"))
    if o.is_text or n.is_text:
        if (o.text or "") != (n.text or ""):
            b.append(("chg", "code changed (%s)" % (n.lang or o.lang or "text")))
        return d

    pool = defaultdict(list)
    for e in o.elements:
        pool[e["key"]].append(e)
    matched_old, pairs, un_new = set(), [], []
    for e in n.elements:
        if pool[e["key"]]:
            a = pool[e["key"]].pop(0)
            matched_old.add(a["id"])
            pairs.append((a, e))
        else:
            un_new.append(e)
    un_old = [e for e in o.elements if e["id"] not in matched_old]

    def pchanges(a, e):
        ks = list(OrderedDict.fromkeys(list(a["pvals"]) + list(e["pvals"])))
        ks = [k for k in ks if a["pvals"].get(k) != e["pvals"].get(k)]
        if ks:
            d.pins_old[a["id"]] = d.pins_new[e["id"]] = set(ks)
        return ["%s %s → %s" % (k, a["pvals"].get(k, "—"), e["pvals"].get(k, "—")) for k in ks]

    elem_bullets = []
    for a, e in pairs:
        ch = pchanges(a, e)
        if ch:
            elem_bullets.append(("chg", "%s: %s" % (e["key"], "; ".join(ch))))
            d.marks_old[a["id"]] = d.marks_new[e["id"]] = "chg"

    by_old, by_new = defaultdict(list), defaultdict(list)
    for e in un_old:
        by_old[e["etype"]].append(e)
    for e in un_new:
        by_new[e["etype"]].append(e)
    for t in list(OrderedDict.fromkeys(list(by_new) + list(by_old))):
        olds, news = by_old[t], by_new[t]
        if olds and len(olds) == len(news) and len(olds) <= 3:
            for a, e in zip(olds, news):
                txt = "%s %s → %s" % (t, a["operand"], e["operand"])
                ch = pchanges(a, e)
                if ch:
                    txt += " (" + "; ".join(ch) + ")"
                elem_bullets.append(("chg", txt))
                d.marks_old[a["id"]] = d.marks_new[e["id"]] = "chg"
            continue
        for e in news:
            elem_bullets.append(("add", "added " + e["key"]))
            d.marks_new[e["id"]] = "add"
        for a in olds:
            elem_bullets.append(("del", "removed " + a["key"]))
            d.marks_old[a["id"]] = "del"
    b.extend(elem_bullets)
    if not elem_bullets and o.statements != n.statements:
        b.append(("chg", "wiring changed (same elements, connected differently)"))
    return d


def align_nets(old, new):
    fo = [x.fingerprint() for x in old]
    fn = [x.fingerprint() for x in new]
    sm = difflib.SequenceMatcher(a=fo, b=fn, autojunk=False)
    result = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for i, j in zip(range(i1, i2), range(j1, j2)):
                result.append(NetDiff("same", old[i], new[j]))
            continue
        olds, news = list(range(i1, i2)), list(range(j1, j2))
        paired = {}
        used = set()
        for i in olds:                       # same title first
            if not old[i].title:
                continue
            for j in news:
                if j not in used and new[j].title == old[i].title:
                    paired[i] = j
                    used.add(j)
                    break
        for i in olds:                       # then by similarity
            if i in paired:
                continue
            best, score = None, 0.0
            for j in news:
                if j in used:
                    continue
                r = difflib.SequenceMatcher(a=fo[i], b=fn[j], autojunk=False).ratio()
                if r > score:
                    best, score = j, r
            titles_differ = old[i].title and new[best].title and old[i].title != new[best].title if best is not None else False
            if best is not None and score >= (0.6 if titles_differ else 0.35):
                paired[i] = best
                used.add(best)
        block = []
        for i in olds:
            if i in paired:
                block.append((paired[i], compare_nets(old[i], new[paired[i]])))
            else:
                nd = NetDiff("removed", old[i], None)
                nd.bullets.append(("del", "network removed"))
                for e in old[i].elements:
                    nd.marks_old[e["id"]] = "del"
                block.append((j1 - 0.5 + (i - i1) * 0.001, nd))
        for j in news:
            if j not in used:
                nd = NetDiff("added", None, new[j])
                nd.bullets.append(("add", "network added"))
                for e in new[j].elements:
                    nd.marks_new[e["id"]] = "add"
                block.append((j, nd))
        block.sort(key=lambda x: x[0])
        result.extend(nd for _, nd in block)
    for nd in result:
        if nd.status == "changed" and not nd.bullets:
            nd.status = "same"
    return result


def diff_file(path, status, old_bytes, new_bytes):
    rep = FileReport(path, status)
    ext = Path(path).suffix.lower()
    if ext != ".xml":
        a = old_bytes.decode("utf-8-sig", "replace") if old_bytes is not None else ""
        b = new_bytes.decode("utf-8-sig", "replace") if new_bytes is not None else ""
        rep.textdiff = text_lines_diff(a, b)
        rep.old = Doc("other", Path(path).stem) if old_bytes is not None else None
        rep.new = Doc("other", Path(path).stem) if new_bytes is not None else None
        return rep
    try:
        rep.old = parse_doc(old_bytes, path) if old_bytes is not None else None
        rep.new = parse_doc(new_bytes, path) if new_bytes is not None else None
        o, n = rep.old, rep.new
        doc = n or o
        if o is None:
            rep.bullets.append(("add", "new %s" % ((doc.btype or "file").lower() if doc.btype != "Tag table" else "tag table")))
        if n is None:
            rep.bullets.append(("del", "%s deleted" % (doc.btype or "file")))
        if doc.kind == "code":
            if o and n:
                if o.title != n.title:
                    rep.bullets.append(("chg", 'block title "%s" → "%s"' % (o.title, n.title)))
                if o.number != n.number:
                    rep.bullets.append(("chg", "block number %s → %s" % (o.number, n.number)))
                oi, ni = o.iface, n.iface
                if "GRAPH" in (o.lang, n.lang):      # step/transition members TIA generates itself
                    oi = OrderedDict((k, v) for k, v in oi.items() if not v.startswith("G7_"))
                    ni = OrderedDict((k, v) for k, v in ni.items() if not v.startswith("G7_"))
                rep.bullets.extend(dict_diff(oi, ni, "interface"))
            rep.nets = align_nets(o.nets if o else [], n.nets if n else [])
        elif doc.kind in ("data", "udt"):
            rep.bullets.extend(dict_diff(o.iface if o else OrderedDict(), n.iface if n else OrderedDict(),
                                         "member" if doc.kind == "udt" else "variable"))
        elif doc.kind == "tags":
            rep.bullets.extend(tag_diff(o.tags if o else OrderedDict(), n.tags if n else OrderedDict()))
        if doc.kind == "other" or (o and n and not rep.bullets and not any(x.status != "same" for x in rep.nets)):
            rep.textdiff = text_lines_diff(old_bytes.decode("utf-8-sig", "replace") if old_bytes else "",
                                           new_bytes.decode("utf-8-sig", "replace") if new_bytes else "")
    except Exception as ex:  # never crash on an unexpected export
        rep.error = "%s: %s" % (type(ex).__name__, ex)
        rep.textdiff = text_lines_diff(old_bytes.decode("utf-8-sig", "replace") if old_bytes else "",
                                       new_bytes.decode("utf-8-sig", "replace") if new_bytes else "")
    return rep


# =============================================================================
#  Ladder drawing (SVG)
# =============================================================================

CW = 6.6                       # width of one character at 11px monospace
HL = {"add": ("#16a34a", "#dcfce7"), "del": ("#dc2626", "#fee2e2"), "chg": ("#d97706", "#fef3c7")}
BUS, GAP = 14, 6


class B:
    __slots__ = ("w", "h", "L", "svg")

    def __init__(self, w, h, L, svg):
        self.w, self.h, self.L, self.svg = w, h, L, svg


def esc(s):
    return html.escape(str(s), quote=True)


def trunc(s, n=30):
    s = str(s)
    return s if len(s) <= n else s[: n - 1] + "…"


def tmid(s, n=34):
    s = str(s)
    if len(s) <= n:
        return s
    head = max(6, n // 3)
    return s[:head] + "…" + s[-(n - head - 1):]


def tw(s):
    return len(s) * CW


def g(x, y, svg):
    return '<g transform="translate(%.1f,%.1f)">%s</g>' % (x, y, svg)


def hl(w, h, mark):
    if not mark:
        return ""
    st, fill = HL[mark]
    return '<rect x="1" y="1" width="%.1f" height="%.1f" rx="6" fill="%s" stroke="%s" stroke-width="1.5"/>' % (
        w - 2, h - 2, fill, st)


def text(x, y, s, cls="t", anchor="middle", full=None):
    tip = "<title>%s</title>" % esc(full) if full and full != s else ""
    return '<text class="%s" x="%.1f" y="%.1f" text-anchor="%s">%s%s</text>' % (cls, x, y, anchor, tip, esc(s))


def blk_contact(label, mark, sym="", neg=False, below=""):
    lab, bl = tmid(label, 32), tmid(below, 32)
    w = max(64, tw(lab) + 16, tw(bl) + 16 if bl else 0)
    L, h = 30, 50 + (14 if bl else 0)
    cx = w / 2
    s = [hl(w, h, mark)]
    s.append('<path class="w" d="M0 %d H%.1f M%.1f %d H%.1f M%.1f %d V%d M%.1f %d V%d"/>' % (
        L, cx - 8, cx + 8, L, w, cx - 8, L - 11, L + 11, cx + 8, L - 11, L + 11))
    if neg:
        s.append('<path class="w" d="M%.1f %d L%.1f %d"/>' % (cx - 6, L + 9, cx + 6, L - 9))
    if sym:
        s.append(text(cx, L + 4, sym, "ts"))
    s.append(text(cx, L - 17, lab, full=label))
    if bl:
        s.append(text(cx, L + 26, bl, "tm", full=below))
    return B(w, h, L, "".join(s))


def blk_coil(label, mark, sym="", neg=False):
    lab = tmid(label, 32)
    w = max(64, tw(lab) + 16)
    L, h = 30, 50
    cx = w / 2
    s = [hl(w, h, mark)]
    s.append('<path class="w" d="M0 %d H%.1f M%.1f %d H%.1f M%.1f %d Q%.1f %d %.1f %d M%.1f %d Q%.1f %d %.1f %d"/>' % (
        L, cx - 9, cx + 9, L, w,
        cx - 5, L - 11, cx - 13, L, cx - 5, L + 11,
        cx + 5, L - 11, cx + 13, L, cx + 5, L + 11))
    if neg:
        s.append('<path class="w" d="M%.1f %d L%.1f %d"/>' % (cx - 5, L + 8, cx + 5, L - 8))
    if sym:
        s.append(text(cx, L + 4, sym, "ts"))
    s.append(text(cx, L - 17, lab, full=label))
    return B(w, h, L, "".join(s))


def blk_cmp(p, mark):
    a, b = trunc(p.in1, 30), trunc(p.in2, 30)
    op = CMP_OPS.get(p.name, p.name)
    w = max(72, tw(a) + 16, tw(b) + 16)
    L, h = 36, 76
    cx = w / 2
    s = [hl(w, h, mark)]
    s.append('<path class="w" d="M0 %d H%.1f M%.1f %d H%.1f"/>' % (L, cx - 20, cx + 20, L, w))
    s.append('<rect class="w" x="%.1f" y="%d" width="40" height="30" fill="#fff"/>' % (cx - 20, L - 15))
    s.append(text(cx, L - 1, op, "ts"))
    if p.extra:
        s.append(text(cx, L + 11, trunc(p.extra, 6), "tm"))
    s.append(text(cx, L - 21, a, full=p.in1))
    s.append(text(cx, L + 30, b, full=p.in2))
    return B(w, h, L, "".join(s))


def blk_not(mark):
    w, L, h = 60, 30, 50
    cx = w / 2
    s = [hl(w, h, mark),
         '<path class="w" d="M0 %d H%.1f M%.1f %d H%.1f M%.1f %d V%d M%.1f %d V%d"/>' % (
             L, cx - 14, cx + 14, L, w, cx - 14, L - 11, L + 11, cx + 14, L - 11, L + 11),
         text(cx, L + 4, "NOT", "ts")]
    return B(w, h, L, "".join(s))


def blk_box(p, mark, changed_pins=None):
    title, inst = trunc(p.title, 34), trunc(p.instance, 40)
    ins = list(p.ins) or [("", "", True)]
    outs = list(p.outs)
    rows = max(len(ins), len(outs), 1)
    lw = max([tw(trunc(v, 34)) for _, v, m in ins if not m] + [0]) + 20
    rw = max([tw(trunc(v, 34)) for _, v, pw in outs if not pw] + [0]) + 20
    lw, rw = max(lw, 28), max(rw, 28)
    pin_l = max([len(x) for x, _, _ in ins] + [0]) * CW
    pin_r = max([len(x) for x, _, _ in outs] + [0]) * CW
    bw = max(84, tw(title) + 22, pin_l + pin_r + 30)
    ROW, HEAD = 20, 20
    top = 22 if inst else 6
    L = top + HEAD + ROW / 2
    h = top + HEAD + rows * ROW + 8
    w = lw + bw + rw
    bx = lw
    changed_pins = changed_pins or set()
    if changed_pins and mark == "chg":
        s = ['<rect x="1" y="1" width="%.1f" height="%.1f" rx="6" fill="none" stroke="%s" stroke-width="1.5" '
             'stroke-dasharray="4 3"/>' % (w - 2, h - 2, HL["chg"][0])]
    else:
        s = [hl(w, h, mark)]

    def pin_hl(x, y, val, anchor):
        vw = tw(trunc(val, 34)) + 8
        x0 = x - vw + 4 if anchor == "end" else x - 4
        return '<rect x="%.1f" y="%.1f" width="%.1f" height="16" rx="3" fill="%s" stroke="%s"/>' % (
            x0, y - 8, vw, HL["chg"][1], HL["chg"][0])
    if inst:
        s.append(text(bx + bw / 2, top - 7, inst, "tm", full=p.instance))
    s.append('<rect class="w box" x="%.1f" y="%d" width="%.1f" height="%d" rx="2"/>' % (bx, top, bw, HEAD + rows * ROW))
    s.append(text(bx + bw / 2, top + 14, title, "tb", full=p.title))
    for i, (pin, val, main) in enumerate(ins):
        y = L + i * ROW
        if main:
            s.append('<path class="w" d="M0 %.1f H%.1f"/>' % (y, bx))
        else:
            s.append('<path class="w" d="M%.1f %.1f H%.1f"/>' % (bx - 8, y, bx))
            if pin in changed_pins:
                s.append(pin_hl(bx - 11, y, val, "end"))
            s.append(text(bx - 11, y + 4, trunc(val, 34), "t", "end", full=val))
        if pin:
            s.append(text(bx + 5, y + 4, pin, "tp", "start"))
    for i, (pin, val, pw) in enumerate(outs):
        y = L + i * ROW
        if pw:
            s.append('<path class="w" d="M%.1f %.1f H%.1f"/>' % (bx + bw, y, w))
        else:
            s.append('<path class="w" d="M%.1f %.1f H%.1f"/>' % (bx + bw, y, bx + bw + 8))
            if pin in changed_pins:
                s.append(pin_hl(bx + bw + 11, y, val, "start"))
            s.append(text(bx + bw + 11, y + 4, trunc(val, 34), "t", "start", full=val))
        s.append(text(bx + bw - 5, y + 4, pin, "tp", "end"))
    return B(w, h, L, "".join(s))


def pad(w):
    return B(max(w, 0), 1, 0, '<path class="w" d="M0 0 H%.1f"/>' % max(w, 0))


def series(blocks):
    blocks = [b for b in blocks if b is not None]
    if not blocks:
        return B(0, 20, 10, "")
    L = max(b.L for b in blocks)
    x, h, out = 0.0, 0.0, []
    for b in blocks:
        dy = L - b.L
        out.append(g(x, dy, b.svg))
        x += b.w
        h = max(h, dy + b.h)
    return B(x, h, L, "".join(out))


def par_block(bbs):
    W = max(b.w for b in bbs) + 2 * BUS
    y, out, ys = 0.0, [], []
    for b in bbs:
        out.append(g(BUS, y, b.svg))
        ly = y + b.L
        out.append('<path class="w" d="M0 %.1f H%d M%.1f %.1f H%.1f"/>' % (ly, BUS, BUS + b.w, ly, W))
        ys.append(ly)
        y += b.h + GAP
    out.append('<path class="w" d="M0 %.1f V%.1f M%.1f %.1f V%.1f"/>' % (ys[0], ys[-1], W, ys[0], ys[-1]))
    return B(W, y - GAP, ys[0], "".join(out))


def split_block(bbs):
    W = max(b.w for b in bbs) + BUS
    y, out, ys = 0.0, [], []
    for b in bbs:
        out.append(g(BUS, y, b.svg))
        ly = y + b.L
        out.append('<path class="w" d="M0 %.1f H%d"/>' % (ly, BUS))
        ys.append(ly)
        y += b.h + GAP
    out.append('<path class="w" d="M0 %.1f V%.1f"/>' % (ys[0], ys[-1]))
    return B(W, y - GAP, ys[0], "".join(out))


def make_trie(chains):
    if len(chains) == 1:
        return {"pre": chains[0], "kids": []}
    k = 0
    while all(len(c) > k for c in chains) and len({c[k]["id"] for c in chains}) == 1:
        k += 1
    groups = OrderedDict()
    for c in chains:
        rest = c[k:]
        if rest:
            groups.setdefault(rest[0]["id"], []).append(rest)
    if len(groups) <= 1:
        return {"pre": chains[0], "kids": []}
    return {"pre": chains[0][:k], "kids": [make_trie(v) for v in groups.values()]}


def net_svg(net, marks, pins=None):
    pins = pins or {}
    if not net.term_chains:
        return '<div class="empty">empty network</div>'

    def elem(it):
        mark = marks.get(it["id"])
        if it["t"] == "pc":
            return blk_contact(it["text"], mark, neg=it["neg"])
        p = it["p"]
        if p.kind == "contact":
            return blk_contact(p.operand, mark, sym=EDGE.get(p.name, ""), neg="operand" in p.negated, below=p.extra)
        if p.kind == "coil":
            sym = COIL_SYM.get(p.name, "" if p.name == "Coil" else p.name[:1])
            return blk_coil(p.operand, mark, sym=sym, neg=p.etype == "negated coil")
        if p.kind == "cmp":
            return blk_cmp(p, mark)
        if p.kind == "not":
            return blk_not(mark)
        return blk_box(p, mark, pins.get(it["id"]))

    def blk(it):
        if it["t"] == "par":
            return par_block([series([blk(x) for x in br]) for br in it["br"]])
        return elem(it)

    def is_coil(it):
        return it["t"] == "part" and it["p"].kind == "coil"

    def seq(items):
        return series([blk(x) for x in items])

    def nat(node):
        if not node["kids"]:
            body, term = node["pre"][:-1], node["pre"][-1]
            return seq(body).w + (24 if is_coil(term) else 0) + blk(term).w
        gap = 16 if node["pre"] and node["pre"][-1]["t"] == "par" else 0
        return seq(node["pre"]).w + gap + BUS + max(nat(k) for k in node["kids"])

    def render(node, avail):
        if not node["kids"]:
            body, term = node["pre"][:-1], node["pre"][-1]
            bb, tb = seq(body), blk(term)
            if is_coil(term):
                return series([bb if body else None, pad(avail - bb.w - tb.w), tb])
            return series([bb if body else None, tb])
        head = seq(node["pre"]) if node["pre"] else None
        hw = head.w if head else 0
        gap = 16 if node["pre"] and node["pre"][-1]["t"] == "par" else 0
        kb = [render(k, avail - hw - gap - BUS) for k in node["kids"]]
        return series([head, pad(gap) if gap else None, split_block(kb)])

    root = make_trie(net.term_chains)
    body = render(root, nat(root))
    W, H = body.w + 22, body.h + 14
    return ('<svg class="lad" width="%.0f" height="%.0f" viewBox="0 0 %.0f %.0f" xmlns="http://www.w3.org/2000/svg">'
            '<path class="rail" d="M5 0 V%.0f"/>%s</svg>') % (W, H, W, H, H, g(6, 7, body.svg))


# ---- FBD drawing -----------------------------------------------------------

def fbd_label(txt, mark=None, neg=False):
    lab = tmid(txt, 34)
    w = tw(lab) + 22
    s = []
    if mark:
        st, fill = HL[mark]
        s.append('<rect x="1" y="1" width="%.1f" height="18" rx="3" fill="%s" stroke="%s" stroke-width="1.3"/>' % (
            tw(lab) + 8, fill, st))
    s.append(text(4, 14, lab, "t", "start", full=txt))
    s.append('<path class="w" d="M%.1f 10 H%.1f"/>' % (tw(lab) + 10, w))
    return B(w, 20, 10, "".join(s))


def fbd_boxlike(title, top_label, rows, outs, mark, top_mark=None, gate=False, neg_out=False):
    """rows: [{pin, block(B), neg, hl}]  outs: [(pin, value, is_power, hl)]"""
    ROW, HEAD, WIRE = 20, (20 if not gate else 18), 14
    pin_l = max([len(r["pin"]) for r in rows] + [0]) * CW
    pin_r = max([len(o[0]) for o in outs] + [0]) * CW
    bw = max(46 if gate else 84, tw(trunc(title, 34)) + 20, pin_l + pin_r + 26)
    lw = max([r["block"].w for r in rows] + [0]) + WIRE
    top = 20 if top_label else 4
    # vertical placement of input rows
    ys, cur, placed = [], top + HEAD, []
    for r in rows:
        b = r["block"]
        y = cur + max(b.L, ROW / 2)
        placed.append((lw - WIRE - b.w, y - b.L, b))
        ys.append(y)
        cur = max(cur + ROW, y - b.L + b.h + 4)
    n_out = len(outs)
    out_ys = [ys[i] if i < len(ys) else (ys[-1] if ys else top + HEAD + ROW / 2) + ROW * (i - len(ys) + 1)
              for i in range(n_out)]
    last = max(ys + out_ys + [top + HEAD + ROW / 2])
    box_h = last + ROW / 2 + 2 - top
    rw = max([tw(trunc(o[1], 34)) for o in outs if not o[2]] + [0]) + 20 if outs else 0
    if outs and all(o[2] for o in outs):
        rw = 18
    x0 = lw
    w = lw + bw + rw
    h = max(top + box_h, max([py + b.h for _, py, b in placed] + [0])) + 4
    s = []
    if mark and mark != "chg":
        s.append(hl(w, h, mark))
    elif mark == "chg":
        s.append('<rect x="1" y="1" width="%.1f" height="%.1f" rx="6" fill="none" stroke="%s" stroke-width="1.5" '
                 'stroke-dasharray="4 3"/>' % (w - 2, h - 2, HL["chg"][0]))
    for px, py, b in placed:
        s.append(g(px, py, b.svg))
    s.append('<rect class="w box" x="%.1f" y="%d" width="%.1f" height="%.1f" rx="2"/>' % (x0, top, bw, box_h))
    s.append(text(x0 + bw / 2, top + 14, trunc(title, 34), "tb", full=title))
    if top_label:
        lab = trunc(top_label, 40)
        if top_mark:
            st, fill = HL[top_mark]
            s.append('<rect x="%.1f" y="1" width="%.1f" height="16" rx="3" fill="%s" stroke="%s"/>' % (
                x0 + bw / 2 - tw(lab) / 2 - 4, tw(lab) + 8, fill, st))
        s.append(text(x0 + bw / 2, 13, lab, "t" if gate else "tm", full=top_label))
    for r, y in zip(rows, ys):
        end = x0 - (7 if r["neg"] else 0)
        s.append('<path class="w" d="M%.1f %.1f H%.1f"/>' % (lw - WIRE, y, end))
        if r["neg"]:
            s.append('<circle class="w" cx="%.1f" cy="%.1f" r="3.5" fill="#fff"/>' % (x0 - 3.5, y))
        if r["pin"]:
            s.append(text(x0 + 5, y + 4, r["pin"], "tp", "start"))
    for (pin, val, pw, hlp), y in zip(outs, out_ys):
        start = x0 + bw + (7 if (neg_out and pw) else 0)
        if neg_out and pw:
            s.append('<circle class="w" cx="%.1f" cy="%.1f" r="3.5" fill="#fff"/>' % (x0 + bw + 3.5, y))
        if pw:
            s.append('<path class="w" d="M%.1f %.1f H%.1f"/>' % (start, y, w))
        else:
            s.append('<path class="w" d="M%.1f %.1f H%.1f"/>' % (start, y, x0 + bw + 8))
            if hlp:
                lab = trunc(val, 34)
                s.append('<rect x="%.1f" y="%.1f" width="%.1f" height="16" rx="3" fill="%s" stroke="%s"/>' % (
                    x0 + bw + 7, y - 8, tw(lab) + 8, HL["chg"][1], HL["chg"][0]))
            s.append(text(x0 + bw + 11, y + 4, trunc(val, 34), "t", "start", full=val))
        if pin:
            s.append(text(x0 + bw - 5, y + 4, pin, "tp", "end"))
    return B(w, h, ys[0] if ys else top + HEAD + ROW / 2, "".join(s))


def fbd_block(node, marks, pins):
    if node is None:
        return B(0, 20, 10, "")
    t = node["t"]

    def row(r, pin_hl=False):
        if "node" in r:
            b = fbd_block(r["node"], marks, pins) if r["node"] else B(0, 20, 10, "")
        else:
            mk = marks.get(r.get("id")) or ("chg" if pin_hl else None)
            b = fbd_label(r["op"], mk)
        return {"pin": r["pin"], "block": b, "neg": r["neg"]}

    if t == "op":
        return fbd_label(node["text"], marks.get(node["id"]))
    if t == "gate":
        title = {"and": "&", "or": ">=1", "not": "NOT"}[node["kind"]]
        rows = [row(r) for r in node["ins"]]
        return fbd_boxlike(title, "", rows, [("", "", True, False)], marks.get(node.get("pid")), gate=True,
                           neg_out=node["neg_out"])
    if t == "assign":
        p = node["p"]
        sym = {"Coil": "=", "SCoil": "S", "RCoil": "R", "PCoil": "P", "NCoil": "N",
               "TrCoil": "Trans", "SvCoil": "Superv", "IlCoil": "Interl"}.get(p.name, p.name)
        mark = marks.get(p.uid)
        rows = [{"pin": "", "block": fbd_block(node["in"], marks, pins) if node["in"] else B(0, 20, 10, ""),
                 "neg": p.etype == "negated coil"}]
        return fbd_boxlike(sym, p.operand, rows, [], None, top_mark=mark, gate=True)
    if t == "box":
        p = node["p"]
        changed = pins.get(p.uid, set())
        rows = []
        if p.kind == "cmp":
            if node["main"]:
                rows.append({"pin": "pre", "block": fbd_block(node["main"], marks, pins), "neg": False})
            for pin, val in (("IN1", p.in1), ("IN2", p.in2)):
                rows.append({"pin": pin, "block": fbd_label(val), "neg": False})
            title = CMP_OPS.get(p.name, p.name) + (" " + p.extra if p.extra else "")
            return fbd_boxlike(title, "", rows, [("", "", True, False)], marks.get(p.uid))
        for pin, val, main in p.ins:
            if main:
                if not node["main_pin"] and not node["main"]:
                    continue
                rows.append({"pin": node["main_pin"] or "EN",
                             "block": fbd_block(node["main"], marks, pins) if node["main"] else B(0, 20, 10, ""),
                             "neg": False})
                continue
            neg = pin in p.negated
            if pin in node["sec"]:
                rows.append({"pin": pin, "block": fbd_block(node["sec"][pin], marks, pins), "neg": neg})
            else:
                v = val[4:] if neg and val.startswith("NOT ") else val
                rows.append({"pin": pin, "block": fbd_label(v, "chg" if pin in changed else None), "neg": neg})
        outs = [(pin, val, pw, pin in changed) for pin, val, pw in p.outs]
        if not any(o[2] for o in outs):
            outs = [("ENO" if p.is_call else "", "", True, False)] + outs if p.is_call else outs
        return fbd_boxlike(p.title, p.instance, rows, outs, marks.get(p.uid))
    return B(0, 20, 10, "")


def net_svg_fbd(net, marks, pins=None):
    pins = pins or {}
    roots = net.fbd_roots()
    if not roots:
        return '<div class="empty">empty network</div>'
    blocks = [fbd_block(r, marks, pins) for r in roots]
    W = max(b.w for b in blocks) + 16
    y, out = 6.0, []
    for b in blocks:
        out.append(g(8, y, b.svg))
        y += b.h + 14
    H = y - 8
    return ('<svg class="lad" width="%.0f" height="%.0f" viewBox="0 0 %.0f %.0f" xmlns="http://www.w3.org/2000/svg">'
            '%s</svg>') % (W, H, W, H, "".join(out))


def draw_net(net, marks, pins, view):
    v = view
    if v == "auto":
        v = "fbd" if (net.lang or "").upper() in ("FBD", "F_FBD") else "lad"
    return net_svg_fbd(net, marks, pins) if v == "fbd" else net_svg(net, marks, pins)


# =============================================================================
#  S7-GRAPH (sequences): parse, compare, draw
# =============================================================================

GRAPH_COIL_NAMES = {"TrCoil": "Transition", "SvCoil": "Supervision", "IlCoil": "Interlock"}


def ml2(el):
    """Text of a <Title>/<Comment> holding <MultiLanguageText Lang=...> (GRAPH style)."""
    if el is None:
        return ""
    texts = OrderedDict()
    for m in el.iter("MultiLanguageText"):
        t = (m.text or "").strip()
        if t:
            texts[m.get("Lang", "")] = t
    for c in CULTURES:
        if c in texts:
            return texts[c]
    return next(iter(texts.values()), "")


def cond_net(el, title):
    n = Net(0, title, "", (el.get("ProgrammingLanguage") if el is not None else "") or "FBD")
    fn = kid(el, "FlgNet") if el is not None else None
    if fn is not None:
        n.parse_flg(fn)
    return n


def cond_text(n):
    for p in n.parts.values():
        if p.name in GRAPH_COIL_NAMES:
            return n.expr_text(n._pred(p))
    return " ; ".join(n.statements)


def parse_graph_sequence(seq, idx):
    g = {"title": ml2(kid(seq, "Title")), "steps": OrderedDict(), "trans": OrderedDict(),
         "branches": OrderedDict(), "edges": [], "jumps": []}
    for s in kids(kid(seq, "Steps"), "Step"):
        num = s.get("Number", "?")
        acts = []
        for a in kids(kid(s, "Actions"), "Action"):
            txt = " ".join(x for x in tokens_text(a).split())
            if not txt and not a.get("Qualifier"):
                continue
            q = a.get("Qualifier", "")
            ev = a.get("Event", "")
            acts.append(("%s%s: %s" % (q, " " + ev if ev else "", txt)).strip(": "))
        sup = cond_net(kid(kid(s, "Supervisions"), "Supervision"), "S%s supervision" % num)
        il = cond_net(kid(kid(s, "Interlocks"), "Interlock"), "S%s interlock" % num)
        g["steps"]["S" + num] = {"num": num, "name": s.get("Name", ""), "init": s.get("Init") == "true",
                                 "tmax": s.get("MaximumStepTime", ""), "twarn": s.get("WarningTime", ""),
                                 "actions": acts, "sup": sup, "il": il, "sup_txt": cond_text(sup), "il_txt": cond_text(il)}
    for t in kids(kid(seq, "Transitions"), "Transition"):
        num = t.get("Number", "?")
        c = cond_net(t, "T%s condition" % num)
        g["trans"]["T" + num] = {"num": num, "name": t.get("Name", ""), "cond": c, "txt": cond_text(c),
                                 "missing": t.get("IsMissing") == "true"}
    for b in kids(kid(seq, "Branches"), "Branch"):
        g["branches"]["B" + b.get("Number", "?")] = {"type": b.get("Type", ""), "card": b.get("Cardinality", "")}

    def ref(node):
        for c in (node if node is not None else []):
            pre = {"StepRef": "S", "TransitionRef": "T", "BranchRef": "B"}.get(c.tag)
            if pre:
                return pre + c.get("Number", "?"), int(c.get("Out", c.get("In", "0")) or 0)
        return None, 0

    for c in kids(kid(seq, "Connections"), "Connection"):
        a, ap = ref(kid(c, "NodeFrom"))
        b, bp = ref(kid(c, "NodeTo"))
        link = ktext(c, "LinkType") or "Direct"
        if a and b:
            (g["jumps"] if link == "Jump" else g["edges"]).append((a, ap, b, bp))
    return g


def graph_summary(g):
    """Readable lines for the whole sequence - used for 'Logic as text' and to spot changes."""
    out = []
    for sid, s in sorted(g["steps"].items(), key=lambda x: natkey(x[0])):
        out.append("%s %s%s  [max %s, warn %s]" % (sid, s["name"], " (initial step)" if s["init"] else "", s["tmax"], s["twarn"]))
        for a in s["actions"]:
            out.append("    action %s" % a)
        if s["sup_txt"]:
            out.append("    supervision: %s" % s["sup_txt"])
        if s["il_txt"]:
            out.append("    interlock: %s" % s["il_txt"])
    for tid, t in sorted(g["trans"].items(), key=lambda x: natkey(x[0])):
        out.append("%s %s: %s" % (tid, t["name"], t["txt"] or "(no condition)"))
    out += ["%s  ->  %s" % (a, b) for a, b in sorted(graph_links(g))]
    return out


def graph_links(g):
    """Sequence structure with branches resolved: {(from, to-description)}."""
    succ = defaultdict(list)
    for a, ap, b, bp in g["edges"]:
        succ[a].append((ap, b))
    res = set()

    def through(n, seen=()):
        if not n.startswith("B"):
            return [n]
        if n in seen:
            return []
        out = []
        for _, b in sorted(succ[n]):
            out += through(b, seen + (n,))
        return out

    for a in list(g["steps"]) + list(g["trans"]):
        targets = []
        for _, b in sorted(succ[a]):
            targets += through(b)
        for t in targets:
            res.add((a, t))
    for a, ap, b, bp in g["jumps"]:
        res.add((a, "jump to " + b))
    return res


def compare_graph(o, n):
    d = NetDiff("changed", o, n)
    d.details = []
    go, gn = o.graph, n.graph
    b = d.bullets
    mo, mn = d.marks_old, d.marks_new
    for kind, so, sn in (("step", go["steps"], gn["steps"]), ("transition", go["trans"], gn["trans"])):
        for k in sn:
            if k not in so:
                b.append(("add", '%s %s "%s" added' % (kind, k, sn[k]["name"])))
                mn[k] = "add"
        for k in so:
            if k not in sn:
                b.append(("del", '%s %s "%s" removed' % (kind, k, so[k]["name"])))
                mo[k] = "del"
        for k in sn:
            if k not in so:
                continue
            x, y, ch = so[k], sn[k], []
            if x["name"] != y["name"]:
                ch.append('name "%s" → "%s"' % (x["name"], y["name"]))
            if kind == "step":
                if x["init"] != y["init"]:
                    ch.append("is now %s initial step" % ("an" if y["init"] else "not an"))
                if x["tmax"] != y["tmax"]:
                    ch.append("max time %s → %s" % (x["tmax"] or "—", y["tmax"] or "—"))
                if x["twarn"] != y["twarn"]:
                    ch.append("warning time %s → %s" % (x["twarn"] or "—", y["twarn"] or "—"))
                added = [a for a in y["actions"] if a not in x["actions"]]
                removed = [a for a in x["actions"] if a not in y["actions"]]
                if len(added) == len(removed) == 1:
                    ch.append("action %s → %s" % (removed[0], added[0]))
                else:
                    ch += ["action added: %s" % a for a in added] + ["action removed: %s" % a for a in removed]
                for what, key, tkey in (("supervision", "sup", "sup_txt"), ("interlock", "il", "il_txt")):
                    if x[tkey] != y[tkey]:
                        ch.append("%s changed" % what)
                        d.details.append(("%s %s %s" % (k, y["name"], what), compare_nets(x[key], y[key])))
            else:
                if x["txt"] != y["txt"]:
                    ch.append("condition changed")
                    d.details.append(('%s "%s" condition' % (k, y["name"]), compare_nets(x["cond"], y["cond"])))
            if ch:
                b.append(("chg", "%s %s: %s" % (kind, k, "; ".join(ch))))
                mo[k] = mn[k] = "chg"
    lo, ln = graph_links(go), graph_links(gn)
    srcs = sorted({a for a, _ in (ln ^ lo)}, key=natkey)
    for a in srcs:
        if not ((a in go["steps"] or a in go["trans"]) and (a in gn["steps"] or a in gn["trans"])):
            continue                          # new/removed steps are reported above
        was = sorted([t for x, t in lo if x == a], key=natkey)
        now = sorted([t for x, t in ln if x == a], key=natkey)
        fmt = lambda ts: ", ".join(t.replace("jump to ", "jump ") for t in ts) or "nothing"
        b.append(("chg", "sequence: after %s now %s (was %s)" % (a, fmt(now), fmt(was))))
        mn.setdefault(a, "chg")
        mo.setdefault(a, "chg")
    return d


def graph_svg(g, marks):
    if not g["steps"] and not g["trans"]:
        return '<div class="empty">empty sequence</div>'
    CW, STEP_W, STEP_H, TR_H, BR_H, GAP = 300, 236, 42, 40, 14, 14
    nodes = list(g["steps"]) + list(g["trans"]) + list(g["branches"])
    succ, pred = defaultdict(list), defaultdict(list)
    for a, ap, b, bp in g["edges"]:
        if a in nodes and b in nodes:
            succ[a].append((ap, b))
            pred[b].append(a)
    for k in succ:
        succ[k].sort(key=lambda x: (x[0], natkey(x[1])))
    inits = [k for k, s in g["steps"].items() if s["init"]]
    roots = inits + sorted([n for n in nodes if not pred[n] and n not in inits], key=natkey)
    rank, visiting = {}, set()

    def rk(n):                                  # longest path from a root
        if n in rank:
            return rank[n]
        if n in visiting:
            return 0
        visiting.add(n)
        r = max([rk(p) + 1 for p in pred[n]] or [0])
        visiting.discard(n)
        rank[n] = r
        return r

    for n in nodes:
        rk(n)
    col = {}
    state = {"next": 0}

    def place(n, c):
        if n in col:
            return
        col[n] = c
        state["next"] = max(state["next"], c + 1)
        for i, (_, ch) in enumerate(succ[n]):
            if ch not in col:
                place(ch, c if i == 0 else state["next"])

    for r in roots:
        if r not in col:
            place(r, state["next"] if col else 0)
    for n in nodes:
        if n not in col:
            place(n, state["next"])
    height = lambda n: STEP_H if n[0] == "S" else TR_H if n[0] == "T" else BR_H
    nr = max(rank.values()) + 1
    rh = [0] * nr
    for n in nodes:
        rh[rank[n]] = max(rh[rank[n]], height(n))
    ry, y = [], 10
    for h_ in rh:
        ry.append(y)
        y += h_ + GAP
    jumps = defaultdict(list)
    for a, ap, b, bp in g["jumps"]:
        jumps[a].append(b)
    H = y + 14
    ncol = max(col.values()) + 1
    W = ncol * CW + 230
    X = lambda n: 24 + STEP_W / 2 + col[n] * CW
    top = lambda n: ry[rank[n]] + (rh[rank[n]] - height(n)) / 2
    bot = lambda n: top(n) + height(n)
    s = []
    # branch bars span the columns they connect
    for bid, br in g["branches"].items():
        if bid not in col:
            continue
        others = [X(c) for _, c in succ[bid]] + [X(p) for p in pred[bid]] + [X(bid)]
        x1, x2 = min(others), max(others)
        yb = top(bid) + BR_H / 2
        if br["type"].startswith("Sim"):
            s.append('<path class="w" d="M%.1f %.1f H%.1f M%.1f %.1f H%.1f"/>' % (x1, yb - 2, x2, x1, yb + 2, x2))
        else:
            s.append('<path class="w" d="M%.1f %.1f H%.1f"/>' % (x1, yb, x2))
    # links
    for a, ap, b, bp in g["edges"]:
        if a not in col or b not in col:
            continue
        xa, xb = X(a), X(b)
        ya = top(a) + BR_H / 2 if a[0] == "B" else bot(a)
        yb = top(b) + BR_H / 2 if b[0] == "B" else top(b)
        if abs(xa - xb) < 1:
            s.append('<path class="w" d="M%.1f %.1f V%.1f"/>' % (xa, ya, yb))
        elif a[0] == "B":
            s.append('<path class="w" d="M%.1f %.1f V%.1f"/>' % (xb, ya, yb))
        elif b[0] == "B":
            s.append('<path class="w" d="M%.1f %.1f V%.1f"/>' % (xa, ya, yb))
        else:
            ym = (ya + yb) / 2
            s.append('<path class="w" d="M%.1f %.1f V%.1f H%.1f V%.1f"/>' % (xa, ya, ym, xb, yb))
    # steps
    for sid, st in g["steps"].items():
        if sid not in col:
            continue
        x, t = X(sid), top(sid)
        mark = marks.get(sid)
        if mark:
            stc, fill = HL[mark]
            s.append('<rect x="%.1f" y="%.1f" width="%d" height="%d" rx="6" fill="%s" stroke="%s" stroke-width="1.5"/>' % (
                x - STEP_W / 2 - 5, t - 5, STEP_W + 10, STEP_H + 10, fill, stc))
        s.append('<rect class="w box" x="%.1f" y="%.1f" width="%d" height="%d" rx="2"/>' % (x - STEP_W / 2, t, STEP_W, STEP_H))
        if st["init"]:
            s.append('<rect class="w" x="%.1f" y="%.1f" width="%d" height="%d" rx="1"/>' % (
                x - STEP_W / 2 + 3, t + 3, STEP_W - 6, STEP_H - 6))
        s.append(text(x - STEP_W / 2 + 10, t + 17, sid, "tb", "start"))
        s.append(text(x - STEP_W / 2 + 38, t + 17, trunc(st["name"], 30), "t", "start", full=st["name"]))
        acts = st["actions"]
        sub = (acts[0] if acts else "no actions") + (" +%d" % (len(acts) - 1) if len(acts) > 1 else "")
        s.append(text(x - STEP_W / 2 + 10, t + 33, tmid(sub, 36), "tm", "start", full="\n".join(acts) or None))
        if st["sup_txt"] or st["il_txt"]:
            flags = ("S" if st["sup_txt"] else "") + ("I" if st["il_txt"] else "")
            s.append(text(x + STEP_W / 2 - 8, t + 17, flags, "tp", "end",
                          full=("Supervision: %s\n" % st["sup_txt"] if st["sup_txt"] else "") +
                               ("Interlock: %s" % st["il_txt"] if st["il_txt"] else "")))
    # transitions
    for tid, tr in g["trans"].items():
        if tid not in col:
            continue
        x, t = X(tid), top(tid)
        yc = t + TR_H / 2
        mark = marks.get(tid)
        lab = "%s %s" % (tid, tr["name"])
        cond = tr["txt"] or "(no condition)"
        tw_ = max(tw(trunc(lab, 34)) * 1.05, tw(trunc(cond, 44))) + 34
        if mark:
            stc, fill = HL[mark]
            s.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%d" rx="6" fill="%s" stroke="%s" stroke-width="1.5"/>' % (
                x - 18, t + 1, tw_, TR_H - 2, fill, stc))
        if not any(a == tid for a, *_ in g["edges"]) and not jumps.get(tid):
            pass
        s.append('<path class="w" d="M%.1f %.1f V%.1f" />' % (x, t, t + TR_H))
        s.append('<path class="w" d="M%.1f %.1f H%.1f" style="stroke-width:3"/>' % (x - 12, yc, x + 12))
        s.append(text(x + 18, yc - 3, trunc(lab, 34), "tb", "start", full=lab))
        s.append(text(x + 18, yc + 11, trunc(cond, 44), "tm", "start", full=cond))
        for j in jumps.get(tid, []):
            s.append('<path class="w" d="M%.1f %.1f V%.1f M%.1f %.1f L%.1f %.1f L%.1f %.1f"/>' % (
                x, t + TR_H, t + TR_H + 10, x - 4, t + TR_H + 5, x, t + TR_H + 10, x + 4, t + TR_H + 5))
            s.append(text(x + 7, t + TR_H + 11, j, "tb", "start", full="jump to " + j))
    return ('<svg class="lad" width="%.0f" height="%.0f" viewBox="0 0 %.0f %.0f" xmlns="http://www.w3.org/2000/svg">%s</svg>'
            % (W, H, W, H, "".join(s)))


def html_graph_net(nd, meta):
    view = meta.get("view", "auto")
    h = ['<div class="side">']
    if nd.old is not None and nd.status != "same":
        h.append('<div class="cap">Before · <code>%s</code></div><div class="ladwrap">%s</div>' % (
            esc(meta["old_short"]), graph_svg(nd.old.graph, nd.marks_old)))
    if nd.new is not None:
        cap = "After" if nd.status != "same" else "Sequence"
        h.append('<div class="cap">%s · <code>%s</code></div><div class="ladwrap">%s</div>' % (
            cap, esc(meta["new_short"]), graph_svg(nd.new.graph, nd.marks_new)))
    h.append("</div>")
    for label, cd in getattr(nd, "details", []) or []:
        h.append('<div class="gdet side"><div class="gdh">%s</div>%s' % (esc(label), html_bullets(cd.bullets)))
        h.append('<div class="cap">Before</div><div class="ladwrap">%s</div>' % draw_net(cd.old, cd.marks_old, cd.pins_old, view))
        h.append('<div class="cap">After</div><div class="ladwrap">%s</div></div>' % draw_net(cd.new, cd.marks_new, cd.pins_new, view))
    return "".join(h)


# =============================================================================
#  Report: terminal + HTML
# =============================================================================

SIGN = {"add": "+", "del": "-", "chg": "~"}


def print_report(meta, reports):
    print("PLC Diff  %s  →  %s" % (meta["old_label"], meta["new_label"]))
    if not reports:
        print("\nNo exported files changed.")
        return
    for r in reports:
        doc = r.doc
        head = doc.heading() if doc else ""
        print("\n%s%s%s" % ("[SAFETY] " if r.safety else "", r.path, "   (%s)" % head if head else ""))
        if r.error:
            print("  ! couldn't read this file fully (%s) - showing a text diff in the page" % r.error)
        for c, t in r.bullets:
            print("  %s %s" % (SIGN[c], t))
        for nd in r.nets:
            if nd.status == "same":
                continue
            net = nd.new or nd.old
            print("  %s" % net.label())
            for c, t in nd.bullets:
                print("    %s %s" % (SIGN[c], t))
        if r.textdiff and not r.bullets and not any(n.status != "same" for n in r.nets):
            adds = sum(1 for l in r.textdiff if l.startswith("+"))
            dels = sum(1 for l in r.textdiff if l.startswith("-"))
            print("  ~ text changed (+%d / -%d lines)" % (adds, dels))


CSS = r"""
:root{--bg:#f4f5f7;--card:#fff;--ink:#0f172a;--muted:#64748b;--line:#e2e8f0;
--add:#15803d;--addbg:#dcfce7;--del:#b91c1c;--delbg:#fee2e2;--chg:#b45309;--chgbg:#fef3c7;--accent:#0e7490}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.5 "Segoe UI",system-ui,-apple-system,sans-serif}
header{background:#0f172a;color:#cbd5e1;padding:22px 28px 20px}
header .brand{font:600 12px/1 "Segoe UI",sans-serif;letter-spacing:.12em;text-transform:uppercase;color:#5eead4}
header h1{margin:8px 0 10px;color:#fff;font-size:22px;font-weight:600}
.range{display:flex;flex-wrap:wrap;align-items:center;gap:10px;font-size:13px}
.rev{background:#1e293b;border:1px solid #334155;border-radius:6px;padding:4px 9px}
.rev code{color:#fff;font:600 12px Consolas,monospace;margin-right:6px}
.arrow{color:#64748b}
main{max-width:1500px;margin:0 auto;padding:22px 28px 60px}
.legend{display:flex;flex-wrap:wrap;gap:16px;font-size:12px;color:var(--muted);margin:0 0 18px}
.legend span{display:inline-flex;align-items:center;gap:6px}
.sw{width:14px;height:14px;border-radius:4px;border:1.5px solid}
.sw.add{background:var(--addbg);border-color:#16a34a}.sw.del{background:var(--delbg);border-color:#dc2626}.sw.chg{background:var(--chgbg);border-color:#d97706}
.toc{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 18px;margin-bottom:22px}
.toc h2{font-size:13px;margin:0 0 8px;color:var(--muted);font-weight:600;text-transform:uppercase;letter-spacing:.06em}
.toc a{color:var(--ink);text-decoration:none}.toc a:hover{color:var(--accent)}
.toc li{margin:3px 0}.toc ul{margin:0;padding-left:18px}
.count{color:var(--muted);font-size:12px;margin-left:6px}
.file{background:var(--card);border:1px solid var(--line);border-radius:12px;margin-bottom:26px;overflow:hidden}
.file>.fh{padding:14px 18px;border-bottom:1px solid var(--line);display:flex;flex-wrap:wrap;align-items:baseline;gap:10px}
.fh h2{margin:0;font-size:17px}
.fh .path{color:var(--muted);font:12px Consolas,monospace;word-break:break-all}
.fh .kind{font-size:12px;color:var(--accent);font-weight:600}
.fbody{padding:12px 18px 18px}
ul.b{list-style:none;margin:6px 0 10px;padding:0}
ul.b li{padding:3px 9px;border-radius:6px;margin:3px 0;font-size:13px;display:flex;gap:8px}
ul.b li::before{font:700 13px Consolas,monospace;width:10px;flex:none}
ul.b li.add{background:var(--addbg);color:#14532d}ul.b li.add::before{content:"+";color:var(--add)}
ul.b li.del{background:var(--delbg);color:#7f1d1d}ul.b li.del::before{content:"−";color:var(--del)}
ul.b li.chg{background:var(--chgbg);color:#78350f}ul.b li.chg::before{content:"~";color:var(--chg)}
.net{border:1px solid var(--line);border-left:4px solid var(--line);border-radius:10px;margin:14px 0;padding:12px 14px}
.net.changed{border-left-color:#d97706}.net.added{border-left-color:#16a34a}.net.removed{border-left-color:#dc2626}
.nh{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap}
.nh b{font-size:14px}.nh .nt{color:var(--muted)}
.badge{font:600 10px/1 "Segoe UI",sans-serif;text-transform:uppercase;letter-spacing:.06em;padding:4px 7px;border-radius:999px}
.badge.changed{background:var(--chgbg);color:var(--chg)}.badge.added{background:var(--addbg);color:var(--add)}
.badge.removed{background:var(--delbg);color:var(--del)}.badge.same{background:#f1f5f9;color:var(--muted)}
.side{margin-top:8px}
.side .cap{font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);margin:10px 0 4px}
.side .cap code{font:600 11px Consolas,monospace;text-transform:none;color:var(--ink)}
.ladwrap{overflow-x:auto;background:#fff;border:1px solid var(--line);border-radius:8px;padding:6px}
svg.lad{display:block}
svg.lad .w{stroke:#334155;stroke-width:1.4;fill:none}
svg.lad .box{fill:#f8fafc}
svg.lad .rail{stroke:#0f172a;stroke-width:3}
svg.lad text{font:11px Consolas,"Cascadia Mono","DejaVu Sans Mono",Menlo,monospace;fill:#0f172a}
svg.lad .ts{font-weight:700;font-size:11px}
svg.lad .tm{fill:#64748b;font-size:10px}
svg.lad .tp{fill:#475569;font-size:10px}
svg.lad .tb{font:600 11px "Segoe UI",system-ui,sans-serif}
.gdet{border-top:1px dashed var(--line);margin-top:12px;padding-top:10px}
.gdh{font-weight:600;font-size:13px;margin-bottom:2px}
details{margin-top:10px}summary{cursor:pointer;color:var(--accent);font-size:12px;font-weight:600}
pre.code{margin:6px 0 0;background:#0f172a;color:#e2e8f0;border-radius:8px;padding:10px 12px;overflow-x:auto;font:12px/1.55 Consolas,monospace}
pre.code .a{color:#86efac}pre.code .d{color:#fca5a5}pre.code .h{color:#7dd3fc}pre.code .c{color:#94a3b8}
.empty{color:var(--muted);font-style:italic;padding:8px}
.err{background:var(--delbg);color:#7f1d1d;border-radius:8px;padding:8px 12px;font-size:13px;margin:6px 0}
.nothing{background:var(--card);border:1px dashed var(--line);border-radius:12px;padding:40px;text-align:center;color:var(--muted)}
.commits{margin:12px 0 0;font-size:12px;color:#94a3b8}.commits code{color:#e2e8f0;font-family:Consolas,monospace;margin-right:6px}
footer{color:var(--muted);font-size:12px;text-align:center;padding:10px}
.same-list{color:var(--muted);font-size:12px;margin-top:10px}
header{position:relative}
.printbtn{position:absolute;right:28px;top:22px;font:600 13px "Segoe UI",sans-serif;background:#1e293b;color:#e2e8f0;
border:1px solid #334155;border-radius:8px;padding:7px 12px;cursor:pointer}.printbtn:hover{border-color:#64748b}
.sbadge{display:inline-block;background:#dc2626;color:#fff;font:700 10px/1 "Segoe UI",sans-serif;letter-spacing:.06em;
padding:4px 7px;border-radius:5px;vertical-align:2px;margin-left:6px}
.file.safety{border-color:#fca5a5;box-shadow:0 0 0 1px #fecaca}
.safetybar{background:#fef2f2;border:1px solid #fecaca;color:#7f1d1d;border-radius:10px;padding:12px 16px;margin:0 0 18px;
display:flex;flex-direction:column;gap:2px}.safetybar small{color:#991b1b}
.signoff{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px 18px;margin-top:26px;break-inside:avoid}
.signoff h2{margin:0 0 6px;font-size:16px}.signoff table{width:100%;border-collapse:collapse;margin-top:10px}
.signoff th{text-align:left;font-size:12px;color:var(--muted);width:16%;padding:8px}.signoff td{border-bottom:1px solid #94a3b8;height:34px}
.signoff td.tall{height:70px}
@media print{
  body{background:#fff}header{background:#fff;color:#0f172a;border-bottom:2px solid #0f172a;padding:10px 0}
  header h1{color:#0f172a}.rev{background:#fff;border-color:#cbd5e1;color:#0f172a}.rev code{color:#0f172a}
  .commits{color:#334155}.commits code{color:#0f172a}.brand{color:#0e7490}
  .printbtn,.legend span:last-child,details summary{display:none}
  main{padding:10px 0;max-width:none}.ladwrap{overflow:visible;border:0;padding:0}svg.lad{max-width:100%;height:auto}
  .net{break-inside:avoid}.file{break-inside:auto;border:1px solid #cbd5e1}.toc{break-after:avoid}
  *{-webkit-print-color-adjust:exact;print-color-adjust:exact}
}
"""


def html_bullets(bullets):
    if not bullets:
        return ""
    return '<ul class="b">' + "".join('<li class="%s"><span>%s</span></li>' % (c, esc(t)) for c, t in bullets) + "</ul>"


def html_codediff(lines):
    out = []
    for l in lines:
        cls = "a" if l.startswith("+") else "d" if l.startswith("-") else "h" if l.startswith("@@") else "c"
        out.append('<span class="%s">%s</span>' % (cls, esc(l)))
    return '<pre class="code">%s</pre>' % "\n".join(out)


def html_net(nd, meta):
    net = nd.new or nd.old
    st = nd.status
    badge = {"same": "unchanged", "changed": "changed", "added": "added", "removed": "removed"}[st]
    head = "Network %d" % net.idx if net.graph is None else "GRAPH"
    h = ['<section class="net %s"><div class="nh"><span class="badge %s">%s</span><b>%s</b>%s</div>' % (
        st, st, badge, head, '<span class="nt">%s</span>' % esc(net.title) if net.title else "")]
    h.append(html_bullets([b for b in nd.bullets if not (st in ("added", "removed") and b[1].startswith("network"))]))
    if net.graph is not None:
        if st == "added":
            nd.marks_new = {k: "add" for k in list(net.graph["steps"]) + list(net.graph["trans"])}
        if st == "removed":
            nd.marks_old = {k: "del" for k in list(net.graph["steps"]) + list(net.graph["trans"])}
        h.append(html_graph_net(nd, meta))
        if st != "same":
            lines = [l for l in difflib.ndiff(nd.old.statements if nd.old else [], nd.new.statements if nd.new else [])
                     if not l.startswith("?")]
            h.append("<details><summary>Sequence as text</summary>%s</details>" % html_codediff(
                [("+" if l[0] == "+" else "-" if l[0] == "-" else " ") + " " + l[2:] for l in lines]))
        h.append("</section>")
        return "".join(h)
    if net.is_text:
        if st == "same":
            h.append('<pre class="code">%s</pre>' % esc(net.text))
        else:
            lines = text_lines_diff(nd.old.text if nd.old else "", nd.new.text if nd.new else "", context=50)
            h.append(html_codediff(lines))
        h.append("</section>")
        return "".join(h)
    h.append('<div class="side">')
    if nd.old is not None and st != "same":
        h.append('<div class="cap">Before · <code>%s</code></div><div class="ladwrap">%s</div>' % (
            esc(meta["old_short"]), draw_net(nd.old, nd.marks_old, nd.pins_old, meta.get("view", "auto"))))
    if nd.new is not None:
        cap = "After" if st != "same" else "Network"
        h.append('<div class="cap">%s · <code>%s</code></div><div class="ladwrap">%s</div>' % (
            cap, esc(meta["new_short"]), draw_net(nd.new, nd.marks_new, nd.pins_new, meta.get("view", "auto"))))
    h.append("</div>")
    if st != "same":
        a = nd.old.statements if nd.old else []
        b = nd.new.statements if nd.new else []
        lines = [l for l in difflib.ndiff(a, b) if not l.startswith("?")]
        h.append("<details><summary>Logic as text</summary>%s</details>" % html_codediff(
            [("+" if l[0] == "+" else "-" if l[0] == "-" else " ") + " " + l[2:] for l in lines]))
    h.append("</section>")
    return "".join(h)


def build_html(meta, reports, show_all):
    h = ['<!doctype html><html lang="en"><head><meta charset="utf-8">'
         '<meta name="viewport" content="width=device-width,initial-scale=1">'
         '<title>PLC Diff · %s → %s</title><style>%s</style></head><body>' % (
             esc(meta["old_short"]), esc(meta["new_short"]), CSS)]
    title = meta.get("repo", "What changed") + (" · safety changes" if meta.get("safety_only") else "")
    h.append('<header><button class="printbtn" onclick="window.print()">Print / save as PDF</button>'
             '<div class="brand">PLC Diff%s</div><h1>%s</h1><div class="range">'
             '<span class="rev"><code>%s</code>%s</span><span class="arrow">→</span>'
             '<span class="rev"><code>%s</code>%s</span></div>' % (
                 " · change report" if not meta.get("safety_only") else " · safety change report", esc(title),
                 esc(meta["old_short"]), esc(meta["old_desc"]), esc(meta["new_short"]), esc(meta["new_desc"])))
    if meta.get("log"):
        h.append('<div class="commits">' + "<br>".join(
            "<code>%s</code>%s" % (esc(c), esc(s)) for c, s in meta["log"][:15]) + "</div>")
    h.append("</header><main>")
    h.append('<div class="legend"><span><i class="sw add"></i>added</span><span><i class="sw del"></i>removed</span>'
             '<span><i class="sw chg"></i>changed</span><span>Hover a shortened name to see it in full.</span></div>')
    safe = [r for r in reports if r.safety]
    if safe:
        names = ", ".join(esc(r.doc.name if r.doc else Path(r.path).stem) for r in safe[:8]) + (" …" if len(safe) > 8 else "")
        h.append('<div class="safetybar"><b>⚠ %d safety (F) block%s changed</b><span>%s</span>'
                 '<small>Check these against your safety review and the F-signature in TIA before downloading.</small></div>'
                 % (len(safe), "" if len(safe) == 1 else "s", names))
    if not reports:
        if meta.get("moved"):
            h.append('<div class="nothing"><b>%d file%s only moved to another folder</b> (same content), so there is nothing to '
                     'draw.<br>Pick an earlier version in <b>From</b> to see the changes made before the move.</div>' % (
                         meta["moved"], " was" if meta["moved"] == 1 else "s were"))
        else:
            h.append('<div class="nothing">No exported files changed between these two versions.</div>')
    else:
        h.append('<nav class="toc"><h2>%d file%s changed</h2><ul>' % (len(reports), "" if len(reports) == 1 else "s"))
        for i, r in enumerate(reports):
            doc = r.doc
            nm = doc.name if doc else Path(r.path).name
            nn = sum(1 for n in r.nets if n.status != "same")
            extra = "%d network%s" % (nn, "" if nn == 1 else "s") if nn else ""
            if r.bullets:
                extra = ", ".join(x for x in [extra, "%d other change%s" % (len(r.bullets), "" if len(r.bullets) == 1 else "s")] if x)
            h.append('<li><a href="#f%d">%s</a>%s<span class="count">%s</span></li>' % (
                i, esc(nm), ' <span class="sbadge">SAFETY</span>' if r.safety else "", esc(extra)))
        h.append("</ul></nav>")
        for i, r in enumerate(reports):
            doc = r.doc
            nm = doc.name if doc else Path(r.path).name
            h.append('<article class="file%s" id="f%d"><div class="fh"><h2>%s</h2>%s<span class="kind">%s</span>'
                     '<span class="path">%s</span></div><div class="fbody">' % (
                         " safety" if r.safety else "", i, esc(nm), '<span class="sbadge">SAFETY</span>' if r.safety else "",
                         esc(doc.heading() if doc else ""), esc(r.path)))
            if r.error:
                h.append('<div class="err">Couldn\'t read this export fully (%s). Showing the raw text changes instead. '
                         'Send this file to get it supported.</div>' % esc(r.error))
            h.append(html_bullets(r.bullets))
            same = []
            for nd in r.nets:
                if nd.status == "same" and not show_all:
                    same.append(nd.new.idx)
                    continue
                if nd.status == "same":
                    h.append("<details><summary>Network %d%s (unchanged)</summary>%s</details>" % (
                        nd.new.idx, " · " + esc(nd.new.title) if nd.new.title else "", html_net(nd, meta)))
                else:
                    h.append(html_net(nd, meta))
            if same and any(n.status != "same" for n in r.nets):
                h.append('<div class="same-list">%d unchanged network%s hidden (run with --all to show them).</div>' % (
                    len(same), "" if len(same) == 1 else "s"))
            if r.textdiff and (r.error or not (r.bullets or any(n.status != "same" for n in r.nets))):
                if doc and doc.kind != "other" and not r.error:
                    h.append('<div class="same-list">Only formatting or metadata changed. Raw text diff:</div>')
                h.append(html_codediff(r.textdiff[:600]))
            h.append("</div></article>")
    if meta.get("signoff"):
        h.append('<section class="signoff"><h2>%s</h2><p>Changes from <b>%s</b> %s to <b>%s</b> %s%s.</p>'
                 '<table><tr><th>Reviewed by</th><td></td><th>Date</th><td></td></tr>'
                 '<tr><th>F-signature (TIA)</th><td></td><th>Signature</th><td></td></tr>'
                 '<tr><th>Remarks</th><td colspan="3" class="tall"></td></tr></table></section>' % (
                     "Safety review sign-off" if meta.get("safety_only") else "Review sign-off",
                     esc(meta["old_short"]), esc(meta["old_desc"]), esc(meta["new_short"]), esc(meta["new_desc"]),
                     " · %d safety block%s" % (len(safe), "" if len(safe) == 1 else "s") if meta.get("safety_only") else ""))
    h.append("</main><footer>PLC Diff %s · generated %s</footer></body></html>" % (
        VERSION, datetime.datetime.now().strftime("%Y-%m-%d %H:%M")))
    return "".join(h)


# =============================================================================
#  Git
# =============================================================================

NOWIN = {"creationflags": 0x08000000} if os.name == "nt" else {}     # no console windows popping up


def ensure_git_on_path():
    """Git Bash knows where git is, but a double-clicked app may not: look in the usual install folders."""
    import shutil
    try:
        base = Path(os.environ.get("APPDATA") or str(Path.home() / ".config"))
        sp = base / "plc-diff" / "settings.json"
        saved = json.loads((sp if sp.exists() else base / "aurora-diff" / "settings.json").read_text(encoding="utf-8")).get("git_dir", "")
    except Exception:
        saved = ""
    if saved and Path(saved).is_dir() and saved not in os.environ.get("PATH", ""):
        root = Path(saved).parent if Path(saved).name.lower() in ("cmd", "bin") else Path(saved)
        extra = [saved, str(root / "mingw64" / "bin"), str(root / "usr" / "bin")]
        os.environ["PATH"] = os.pathsep.join(extra + [os.environ.get("PATH", "")])
    if shutil.which("git"):
        return True
    if os.name != "nt":
        return False
    bases = [os.environ.get("ProgramFiles", r"C:\Program Files"), os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
             os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs"), os.environ.get("ProgramW6432", "")]
    for b in bases:
        if not b:
            continue
        root = Path(b) / "Git"
        if (root / "cmd" / "git.exe").exists():
            extra = [str(root / "cmd"), str(root / "mingw64" / "bin"), str(root / "usr" / "bin")]
            os.environ["PATH"] = os.pathsep.join(extra + [os.environ.get("PATH", "")])
            return True
    return False


def git(root, *args, check=True):
    r = subprocess.run(["git", "-c", "core.quotepath=off"] + list(args), cwd=root,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, **NOWIN)
    if check and r.returncode != 0:
        raise RuntimeError(r.stderr.decode("utf-8", "replace").strip() or "git %s failed" % args[0])
    return r.stdout


def repo_root():
    try:
        return Path(git(Path.cwd(), "rev-parse", "--show-toplevel").decode().strip())
    except Exception:
        sys.exit("PLC Diff: run this from inside a project's Git repo.")


def rev_ok(root, rev):
    return subprocess.run(["git", "rev-parse", "--verify", "--quiet", rev + "^{commit}"], cwd=root,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                          **NOWIN).returncode == 0


def rev_desc(root, rev):
    if rev == WORK:
        return "now", "uncommitted (files on disk)"
    out = git(root, "log", "-1", "--format=%h%x00%s", rev).decode("utf-8", "replace").strip()
    short, _, subj = out.partition("\0")
    return short, subj


def changed_files(root, old, new, path, moves=None):
    paths = list(path) if isinstance(path, (list, tuple)) else [path]
    args = ["-c", "diff.renameLimit=100000", "diff", "--name-status", "-z", "-M", old] + \
        ([] if new == WORK else [new]) + ["--"] + paths
    toks = git(root, *args).decode("utf-8", "replace").split("\0")
    res, i = [], 0
    while i < len(toks) and toks[i]:
        st = toks[i]
        i += 1
        if st[0] in "RC":
            if st[1:] != "100":              # a pure move (same content) has nothing to show
                res.append(("R", toks[i], toks[i + 1]))
            elif moves is not None:
                moves.append((toks[i], toks[i + 1]))
            i += 2
        else:
            res.append((st[0], toks[i], toks[i]))
            i += 1
    if new == WORK:
        for p in git(root, "ls-files", "--others", "--exclude-standard", "-z", "--", *paths).decode("utf-8", "replace").split("\0"):
            if p:
                res.append(("A", p, p))
    return res


def read_rev(root, rev, p):
    if rev == WORK:
        return (root / p).read_bytes()
    return git(root, "show", "%s:%s" % (rev, p))


def blocks_at(root, rev, paths):
    """Every export that existed in this version, as [(path in the repo, block name)]."""
    skip = tuple(p[len(":(exclude)"):].rstrip("/") + "/" for p in paths if p.startswith(":(exclude)"))
    seen, out = set(), []
    for p in [x for x in paths if not x.startswith(":(")]:
        args = ["ls-tree", "-r", "--name-only", rev]
        if p != ".":
            args += ["--", p]
        for rel in git(root, *args, check=False).decode("utf-8", "replace").splitlines():
            rel = rel.strip()
            if rel.lower().endswith(".xml") and rel not in seen and not rel.startswith(skip):
                seen.add(rel)
                out.append((rel, Path(rel).stem))
    return sorted(out, key=lambda x: natkey(x[0]))


def workspace_path(root, plc, rel):
    """Where an export from any version belongs in the workspace now. A PLC folder may have moved since that
    version (see plc_prefixes): an export from an earlier folder goes to the same place in the current one."""
    pref = plc_prefixes(root).get(plc) if plc else None
    if not pref:
        return rel
    base = "" if pref[0] == "." else pref[0].rstrip("/") + "/"
    for old in sorted(pref[1:], key=len, reverse=True):
        old = old.strip("/")
        if not old or not rel.startswith(old + "/"):
            continue
        if old.rsplit("/", 1)[-1].lower() in TIA_FOLDERS:   # exports sat directly in vci/: keep "Program blocks/..."
            parent = old.rsplit("/", 1)[0] if "/" in old else ""
            return base + rel[len(parent) + 1 if parent else 0:]
        return base + rel[len(old) + 1:]
    return rel


def restore_block(root, rev, rel, dest_dir=None, dest_rel=None):
    """Write one export as it was in `rev` back into the workspace at dest_rel (default: where it was then),
    or into dest_dir. Returns the file written."""
    data = read_rev(root, rev, rel)
    dest = (Path(dest_dir) / Path(rel).name) if dest_dir else (root / (dest_rel or rel))
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    return dest



# =============================================================================
#  PLCs: one folder per PLC under vci/  (vci/<PLC>/Program blocks/...)
# =============================================================================

TIA_FOLDERS = {"program blocks", "plc tags", "plc data types", "technology objects", "external source files",
               "watch and force tables", "traces", "device configuration", "software units", "online backups",
               "plc alarm text lists", "plc supervisions", "text lists", "cross-device functions", "security settings"}


PROJECT_CFG = "plcdiff.json"        # committed in the repo, so everyone shares the same PLC folders


def load_project_cfg(root):
    try:
        c = json.loads((Path(root) / PROJECT_CFG).read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(c, dict) or not isinstance(c.get("plcs"), list):
        return None
    c["plcs"] = [x for x in c["plcs"] if isinstance(x, dict) and x.get("name") and x.get("path") is not None]
    return c


def save_project_cfg(root, cfg):
    (Path(root) / PROJECT_CFG).write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")


def plc_map(root):
    """PLC name -> folder (relative, with /) that holds its exports."""
    if root is None:
        return OrderedDict()
    cfg = load_project_cfg(root)
    if cfg is not None:
        return OrderedDict((x["name"], str(x["path"]).replace("\\", "/").strip("/") or ".") for x in cfg["plcs"])
    return OrderedDict((n, "vci/" + n) for n in _auto_plcs(root)[0])


def plc_prefixes(root):
    """PLC name -> [current folder, earlier folders]. Earlier folders keep the history visible after a move:
    either listed as "was" in plcdiff.json, or - for exports that used to sit directly in vci/ - inferred when
    exactly one PLC folder now holds TIA folders (Program blocks, PLC tags ...) directly."""
    out = OrderedDict((n, [d]) for n, d in plc_map(root).items())
    if root is None:
        return out
    cfg = load_project_cfg(root)
    explicit = False
    if cfg is not None:
        for x in cfg["plcs"]:
            for w in x.get("was", []) or []:
                if x["name"] in out:
                    out[x["name"]].append(str(w).replace("\\", "/").strip("/"))
                    explicit = True
    if not explicit and "vci" not in [v[0] for v in out.values()]:
        cands = []
        for n, (d, *_) in out.items():
            try:
                tia = [c.name for c in (Path(root) / d).iterdir() if c.is_dir() and c.name.lower() in TIA_FOLDERS]
            except Exception:
                tia = []
            if tia:
                cands.append((n, tia))
        if len(cands) == 1:
            out[cands[0][0]] += ["vci/" + t for t in cands[0][1]]
    return out


def list_plcs(root):
    """(plc names, legacy) - legacy = exports sit directly in vci/ (one PLC, old layout)."""
    cfg = load_project_cfg(root)
    if cfg is not None:
        return [x["name"] for x in cfg["plcs"]], False
    return _auto_plcs(root)


def _auto_plcs(root):
    vci = Path(root) / "vci"
    if not vci.is_dir():
        return [], False
    dirs = sorted([d for d in vci.iterdir() if d.is_dir() and not d.name.startswith(".")], key=lambda d: natkey(d.name))
    plcs = [d.name for d in dirs if d.name.lower() not in TIA_FOLDERS]
    legacy = any(d.name.lower() in TIA_FOLDERS for d in dirs)
    return plcs, legacy


def plc_of(path, mapping=None):
    path = path.replace("\\", "/")
    if mapping:
        best, bl = "", -1
        for name, ds in mapping.items():
            for d in (ds if isinstance(ds, list) else [ds]):
                pre = "" if d == "." else d.rstrip("/") + "/"
                if path.startswith(pre) and len(pre) > bl:
                    best, bl = name, len(pre)
        if best:
            return best
        parts = path.split("/")
        if len(parts) > 2 and parts[0] == "releases" and parts[1] in mapping:
            return parts[1]
        return ""
    parts = path.split("/")
    if len(parts) > 2 and parts[0] in ("vci", "releases") and parts[1].lower() not in TIA_FOLDERS:
        return parts[1]
    return ""


def plc_paths(plc, root=None):
    if not plc:
        return ["."]
    m = plc_map(root) if root is not None else {}
    base = m.get(plc, "vci/%s" % plc)
    out = [base, "releases/%s" % plc]
    if root is not None:
        out += plc_prefixes(root).get(plc, [base])[1:]          # earlier locations of this PLC's exports
    pre = "" if base == "." else base.rstrip("/") + "/"
    for n, d in m.items():                     # another PLC's folder inside this one: leave it out
        if n != plc and d != base and (d + "/").startswith(pre):
            out.append(":(exclude)%s" % d)
    return out


# =============================================================================
#  Change history database
# =============================================================================

EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
SCHEMA = """
CREATE TABLE IF NOT EXISTS commits (
    hash TEXT PRIMARY KEY, short TEXT, date TEXT, author TEXT, message TEXT, seq INTEGER);
CREATE TABLE IF NOT EXISTS changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    commit_hash TEXT REFERENCES commits(hash) ON DELETE CASCADE,
    date TEXT, path TEXT, block TEXT, block_type TEXT, language TEXT,
    network INTEGER, network_title TEXT, kind TEXT, change TEXT);
CREATE INDEX IF NOT EXISTS ix_changes_date ON changes(date);
CREATE INDEX IF NOT EXISTS ix_changes_block ON changes(block);
"""
KIND_WORD = {"add": "added", "del": "removed", "chg": "changed"}


def ensure_ignored(root, entry=".plcdiff/"):
    gi = root / ".gitignore"
    try:
        cur = gi.read_text(encoding="utf-8") if gi.exists() else ""
    except Exception:
        return
    if entry not in cur.split():
        with open(gi, "a", encoding="utf-8") as f:
            f.write(("" if cur.endswith("\n") or not cur else "\n") + "\n# PLC Diff change history (rebuilt from Git)\n" + entry + "\n")
        print("Added %s to .gitignore (the database can always be rebuilt from Git)." % entry)


def rows_for_report(rep):
    """Turn one file's diff into database rows (without commit info)."""
    doc = rep.doc
    base = {"path": rep.path, "block": doc.name if doc else Path(rep.path).stem,
            "block_type": doc.btype if doc else "", "language": doc.lang if doc else "",
            "network": None, "network_title": ""}
    out = []
    if rep.status in ("A", "D") and doc is not None:
        n = len(doc.nets)
        what = (doc.btype or "file") if doc.kind != "tags" else "tag table"
        txt = ("new %s" % what if rep.status == "A" else "%s deleted" % what) + (" (%d network%s)" % (n, "" if n == 1 else "s") if n else "")
        if doc.kind == "tags":
            txt += " (%d tags)" % len(doc.tags)
        return [dict(base, kind="add" if rep.status == "A" else "del", change=txt)]
    for c, t in rep.bullets:
        out.append(dict(base, kind=c, change=t))
    for nd in rep.nets:
        if nd.status == "same":
            continue
        net = nd.new or nd.old
        for c, t in nd.bullets:
            out.append(dict(base, network=net.idx, network_title=net.title, kind=c, change=t))
    if not out and rep.textdiff:
        a = sum(1 for l in rep.textdiff if l.startswith("+"))
        d = sum(1 for l in rep.textdiff if l.startswith("-"))
        out.append(dict(base, kind="chg", change="text changed (+%d / -%d lines)" % (a, d)))
    if rep.error and not out:
        out.append(dict(base, kind="chg", change="changed (couldn't read fully: %s)" % rep.error))
    return out


def update_history(root, path=".", quiet=False):
    db_dir = root / ".plcdiff"
    db_dir.mkdir(exist_ok=True)
    ensure_ignored(root)
    con = sqlite3.connect(str(db_dir / "changes.db"))
    con.execute("PRAGMA foreign_keys = ON")
    con.executescript(SCHEMA)
    if not rev_ok(root, "HEAD"):
        return con
    log = git(root, "log", "--reverse", "--first-parent", "--format=%H%x00%h%x00%aI%x00%an%x00%s", "HEAD")
    commits = [l.split("\0") for l in log.decode("utf-8", "replace").splitlines() if l.count("\0") == 4]
    hashes = [c[0] for c in commits]
    known = {r[0] for r in con.execute("SELECT hash FROM commits")}
    stale = known - set(hashes)
    if stale:                                  # history was rewritten (amend/rebase)
        con.executemany("DELETE FROM changes WHERE commit_hash = ?", [(h,) for h in stale])
        con.executemany("DELETE FROM commits WHERE hash = ?", [(h,) for h in stale])
    new = [c for c in commits if c[0] not in known]
    prev = {h: (hashes[i - 1] if i else EMPTY_TREE) for i, h in enumerate(hashes)}
    for n, (h, short, date, author, msg) in enumerate(new, 1):
        if not quiet:
            print("\r  reading commit %d/%d  %s %s" % (n, len(new), short, msg[:50].ljust(50)), end="", flush=True)
        when = date[:16].replace("T", " ")
        con.execute("INSERT INTO commits VALUES (?,?,?,?,?,?)", (h, short, when, author, msg, hashes.index(h)))
        parent = prev[h]
        for st, op, np_ in changed_files(root, parent, h, path):
            ext = Path(np_).suffix.lower()
            if ext != ".xml" and ext not in TEXT_EXT:
                continue
            ob = read_rev(root, parent, op) if st != "A" and parent != EMPTY_TREE else None
            nb = read_rev(root, h, np_) if st != "D" else None
            if ob is None and st != "A":
                st = "A"
            rep = diff_file(np_, st, ob, nb)
            for r in rows_for_report(rep):
                con.execute("INSERT INTO changes (commit_hash, date, path, block, block_type, language, network, "
                            "network_title, kind, change) VALUES (?,?,?,?,?,?,?,?,?,?)",
                            (h, when, r["path"], r["block"], r["block_type"], r["language"], r["network"],
                             r["network_title"], r["kind"], r["change"]))
        con.commit()
    if new and not quiet:
        print()
    con.commit()
    return con


def history_rows(con, root=None):
    q = ("SELECT c.date, m.short, m.message, m.author, c.block, c.block_type, c.language, c.network, "
         "c.network_title, c.kind, c.change, c.path FROM changes c JOIN commits m ON m.hash = c.commit_hash "
          "ORDER BY m.seq DESC, c.id")
    keys = ["date", "commit", "message", "author", "block", "block_type", "language", "network", "network_title",
            "kind", "change", "path"]
    rows = [dict(zip(keys, r)) for r in con.execute(q)]
    mapping = plc_prefixes(root) if root is not None else None
    for r in rows:
        r["plc"] = plc_of(r["path"] or "", mapping)
        r["safety"] = (r["language"] or "").upper().startswith("F_")
    return rows


def write_csv(rows, out):
    import csv
    with open(out, "w", encoding="utf-8-sig", newline="") as f:     # BOM + ';' so Excel opens it directly
        w = csv.writer(f, delimiter=";")
        w.writerow(["Date", "Time", "Commit", "Message", "Author", "PLC", "Block", "Type", "Language",
                    "Network", "Network title", "Kind", "Change", "File"])
        for r in rows:
            d, _, t = r["date"].partition(" ")
            w.writerow([d, t, r["commit"], r["message"], r["author"], r.get("plc", ""), r["block"], r["block_type"], r["language"],
                        r["network"] or "", r["network_title"], KIND_WORD.get(r["kind"], r["kind"]), r["change"],
                        r["path"]])


HIST_CSS = r"""
.tools{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin:0 0 14px}
.tools input,.tools select{font:14px "Segoe UI",system-ui,sans-serif;padding:8px 10px;border:1px solid var(--line);
border-radius:8px;background:#fff;color:var(--ink)}
.tools input[type=search]{flex:1;min-width:220px}
.tools .n{color:var(--muted);font-size:13px}
table.h{width:100%;border-collapse:separate;border-spacing:0;background:var(--card);border:1px solid var(--line);
border-radius:10px;overflow:hidden;font-size:13px}
table.h th{position:sticky;top:0;background:#f8fafc;text-align:left;font-weight:600;color:var(--muted);
font-size:11px;text-transform:uppercase;letter-spacing:.05em;padding:9px 10px;border-bottom:1px solid var(--line)}
table.h td{padding:7px 10px;border-bottom:1px solid #f1f5f9;vertical-align:top}
table.h tr.first td{border-top:2px solid var(--line)}
table.h td.when{white-space:nowrap;font-variant-numeric:tabular-nums}
table.h td.when small{display:block;color:var(--muted)}
table.h td.cm code{font:600 12px Consolas,monospace}
table.h td.cm div{color:var(--muted);font-size:12px;max-width:260px}
table.h td.cm .au{font-size:12px;color:var(--accent);font-weight:600}
table.h td.blk b{font-weight:600}
.sb{display:inline-block;background:#dc2626;color:#fff;font:700 9.5px/1 "Segoe UI",sans-serif;letter-spacing:.05em;padding:3px 6px;border-radius:4px;vertical-align:1px}table.h td.blk small{display:block;color:var(--muted)}
table.h td.nw{color:var(--muted);white-space:nowrap}
.k{display:inline-block;font:700 12px Consolas,monospace;width:16px;text-align:center;border-radius:4px;margin-right:6px}
.k.add{background:var(--addbg);color:var(--add)}.k.del{background:var(--delbg);color:var(--del)}.k.chg{background:var(--chgbg);color:var(--chg)}
.tablewrap{overflow-x:auto}
.charts{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;margin:0 0 16px}
.charts[hidden]{display:none}
.cbox{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px;min-width:0}
.cbox.wide{grid-column:1/-1}
.ctitle{font-weight:600;font-size:13px;margin-bottom:6px}
.legend2{display:flex;gap:14px;font-size:12px;color:var(--muted);margin-bottom:4px}
.legend2 i{display:inline-block;width:10px;height:10px;border-radius:3px;margin-right:5px;vertical-align:-1px}
svg.chart{max-width:100%;height:auto;display:block}
svg.chart .grid{stroke:#eef2f6;stroke-width:1}
svg.chart .ax{font:11px "Segoe UI",system-ui,sans-serif;fill:#64748b}
svg.chart rect:hover{opacity:.85}
.rk{display:grid;grid-template-columns:minmax(80px,40%) 1fr 34px;gap:8px;align-items:center;font-size:12.5px;padding:3px 0}
.rl{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.rb{background:#f1f5f9;border-radius:4px;height:10px;overflow:hidden}.rb i{display:block;height:100%;background:#0e7490;border-radius:4px}
.rv{text-align:right;font-variant-numeric:tabular-nums;color:var(--muted)}
.cempty{color:var(--muted);font-size:13px;padding:10px 0}.cmore{color:var(--muted);font-size:12px;margin-top:4px}
@media (max-width:900px){.charts{grid-template-columns:1fr}}
"""

HIST_JS = r"""
const rows = DATA;
const $ = s => document.querySelector(s);
const blocks = [...new Set(rows.map(r => r.block))].sort();
const plcs = [...new Set(rows.map(r => r.plc).filter(Boolean))].sort();
$('#plc').innerHTML = '<option value="">All PLCs</option>' + plcs.map(b => `<option>${esc(b)}</option>`).join('');
$('#plc').style.display = plcs.length ? '' : 'none';
const want = new URLSearchParams(location.search).get('plc');
if (want && plcs.includes(want)) $('#plc').value = want;
$('#blk').innerHTML = '<option value="">All blocks</option>' + blocks.map(b => `<option>${esc(b)}</option>`).join('');
function esc(s){return String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}
const SIGN = {add:'+', del:'−', chg:'~'};
function render(){
  const q = $('#q').value.toLowerCase().trim(), b = $('#blk').value, k = $('#kind').value, pl = $('#plc').value;
  const from = $('#from').value, to = $('#to').value;
  const words = q ? q.split(/\s+/) : [];
  const out = [], vis = []; let last = null, n = 0;
  for (const r of rows){
    if (b && r.block !== b) continue;
    if (pl && r.plc !== pl) continue;
    if (k === 'safety' ? !r.safety : (k && r.kind !== k)) continue;
    const d = r.date.slice(0,10);
    if (from && d < from) continue;
    if (to && d > to) continue;
    if (words.length){
      const hay = [r.date, r.commit, r.message, r.author, r.plc, r.block, r.network_title, r.change, r.path].join(' ').toLowerCase();
      if (!words.every(w => hay.includes(w))) continue;
    }
    n++; vis.push(r);
    const first = r.commit !== last; last = r.commit;
    const [dd, tt] = r.date.split(' ');
    out.push(`<tr class="${first ? 'first' : ''}">
      <td class="when">${first ? `${esc(dd)}<small>${esc(tt)}</small>` : ''}</td>
      <td class="cm">${first ? `<code>${esc(r.commit)}</code> <span class="au">${esc(r.author)}</span><div>${esc(r.message)}</div>` : ''}</td>
      <td class="blk"><b>${esc(r.block)}</b>${r.safety ? ' <span class="sb">SAFETY</span>' : ''}<small>${esc([r.plc, r.block_type, r.language].filter(Boolean).join(' · '))}</small></td>
      <td class="nw">${r.network ? 'Network ' + r.network + (r.network_title ? '<br><small>' + esc(r.network_title) + '</small>' : '') : ''}</td>
      <td><span class="k ${r.kind}">${SIGN[r.kind] || ''}</span>${esc(r.change)}</td></tr>`);
  }
  $('#rows').innerHTML = out.join('') || '<tr><td colspan="5" style="padding:30px;text-align:center;color:#64748b">No changes match.</td></tr>';
  $('#count').textContent = n + ' change' + (n === 1 ? '' : 's');
  drawCharts(vis);
}
// ---------- charts (follow the filters above) ----------
const KC = {add: '#16a34a', del: '#dc2626', chg: '#d97706'}, KN = {add: 'Added', del: 'Removed', chg: 'Changed'};
const BAR = '#0e7490';
function dayKey(d){ return d.slice(0, 10); }
function weekKey(d){ const t = new Date(d.slice(0, 10) + 'T00:00:00Z'); const w = (t.getUTCDay() + 6) % 7; t.setUTCDate(t.getUTCDate() - w);
  return t.toISOString().slice(0, 10); }
function drawTimeline(vis){
  if (!vis.length) return '<div class="cempty">No changes in this selection.</div>';
  const days = vis.map(r => dayKey(r.date)).sort();
  const span = (new Date(days[days.length - 1]) - new Date(days[0])) / 864e5;
  const weekly = span > 120, key = weekly ? weekKey : dayKey;
  const buckets = new Map();
  let cur = new Date((weekly ? weekKey(days[0]) : days[0]) + 'T00:00:00Z'), end = new Date(days[days.length - 1] + 'T00:00:00Z');
  while (cur <= end){ buckets.set(cur.toISOString().slice(0, 10), {add: 0, del: 0, chg: 0}); cur.setUTCDate(cur.getUTCDate() + (weekly ? 7 : 1)); }
  for (const r of vis){ const b = buckets.get(key(r.date)); if (b && b[r.kind] !== undefined) b[r.kind]++; }
  const box = $('#charts'), W = Math.max(420, (box ? box.clientWidth : 800) - 32), H = 200, L = 36, B = 24, T = 8;
  const ks = [...buckets.keys()], pw = W - L - 8, ph = H - B - T;
  const max = Math.max(1, ...[...buckets.values()].map(v => v.add + v.del + v.chg));
  const step = niceStep(max), top = Math.ceil(max / step) * step;
  const bw = pw / ks.length, gap = Math.max(2, Math.min(bw * 0.3, bw - 26));
  let g = '';
  for (let v = 0; v <= top; v += step){ const y = T + ph - v / top * ph;
    g += `<line x1="${L}" x2="${W - 8}" y1="${y}" y2="${y}" class="grid"/><text x="${L - 6}" y="${y + 4}" class="ax" text-anchor="end">${v}</text>`; }
  ks.forEach((k, i) => {
    const v = buckets.get(k); let y = T + ph; const x = L + i * bw + gap / 2, w = Math.max(1, bw - gap);
    const tip = `${weekly ? 'Week of ' : ''}${k}: ${v.add} added, ${v.del} removed, ${v.chg} changed`;
    for (const kind of ['chg', 'add', 'del']){ if (!v[kind]) continue;
      const h = v[kind] / top * ph; y -= h;
      g += `<rect x="${x}" y="${y}" width="${w}" height="${Math.max(h - (h > 3 ? 1 : 0), 1)}" rx="${Math.min(3, w / 3)}" fill="${KC[kind]}"><title>${esc(tip)}</title></rect>`; }
    g += `<rect x="${L + i * bw}" y="${T}" width="${bw}" height="${ph}" fill="transparent"><title>${esc(tip)}</title></rect>`;
  });
  const every = Math.ceil(ks.length / Math.max(2, Math.floor(pw / 70)));
  ks.forEach((k, i) => { if (i % every === 0) g += `<text x="${L + i * bw + bw / 2}" y="${H - 8}" class="ax" text-anchor="middle">${k.slice(5)}</text>`; });
  const leg = ['add', 'del', 'chg'].map(k => `<span><i style="background:${KC[k]}"></i>${KN[k]}</span>`).join('');
  return `<div class="ctitle">Changes per ${weekly ? 'week' : 'day'}</div><div class="legend2">${leg}</div>
    <svg viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" class="chart" role="img" aria-label="Changes per ${weekly ? 'week' : 'day'}">${g}</svg>`;
}
function niceStep(max){ const raw = max / 4, p = Math.pow(10, Math.floor(Math.log10(raw))), f = raw / p; return Math.max(1, (f <= 1 ? 1 : f <= 2 ? 2 : f <= 5 ? 5 : 10) * p); }
function drawRank(title, vis, keyFn, limit){
  const c = new Map(); for (const r of vis){ const k = keyFn(r); if (k) c.set(k, (c.get(k) || 0) + 1); }
  const items = [...c.entries()].sort((a, b) => b[1] - a[1]); const shown = items.slice(0, limit);
  if (!shown.length) return `<div class="ctitle">${title}</div><div class="cempty">—</div>`;
  const max = shown[0][1], rowsH = shown.map(([k, v]) => `<div class="rk" title="${esc(k)}: ${v} change${v === 1 ? '' : 's'}">
      <span class="rl">${esc(k)}</span><span class="rb"><i style="width:${Math.max(2, v / max * 100)}%"></i></span><span class="rv">${v}</span></div>`).join('');
  const more = items.length > limit ? `<div class="cmore">+ ${items.length - limit} more</div>` : '';
  return `<div class="ctitle">${title}</div>${rowsH}${more}`;
}
function drawCharts(vis){
  const el = $('#charts'); if (!el || el.hidden) return;
  el.innerHTML = `<div class="cbox wide">${drawTimeline(vis)}</div>
    <div class="cbox">${drawRank('By PLC', vis, r => r.plc || '(no PLC)', 8)}</div>
    <div class="cbox">${drawRank('By person', vis, r => r.author, 8)}</div>
    <div class="cbox">${drawRank('Most changed blocks', vis, r => r.block, 10)}</div>`;
}
function toggleCharts(){
  const el = $('#charts'); el.hidden = !el.hidden;
  $('#ctog').textContent = el.hidden ? 'Show charts' : 'Hide charts';
  try { localStorage.setItem('plcdiff.charts', el.hidden ? '0' : '1'); } catch (e) {}
  if (!el.hidden) render();
}
try { if (localStorage.getItem('plcdiff.charts') === '0') { $('#charts').hidden = true; $('#ctog').textContent = 'Show charts'; } } catch (e) {}
['#q','#plc','#blk','#kind','#from','#to'].forEach(s => $(s).addEventListener('input', render));
render();
"""


def build_history_html(repo, rows, n_commits):
    data = json.dumps(rows, ensure_ascii=False).replace("</", "<\\/")
    first = rows[-1]["date"][:10] if rows else ""
    last = rows[0]["date"][:10] if rows else ""
    h = ['<!doctype html><html lang="en"><head><meta charset="utf-8">'
         '<meta name="viewport" content="width=device-width,initial-scale=1">'
         '<title>PLC Diff · change history</title><style>%s%s</style></head><body>' % (CSS, HIST_CSS)]
    h.append('<header><div class="brand">PLC Diff · change history</div><h1>%s</h1>'
             '<div class="range"><span class="rev"><code>%d</code>changes</span>'
             '<span class="rev"><code>%d</code>commit%s</span>%s</div></header><main>' % (
                 esc(repo), len(rows), n_commits, "" if n_commits == 1 else "s",
                 '<span class="rev">%s → %s</span>' % (esc(first), esc(last)) if rows else ""))
    h.append('<div class="tools"><input type="search" id="q" placeholder="Search: tag, block, network, message…">'
             '<select id="plc"></select><select id="blk"></select><select id="kind"><option value="">All kinds</option>'
             '<option value="add">Added</option><option value="del">Removed</option><option value="chg">Changed</option>'
             '<option value="safety">Safety (F) blocks</option>'
             '</select><input type="date" id="from" title="From"><input type="date" id="to" title="To">'
             '<span class="n" id="count"></span><button class="btn" id="ctog" onclick="toggleCharts()" '
             'style="font:600 13px Segoe UI,sans-serif;border:1px solid #e2e8f0;background:#fff;border-radius:8px;padding:7px 12px;cursor:pointer">'
             'Hide charts</button></div><div class="charts" id="charts"></div>')
    h.append('<div class="tablewrap"><table class="h"><thead><tr><th>When</th><th>Commit</th><th>Block</th>'
             '<th>Network</th><th>Change</th></tr></thead><tbody id="rows"></tbody></table></div>')
    h.append('<p class="same-list">Times are when each change was committed. To see the drawings of a change, '
             'use <b>Compare versions</b> and pick that commit.</p>')
    h.append("</main><footer>PLC Diff %s · updated %s</footer><script>const DATA = %s;%s</script></body></html>" % (
        VERSION, datetime.datetime.now().strftime("%Y-%m-%d %H:%M"), data, HIST_JS))
    return "".join(h)


def is_our_hook(hook):
    try:
        t = hook.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return False
    return "plc-diff" in t or "aurora-diff" in t


def install_hook(root):
    hook = root / ".git" / "hooks" / "post-commit"
    py = Path(sys.executable).as_posix()
    me = Path(__file__).resolve().as_posix()
    body = ("#!/bin/sh\n# plc-diff: keep the change history up to date after every commit\n"
            '"%s" "%s" --history --no-open --quiet || true\n' % (py, me))
    if hook.exists() and not is_our_hook(hook):
        raise RuntimeError("A different post-commit hook already exists at %s - not overwriting it." % hook)
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text(body, encoding="utf-8", newline="\n")
    try:
        os.chmod(hook, 0o755)
    except Exception:
        pass
    print("Installed: the change history now updates automatically after every commit.\n  %s" % hook)


def run_history(a):
    root = repo_root()
    con = update_history(root, a.path, quiet=a.quiet)
    rows = history_rows(con, root)
    n_commits = con.execute("SELECT COUNT(*) FROM commits").fetchone()[0]
    db = root / ".plcdiff" / "changes.db"
    out = Path(a.out) if a.out else root / ".plcdiff" / "history.html"
    out.write_text(build_history_html(root.name, rows, n_commits), encoding="utf-8")
    if a.csv:
        write_csv(rows, a.csv)
    if a.quiet:
        return
    print("Change history: %d changes in %d commits" % (len(rows), n_commits))
    for r in rows[:12]:
        net = "  Network %s" % r["network"] if r["network"] else ""
        print("  %s  %s  %s%s  %s %s" % (r["date"], r["commit"], r["block"], net, SIGN.get(r["kind"], " "), r["change"]))
    if len(rows) > 12:
        print("  … (%d more on the page)" % (len(rows) - 12))
    print("\nDatabase: %s" % db)
    if a.csv:
        print("CSV:      %s" % Path(a.csv).resolve())
    print("Page:     %s" % out.resolve())
    if not a.no_open:
        webbrowser.open(out.resolve().as_uri())


# =============================================================================
#  The app (double-click the file): a small local web app in your browser
# =============================================================================

GITIGNORE = """# TIA project internals (the project itself should live outside this repo)
*.ap1*
*.ap2*
IM/
System/
Logs/
XRef/
TMP/
UserFiles/
AdditionalFiles/
*.S7TIA/

# Windows / Office junk
Thumbs.db
desktop.ini
~$*
*.tmp
*.bak

# PLC Diff change history (rebuilt from Git)
.plcdiff/
"""


def settings_path():
    base = os.environ.get("APPDATA") or str(Path.home() / ".config")
    return Path(base) / "plc-diff" / "settings.json"


def load_settings():
    st = None
    for p in (settings_path(), settings_path().parent.parent / "aurora-diff" / "settings.json"):   # old name
        try:
            st = json.loads(p.read_text(encoding="utf-8"))
            break
        except Exception:
            st = None
    st = st or {"recent": []}
    if "projects" not in st:
        st["projects"] = list(st.get("recent", []))
    return st


def save_settings(st):
    try:
        p = settings_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(st, indent=2), encoding="utf-8")
    except Exception:
        pass


def run(cwd, args, inp=None, timeout=120):
    """Run a command, return (ok, output)."""
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    try:
        r = subprocess.run(args, cwd=str(cwd) if cwd else None, input=inp.encode("utf-8") if inp is not None else None,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           stdin=None if inp is not None else subprocess.DEVNULL,
                           timeout=timeout, env=env, **NOWIN)
        return r.returncode == 0, r.stdout.decode("utf-8", "replace").strip()
    except FileNotFoundError:
        return False, "%s is not installed (or not on the Windows PATH)." % args[0]
    except subprocess.TimeoutExpired:
        return False, "Timed out after %d s." % timeout


def find_doubled(root, max_depth=4):
    """Folders like 'vci/Program blocks/Program blocks' that a misconfigured VCI workspace creates."""
    out = []

    def walk(d, depth):
        if depth > max_depth:
            return
        try:
            entries = [e for e in d.iterdir() if e.is_dir() and e.name != ".git"]
        except Exception:
            return
        for e in entries:
            if e.name.lower() == d.name.lower():
                out.append(e)
            else:
                walk(e, depth + 1)

    walk(root, 0)
    return out


def merge_up(folder):
    """Move everything inside folder into its parent (overwriting), then delete folder."""
    import shutil
    parent = folder.parent
    for item in list(folder.iterdir()):
        dest = parent / item.name
        if item.is_dir():
            shutil.copytree(str(item), str(dest), dirs_exist_ok=True)
            shutil.rmtree(str(item))
        else:
            if dest.exists():
                dest.unlink()
            shutil.move(str(item), str(dest))
    shutil.rmtree(str(folder), ignore_errors=True)


def find_tia_projects(root, max_depth=4):
    hits = []

    def walk(d, depth):
        if depth > max_depth or len(hits) > 5:
            return
        try:
            for e in d.iterdir():
                if e.name == ".git":
                    continue
                if e.is_file() and re.match(r"\.ap\d+$", e.suffix.lower()):
                    hits.append(e)
                elif e.is_dir():
                    walk(e, depth + 1)
        except Exception:
            pass

    walk(root, 0)
    return hits


def compute_reports(root, old, new, path=".", safety_only=False):
    os_, od = rev_desc(root, old)
    ns, nd = rev_desc(root, new)
    log = []
    if new != WORK:
        out = git(root, "log", "--format=%h%x00%s", "%s..%s" % (old, new), check=False).decode("utf-8", "replace")
        log = [tuple(l.split("\0", 1)) for l in out.splitlines() if "\0" in l]
    meta = {"old_short": os_, "old_desc": od, "new_short": ns, "new_desc": nd, "repo": root.name,
            "old_label": "%s %s" % (os_, od), "new_label": "%s %s" % (ns, nd), "log": log, "view": "auto"}
    reports = []
    moves = []
    files = changed_files(root, old, new, path, moves)
    meta["moved"] = len(moves)
    for st, op, np_ in sorted(files, key=lambda x: natkey(x[2])):
        ext = Path(np_).suffix.lower()
        if ext != ".xml" and ext not in TEXT_EXT:
            continue
        ob = read_rev(root, old, op) if st != "A" else None
        nb = read_rev(root, new, np_) if st != "D" else None
        if ob is not None and nb is not None and ob.replace(b"\r\n", b"\n") == nb.replace(b"\r\n", b"\n"):
            continue
        rep = diff_file(np_, st, ob, nb)
        if rep.bullets or rep.textdiff or rep.error or any(n.status != "same" for n in rep.nets):
            if safety_only and not rep.safety:
                continue
            reports.append(rep)
    meta["safety_only"] = safety_only
    return meta, reports


def suggest_message(reports, max_lines=25, plc=""):
    if not reports:
        return ""
    names = []
    for r in reports:
        n = r.doc.name if r.doc else Path(r.path).stem
        if n not in names:
            names.append(n)
    if len(names) == 1:
        title = "Update %s" % names[0]
    elif len(names) <= 3:
        title = "Update %s" % ", ".join(names)
    else:
        title = "Update %d blocks (%s, …)" % (len(names), ", ".join(names[:2]))
    if plc:
        title = "%s: %s" % (plc, title)
    lines = []
    for r in reports:
        for row in rows_for_report(r):
            where = row["block"] + (" N%d" % row["network"] if row["network"] else "")
            lines.append("- %s: %s" % (where, row["change"]))
    if len(lines) > max_lines:
        lines = lines[:max_lines] + ["- … and %d more" % (len(lines) - max_lines)]
    return title + "\n\n" + "\n".join(lines)


def repo_info(root):
    info = {"path": str(root), "name": root.name, "exists": root.is_dir()}
    if not root.is_dir():
        return info
    ok, top = run(root, ["git", "rev-parse", "--show-toplevel"])
    info["is_repo"] = ok and Path(top).resolve() == root.resolve()
    info["inside_repo"] = ok and not info["is_repo"]
    info["top"] = top if ok else ""
    if not info["is_repo"]:
        return info
    ok, br = run(root, ["git", "rev-parse", "--abbrev-ref", "HEAD"])
    info["branch"] = br if ok else ""
    info["has_commits"] = rev_ok(root, "HEAD")
    ok, url = run(root, ["git", "remote", "get-url", "origin"])
    info["remote"] = url if ok else ""
    ok, ab = run(root, ["git", "rev-list", "--left-right", "--count", "@{u}...HEAD"])
    if ok and ab.split():
        behind, ahead = ab.split()[:2]
        info["ahead"], info["behind"] = int(ahead), int(behind)
    else:
        info["ahead"] = info["behind"] = None
    try:
        toks = git(root, "status", "--porcelain", "-z", "-uall").decode("utf-8", "replace").split("\0")
    except Exception:
        toks = []
    files, i = [], 0
    while i < len(toks):
        t = toks[i]
        i += 1
        if len(t) < 4:
            continue
        files.append(t)
        if t[0] in "RC":            # renames carry the old path as an extra entry
            i += 1
    info["dirty"] = len(files)
    info["dirty_files"] = files[:300]
    plcs, legacy = list_plcs(root)
    info["plcs"], info["legacy"] = plcs, legacy
    m = plc_map(root)
    info["plc_paths"] = m
    pref = plc_prefixes(root)
    info["plc_prefixes"] = pref
    info["configured"] = load_project_cfg(root) is not None
    info["loose_in_vci"] = _auto_plcs(root)[1] and "vci" not in m.values()
    counts = {}
    for l in files:
        pth = l[3:]
        k = plc_of(pth, pref) or ("(other)" if not pth.startswith("vci/") else "(vci)")
        counts[k] = counts.get(k, 0) + 1
    info["plc_dirty"] = counts
    ok, sl = run(root, ["git", "shortlog", "-sne", "HEAD"]) if info["has_commits"] else (False, "")
    info["people"] = [l.strip() for l in sl.splitlines() if l.strip()][:20] if ok else []
    info["doubled"] = [str(p.relative_to(root)).replace("\\", "/") for p in find_doubled(root)]
    info["tia_inside"] = [str(p.relative_to(root)).replace("\\", "/") for p in find_tia_projects(root)]
    gi = root / ".gitignore"
    info["gitignore"] = gi.exists()
    info["hook"] = is_our_hook(root / ".git" / "hooks" / "post-commit")
    return info


def project_summary(root):
    root = Path(root)
    d = {"path": str(root), "name": root.name, "exists": root.is_dir()}
    if not d["exists"]:
        return d
    ok, top = run(root, ["git", "rev-parse", "--show-toplevel"])
    d["is_repo"] = ok and Path(top).resolve() == root.resolve()
    if not d["is_repo"]:
        return d
    d["plcs"], d["legacy"] = list_plcs(root)
    ok, out = run(root, ["git", "status", "--porcelain"])
    d["dirty"] = len([l for l in out.splitlines() if l.strip()]) if ok else 0
    ok, out = run(root, ["git", "log", "-1", "--format=%an%x00%ad%x00%s", "--date=format:%Y-%m-%d %H:%M"])
    if ok and out.count("\0") == 2:
        d["last_author"], d["last_date"], d["last_msg"] = out.split("\0")
    ok, url = run(root, ["git", "remote", "get-url", "origin"])
    d["remote"] = url if ok else ""
    ok, ab = run(root, ["git", "rev-list", "--left-right", "--count", "@{u}...HEAD"])
    if ok and len(ab.split()) >= 2:
        d["behind"], d["ahead"] = int(ab.split()[0]), int(ab.split()[1])
    return d


def scan_for_projects(base, max_depth=4, limit=200):
    found = []
    skip = {"node_modules", "$recycle.bin", "system volume information", "windows", "program files",
            "program files (x86)", "appdata", ".plcdiff", ".aurora"}

    def walk(d, depth):
        if depth > max_depth or len(found) >= limit:
            return
        try:
            if (d / ".git").exists():
                if (d / "vci").is_dir():
                    found.append(d)
                return                       # don't look inside other repos
            for e in d.iterdir():
                if e.is_dir() and not e.name.startswith(".") and e.name.lower() not in skip:
                    walk(e, depth + 1)
        except Exception:
            pass

    walk(Path(base), 0)
    return found


def create_project(parent, name, plcs, url=""):
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9 _.\-]{0,80}$", name):
        return False, "Use a simple project name (letters, numbers, spaces, _ - .).", None
    parent = Path(parent)
    if not parent.is_dir():
        return False, "The folder %s doesn't exist." % parent, None
    root = parent / name
    if root.exists() and any(root.iterdir()):
        return False, "%s already exists and isn't empty." % root, None
    bad = [p for p in plcs if not re.match(r"^[A-Za-z0-9][A-Za-z0-9 _.\-]{0,60}$", p) or p.lower() in TIA_FOLDERS]
    if bad:
        return False, "Use simple PLC names (letters, numbers, _ - .): %s" % ", ".join(bad), None
    ok, log = init_repo(root)
    if not ok:
        return False, log[-800:], None
    for plc in plcs:
        for sub in ("vci", "releases"):
            (root / sub / plc).mkdir(parents=True, exist_ok=True)
            (root / sub / plc / ".gitkeep").write_text("", encoding="utf-8")
    if plcs:
        run(root, ["git", "add", "-A"])
        run(root, ["git", "commit", "-m", "Add PLC folders: %s" % ", ".join(plcs)])
    if url:
        ok, out = run(root, ["git", "remote", "add", "origin", url])
        if ok:
            ok, out = push(root)
        if not ok:
            return True, "Project created, but GitHub didn't work: %s\nYou can connect it later in Setup." % out[-400:], root
    return True, "", root


TEXT_LANGS = {"SCL", "STL", "F_SCL", "F_STL", "DB", "F_DB"}


def health_check(root, folders, progress=None):
    """Read and draw every export; return (checked, problems) where problems = [(path, block, lang, [issues])]."""
    files = []
    for d in folders:
        base = root / d if d != "." else root
        if base.is_dir():
            files += [f for f in base.rglob("*.xml") if ".git" not in f.parts and ".plcdiff" not in f.parts]
    files = sorted(set(files), key=lambda f: natkey(str(f)))
    problems, kinds = [], defaultdict(int)
    for i, f in enumerate(files):
        rel = f.relative_to(root).as_posix()
        if progress:
            progress(i + 1, len(files))
        issues, name, lang = [], f.stem, ""
        try:
            doc = parse_doc(f.read_bytes(), rel)
            name, lang = doc.name, doc.lang
            kinds[doc.kind] += 1
            if doc.kind == "other":
                issues.append("not a block, data type or tag table the app understands%s" % (
                    " (%s)" % doc.btype if doc.btype else ""))
            for net in doc.nets:
                where = "GRAPH sequence" if net.graph is not None else "network %d" % net.idx
                try:
                    if net.graph is not None:
                        graph_svg(net.graph, {})
                        for k, tr in net.graph["trans"].items():
                            draw_net(tr["cond"], {}, {}, "auto")
                        continue
                    if net.is_text:
                        if (net.lang or doc.lang or "").upper() not in TEXT_LANGS and net.text.strip():
                            issues.append("%s (%s) could only be read as text" % (where, net.lang or doc.lang))
                        continue
                    bad = [p for p in net.parts.values() if p.operand == "???"]
                    if bad:
                        issues.append("%s: %d element%s without an operand (%s)" % (
                            where, len(bad), "" if len(bad) == 1 else "s", ", ".join(sorted({p.name for p in bad}))))
                    draw_net(net, {}, {}, "auto")
                except Exception as ex:
                    issues.append("%s: can't be drawn (%s: %s)" % (where, type(ex).__name__, ex))
        except Exception as ex:
            issues.append("can't be read (%s: %s)" % (type(ex).__name__, ex))
        if issues:
            problems.append((rel, name, lang, issues))
    return len(files), problems, dict(kinds)


def build_health_html(repo, checked, problems, kinds, folders):
    ok = checked - len(problems)
    h = ['<!doctype html><html lang="en"><head><meta charset="utf-8"><title>PLC Diff · block check</title>'
         '<style>%s</style></head><body>' % CSS]
    h.append('<header><div class="brand">PLC Diff · check all blocks</div><h1>%s</h1><div class="range">'
             '<span class="rev"><code>%d</code>files checked</span><span class="rev"><code>%d</code>fully readable</span>'
             '<span class="rev"><code>%d</code>with problems</span></div></header><main>' % (esc(repo), checked, ok, len(problems)))
    h.append('<p class="same-list">Folders checked: %s · %s</p>' % (
        esc(", ".join(folders)), esc(", ".join("%d %s" % (v, k) for k, v in sorted(kinds.items())))))
    if not problems:
        h.append('<div class="nothing"><b>All %d files can be read and drawn.</b></div>' % checked)
    else:
        txt = "PLC Diff %s block check - %s\n%d files checked, %d with problems\n\n" % (VERSION, repo, checked, len(problems))
        txt += "\n".join("%s\n  - %s" % (p, "\n  - ".join(iss)) for p, _, _, iss in problems)
        h.append('<div class="toc"><h2>Send this list to the PLC Diff maintainer</h2>'
                 '<p style="font-size:13px;color:#64748b;margin:0 0 8px">It names the files and what the app could not read. '
                 'Attach one or two of those XML files too.</p>'
                 '<textarea id="lst" readonly style="width:100%%;height:140px;font:12px Consolas,monospace">%s</textarea>'
                 '<button onclick="navigator.clipboard.writeText(document.getElementById(\'lst\').value);this.textContent=\'Copied\'"'
                 ' style="margin-top:8px;font:600 13px Segoe UI,sans-serif;padding:7px 12px;border-radius:8px;border:1px solid #cbd5e1;'
                 'background:#fff;cursor:pointer">Copy list</button></div>' % esc(txt))
        h.append('<article class="file"><div class="fh"><h2>Files with problems</h2></div><div class="fbody"><ul class="b">')
        for p, name, lang, iss in problems:
            h.append('<li class="chg"><span><b>%s</b> <small style="color:#64748b">%s · %s</small><br>%s</span></li>' % (
                esc(name), esc(lang or "?"), esc(p), "<br>".join(esc(x) for x in iss)))
        h.append("</ul></div></article>")
    h.append("</main><footer>PLC Diff %s · checked %s</footer></body></html>" % (
        VERSION, datetime.datetime.now().strftime("%Y-%m-%d %H:%M")))
    return "".join(h)


def tool_checks():
    ok, gv = run(None, ["git", "--version"])
    lok, lv = run(None, ["git", "lfs", "version"]) if ok else (False, "")
    _, name = run(None, ["git", "config", "--global", "user.name"]) if ok else (False, "")
    _, mail = run(None, ["git", "config", "--global", "user.email"]) if ok else (False, "")
    return {"git": ok, "git_version": gv if ok else "", "lfs": lok, "lfs_version": lv.split(" (")[0] if lok else "",
            "user_name": name if ok and name and "not" not in name.lower() else "",
            "user_email": mail if ok and mail and "@" in mail else ""}


def pick_folder(initial=""):
    code = ("import tkinter as t, tkinter.filedialog as f\n"
            "r=t.Tk(); r.withdraw(); r.attributes('-topmost', True)\n"
            "p=f.askdirectory(title='Choose your project folder (the Git repo)', initialdir=%r, mustexist=True)\n"
            "print(p or '')" % (initial or str(Path.home())))
    exe = sys.executable
    ok, out = run(None, [exe, "-c", code], timeout=600)
    if not ok:
        return None, "The folder picker couldn't open here. Paste the folder path instead."
    return (out.strip().splitlines() or [""])[-1], ""


def pick_file(initial="", kinds=(("TIA archive", "*.zap*"), ("All files", "*.*"))):
    code = ("import tkinter as t, tkinter.filedialog as f\n"
            "r=t.Tk(); r.withdraw(); r.attributes('-topmost', True)\n"
            "p=f.askopenfilename(title='Choose the project archive', initialdir=%r, filetypes=%r)\n"
            "print(p or '')" % (initial or str(Path.home()), list(kinds)))
    ok, out = run(None, [sys.executable, "-c", code], timeout=600)
    if not ok:
        return None, "The file picker couldn't open here. Paste the file path instead."
    return (out.strip().splitlines() or [""])[-1], ""


def init_repo(root):
    steps = []

    def step(args):
        ok, out = run(root, args)
        steps.append("$ " + " ".join(args) + ("\n" + out if out else ""))
        return ok

    root.mkdir(parents=True, exist_ok=True)
    if not step(["git", "init"]):
        return False, "\n".join(steps)
    step(["git", "symbolic-ref", "HEAD", "refs/heads/main"])
    lfs = step(["git", "lfs", "install", "--local"])
    for d in ("vci", "releases", "docs"):
        (root / d).mkdir(exist_ok=True)
    for d in ("releases", "docs"):
        k = root / d / ".gitkeep"
        if not k.exists():
            k.write_text("", encoding="utf-8")
    gi = root / ".gitignore"
    if not gi.exists():
        gi.write_text(GITIGNORE, encoding="utf-8")
    ga = root / ".gitattributes"
    if not ga.exists():
        ga.write_text("*.zap* filter=lfs diff=lfs merge=lfs -text\n", encoding="utf-8")
    rd = root / "README.md"
    if not rd.exists():
        rd.write_text("# %s\n\nTIA Portal exports (VCI) for %s.\n" % (root.name, root.name), encoding="utf-8")
    step(["git", "add", "-A"])
    ok = step(["git", "commit", "-m", "Repo setup: folders, gitignore, LFS for archives"])
    if not lfs:
        steps.append("(Git LFS isn't installed - archives will be stored normally. That's fine for now.)")
    return ok, "\n".join(steps)


def _push_once(root):
    ok, up = run(root, ["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"])
    if ok:
        return run(root, ["git", "push", "--follow-tags"], timeout=300)
    return run(root, ["git", "push", "-u", "origin", "HEAD", "--follow-tags"], timeout=300)


def pull(root):
    """Get everyone else's commits and put ours on top (rebase). Uncommitted exports are kept aside and put back."""
    ok, up = run(root, ["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"])
    if not ok:
        ok, out = run(root, ["git", "fetch", "origin"], timeout=300)
        if not ok:
            return False, out
        ok, br = run(root, ["git", "rev-parse", "--abbrev-ref", "HEAD"])
        if not rev_ok(root, "origin/%s" % br):
            return True, "Nothing on GitHub yet."
        run(root, ["git", "branch", "--set-upstream-to=origin/%s" % br])
    ok, out = run(root, ["git", "pull", "--rebase", "--autostash"], timeout=300)
    if ok:
        return True, out
    if "CONFLICT" in out or "could not apply" in out or "Resolve all conflicts" in out:
        files = sorted(set(re.findall(r"CONFLICT \([^)]*\): (?:Merge conflict in )?(\S.*)", out)))
        run(root, ["git", "rebase", "--abort"])
        run(root, ["git", "stash", "pop"])
        return False, ("Someone else changed the same file%s as you:\n  %s\nNothing was lost - your commits and exports "
                       "are as they were. Open the Changes tab and choose whose version to keep." % ("" if len(files) == 1 else "s", "\n  ".join(files) or "(see Git)"))
    return False, out


def push(root):
    """Push; if GitHub has newer commits from someone else, get them first and push again."""
    ok, out = _push_once(root)
    if ok:
        return ok, out
    if any(x in out for x in ("rejected", "fetch first", "non-fast-forward", "Updates were rejected")):
        ok, pout = pull(root)
        if not ok:
            return False, pout
        return _push_once(root)
    return False, out


def clone(url, parent):
    name = re.sub(r"\.git$", "", url.rstrip("/").split("/")[-1]) or "repo"
    dest = Path(parent) / name
    if dest.exists() and any(dest.iterdir()):
        return False, "%s already exists and isn't empty." % dest, dest
    ok, out = run(None, ["git", "clone", url, str(dest)], timeout=900)
    if ok:
        run(dest, ["git", "lfs", "install", "--local"])
    return ok, out, dest


APP_CSS = r"""
:root{--bg:#f4f5f7;--card:#fff;--ink:#0f172a;--muted:#64748b;--line:#e2e8f0;--accent:#0e7490;--accent2:#0f766e;
--add:#15803d;--addbg:#dcfce7;--del:#b91c1c;--delbg:#fee2e2;--chg:#b45309;--chgbg:#fef3c7}
*{box-sizing:border-box}html,body{height:100%}
body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.5 "Segoe UI",system-ui,-apple-system,sans-serif;display:flex;flex-direction:column}
.top{background:#0f172a;color:#cbd5e1;display:flex;align-items:center;gap:18px;padding:0 20px;height:58px;flex:none}
.logo{font:700 13px "Segoe UI",sans-serif;letter-spacing:.14em;text-transform:uppercase;color:#5eead4;white-space:nowrap}
.repo{display:flex;align-items:center;gap:10px;min-width:0;flex:1}
.repo .nm{color:#fff;font-weight:600;font-size:15px;white-space:nowrap}
.repo .pth{color:#94a3b8;font:12px Consolas,monospace;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.btn{font:600 13px "Segoe UI",system-ui,sans-serif;border:1px solid var(--line);background:#fff;color:var(--ink);
border-radius:8px;padding:7px 13px;cursor:pointer;white-space:nowrap}
.btn:hover{border-color:#94a3b8}.btn:disabled{opacity:.5;cursor:default}
.btn.pri{background:var(--accent);border-color:var(--accent);color:#fff}.btn.pri:hover{background:#155e75}
.btn.dark{background:#1e293b;border-color:#334155;color:#e2e8f0}.btn.dark:hover{border-color:#64748b}
.btn.warn{background:#fff7ed;border-color:#fdba74;color:#9a3412}
.btn.hot{background:#f59e0b;border-color:#f59e0b;color:#111}
.newver{background:#fef3c7;color:#78350f;padding:8px 20px;display:flex;gap:12px;align-items:center;font-weight:600;font-size:13px;flex:none}
.plcsel{font:600 13px "Segoe UI",system-ui,sans-serif;background:#1e293b;color:#e2e8f0;border:1px solid #334155;border-radius:8px;padding:7px 10px;max-width:240px}
.plcs{display:grid;gap:8px;margin:6px 0 12px}
.plcrow{display:flex;gap:10px;align-items:center;background:#f8fafc;border:1px solid var(--line);border-radius:10px;padding:8px 12px}
.plcrow b{min-width:140px}.plcrow code{flex:1;font-size:12px;color:var(--muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.plcrow.on{border-color:var(--accent);background:#ecfeff}
.people{font:12.5px Consolas,monospace;color:var(--ink);background:#f8fafc;border-radius:8px;padding:8px 10px;white-space:pre}
ol.steps{margin:6px 0 0;padding-left:20px;color:var(--ink);font-size:13px}ol.steps li{margin:4px 0}
.or{color:var(--muted);margin:26px 0 10px;font-size:12px;text-transform:uppercase;letter-spacing:.08em}
.tabs{display:flex;gap:2px;background:#0f172a;padding:0 14px;flex:none}
.tab{color:#94a3b8;padding:9px 16px;border-radius:8px 8px 0 0;cursor:pointer;font-weight:600;font-size:13px;user-select:none;display:flex;gap:7px;align-items:center}
.tab:hover{color:#e2e8f0}.tab.on{background:var(--bg);color:var(--ink)}
.pill{background:#f59e0b;color:#111;border-radius:999px;font-size:11px;padding:0 7px;line-height:18px}
.page{flex:1;min-height:0;display:none;flex-direction:column}.page.on{display:flex}
.bar{display:flex;flex-wrap:wrap;gap:10px;align-items:center;padding:12px 20px;border-bottom:1px solid var(--line);background:var(--card);flex:none}
.bar .grow{flex:1}
.bar select,.bar input[type=text]{font:13px "Segoe UI",system-ui,sans-serif;padding:7px 9px;border:1px solid var(--line);border-radius:8px;background:#fff;max-width:340px}
.frame{flex:1;min-height:0;border:0;width:100%;background:var(--bg)}
.loading{position:fixed;left:50%;top:50%;transform:translate(-50%,-50%);background:#0f172a;color:#e2e8f0;border-radius:12px;
padding:16px 22px;font-size:14px;z-index:8;box-shadow:0 10px 40px rgba(15,23,42,.3);display:none;max-width:420px;text-align:center}
.loading.on{display:block}.loading small{display:block;color:#94a3b8;margin-top:4px}
.split{flex:1;min-height:0;display:grid;grid-template-columns:1fr 360px;grid-template-rows:minmax(0,1fr)}
.split>.frame{height:100%}
.side{border-left:1px solid var(--line);background:var(--card);padding:16px;overflow:auto;display:flex;flex-direction:column;gap:12px}
.side h3{margin:0;font-size:13px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted)}
textarea{width:100%;min-height:200px;font:12.5px/1.5 Consolas,monospace;border:1px solid var(--line);border-radius:8px;padding:10px;resize:vertical}
label.ck{display:flex;gap:8px;align-items:center;font-size:13px}
.msg{font-size:13px;border-radius:8px;padding:9px 11px;white-space:pre-wrap;word-break:break-word}
.msg.ok{background:var(--addbg);color:#14532d}.msg.err{background:var(--delbg);color:#7f1d1d}.msg.info{background:#e0f2fe;color:#0c4a6e}
.msg.info,.msg.ok{white-space:normal}
.warnbox{background:#fff7ed;border:1px solid #fed7aa;border-radius:10px;padding:10px 12px;font-size:13px;color:#7c2d12}
.warnbox b{display:block;margin-bottom:4px}
.files{font:12px Consolas,monospace;color:var(--muted);max-height:160px;overflow:auto;background:#f8fafc;border-radius:8px;padding:8px}
.wrap{max-width:900px;margin:0 auto;padding:22px 20px 60px;overflow:auto;width:100%}
.scroll{flex:1;overflow:auto}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px 18px;margin-bottom:16px}
.card h2{margin:0 0 10px;font-size:16px}
.chk{display:flex;gap:12px;align-items:flex-start;padding:10px 0;border-top:1px solid #f1f5f9}
.chk:first-of-type{border-top:0}
.dot{width:22px;height:22px;border-radius:50%;flex:none;display:grid;place-items:center;font:700 12px sans-serif;color:#fff;margin-top:1px}
.dot.g{background:#16a34a}.dot.r{background:#dc2626}.dot.y{background:#f59e0b}
.chk .t{flex:1}.chk .t>b{display:block}.chk .t span{color:var(--muted);font-size:13px}
.chk .a{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.chk input[type=text]{font:13px "Segoe UI",sans-serif;padding:6px 8px;border:1px solid var(--line);border-radius:7px;width:220px}
.logo{cursor:pointer}
#p-welcome{overflow:auto}
.home{max-width:1180px;margin:0 auto;padding:24px 22px 60px;width:100%}
.homehead{display:flex;flex-wrap:wrap;gap:12px;align-items:center;justify-content:space-between;margin-bottom:16px}
.homehead h1{margin:0;font-size:22px}.actions{display:flex;flex-wrap:wrap;gap:8px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:14px}
.pcard{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px;cursor:pointer;display:flex;flex-direction:column;gap:6px;transition:border-color .15s,box-shadow .15s}
.pcard:hover{border-color:var(--accent);box-shadow:0 4px 16px rgba(15,23,42,.07)}
.pcard.cur{border-color:#5eead4}
.pcard .ph{display:flex;align-items:center;gap:8px}.pcard .ph b{font-size:16px;flex:1}
.pcard small{color:var(--muted);font:12px Consolas,monospace;word-break:break-all}
.pcard .x{border:0;background:none;color:#94a3b8;font-size:18px;cursor:pointer;line-height:1;padding:2px 6px;border-radius:6px}
.pcard .x:hover{background:#f1f5f9;color:var(--ink)}
.chips{display:flex;flex-wrap:wrap;gap:5px}.chip{background:#ecfeff;color:#155e75;border-radius:999px;font-size:11.5px;font-weight:600;padding:1px 9px}
.pcard .st{font-size:13px;font-weight:600;border-radius:7px;padding:4px 9px;align-self:flex-start}
.st.ok{background:var(--addbg);color:#14532d}.st.warn{background:var(--chgbg);color:#78350f}.st.bad{background:var(--delbg);color:#7f1d1d}.st.muted{background:#f1f5f9;color:var(--muted)}
.pcard .last{font-size:12px;color:var(--muted);border-top:1px solid #f1f5f9;padding-top:6px}
.empty-home{grid-column:1/-1;background:var(--card);border:1px dashed var(--line);border-radius:12px;padding:30px;text-align:center;color:var(--muted)}
.form{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.form label{display:flex;flex-direction:column;gap:4px;font-size:13px;font-weight:600}
.form label small{font-weight:400;color:var(--muted)}
.form input{font:13px "Segoe UI",sans-serif;padding:8px 10px;border:1px solid var(--line);border-radius:8px}
.form .row{display:flex;gap:6px}.form .row input{flex:1}
@media (max-width:700px){.form{grid-template-columns:1fr}}
.relpanel{background:var(--card);border-bottom:1px solid var(--line);padding:14px 20px;flex:none}
.relpanel[hidden]{display:none}
.safebox{background:#fef2f2;border:1px solid #fecaca;border-radius:10px;padding:10px 12px;font-size:13px;color:#7f1d1d}
.safebox b{display:block;margin-bottom:4px}.safebox label{display:flex;gap:8px;align-items:flex-start;margin-top:8px;font-weight:600}
.welcome{max-width:620px;margin:60px auto;text-align:center;padding:0 20px}
.welcome h1{font-size:26px;margin:0 0 8px}.welcome p{color:var(--muted)}
.recent{margin-top:26px;text-align:left}
.recent a{display:block;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 14px;margin:8px 0;color:var(--ink);text-decoration:none;cursor:pointer}
.recent a:hover{border-color:var(--accent)}.recent a small{display:block;color:var(--muted);font:12px Consolas,monospace}
.pathrow{display:flex;gap:8px;margin-top:14px}.pathrow input{flex:1;font:13px Consolas,monospace;padding:9px 10px;border:1px solid var(--line);border-radius:8px}
.pathrow select{font:13px Segoe UI,sans-serif;padding:9px 10px;border:1px solid var(--line);border-radius:8px;background:#fff}
.hint{padding:8px 2px;font-size:13px;color:var(--muted)}
.toast{position:fixed;right:18px;bottom:18px;max-width:420px;z-index:9;box-shadow:0 8px 30px rgba(15,23,42,.18)}
.spin{display:inline-block;width:14px;height:14px;border:2px solid #cbd5e1;border-top-color:var(--accent);border-radius:50%;animation:s 1s linear infinite;vertical-align:-2px}
@keyframes s{to{transform:rotate(360deg)}}
@media (max-width:900px){.split{grid-template-columns:1fr;grid-template-rows:minmax(0,1fr) auto}.side{border-left:0;border-top:1px solid var(--line)}}
"""

APP_JS = r"""
const T = TOKEN;
const $ = s => document.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
let S = {};                       // last state from server
function loadFrame(id, url, msg){
  const f = document.getElementById(id), l = document.getElementById('loading');
  document.getElementById('loadmsg').textContent = msg || 'Comparing…';
  l.classList.add('on');
  f.onload = () => l.classList.remove('on');
  f.src = url;
}
let TAB = '';
const plcOf = f => {
  const p = f.slice(3), m = (S.repo && (S.repo.plc_prefixes || S.repo.plc_paths)) || {};
  let best = '', bl = -1;
  for (const [n, ds] of Object.entries(m)) for (const d of [].concat(ds)) {
    const pre = d === '.' ? '' : d.replace(/\/$/, '') + '/'; if (p.startsWith(pre) && pre.length > bl) { best = n; bl = pre.length; } }
  if (best) return best;
  const r = p.match(/^releases\/([^/]+)\//); return r && m[r[1]] !== undefined ? r[1] : '';
};
const plcQ = () => S.plc ? '&plc=' + encodeURIComponent(S.plc) : '';
const plcDirty = () => !S.repo ? 0 : (S.plc ? ((S.repo.plc_dirty || {})[S.plc] || 0) : S.repo.dirty);
async function api(path, body){
  const r = await fetch(path, {method: body ? 'POST' : 'GET', headers: {'X-Token': T, 'Content-Type': 'application/json'},
                               body: body ? JSON.stringify(body) : undefined});
  const j = await r.json().catch(() => ({error: 'Bad response from the app'}));
  if (!r.ok && !j.error) j.error = 'Error ' + r.status;
  return j;
}
function toast(text, kind='ok', ms=6000){
  const d = document.createElement('div'); d.className = 'msg toast ' + kind; d.textContent = text;
  document.body.appendChild(d); setTimeout(() => d.remove(), ms);
}
function busy(btn, on, label){
  if (!btn) return;
  if (on){ btn.dataset.l = btn.innerHTML; btn.innerHTML = '<span class="spin"></span> ' + (label || 'Working…'); btn.disabled = true; }
  else { btn.innerHTML = btn.dataset.l || btn.innerHTML; btn.disabled = false; }
}
function show(tab){
  TAB = tab; HOME = false;
  $('#tabs').style.display = 'flex';
  document.querySelectorAll('.tab').forEach(t => t.classList.toggle('on', t.dataset.t === tab));
  document.querySelectorAll('.page').forEach(p => p.classList.toggle('on', p.id === 'p-' + tab));
  if (tab === 'changes') loadChanges();
  if (tab === 'compare') loadRevs();
  if (tab === 'history') loadHistory();
  if (tab === 'setup') loadSetup();
}
async function refresh(){
  S = await api('/api/state');
  $('#newver').style.display = S.stale ? 'flex' : 'none';
  if (HOME) return S;
  const r = S.repo;
  $('#rname').textContent = r ? r.name : 'No folder chosen';
  $('#rpath').textContent = r ? r.path : '';
  $('#tabs').style.display = r && r.is_repo ? 'flex' : (r ? 'flex' : 'none');
  const n = r && r.is_repo ? plcDirty() : 0;
  $('#pill').style.display = n ? 'inline' : 'none'; $('#pill').textContent = n;
  const plcs = r && r.is_repo ? (r.plcs || []) : [];
  const sel = $('#plcsel');
  sel.style.display = plcs.length ? 'inline-block' : 'none';
  const pd = (r && r.plc_dirty) || {};
  sel.innerHTML = '<option value="">All PLCs' + (r && r.dirty ? ' (' + r.dirty + ')' : '') + '</option>' +
     plcs.map(p => `<option value="${esc(p)}">${esc(p)}${pd[p] ? ' (' + pd[p] + ')' : ''}</option>`).join('');
  sel.value = S.plc || '';
  const behind = r && r.is_repo ? (r.behind || 0) : 0;
  $('#pullbtn').style.display = r && r.is_repo && r.remote ? 'inline-block' : 'none';
  $('#pullbtn').textContent = behind ? 'Get latest (' + behind + ' new)' : 'Get latest';
  $('#pullbtn').classList.toggle('hot', behind > 0);
  const sp = r && r.is_repo && (r.ahead || 0) > 0;
  $('#syncbtn').style.display = r && r.is_repo && r.remote && (r.ahead == null || r.ahead > 0) ? 'inline-block' : 'none';
  $('#syncbtn').textContent = sp ? 'Push ' + r.ahead + ' commit' + (r.ahead > 1 ? 's' : '') : 'Push';
  return S;
}
// ---------- projects home
let HOME = false;
async function renderHome(){
  const el = $('#p-welcome');
  if (!el.querySelector('.home')) el.innerHTML = `<div class="home"><div class="homehead"><h1>Projects</h1><div class="actions">
      <button class="btn pri" onclick="panel('new')">+ New project</button>
      <button class="btn" onclick="addExisting(this)">Add existing folder</button>
      <button class="btn" onclick="scanFolder(this)">Find projects in a folder…</button>
      <button class="btn" onclick="panel('clone')">Get from GitHub</button>
      <button class="btn" onclick="checkAll(this)" title="See if colleagues pushed anything">Check GitHub</button></div></div>
      <div id="panel"></div><div class="grid" id="cards"><div class="msg info">Loading…</div></div></div>`;
  const j = await api('/api/projects');
  if (j.error) return toast(j.error, 'err');
  const cards = j.projects.map(p => {
    let st = '', cls = 'ok';
    if (!p.exists) { st = 'Folder not found'; cls = 'bad'; }
    else if (!p.is_repo) { st = 'Not set up yet — open it to set up'; cls = 'warn'; }
    else {
      const bits = [];
      if (p.dirty) bits.push(p.dirty + ' uncommitted');
      if (p.behind) bits.push(p.behind + ' new on GitHub');
      if (p.ahead) bits.push(p.ahead + ' to push');
      if (!p.remote) bits.push('not on GitHub');
      st = bits.length ? bits.join(' · ') : 'Up to date';
      cls = (p.dirty || p.behind || p.ahead) ? 'warn' : (p.remote ? 'ok' : 'muted');
    }
    const plcs = (p.plcs || []).map(x => `<span class="chip">${esc(x)}</span>`).join('');
    const last = p.last_date ? `<div class="last">${esc(p.last_author)} · ${esc(p.last_date)}<br>${esc(p.last_msg)}</div>` : '';
    const cur = p.path === j.current ? ' cur' : '';
    return `<div class="pcard${cur}" onclick='openRepo(${esc(JSON.stringify(p.path))})'>
      <div class="ph"><b>${esc(p.name)}</b><button class="x" title="Remove from the list (doesn't delete anything)"
        onclick='event.stopPropagation(); removeProject(${esc(JSON.stringify(p.path))})'>×</button></div>
      <small>${esc(p.path)}</small>${plcs ? '<div class="chips">' + plcs + '</div>' : ''}
      <div class="st ${cls}">${esc(st)}</div>${last}</div>`;
  }).join('');
  $('#cards').innerHTML = cards || `<div class="empty-home"><b>No projects yet.</b><br>Create a new one, add a folder you already have,
     or let the app find them: <b>Find projects in a folder…</b> and choose e.g. your D: drive.</div>`;
}
function panel(kind){
  const p = $('#panel');
  if (p.dataset.k === kind) { p.innerHTML = ''; p.dataset.k = ''; return; }
  p.dataset.k = kind;
  if (kind === 'new') p.innerHTML = `<div class="card"><h2>New project</h2>
    <div class="form">
      <label>Project name<input id="np-name" placeholder="e.g. 007_BOREAS"></label>
      <label>Create it in<span class="row"><input id="np-parent" placeholder="e.g. D:\\Projects"><button class="btn" onclick="pickInto('#np-parent', this)">Choose…</button></span></label>
      <label>PLCs <small>(comma-separated, one folder each)</small><input id="np-plcs" value="PLC1"></label>
      <label>GitHub address <small>(optional — create an empty private repo on GitHub first)</small><input id="np-url" placeholder="https://github.com/you/007_BOREAS.git"></label>
    </div>
    <div class="msg info" style="font-size:12px;margin:10px 0">The app creates the folders (vci, releases, docs, one per PLC), a .gitignore for TIA, LFS for archives,
      makes the first commit and, if you gave an address, puts it on GitHub. Then it shows the VCI folder to use in TIA for each PLC.
      Keep the TIA project itself <b>outside</b> this folder.</div>
    <button class="btn pri" onclick="createProject(this)">Create project</button> <button class="btn" onclick="panel('new')">Cancel</button></div>`;
  if (kind === 'clone') p.innerHTML = `<div class="card"><h2>Get a project from GitHub</h2>
    <div class="form">
      <label>GitHub address<input id="curl" placeholder="https://github.com/team/Project.git"></label>
      <label>Put it in<span class="row"><input id="cparent" placeholder="e.g. D:\\Projects"><button class="btn" onclick="pickInto('#cparent', this)">Choose…</button></span></label>
    </div><br><button class="btn pri" onclick="doClone(this)">Download project</button> <button class="btn" onclick="panel('clone')">Cancel</button></div>`;
}
async function pickInto(sel, btn){
  busy(btn, true, 'Choose…'); const j = await api('/api/pick', {}); busy(btn, false);
  if (j.error) return toast(j.error, 'err'); if (j.path) $(sel).value = j.path;
}
async function createProject(btn){
  busy(btn, true, 'Creating…');
  const j = await api('/api/newproject', {name: $('#np-name').value, parent: $('#np-parent').value, plcs: $('#np-plcs').value, url: $('#np-url').value});
  busy(btn, false);
  if (j.error) return toast(j.error, 'err', 15000);
  if (j.warning) toast(j.warning, 'err', 15000); else toast('Project created.');
  HOME = false; await refresh(); show('setup');
}
async function addExisting(btn){
  busy(btn, true, 'Choose in the window that opened…'); const j = await api('/api/pick', {}); busy(btn, false);
  if (j.error) return toast(j.error, 'err');
  if (!j.path) return;
  const r = await api('/api/projects/add', {path: j.path});
  r.error ? toast(r.error, 'err') : renderHome();
}
async function scanFolder(btn){
  busy(btn, true, 'Choose where to look…'); const j = await api('/api/pick', {}); busy(btn, false);
  if (j.error || !j.path) return j.error && toast(j.error, 'err');
  busy(btn, true, 'Looking for projects…'); const r = await api('/api/scan', {path: j.path}); busy(btn, false);
  if (r.error) return toast(r.error, 'err');
  toast(r.found ? `Found ${r.found} project${r.found > 1 ? 's' : ''} (${r.added} new).` : 'No projects with a vci folder found there.', r.found ? 'ok' : 'info');
  renderHome();
}
async function removeProject(path){
  if (!confirm('Remove this project from the list? Nothing is deleted from your disk.')) return;
  await api('/api/projects/remove', {path}); renderHome();
}
async function checkAll(btn){
  busy(btn, true, 'Checking GitHub…'); const j = await api('/api/fetchall', {}); busy(btn, false);
  toast('Checked ' + (j.checked || 0) + ' project' + (j.checked === 1 ? '' : 's') + '.'); renderHome();
}
function goHome(){
  HOME = true; TAB = '';
  document.querySelectorAll('.page').forEach(p => p.classList.remove('on'));
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('on'));
  $('#tabs').style.display = 'none';
  ['#plcsel', '#pullbtn', '#syncbtn'].forEach(x => $(x).style.display = 'none');
  $('#rname').textContent = 'Projects'; $('#rpath').textContent = '';
  $('#p-welcome').classList.add('on'); renderHome();
}
async function pick(btn){
  busy(btn, true, 'Choose in the window that opened…');
  const j = await api('/api/pick', {});
  busy(btn, false);
  if (j.error) return toast(j.error, 'err');
  if (j.path) openRepo(j.path);
}
async function pickParent(btn){
  busy(btn, true, 'Choose…'); const j = await api('/api/pick', {}); busy(btn, false);
  if (j.error) return toast(j.error, 'err'); if (j.path) $('#cparent').value = j.path;
}
async function doClone(btn){
  busy(btn, true, 'Downloading…');
  const j = await api('/api/clone', {url: $('#curl').value.trim(), parent: $('#cparent').value.trim()});
  busy(btn, false);
  if (j.error) return toast(j.error, 'err', 15000);
  toast('Downloaded to ' + j.path); HOME = false; await refresh(); show('setup');
}
async function setPlc(v){
  await api('/api/plc', {plc: v}); await refresh(); if (TAB) show(TAB);
}
async function pullNow(btn){
  busy(btn, true, 'Getting latest…'); const j = await api('/api/pull', {}); busy(btn, false);
  if (j.error) return toast(j.error, 'err', 20000);
  toast('Up to date with GitHub.'); await refresh(); if (TAB) show(TAB);
}
async function fetchBg(){
  if (!S.repo || !S.repo.is_repo || !S.repo.remote) return;
  await api('/api/fetch', {}); await refresh();
}
async function openRepo(path){
  if (!path) return;
  const j = await api('/api/open', {path});
  if (j.error) return toast(j.error, 'err');
  HOME = false;
  await refresh();
  if (!S.repo.is_repo) { show('setup'); }
  else show(plcDirty() ? 'changes' : 'history');
  fetchBg();
}
function chooseAgain(){ goHome(); }
// ---------- changes
async function loadChanges(){
  await refresh();
  const r = S.repo;
  if (!r || !r.is_repo){ $('#ch-side').innerHTML = ''; $('#ch-frame').src = 'about:blank'; return; }
  const w = [];
  if (r.doubled && r.doubled.length) w.push(`<div class="warnbox"><b>TIA exported into a doubled folder</b>
     ${r.doubled.map(esc).join('<br>')}<br>This makes everything look new. The real fix is the VCI workspace folder in TIA
     (set it to the <code>vci</code> folder), but the app can tidy it up now.<br><br>
     <button class="btn warn" onclick="fixDouble(this)">Move the files to the right place</button></div>`);
  if (r.tia_inside && r.tia_inside.length) w.push(`<div class="warnbox"><b>A TIA project is inside this repo</b>${r.tia_inside.map(esc).join('<br>')}
     <br>Move the project folder outside the repo (e.g. next to it) so it never gets committed.</div>`);
  if (!r.has_commits){
    $('#ch-frame').src = 'about:blank';
    $('#ch-side').innerHTML = w.join('') + '<div class="msg info">This repo has no commits yet. Export from TIA, then commit here.</div>' + commitBox('Initial TIA export');
    return;
  }
  if ((r.ahead || 0) > 0 && (r.behind || 0) > 0) w.unshift(`<div class="warnbox"><b>You and someone else both committed since the last sync</b>
     Click <b>Get latest</b> at the top first; it usually combines everything by itself. If it says the same file was changed, choose:<br><br>
     <button class="btn warn" onclick="resolve(this,'mine')">Keep my version</button>
     <button class="btn" onclick="resolve(this,'theirs')">Use theirs (save mine as backup)</button></div>`);
  const others = Object.entries(r.plc_dirty || {}).filter(([k, v]) => S.plc && k !== S.plc && v);
  if (others.length) w.push(`<div class="msg info" style="font-size:12px">Not included here (other PLCs / files): ${others.map(([k, v]) => esc(k) + ' — ' + v).join(', ')}.
     Switch PLC at the top to commit those.</div>`);
  if (r.legacy && (r.plcs || []).length === 0 && r.people && r.people.length > 1) w.push(`<div class="msg info" style="font-size:12px">Several people commit here. Give each PLC its own folder in <b>Setup</b> so everyone only commits their own PLC.</div>`);
  const n = plcDirty();
  const who = S.plc ? ' for <b>' + esc(S.plc) + '</b>' : '';
  if (!n){
    $('#ch-head').innerHTML = 'Nothing new' + who + ' since the last commit. <a href="#" onclick="lastCommit();return false">Show what the last commit changed</a>';
    $('#ch-frame').src = 'about:blank';
    $('#ch-side').innerHTML = w.join('') + `<div class="msg info">After you change something in TIA, run <b>Export to workspace</b>, then click <b>Refresh</b>.</div>`;
    return;
  }
  $('#ch-head').innerHTML = `<b>${n}</b> file${n > 1 ? 's' : ''} changed${who} since the last commit`;
  loadFrame('ch-frame', '/view/report?old=HEAD&new=WORK&t=' + T + '&view=' + $('#view').value + ($('#allnets').checked ? '&all=1' : '') + plcQ());
  const mine = r.dirty_files.filter(f => !S.plc || plcOf(f) === S.plc);
  $('#ch-side').innerHTML = w.join('') + '<h3>Files</h3><div class="files">' + mine.map(esc).join('<br>') + '</div>' + commitBox('');
  const sug = await api('/api/suggest');
  if ($('#cmsg') && !$('#cmsg').value) $('#cmsg').value = sug.message || '';
  SAFETY = sug.safety || [];
  if (SAFETY.length && $('#cmsg')){
    const d = document.createElement('div'); d.className = 'safebox';
    d.innerHTML = `<b>⚠ This commit changes ${SAFETY.length} safety (F) block${SAFETY.length > 1 ? 's' : ''}</b>${SAFETY.slice(0, 6).map(esc).join(', ')}${SAFETY.length > 6 ? ' …' : ''}
      <label><input type="checkbox" id="safeack"> I have reviewed these safety changes (and the F-signature in TIA).</label>`;
    $('#cmsg').before(d);
  }
}
let SAFETY = [];
function commitBox(def){
  const canPush = S.repo && S.repo.remote;
  return `<h3>Commit</h3><textarea id="cmsg" placeholder="What changed and why">${esc(def)}</textarea>
    <div class="msg info" style="font-size:12px">The message is filled in from the changes. Add the <b>why</b> on the first line, e.g. "Door interlock requested by customer".</div>
    ${canPush ? '<label class="ck"><input type="checkbox" id="cpush" checked> Push to GitHub afterwards</label>' : '<div class="msg info" style="font-size:12px">No GitHub link yet — add one in Setup to back up online.</div>'}
    <button class="btn pri" onclick="doCommit(this)">Commit ${S.plc ? esc(S.plc) : 'changes'}</button><div id="cres"></div>`;
}
async function doCommit(btn){
  const message = $('#cmsg').value.trim();
  if (!message) return toast('Write a short message first.', 'err');
  if (SAFETY.length && !($('#safeack') && $('#safeack').checked)) return toast('Tick the safety confirmation first.', 'err');
  busy(btn, true, 'Committing…');
  const j = await api('/api/commit', {message, push: !!($('#cpush') && $('#cpush').checked),
                                      safety_needed: SAFETY.length > 0, safety_ack: !!($('#safeack') && $('#safeack').checked)});
  busy(btn, false);
  if (j.error) { $('#cres').innerHTML = `<div class="msg err">${esc(j.error)}</div>`; return; }
  toast(j.pushed ? 'Committed and pushed to GitHub.' : 'Committed.' + (j.push_error ? ' Push failed: ' + j.push_error : ''), j.push_error ? 'err' : 'ok', j.push_error ? 20000 : 9000);
  loadChanges();
}
async function fixDouble(btn){
  busy(btn, true, 'Moving…');
  const j = await api('/api/fixdouble', {});
  busy(btn, false);
  if (j.error) return toast(j.error, 'err');
  toast('Files moved into place.'); loadChanges();
}
function lastCommit(){ show('compare'); setTimeout(() => { $('#from').value = 'HEAD~1'; $('#to').value = 'HEAD'; doCompare(); }, 300); }
// ---------- compare
async function loadRevs(){
  const j = await api('/api/revs');
  if (j.error) return toast(j.error, 'err');
  const opts = j.revs.map(r => `<option value="${esc(r.hash)}">${esc(r.label)}</option>`).join('');
  const f = $('#from').value, t = $('#to').value;
  $('#from').innerHTML = '<option value="HEAD~1">Commit before last</option>' + opts;
  $('#to').innerHTML = '<option value="WORK">Now (files on disk)</option><option value="HEAD">Last commit</option>' + opts;
  if (f) $('#from').value = f; if (t) $('#to').value = t; else $('#to').value = 'HEAD';
}
function doCompare(){
  loadFrame('cmp-frame', '/view/report?old=' + encodeURIComponent($('#from').value) + '&new=' + encodeURIComponent($('#to').value) +
    '&t=' + T + '&view=' + $('#view2').value + ($('#allnets2').checked ? '&all=1' : '') + plcQ() +
    ($('#safe2').checked ? '&safety=1' : '') + ($('#sign2').checked ? '&signoff=1' : ''));
}
// ---------- history
function loadHistory(){ loadFrame('hist-frame', '/view/history?t=' + T + '&r=' + Date.now() + plcQ(), 'Reading the history…'); }
// ---------- setup
async function loadSetup(){
  await refresh();
  const c = S.tools, r = S.repo;
  const row = (state, title, desc, action='') => `<div class="chk"><div class="dot ${state}">${state === 'g' ? '✓' : state === 'r' ? '!' : '•'}</div>
     <div class="t"><b>${title}</b><span>${desc}</span></div><div class="a">${action}</div></div>`;
  let h = '<div class="card"><h2>This PC</h2>';
  h += row('g', 'PLC Diff ' + esc(S.version || ''), esc(S.app_file || ''));
  h += row(c.git ? 'g' : 'r', 'Git', c.git ? esc(c.git_version) : 'Not found. If Git Bash works on this PC, run <code>cygpath -w "$(which git)"</code> in Git Bash and paste the result here.',
     c.git ? '' : `<input type="text" id="gp" placeholder="C:\\Program Files\\Git\\cmd\\git.exe"><button class="btn pri" onclick="setGit(this)">Use this</button>`);
  h += row(c.lfs ? 'g' : 'y', 'Git LFS (for project archives)', c.lfs ? esc(c.lfs_version) : 'Not found — optional. It comes with Git for Windows.');
  const idok = c.user_name && c.user_email;
  h += row(idok ? 'g' : 'r', 'Your name for commits', idok ? esc(c.user_name + ' <' + c.user_email + '>') : 'Git needs a name and email to save commits.',
     `<input type="text" id="un" placeholder="Name" value="${esc(c.user_name)}"><input type="text" id="ue" placeholder="Email" value="${esc(c.user_email)}"><button class="btn" onclick="setId(this)">Save</button>`);
  h += '</div><div class="card"><h2>This project</h2>';
  if (!r){ h += row('y', 'No folder chosen', '', '<button class="btn pri" onclick="chooseAgain()">Choose folder</button>'); }
  else if (!r.exists){ h += row('r', 'Folder not found', esc(r.path), '<button class="btn" onclick="chooseAgain()">Choose another</button>'); }
  else if (r.inside_repo){ h += row('y', 'This folder is inside another Git repo', 'The repo starts at ' + esc(r.top), `<button class="btn pri" onclick='openRepo(${esc(JSON.stringify(r.top))})'>Open that instead</button>`); }
  else if (!r.is_repo){ h += row('r', 'Not a Git repo yet', 'The app can set it up: folders (vci, releases, docs), .gitignore for TIA, LFS for archives, first commit.',
     `<button class="btn pri" onclick="initRepo(this)">Set it up</button>`); }
  else {
    h += row('g', 'Git repo', esc(r.path) + ' · branch ' + esc(r.branch));
    h += row(r.remote ? 'g' : 'y', 'GitHub backup', r.remote ? esc(r.remote) : 'Not connected. Create an empty private repo on GitHub and paste its URL.',
       r.remote ? '' : `<input type="text" id="rurl" placeholder="https://github.com/you/Project.git"><button class="btn" onclick="setRemote(this)">Connect</button>`);
    h += row(r.gitignore ? 'g' : 'y', '.gitignore', r.gitignore ? 'Present' : 'Missing — TIA internals could get committed.', r.gitignore ? '' : `<button class="btn" onclick="addIgnore(this)">Add it</button>`);
    h += row(r.doubled.length ? 'r' : 'g', 'TIA export folder', r.doubled.length ? 'Doubled folder found: ' + r.doubled.map(esc).join(', ') + '. Set the VCI workspace folder in TIA to <code>' + esc(r.path) + '\\vci</code>.' : 'OK — no doubled folders.',
       r.doubled.length ? '<button class="btn warn" onclick="fixDouble(this)">Tidy up now</button>' : '');
    h += row(r.tia_inside.length ? 'r' : 'g', 'TIA project kept outside the repo', r.tia_inside.length ? 'Found: ' + r.tia_inside.map(esc).join(', ') : 'OK');
    h += row(r.hook ? 'g' : 'y', 'Change history updates by itself', r.hook ? 'On — every commit is added to the history.' : 'Off — the history still updates whenever you open the History tab.',
       r.hook ? '' : `<button class="btn" onclick="hook(this)">Turn on</button>`);
    if (!(r.plcs || []).length)
      h += row('g', 'VCI workspace folder to use in TIA', '<code>' + esc(r.path) + '\\vci</code>', `<button class="btn" onclick="copy(${esc(JSON.stringify(r.path + '\\vci'))})">Copy</button>`);
  }
  h += '</div>';
  if (r && r.is_repo){
    const plcs = r.plcs || [], pp = r.plc_paths || {};
    const abs = rel => r.path + (rel === '.' ? '' : '\\' + rel.replace(/\//g, '\\'));
    h += '<div class="card"><h2>PLCs</h2><div style="font-size:13px;color:#64748b;margin-bottom:8px">Each PLC = the folder its TIA VCI workspace exports into. Change it if your exports land somewhere else.</div>';
    if (r.loose_in_vci){
      h += `<div class="warnbox"><b>Some exports sit directly in the vci folder</b>They don't belong to any PLC yet. Choose what they are:
        <div class="pathrow"><input id="mvname" placeholder="PLC name, e.g. PLC_1"></div>
        <div class="pathrow"><button class="btn warn" onclick="useVci(this)">Check the vci folder as this PLC</button>
        <button class="btn" onclick="movePlc(this)">Move them into a PLC folder</button></div></div>`;
    }
    if (plcs.length){
      h += '<div class="plcs">' + plcs.map(p => { const rel = pp[p] || ('vci/' + p); const j = esc(JSON.stringify(p));
        return `<div class="plcrow ${p === S.plc ? 'on' : ''}"><b>${esc(p)}</b><code title="${esc(abs(rel))}">${esc(rel === '.' ? '(whole project)' : rel)}</code>
          <button class="btn" onclick="copy(${esc(JSON.stringify(abs(rel)))})">Copy</button>
          <button class="btn" onclick='changePlcFolder(${j}, this)'>Change folder…</button>
          <button class="btn" title="Stop treating this as a PLC (nothing is deleted)" onclick='removePlc(${j})'>Remove</button>
          ${p === S.plc ? '' : `<button class="btn" onclick='setPlc(${j})'>Work on this</button>`}</div>`; }).join('') + '</div>';
    }
    h += `<div class="pathrow"><input id="newplc" placeholder="PLC name, e.g. PLC2_Robot"><button class="btn" onclick="addPlc(this)">Add PLC (new folder)</button>
          <button class="btn" onclick="addPlcExisting(this)">Add PLC from a folder…</button></div>`;
    h += '</div>';
    h += '<div class="card"><h2>Team</h2>';
    h += row(r.remote ? 'g' : 'y', 'Shared on GitHub', r.remote ? esc(r.remote) : 'Connect GitHub above first — that is where everyone shares their commits.');
    if (r.people && r.people.length) h += '<div class="chk"><div class="t"><b>Who has committed</b><div class="people">' + r.people.map(esc).join('\n') + '</div></div></div>';
    h += `<div class="chk"><div class="t"><b>Adding a colleague</b><ol class="steps">
      <li>On GitHub, open the repo → <b>Settings → Collaborators → Add people</b>, and invite their GitHub account.</li>
      <li>They install Git for Windows and Python, then put <b>PLCDiff.pyw</b> and <b>Start PLC Diff.bat</b> in a folder (or use the shared copy) and start it.</li>
      <li>On the first screen they use <b>Get a project from GitHub</b> with this address: <code>${esc(r.remote || '…')}</code></li>
      <li>In Setup they enter their name, pick <b>their PLC</b> at the top, and point their TIA VCI workspace at that PLC's folder.</li></ol>
      <span>Everyone commits only their own PLC. <b>Push</b> gets the others' work first automatically; <b>Get latest</b> brings it in any time.
      Two people should not edit the same PLC at the same time.</span></div></div></div>`;
  }
  if (r && r.is_repo) h += `<div class="card"><h2>Check all blocks</h2><div class="chk"><div class="t"><b>Can PLC Diff read every block?</b>
     <span>Reads and draws every export${S.plc ? ' of ' + esc(S.plc) : ''} and lists any it can't fully show. Takes a minute for big projects.</span></div>
     <div class="a"><button class="btn pri" onclick="checkAllBlocks()">Check all blocks</button></div></div></div>`;
  if (r && r.is_repo) h += `<div class="card"><h2>Get an old version of a block back</h2>
     <div class="chk"><div class="t"><b>Put an earlier version of one block into the TIA workspace</b>
     <span>Choose a version, then the block. Its file in the workspace folder is replaced with that older version, ready for
     <b>Import from workspace</b> in TIA${S.plc ? '. Only ' + esc(S.plc) + ' blocks are listed' : ''}. No other file is touched.</span></div>
     <div class="a"><button class="btn pri" onclick="toggleRestore(this)">Choose a version…</button></div></div>
     <div id="respanel" hidden>
       <div class="pathrow"><select id="res-rev" onchange="loadBlocks()" style="flex:1"></select></div>
       <div class="pathrow"><input type="text" id="res-find" placeholder="Find a block, e.g. Kette or FC_" oninput="filterBlocks()"></div>
       <div id="res-list" class="plcs"></div>
     </div></div>`;
  if (r) h += `<div class="card"><h2>Folder</h2><div class="chk"><div class="t"><b>${esc(r.name)}</b><span>${esc(r.path)}</span></div>
     <div class="a"><button class="btn" onclick="api('/api/explore',{})">Open in Explorer</button><button class="btn" onclick="chooseAgain()">Choose another folder</button></div></div></div>`;
  $('#setup').innerHTML = h;
}
// ---------- get an old version of a block back
let BLOCKS = [];
async function toggleRestore(btn){
  const p = $('#respanel'); p.hidden = !p.hidden;
  if (p.hidden) return;
  busy(btn, true, 'Reading the versions…');
  const j = await api('/api/revs'); busy(btn, false);
  if (j.error) return toast(j.error, 'err');
  $('#res-rev').innerHTML = (j.revs || []).map(r => `<option value="${esc(r.hash)}">${esc(r.label)}</option>`).join('');
  loadBlocks();
}
async function loadBlocks(){
  $('#res-list').innerHTML = '<div class="hint">Reading the blocks of that version…</div>';
  const j = await api('/api/blocks', {rev: $('#res-rev').value});
  if (j.error){ $('#res-list').innerHTML = ''; return toast(j.error, 'err', 12000); }
  BLOCKS = j.blocks || [];
  filterBlocks();
}
function filterBlocks(){
  if (!BLOCKS.length) return $('#res-list').innerHTML = '<div class="hint">No exports in that version.</div>';
  const q = ($('#res-find').value || '').trim().toLowerCase();
  const rows = BLOCKS.filter(b => !q || b.name.toLowerCase().includes(q) || b.path.toLowerCase().includes(q));
  if (!rows.length) return $('#res-list').innerHTML = '<div class="hint">No block matches that.</div>';
  $('#res-list').innerHTML = rows.slice(0, 200).map(b => { const j = esc(JSON.stringify(b.path)), d = b.dest || b.path;
    return `<div class="plcrow"><b>${esc(b.name)}</b><code title="${esc(d)}">${esc(d)}</code>
      <button class="btn warn" onclick='restoreBlock(${j}, this)'>Put it back</button>
      <button class="btn" onclick='restoreBlock(${j}, this, true)'>Save a copy…</button></div>`; }).join('') +
    (rows.length > 200 ? `<div class="hint">… and ${rows.length - 200} more. Type a few letters to narrow it down.</div>` : '');
}
async function restoreBlock(path, btn, copy, force){
  const rev = $('#res-rev').value, label = ($('#res-rev').selectedOptions[0] || {}).textContent || rev;
  const dest = ((BLOCKS.find(b => b.path === path) || {}).dest) || path;
  if (!copy && !force && !confirm('Replace this file in the workspace:\n\n' + dest +
      '\n\nwith the version from:\n\n' + label.trim() + '\n\nAfterwards, in TIA: right-click the block → Import from workspace.')) return;
  busy(btn, true, copy ? 'Choose the folder…' : 'Putting it back…');
  const j = await api('/api/restoreblock', {rev, path, copy: !!copy, force: !!force});
  busy(btn, false);
  if (j.cancelled) return;
  if (j.dirty) return confirm(j.error + '\n\nPut the old version back anyway?') ? restoreBlock(path, btn, copy, true) : undefined;
  if (j.error) return toast(j.error, 'err', 15000);
  toast(copy ? 'Saved a copy: ' + j.written
             : 'Put back: ' + j.written + ' — now in TIA: right-click the block → Import from workspace.', 'ok', 25000);
  await refresh();
}
async function setGit(btn){ busy(btn, true); const j = await api('/api/gitpath', {path: $('#gp').value}); busy(btn, false);
  j.error ? toast(j.error, 'err', 12000) : (toast('Git found.'), loadSetup()); }
async function resolve(btn, keep){
  if (keep === 'mine' && !confirm('Keep your version of the files you both changed, and push it?')) return;
  busy(btn, true); const j = await api('/api/resolve', {keep}); busy(btn, false);
  j.error ? toast(j.error, 'err', 20000) : toast(j.msg, 'ok', 12000); await refresh(); if (TAB) show(TAB);
}
async function pickFolder(btn){
  busy(btn, true, 'Choose the folder…'); const j = await api('/api/pick', {}); busy(btn, false);
  if (j.error) { toast(j.error, 'err'); return ''; } return j.path || '';
}
async function changePlcFolder(name, btn){
  const path = await pickFolder(btn); if (!path) return;
  const j = await api('/api/plcfolder', {plc: name, path});
  if (j.error) return toast(j.error, 'err', 12000);
  toast(name + ' now checks ' + path); await refresh(); loadSetup();
}
async function addPlcExisting(btn){
  const name = $('#newplc').value.trim(); if (!name) return toast('Type the PLC name first.', 'err');
  const path = await pickFolder(btn); if (!path) return;
  const j = await api('/api/plcfolder', {plc: name, path});
  if (j.error) return toast(j.error, 'err', 12000);
  toast('Added ' + name + ' → ' + path); await refresh(); loadSetup();
}
async function useVci(btn){
  const name = $('#mvname').value.trim(); if (!name) return toast('Type the PLC name first.', 'err');
  busy(btn, true); const j = await api('/api/plcfolder', {plc: name, path: S.repo.path + '/vci'}); busy(btn, false);
  if (j.error) return toast(j.error, 'err', 12000);
  toast(name + ' now checks the vci folder.'); await refresh(); loadSetup();
}
async function removePlc(name){
  if (!confirm('Stop treating ' + name + ' as a PLC? No files are deleted.')) return;
  const j = await api('/api/plcremove', {plc: name});
  if (j.error) return toast(j.error, 'err'); await refresh(); loadSetup();
}
async function restartNow(btn){
  busy(btn, true, 'Restarting…');
  await api('/api/restart', {}).catch(() => {});
  setTimeout(() => location.reload(), 4500);
}
function toggleRelease(){
  const p = $('#relpanel'); p.hidden = !p.hidden;
  $('#rel-info').innerHTML = 'Marks the <b>last commit</b>' + (S.plc ? ' of <b>' + esc(S.plc) + '</b>' : '') +
    ' as a release, so you can always compare against it or go back to it. Uncommitted changes must be committed first.' +
    ' It then appears in the From / To lists' + (S.plc ? ' as <b>' + esc(S.plc) + '/&lt;name&gt;</b>' : '') + '.';
}
async function pickArchive(btn){
  busy(btn, true, 'Choose…'); const j = await api('/api/pickfile', {}); busy(btn, false);
  if (j.error) return toast(j.error, 'err'); if (j.path) $('#rel-arch').value = j.path;
}
async function makeRelease(btn){
  busy(btn, true, 'Marking…');
  const j = await api('/api/release', {name: $('#rel-name').value, archive: $('#rel-arch').value, note: $('#rel-note').value, push: $('#rel-push').checked});
  busy(btn, false);
  if (j.error) return toast(j.error, 'err', 15000);
  toast('Release ' + j.tag + ' marked' + (j.pushed ? ' and put on GitHub.' : '.') + (j.push_error ? ' GitHub: ' + j.push_error : ''), j.push_error ? 'err' : 'ok', 12000);
  $('#relpanel').hidden = true; await loadRevs();
}
function checkAllBlocks(){
  show('compare');
  loadFrame('cmp-frame', '/view/health?t=' + T + plcQ(), 'Checking every block…');
}
async function addPlc(btn){ busy(btn, true); const j = await api('/api/addplc', {name: $('#newplc').value.trim()}); busy(btn, false);
  if (j.error) return toast(j.error, 'err'); toast('Added. Point a new VCI workspace in TIA at ' + j.vci, 'ok', 12000); await refresh(); loadSetup(); }
async function movePlc(btn){ busy(btn, true, 'Moving…'); const j = await api('/api/moveplc', {name: $('#mvname').value.trim()}); busy(btn, false);
  if (j.error) return toast(j.error, 'err', 15000);
  toast('Moved and committed. Now set the VCI workspace in TIA to ' + j.vci, 'ok', 15000); await refresh(); loadSetup(); }
function copy(t){ navigator.clipboard.writeText(t).then(() => toast('Copied: ' + t)); }
async function setId(btn){ busy(btn, true); const j = await api('/api/identity', {name: $('#un').value, email: $('#ue').value}); busy(btn, false);
  j.error ? toast(j.error, 'err') : (toast('Saved.'), loadSetup()); }
async function initRepo(btn){ busy(btn, true, 'Setting up…'); const j = await api('/api/init', {}); busy(btn, false);
  if (j.error) { toast(j.error, 'err', 15000); } else { toast('Repo ready. Now point the TIA VCI workspace at the vci folder.'); } loadSetup(); }
async function setRemote(btn){ busy(btn, true, 'Connecting & pushing…'); const j = await api('/api/remote', {url: $('#rurl').value.trim()}); busy(btn, false);
  j.error ? toast(j.error, 'err', 15000) : toast('Connected to GitHub and pushed.'); loadSetup(); }
async function addIgnore(btn){ busy(btn, true); const j = await api('/api/ignore', {}); busy(btn, false); j.error ? toast(j.error, 'err') : loadSetup(); }
async function hook(btn){ busy(btn, true); const j = await api('/api/hook', {}); busy(btn, false); j.error ? toast(j.error, 'err') : (toast('On.'), loadSetup()); }
async function sync(btn){ busy(btn, true, 'Pushing…'); const j = await api('/api/push', {}); busy(btn, false);
  j.error ? toast(j.error, 'err', 12000) : toast('Pushed to GitHub.'); refresh(); }
// ---------- start
setInterval(() => fetch('/api/ping', {headers: {'X-Token': T}}).catch(() => {}), 5000);
setInterval(fetchBg, 180000);
(async () => {
  await refresh();
  const n = (S.projects_count || 0);
  if (S.repo && S.repo.exists && n <= 1) show(S.repo.is_repo ? (plcDirty() ? 'changes' : 'history') : 'setup');
  else goHome();
  fetchBg();
})();
"""

APP_HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>PLC Diff</title><style>%(css)s</style></head><body>
<div class="top"><div class="logo" onclick="goHome()" title="All projects">PLC Diff</div>
  <div class="repo"><span class="nm" id="rname"></span><span class="pth" id="rpath"></span></div>
  <select id="plcsel" class="plcsel" style="display:none" onchange="setPlc(this.value)" title="Which PLC you're working on"></select>
  <button class="btn dark" id="pullbtn" style="display:none" onclick="pullNow(this)">Get latest</button>
  <button class="btn dark" id="syncbtn" style="display:none" onclick="sync(this)">Push</button>
  <button class="btn dark" onclick="goHome()">All projects</button></div>
<div class="tabs" id="tabs" style="display:none">
  <div class="tab" data-t="changes" onclick="show('changes')">Changes <span class="pill" id="pill" style="display:none"></span></div>
  <div class="tab" data-t="compare" onclick="show('compare')">Compare versions</div>
  <div class="tab" data-t="history" onclick="show('history')">History</div>
  <div class="tab" data-t="setup" onclick="show('setup')">Setup</div></div>
<div class="newver" id="newver" style="display:none">A newer version of PLC Diff is installed.
  <button class="btn hot" onclick="restartNow(this)">Restart now</button></div>
<section class="page" id="p-welcome"></section>
<section class="page" id="p-changes">
  <div class="bar"><span id="ch-head" class="grow"></span>
    <label class="ck"><input type="checkbox" id="allnets" onchange="loadChanges()"> Show unchanged networks</label>
    <select id="view" onchange="loadChanges()"><option value="auto">Draw in block language</option><option value="lad">Draw all as LAD</option><option value="fbd">Draw all as FBD</option></select>
    <button class="btn" onclick="loadChanges()">Refresh</button></div>
  <div class="split"><iframe class="frame" id="ch-frame"></iframe><div class="side" id="ch-side"></div></div></section>
<section class="page" id="p-compare">
  <div class="bar">From <select id="from"></select> to <select id="to"></select>
    <label class="ck"><input type="checkbox" id="allnets2"> Show unchanged networks</label>
    <select id="view2"><option value="auto">Draw in block language</option><option value="lad">Draw all as LAD</option><option value="fbd">Draw all as FBD</option></select>
    <label class="ck" title="Show only failsafe blocks (F_LAD, F_FBD, F-DBs)"><input type="checkbox" id="safe2"> Only safety (F) blocks</label>
    <label class="ck" title="Add a review / signature section at the end, for printing"><input type="checkbox" id="sign2"> Sign-off sheet</label>
    <button class="btn pri" onclick="doCompare()">Compare</button>
    <span class="grow"></span><button class="btn" onclick="toggleRelease()">Mark release…</button></div>
  <div class="relpanel" id="relpanel" hidden>
    <div class="form">
      <label>Release name<input id="rel-name" list="rel-names" placeholder="e.g. FAT"><datalist id="rel-names">
        <option value="FAT"><option value="SAT"><option value="Commissioning"><option value="Site"><option value="v1.0"></datalist></label>
      <label>Project archive <small>(optional .zap file from Project → Archive in TIA)</small>
        <span class="row"><input id="rel-arch" placeholder="e.g. D:\\Projects\\MyProject\\PLC_1_FAT.zap20"><button class="btn" onclick="pickArchive(this)">Choose…</button></span></label>
      <label style="grid-column:1/-1">Note <small>(optional, e.g. what was tested)</small><input id="rel-note" placeholder="FAT with customer, all stations running"></label>
    </div>
    <div class="msg info" id="rel-info" style="font-size:12px;margin:10px 0"></div>
    <label class="ck"><input type="checkbox" id="rel-push" checked> Put it on GitHub too</label>
    <div style="margin-top:10px"><button class="btn pri" onclick="makeRelease(this)">Mark release</button> <button class="btn" onclick="toggleRelease()">Cancel</button></div>
  </div>
  <iframe class="frame" id="cmp-frame"></iframe></section>
<section class="page" id="p-history">
  <div class="bar"><span class="grow">Every change, with the date and time it was committed.</span>
    <a class="btn" id="csv" href="#" onclick="this.href='/view/csv?t='+T">Download for Excel</a>
    <button class="btn" onclick="loadHistory()">Refresh</button></div>
  <iframe class="frame" id="hist-frame"></iframe></section>
<section class="page" id="p-setup"><div class="scroll"><div class="wrap" id="setup"></div></div></section>
<div class="loading" id="loading"><span class="spin"></span> <b id="loadmsg">Comparing…</b><small>Big changes (hundreds of blocks) can take a minute.</small></div>
<script>const TOKEN = %(token)s;%(js)s</script></body></html>"""


def instance_path():
    return settings_path().parent / "running.json"


def stop_running_copy():
    """Ask an already running copy of the app (maybe an older version) to quit, so only one is ever in use."""
    import urllib.request
    try:
        info = json.loads(instance_path().read_text(encoding="utf-8"))
    except Exception:
        return False
    try:
        req = urllib.request.Request("http://127.0.0.1:%d/api/quit" % int(info["port"]), data=b"{}", method="POST",
                                     headers={"X-Token": info["token"], "Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=2).read()
        return True
    except Exception:
        return False


def run_app(port=0, open_browser=True):
    import secrets
    import threading
    import time
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import parse_qs, urlparse

    if os.environ.get("PLCDIFF_WAIT"):
        time.sleep(float(os.environ["PLCDIFF_WAIT"]))
    if stop_running_copy():
        time.sleep(1.5)                       # give the old copy time to free its port
    me = Path(__file__).resolve()
    started_mtime = me.stat().st_mtime
    st = load_settings()
    token = secrets.token_hex(16)
    state = {"repo": Path(st["last"]) if st.get("last") else None, "ping": time.time(), "lock": threading.Lock()}

    def remember(p):
        rec = [x for x in st.get("recent", []) if x != str(p)]
        st["recent"] = [str(p)] + rec[:7]
        st["last"] = str(p)
        if str(p) not in st.setdefault("projects", []):
            st["projects"].append(str(p))
        save_settings(st)

    def cur_plc():
        r = state["repo"]
        if not r:
            return ""
        p = st.get("plc", {}).get(str(r), "")
        plcs, _ = list_plcs(r)
        return p if p in plcs else ""

    def existing_paths(root, plc):
        out = []
        for pth in plc_paths(plc, root):
            if pth.startswith(":(") or (root / pth).exists() or run(root, ["git", "ls-tree", "-r", "--name-only", "HEAD", "--", pth])[1].strip():
                out.append(pth)
        return out

    def need_repo():
        r = state["repo"]
        if r is None or not r.is_dir():
            raise ValueError("Choose a project folder first.")
        return r

    def api_get(path, q):
        if path == "/api/ping":
            state["ping"] = time.time()
            return {"ok": True, "app": "plc-diff"}
        if path == "/api/state":
            r = state["repo"]
            try:
                stale = me.stat().st_mtime != started_mtime
            except Exception:
                stale = False
            return {"repo": repo_info(r) if r else None, "tools": tool_checks(), "recent": st.get("recent", []),
                    "version": VERSION, "stale": stale, "app_file": str(me),
                    "plc": cur_plc(), "home": str(Path.home()), "projects_count": len(st.get("projects", []))}
        if path == "/api/projects":
            out = []
            for pth in st.get("projects", []):
                try:
                    out.append(project_summary(pth))
                except Exception as ex:
                    out.append({"path": pth, "name": Path(pth).name, "exists": False, "error": str(ex)})
            return {"projects": out, "current": str(state["repo"]) if state["repo"] else ""}
        if path == "/api/suggest":
            root = need_repo()
            plc = cur_plc()
            meta, reports = compute_reports(root, "HEAD", WORK, plc_paths(plc, root))
            return {"message": suggest_message(reports, plc=plc),
                    "safety": [r.doc.name if r.doc else Path(r.path).stem for r in reports if r.safety]}
        if path == "/api/revs":
            root = need_repo()
            out = git(root, "log", "-60", "--format=%H%x00%h%x00%ad%x00%s%x00%D", "--date=format:%Y-%m-%d %H:%M",
                      check=False).decode("utf-8", "replace")
            revs = []
            for l in out.splitlines():
                parts = l.split("\0")
                if len(parts) < 5:
                    continue
                h, short, date, subj, refs = parts
                tags = [x.replace("tag: ", "") for x in refs.split(", ") if x.startswith("tag: ")]
                revs.append({"hash": h, "label": "%s  %s  %s%s" % (date, short, trunc(subj, 50),
                                                                    ("  [%s]" % ", ".join(tags)) if tags else "")})
            return {"revs": revs}
        raise KeyError(path)

    def api_post(path, body):
        if path == "/api/quit":
            state["quit"] = True
            return {"ok": True}
        if path == "/api/restart":
            env = dict(os.environ, PLCDIFF_WAIT="1.5")
            flags = (0x00000008 | 0x00000200) if os.name == "nt" else 0      # detached, own process group
            subprocess.Popen([sys.executable, str(me), "--app", "--port", str(state["port"]), "--no-browser"],
                             env=env, close_fds=True, creationflags=flags,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            state["quit"] = True
            return {"ok": True}
        if path == "/api/pick":
            cur = str(state["repo"]) if state["repo"] else ""
            p, err = pick_folder(cur)
            if err:
                return {"error": err}
            return {"path": p}
        if path == "/api/open":
            p = Path(os.path.expandvars(os.path.expanduser(str(body.get("path", "")).strip().strip('"')))).resolve()
            if not p.is_dir():
                return {"error": "That folder doesn't exist: %s" % p}
            state["repo"] = p
            remember(p)
            hook = p / ".git" / "hooks" / "post-commit"
            if hook.exists() and is_our_hook(hook) and Path(__file__).resolve().as_posix() not in \
                    hook.read_text(encoding="utf-8", errors="replace"):
                try:
                    install_hook(p)          # the app moved: point the hook at this copy
                except Exception:
                    pass
            return {"ok": True}
        if path == "/api/gitpath":
            raw = str(body.get("path", "")).strip().strip('"')
            pth = Path(raw)
            if pth.is_file():
                pth = pth.parent
            if not (pth / ("git.exe" if os.name == "nt" else "git")).exists():
                return {"error": "No git.exe in %s. In Git Bash run:  cygpath -w \"$(which git)\"  and paste what it prints." % pth}
            st["git_dir"] = str(pth)
            save_settings(st)
            ensure_git_on_path()
            return {"ok": True}
        if path == "/api/projects/add":
            p = Path(str(body.get("path", "")).strip().strip('"'))
            if not p.is_dir():
                return {"error": "That folder doesn't exist: %s" % p}
            p = p.resolve()
            if str(p) not in st.setdefault("projects", []):
                st["projects"].append(str(p))
                save_settings(st)
            return {"ok": True}
        if path == "/api/projects/remove":
            p = str(body.get("path", ""))
            st["projects"] = [x for x in st.get("projects", []) if x != p]
            st["recent"] = [x for x in st.get("recent", []) if x != p]
            if st.get("last") == p:
                st["last"] = ""
            save_settings(st)
            return {"ok": True}
        if path == "/api/scan":
            base = Path(str(body.get("path", "")).strip().strip('"'))
            if not base.is_dir():
                return {"error": "That folder doesn't exist: %s" % base}
            found = scan_for_projects(base)
            added = 0
            for f in found:
                f = str(f.resolve())
                if f not in st.setdefault("projects", []):
                    st["projects"].append(f)
                    added += 1
            save_settings(st)
            return {"ok": True, "found": len(found), "added": added}
        if path == "/api/fetchall":
            n = 0
            for pth in st.get("projects", []):
                r = Path(pth)
                if r.is_dir() and run(r, ["git", "remote", "get-url", "origin"])[0]:
                    run(r, ["git", "fetch", "--prune", "origin"], timeout=90)
                    n += 1
            return {"ok": True, "checked": n}
        if path == "/api/newproject":
            plcs = [x.strip() for x in str(body.get("plcs", "")).replace(";", ",").split(",") if x.strip()]
            ok, msg, root = create_project(str(body.get("parent", "")).strip().strip('"'), str(body.get("name", "")).strip(),
                                           plcs, str(body.get("url", "")).strip())
            if not ok:
                return {"error": msg}
            state["repo"] = root.resolve()
            remember(state["repo"])
            if plcs:
                st.setdefault("plc", {})[str(state["repo"])] = plcs[0]
                save_settings(st)
            return {"ok": True, "warning": msg, "path": str(root)}
        if path == "/api/clone":
            url, parent = str(body.get("url", "")).strip(), str(body.get("parent", "")).strip().strip('"')
            if not (re.match(r"^(https://|git@|ssh://|file://)\S+$", url) or Path(url).is_dir()):
                return {"error": "Paste the repo address from GitHub's green Code button (https://github.com/…/….git)."}
            if not parent or not Path(parent).is_dir():
                return {"error": "Choose the folder to put the project in (e.g. D:\\Projects)."}
            ok, out, dest = clone(url, parent)
            if not ok:
                return {"error": out[-800:]}
            state["repo"] = dest.resolve()
            remember(state["repo"])
            return {"ok": True, "path": str(dest)}
        if path == "/api/identity":
            name, mail = body.get("name", "").strip(), body.get("email", "").strip()
            if not name or "@" not in mail:
                return {"error": "Enter a name and a valid email."}
            run(None, ["git", "config", "--global", "user.name", name])
            run(None, ["git", "config", "--global", "user.email", mail])
            return {"ok": True}
        root = need_repo()
        if path == "/api/init":
            ok, out = init_repo(root)
            return {"ok": True, "log": out} if ok else {"error": "Setup didn't finish:\n" + out[-800:]}
        if path == "/api/remote":
            url = body.get("url", "")
            if not (re.match(r"^(https://|git@|ssh://|file://)\S+$", url) or Path(url).is_dir()):
                return {"error": "Paste the repo URL from GitHub's green Code button (https://github.com/…/….git)."}
            ok, out = run(root, ["git", "remote", "add", "origin", url])
            if not ok and "already exists" in out:
                ok, out = run(root, ["git", "remote", "set-url", "origin", url])
            if not ok:
                return {"error": out}
            ok, out = push(root)
            return {"ok": True} if ok else {"error": "Linked, but the push failed:\n" + out[-800:]}
        if path == "/api/ignore":
            gi = root / ".gitignore"
            if not gi.exists():
                gi.write_text(GITIGNORE, encoding="utf-8")
            return {"ok": True}
        if path == "/api/fixdouble":
            with state["lock"]:
                for d in sorted(find_doubled(root), key=lambda x: -len(x.parts)):
                    merge_up(d)
            return {"ok": True}
        if path == "/api/hook":
            install_hook(root)
            return {"ok": True}
        if path == "/api/commit":
            msg = body.get("message", "").strip()
            if not msg:
                return {"error": "Write a commit message first."}
            plc = cur_plc() if body.get("plc_only", True) else ""
            with state["lock"]:
                paths = existing_paths(root, plc) if plc else ["."]
                if not paths:
                    return {"error": "No files for %s yet - export it from TIA first." % plc}
                if body.get("safety_needed") and not body.get("safety_ack"):
                    return {"error": "This commit changes safety (F) blocks. Tick the safety confirmation first."}
                ok, out = run(root, ["git", "add", "-A", "--"] + paths)
                if not ok:
                    return {"error": out}
                ok, out = run(root, ["git", "commit", "-F", "-", "--"] + paths, inp=msg + "\n")
                if not ok and ("nothing to commit" in out or "no changes added" in out):
                    return {"error": "Nothing to commit%s." % (" for " + plc if plc else "")}
                if not ok:
                    if "user.email" in out or "identity" in out.lower():
                        return {"error": "Git doesn't know your name/email yet. Set it in the Setup tab, then commit again."}
                    return {"error": out[-800:]}
                try:
                    update_history(root, quiet=True).close()
                except Exception:
                    pass
                res = {"ok": True, "pushed": False}
                if body.get("push"):
                    pok, pout = push(root)
                    res["pushed"] = pok
                    if not pok:
                        res["push_error"] = pout[-500:]
                return res
        if path == "/api/pickfile":
            p, err = pick_file(str(state["repo"]) if state["repo"] else "")
            return {"error": err} if err else {"path": p}
        if path == "/api/release":
            import shutil
            name = str(body.get("name", "")).strip()
            if not re.match(r"^[A-Za-z0-9][A-Za-z0-9 _.\-]{0,60}$", name):
                return {"error": "Use a simple release name, e.g. FAT, SAT, Commissioning or v1.0."}
            plc = cur_plc()
            tag = re.sub(r"\s+", "_", ("%s/%s" % (plc, name)) if plc else name)
            if run(root, ["git", "rev-parse", "-q", "--verify", "refs/tags/" + tag])[0]:
                return {"error": "There is already a release called %s. Pick another name (e.g. %s-2)." % (tag, name)}
            with state["lock"]:
                dirty = [l for l in git(root, "status", "--porcelain", "--", *plc_paths(plc, root)).decode("utf-8", "replace").splitlines() if l.strip()]
                if dirty:
                    return {"error": "There are %d uncommitted change%s%s. Commit them first, so the release contains them."
                                     % (len(dirty), "" if len(dirty) == 1 else "s", " for " + plc if plc else "")}
                arch = str(body.get("archive", "")).strip().strip('"')
                if arch:
                    src = Path(arch)
                    if not src.is_file():
                        return {"error": "Archive not found: %s" % src}
                    dest_dir = root / "releases" / (plc or "")
                    dest_dir.mkdir(parents=True, exist_ok=True)
                    dest = dest_dir / ("%s_%s%s" % (re.sub(r"\s+", "_", name), src.stem, src.suffix))
                    shutil.copy2(str(src), str(dest))
                    rel = dest.relative_to(root).as_posix()
                    run(root, ["git", "add", "--", rel])
                    ok, out = run(root, ["git", "commit", "-m", "Release %s: project archive" % tag, "--", rel])
                    if not ok:
                        return {"error": out[-600:]}
                msg = "Release %s%s\n\n%s" % (name, " (%s)" % plc if plc else "", str(body.get("note", "")).strip())
                ok, out = run(root, ["git", "tag", "-a", tag, "-F", "-"], inp=msg.strip() + "\n")
                if not ok:
                    return {"error": out[-600:]}
                res = {"ok": True, "tag": tag, "pushed": False}
                if body.get("push") and run(root, ["git", "remote", "get-url", "origin"])[0]:
                    pok, pout = push(root)
                    if pok:
                        pok, pout = run(root, ["git", "push", "origin", "refs/tags/" + tag], timeout=300)
                    res["pushed"] = pok
                    if not pok:
                        res["push_error"] = pout[-400:]
            return res
        if path == "/api/blocks":
            rev = str(body.get("rev") or "").strip()
            if not rev:
                return {"error": "Choose a version first."}
            if not rev_ok(root, rev):
                return {"error": "That version is not in this project (any more)."}
            plc = cur_plc()
            return {"plc": plc, "blocks": [{"path": p, "name": n, "dest": workspace_path(root, plc, p)}
                                           for p, n in blocks_at(root, rev, plc_paths(plc, root))]}
        if path == "/api/restoreblock":
            rev = str(body.get("rev") or "").strip()
            rel = str(body.get("path") or "").strip()
            if not rev or not rel:
                return {"error": "Choose a version and a block first."}
            if not rev_ok(root, rev):
                return {"error": "That version is not in this project (any more)."}
            plc = cur_plc()
            if rel not in {p for p, _ in blocks_at(root, rev, plc_paths(plc, root))}:
                return {"error": "That block is not part of %s in that version." % (plc or "this project")}
            with state["lock"]:
                if body.get("copy"):
                    folder, err = pick_folder(str(root))
                    if err:
                        return {"error": err}
                    if not folder:
                        return {"cancelled": True}
                    return {"ok": True, "copy": True, "written": str(restore_block(root, rev, rel, folder))}
                dest = workspace_path(root, plc, rel)
                if not body.get("force") and git(root, "status", "--porcelain", "--", dest).decode("utf-8", "replace").strip():
                    return {"dirty": True, "error": "%s has changes that are not committed yet. "
                                                    "Putting the old version back would lose them." % Path(dest).name}
                return {"ok": True, "written": str(restore_block(root, rev, rel, dest_rel=dest)), "name": Path(rel).stem}
        if path == "/api/push":
            with state["lock"]:
                ok, out = push(root)
            return {"ok": True} if ok else {"error": out[-800:]}
        if path == "/api/fetch":
            ok, url = run(root, ["git", "remote", "get-url", "origin"])
            if not ok:
                return {"ok": True, "remote": False}
            ok, out = run(root, ["git", "fetch", "--prune", "origin"], timeout=120)
            return {"ok": ok, "error": "" if ok else out[-400:]}
        if path == "/api/pull":
            with state["lock"]:
                ok, out = pull(root)
            if ok:
                try:
                    update_history(root, quiet=True).close()
                except Exception:
                    pass
            return {"ok": True, "log": out} if ok else {"error": out[-1200:]}
        if path == "/api/resolve":
            keep = body.get("keep")
            with state["lock"]:
                if keep == "mine":
                    ok, out = run(root, ["git", "pull", "--rebase", "--autostash", "-X", "theirs"], timeout=300)
                    if not ok:
                        run(root, ["git", "rebase", "--abort"])
                        run(root, ["git", "stash", "pop"])
                        return {"error": "Couldn't combine automatically:\n" + out[-600:]}
                    ok, out = _push_once(root)
                    return {"ok": True, "msg": "Your version was kept and pushed."} if ok else {"error": out[-600:]}
                if keep == "theirs":
                    ok, br = run(root, ["git", "rev-parse", "--abbrev-ref", "HEAD"])
                    backup = "backup/%s" % datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
                    run(root, ["git", "branch", backup])
                    ok, out = run(root, ["git", "reset", "--keep", "origin/%s" % br])
                    if not ok:
                        return {"error": out[-600:]}
                    return {"ok": True, "msg": "Now using the version on GitHub. Your commits were saved as '%s'." % backup}
            return {"error": "unknown choice"}
        if path == "/api/plc":
            plc = str(body.get("plc", ""))
            st.setdefault("plc", {})[str(root)] = plc
            save_settings(st)
            return {"ok": True}
        if path in ("/api/plcfolder", "/api/plcremove"):
            name = str(body.get("plc", "")).strip()
            if not re.match(r"^[A-Za-z0-9][A-Za-z0-9 _.\-]{0,80}$", name) or name.lower() in TIA_FOLDERS:
                return {"error": "Use a simple PLC name (letters, numbers, spaces, _ - .)."}
            cfg = load_project_cfg(root) or {"plcs": [{"name": n, "path": d} for n, d in plc_map(root).items()]}
            if path == "/api/plcremove":
                cfg["plcs"] = [x for x in cfg["plcs"] if x["name"] != name]
                msg = "PLC Diff: stop tracking %s as a PLC" % name
            else:
                folder = Path(str(body.get("path", "")).strip().strip('"'))
                if not folder.is_absolute():
                    folder = root / folder
                if not folder.is_dir():
                    return {"error": "That folder doesn't exist: %s" % folder}
                try:
                    rel = folder.resolve().relative_to(root.resolve()).as_posix() or "."
                except ValueError:
                    return {"error": "Choose a folder inside this project (%s)." % root}
                if rel.split("/")[0] in (".git", ".plcdiff"):
                    return {"error": "Choose the folder TIA exports into, not a Git or app folder."}
                for x in cfg["plcs"]:
                    if x["name"] == name:
                        x["path"] = rel
                        break
                else:
                    cfg["plcs"].append({"name": name, "path": rel})
                msg = "PLC Diff: %s exports are in %s" % (name, rel)
            with state["lock"]:
                save_project_cfg(root, cfg)
                run(root, ["git", "add", "--", PROJECT_CFG])
                run(root, ["git", "commit", "-m", msg, "--", PROJECT_CFG])
            if path == "/api/plcfolder":
                st.setdefault("plc", {})[str(root)] = name
                save_settings(st)
            return {"ok": True}
        if path == "/api/addplc":
            name = str(body.get("name", "")).strip()
            if not re.match(r"^[A-Za-z0-9][A-Za-z0-9 _.\-]{0,60}$", name) or name.lower() in TIA_FOLDERS:
                return {"error": "Use a simple name like PLC2_Robot (letters, numbers, _ - .)."}
            (root / "vci" / name).mkdir(parents=True, exist_ok=True)
            (root / "releases" / name).mkdir(parents=True, exist_ok=True)
            cfg = load_project_cfg(root)
            if cfg is not None and name not in [x["name"] for x in cfg["plcs"]]:
                cfg["plcs"].append({"name": name, "path": "vci/%s" % name})
                save_project_cfg(root, cfg)
                run(root, ["git", "add", "--", PROJECT_CFG])
                run(root, ["git", "commit", "-m", "PLC Diff: add PLC %s" % name, "--", PROJECT_CFG])
            k = root / "releases" / name / ".gitkeep"
            if not k.exists():
                k.write_text("", encoding="utf-8")
            st.setdefault("plc", {})[str(root)] = name
            save_settings(st)
            return {"ok": True, "vci": str(root / "vci" / name)}
        if path == "/api/moveplc":
            import shutil
            name = str(body.get("name", "")).strip()
            if not re.match(r"^[A-Za-z0-9][A-Za-z0-9 _.\-]{0,60}$", name) or name.lower() in TIA_FOLDERS:
                return {"error": "Use a simple name like PLC1_Station (letters, numbers, _ - .)."}
            with state["lock"]:
                vci = root / "vci"
                target = vci / name
                target.mkdir(parents=True, exist_ok=True)
                moved = []
                for d in [d for d in vci.iterdir() if d.is_dir() and d.name.lower() in TIA_FOLDERS]:
                    rel = "vci/%s" % d.name
                    tracked = run(root, ["git", "ls-files", "--", rel])[1].strip()
                    if tracked:
                        ok, out = run(root, ["git", "mv", rel, "vci/%s/%s" % (name, d.name)])
                        if not ok:
                            return {"error": out}
                    else:
                        shutil.move(str(d), str(target / d.name))
                    moved.append(d.name)
                if not moved:
                    return {"error": "Nothing to move - the exports are already in PLC folders."}
                cfg = load_project_cfg(root)
                if cfg is not None:
                    for x in cfg["plcs"]:
                        if x["name"] == name:
                            x["was"] = sorted(set((x.get("was") or []) + ["vci/%s" % m_ for m_ in moved]))
                            break
                    else:
                        cfg["plcs"].append({"name": name, "path": "vci/%s" % name, "was": ["vci/%s" % m_ for m_ in moved]})
                    save_project_cfg(root, cfg)
                run(root, ["git", "add", "-A", "--", "vci"] + ([PROJECT_CFG] if cfg is not None else []))
                ok, out = run(root, ["git", "commit", "-m", "Move exports into vci/%s (one folder per PLC)" % name, "--", "vci"]
                              + ([PROJECT_CFG] if cfg is not None else []))
                st.setdefault("plc", {})[str(root)] = name
                save_settings(st)
            return {"ok": True, "moved": moved, "vci": str(target)} if ok else {"error": out[-600:]}
        if path == "/api/explore":
            if os.name == "nt":
                os.startfile(str(root))
            return {"ok": True}
        raise KeyError(path)

    def view(path, q):
        root = need_repo()
        if path == "/view/report":
            old = q.get("old", ["HEAD"])[0]
            new = q.get("new", ["WORK"])[0]
            new = WORK if new == "WORK" else new
            for r in (old, new):
                if r != WORK and not rev_ok(root, r):
                    return "text/html", ('<!doctype html><meta charset="utf-8"><body style="font:14px Segoe UI,sans-serif;'
                                         'padding:30px;color:#64748b">Not enough commits yet to compare (%s).</body>' % esc(r)).encode()
            plc = q.get("plc", [""])[0]
            meta, reports = compute_reports(root, old, new, plc_paths(plc, root) if plc in list_plcs(root)[0] else ".",
                                            safety_only=q.get("safety", ["0"])[0] == "1")
            meta["signoff"] = q.get("signoff", ["0"])[0] == "1"
            meta["view"] = q.get("view", ["auto"])[0]
            if plc:
                meta["repo"] = "%s · %s" % (root.name, plc)
            return "text/html", build_html(meta, reports, q.get("all", ["0"])[0] == "1").encode("utf-8")
        if path == "/view/health":
            plc = q.get("plc", [""])[0]
            m = plc_map(root)
            folders = [m[plc]] if plc in m else (list(m.values()) or ["vci"])
            if _auto_plcs(root)[1] and "vci" not in folders and not plc:
                folders.append("vci")
            checked, problems, kinds = health_check(root, folders)
            return "text/html", build_health_html(root.name + (" · " + plc if plc in m else ""), checked, problems, kinds,
                                                  folders).encode("utf-8")
        if path == "/view/history":
            with state["lock"]:
                con = update_history(root, quiet=True)
            rows = history_rows(con, root)
            n = con.execute("SELECT COUNT(*) FROM commits").fetchone()[0]
            con.close()
            return "text/html", build_history_html(root.name, rows, n).encode("utf-8")
        if path == "/view/csv":
            with state["lock"]:
                con = update_history(root, quiet=True)
            rows = history_rows(con, root)
            con.close()
            tmp = Path(tempfile.gettempdir()) / ("plcdiff-history-%s.csv" % root.name)
            write_csv(rows, str(tmp))
            return "text/csv; charset=utf-8", tmp.read_bytes()
        raise KeyError(path)

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, ctype, data, extra=None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def _json(self, obj, code=200):
            self._send(code, "application/json; charset=utf-8", json.dumps(obj).encode("utf-8"))

        def _host_ok(self):
            host = (self.headers.get("Host") or "").split(":")[0]
            return host in ("127.0.0.1", "localhost")

        def do_GET(self):
            if not self._host_ok():
                return self._send(403, "text/plain", b"forbidden")
            u = urlparse(self.path)
            q = parse_qs(u.query)
            try:
                if u.path in ("/", "/index.html"):
                    page = APP_HTML % {"css": APP_CSS, "js": APP_JS, "token": json.dumps(token)}
                    return self._send(200, "text/html; charset=utf-8", page.encode("utf-8"))
                if u.path.startswith("/api/"):
                    if self.headers.get("X-Token") != token:
                        return self._json({"error": "forbidden"}, 403)
                    return self._json(api_get(u.path, q))
                if u.path.startswith("/view/"):
                    if q.get("t", [""])[0] != token:
                        return self._send(403, "text/plain", b"forbidden")
                    ctype, data = view(u.path, q)
                    extra = {"Content-Disposition": 'attachment; filename="history.csv"'} if "csv" in ctype else None
                    return self._send(200, ctype, data, extra)
                return self._send(404, "text/plain", b"not found")
            except KeyError:
                return self._send(404, "text/plain", b"not found")
            except Exception as ex:
                if u.path.startswith("/api/"):
                    return self._json({"error": str(ex)}, 200)
                return self._send(200, "text/html; charset=utf-8",
                                  ('<!doctype html><meta charset="utf-8"><body style="font:14px Segoe UI,sans-serif;padding:30px">'
                                   '<b>Something went wrong</b><pre>%s</pre></body>' % esc(ex)).encode("utf-8"))

        def do_POST(self):
            if not self._host_ok() or self.headers.get("X-Token") != token:
                return self._json({"error": "forbidden"}, 403)
            n = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except Exception:
                body = {}
            try:
                return self._json(api_post(urlparse(self.path).path, body))
            except KeyError:
                return self._json({"error": "unknown action"}, 404)
            except Exception as ex:
                return self._json({"error": str(ex)})

    srv = None
    for p in ([port] if port else [8765, 8766, 8767, 0]):
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", p), H)
            break
        except OSError:
            continue
    state["port"] = srv.server_address[1]
    url = "http://127.0.0.1:%d/" % state["port"]
    try:
        instance_path().parent.mkdir(parents=True, exist_ok=True)
        instance_path().write_text(json.dumps({"port": state["port"], "token": token, "pid": os.getpid(),
                                               "version": VERSION}), encoding="utf-8")
    except Exception:
        pass
    print("PLC Diff %s is running at %s  (close the browser tab to stop it)" % (VERSION, url))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    if open_browser:
        webbrowser.open(url)
    try:
        while True:                      # stop ~1 minute after the browser tab is closed, or when asked to
            time.sleep(1)
            if state.get("quit") or time.time() - state["ping"] > 60:
                break
    except KeyboardInterrupt:
        pass
    srv.shutdown()
    srv.server_close()
    try:
        info = json.loads(instance_path().read_text(encoding="utf-8"))
        if info.get("pid") == os.getpid():
            instance_path().unlink()
    except Exception:
        pass


# =============================================================================
#  main
# =============================================================================

def main(argv=None):
    if sys.stdout is None:                       # started with pythonw (double-click): no console
        sys.stdout = open(os.devnull, "w")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w")
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    args = sys.argv[1:] if argv is None else list(argv)
    ensure_git_on_path()
    if "--app" in args or (not args and "pythonw" in Path(sys.executable).name.lower()):
        port = 0
        if "--port" in args:
            try:
                port = int(args[args.index("--port") + 1])
            except Exception:
                port = 0
        return run_app(port, open_browser="--no-browser" not in args)
    ap = argparse.ArgumentParser(prog="PLCDiff", description="See TIA Portal block changes between Git versions.")
    ap.add_argument("revs", nargs="*", help="0, 1 or 2 versions (commit, tag, branch)")
    ap.add_argument("--files", nargs=2, metavar=("OLD", "NEW"), help="compare two export files directly")
    ap.add_argument("--all", action="store_true", help="also show unchanged networks of changed blocks")
    ap.add_argument("--text", action="store_true", help="only print the change log")
    ap.add_argument("--work", action="store_true", help="compare against the files on disk")
    ap.add_argument("--out", help="save the HTML page here")
    ap.add_argument("--no-open", action="store_true", help="don't open the browser")
    ap.add_argument("--path", default=".", help="only look inside this folder")
    ap.add_argument("--view", choices=["auto", "lad", "fbd"], default="auto",
                    help="how to draw networks: auto = in the block's own language (default)")
    ap.add_argument("--history", action="store_true", help="update the change database and show the history page")
    ap.add_argument("--csv", help="with --history: also save the history as a CSV file for Excel")
    ap.add_argument("--install-hook", action="store_true", help="update the history automatically after every commit")
    ap.add_argument("--quiet", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--app", action="store_true", help="open the app (same as double-clicking the file)")
    ap.add_argument("--port", type=int, default=0, help=argparse.SUPPRESS)
    a = ap.parse_args(argv)
    if a.install_hook:
        root = repo_root()
        install_hook(root)
        a.no_open = True
        run_history(a)
        return
    if a.history or a.csv:
        run_history(a)
        return

    if a.files:
        po, pn = Path(a.files[0]), Path(a.files[1])
        rep = diff_file(pn.name, "M", po.read_bytes(), pn.read_bytes())
        reports = [rep] if (rep.bullets or rep.textdiff or any(n.status != "same" for n in rep.nets)) else []
        meta = {"old_short": po.name, "old_desc": "", "new_short": pn.name, "new_desc": "", "repo": pn.stem,
                "old_label": str(po), "new_label": str(pn), "log": []}
        slug = "files"
    else:
        root = repo_root()
        if len(a.revs) > 2:
            sys.exit("PLC Diff: give at most two versions.")
        if len(a.revs) == 2:
            old, new = a.revs
        elif len(a.revs) == 1:
            old, new = a.revs[0], (WORK if a.work else "HEAD")
        else:
            dirty = git(root, "status", "--porcelain", "--", a.path).strip()
            if a.work or dirty:
                old, new = "HEAD", WORK
            else:
                old, new = "HEAD~1", "HEAD"
        for r in (old, new):
            if r != WORK and not rev_ok(root, r):
                if r == "HEAD~1":
                    sys.exit("PLC Diff: there is only one commit so far, so nothing to compare yet.\n"
                             "Change something in TIA, export to the workspace and run this again.")
                sys.exit("PLC Diff: '%s' is not a commit, tag or branch in this repo." % r)
        os_, od = rev_desc(root, old)
        ns, nd = rev_desc(root, new)
        log = []
        if new != WORK:
            out = git(root, "log", "--format=%h%x00%s", "%s..%s" % (old, new), check=False).decode("utf-8", "replace")
            log = [tuple(l.split("\0", 1)) for l in out.splitlines() if "\0" in l]
        meta = {"old_short": os_, "old_desc": od, "new_short": ns, "new_desc": nd, "repo": root.name,
                "old_label": "%s %s" % (os_, od), "new_label": "%s %s" % (ns, nd), "log": log}
        reports = []
        for st, op, np_ in sorted(changed_files(root, old, new, a.path), key=lambda x: natkey(x[2])):
            ext = Path(np_).suffix.lower()
            if ext != ".xml" and ext not in TEXT_EXT:
                continue
            ob = read_rev(root, old, op) if st != "A" else None
            nb = read_rev(root, new, np_) if st != "D" else None
            rep = diff_file(np_, st, ob, nb)
            if rep.bullets or rep.textdiff or rep.error or any(n.status != "same" for n in rep.nets):
                reports.append(rep)
        slug = "%s-%s" % (os_, ns)

    meta["view"] = a.view
    print_report(meta, reports)
    if a.text:
        return
    out = Path(a.out) if a.out else Path(tempfile.gettempdir()) / "plc-diff" / ("plcdiff-%s.html" % slug)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(build_html(meta, reports, a.all), encoding="utf-8")
    print("\nReport: %s" % out.resolve())
    if not a.no_open:
        webbrowser.open(out.resolve().as_uri())


if __name__ == "__main__":
    main()
