"""Check the selector's output on the edge-case set.

usage: python check_edges.py <edge_dir> <output.jsonl>
Validates every scorer rule, then each case's expectation ("all", "none", "valid").
"""

import json
import os
import subprocess
import sys

d, out = sys.argv[1:3]
here = os.path.dirname(os.path.abspath(__file__))
expect = json.load(open(os.path.join(d, "expect.json")))
tests = json.load(open(os.path.join(d, "tests.json")))["tests"]
everything = {t["number"] for t in tests}

# scorer rules (answers file: nothing fails, so only the format is judged)
ans = os.path.join(d, "answers.json")
json.dump({"commits": {c: {"failing": [], "kind": "edge"} for c in expect}}, open(ans, "w"))
r = subprocess.run([sys.executable, os.path.join(here, "evaluate.py"), d, ans, out], capture_output=True, text=True)
if "REJECTED" in r.stdout:
    print(r.stdout)
    sys.exit(1)

ok = True
for line in open(out, encoding="utf-8"):
    o = json.loads(line)
    sel = {x for w in o["workers"] for x in w}
    e = expect[o["commit"]]
    good = (e == "valid") or (e == "all" and sel == everything) or (e == "none" and not sel)
    ok &= good
    print(f"{'ok ' if good else 'BAD'} {o['commit']:24s} expect {e:5s} selected {len(sel):4d} workers {len(o['workers'])}")
print("ALL EDGE CASES OK" if ok else "EDGE CASE FAILURES")
sys.exit(0 if ok else 1)
