"""Local evaluator: validate output like the scorer, report per-commit cost vs oracle.

usage: python evaluate.py <sample_dir> <answers.json> <output.jsonl> [-v]
"""

import json
import os
import sys
from collections import defaultdict

sample, answers_p, out_p = sys.argv[1:4]
verbose = "-v" in sys.argv
tests = json.load(open(os.path.join(sample, "tests.json")))["tests"]
ms = {t["number"]: round(t["seconds"] * 1000) for t in tests}
tid = {t["number"]: t["id"] for t in tests}
commits = {f[:-6] for f in os.listdir(os.path.join(sample, "commits")) if f.endswith(".patch")}
ans = json.load(open(answers_p))["commits"]


def reject(code, line=None):
    print(f"REJECTED: {code}" + (f" on line {line}" if line else ""))
    sys.exit(1)


raw = open(out_p, "rb").read()
try:
    text = raw.decode("utf-8")
except UnicodeDecodeError:
    reject("not_utf8")
lines = text.split("\n")
if text.endswith("\n"):
    lines = lines[:-1]
if text == "":
    lines = []
seen = {}


def strict_const(x):
    raise ValueError(x)


for no, ln in enumerate(lines, 1):
    try:
        o = json.loads(ln, parse_constant=strict_const)
    except Exception:
        reject("not_json", no)
    if not (isinstance(o, dict) and set(o) == {"commit", "workers"} and isinstance(o["commit"], str) and isinstance(o["workers"], list)):
        reject("wrong_shape", no)
    if o["commit"] not in commits:
        reject("unknown_commit", no)
    if o["commit"] in seen:
        reject("repeated_commit", no)
    ws = []
    for w in o["workers"]:
        if not isinstance(w, list):
            reject("wrong_shape", no)
        nw = []
        for x in w:
            if isinstance(x, bool) or not isinstance(x, (int, float)) or (isinstance(x, float) and not x.is_integer()):
                reject("wrong_shape", no)
            nw.append(int(x))
        ws.append(nw)
    allx = [x for w in ws for x in w]
    if any(x not in ms for x in allx):
        reject("unknown_test", no)
    if len(allx) != len(set(allx)):
        reject("repeated_test", no)
    ws = [w for w in ws if w]
    if len(ws) > 8:
        reject("too_many_workers", no)
    seen[o["commit"]] = ws
if set(seen) != commits:
    reject("missing_commit")


def cost(ws):
    if not ws:
        return 0
    return max(sum(ms[x] for x in w) for w in ws) + 2000 * len(ws)


def oracle(fail):
    if not fail:
        return 0
    v = sorted((ms[x] for x in fail), reverse=True)
    best = None
    for k in range(1, 9):
        L = [0] * k
        for m in v:
            L[L.index(min(L))] += m
        c = max(L) + 2000 * k
        best = c if best is None else min(best, c)
    return best


tot = defaultdict(lambda: [0, 0, 0, 0])  # cost, oracle, misses, n
T = [0, 0, 0]
for c in sorted(commits):
    ws = seen[c]
    a = ans.get(c, {"failing": [], "kind": "?"})
    sel = {x for w in ws for x in w}
    missing = [x for x in a["failing"] if x not in sel]
    cc = cost(ws)
    oc = oracle(a["failing"])
    k = a["kind"]
    tot[k][0] += cc
    tot[k][1] += oc
    tot[k][2] += 1 if missing else 0
    tot[k][3] += 1
    T[0] += cc + (300000 if missing else 0)
    T[1] += oc
    T[2] += 1 if missing else 0
    if verbose or missing:
        print(f"{c} {k:8s} cost {cc/1000:7.3f} oracle {oc/1000:7.3f} sel {len(sel):4d} fail {len(a['failing']):3d}"
              + (f"  MISS {[tid[x] for x in missing][:5]}" if missing else ""))
for k, (cc, oc, mi, n) in sorted(tot.items()):
    print(f"kind {k:8s} n={n:2d} cost {cc/1000:8.3f} oracle {oc/1000:8.3f} misses {mi}")
print(f"TOTAL simulated {T[0]/1000:.3f} s  oracle {T[1]/1000:.3f} s  misses {T[2]}")
