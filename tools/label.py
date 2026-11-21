"""Label mutants: apply each patch to /opt/sqlglot, run the full suite, record failing tests.

Runs inside the br-env image:  python /tools/label.py <set_dir> <tests.json>
Writes <set_dir>/answers.json in the sample answers format. Resumable.
"""

import json
import os
import shutil
import subprocess
import sys

SET, TESTS = sys.argv[1], sys.argv[2]
SHARD, NSHARDS = (int(sys.argv[3]), int(sys.argv[4])) if len(sys.argv) > 4 else (0, 1)
ROOT = "/opt/sqlglot"
if NSHARDS > 1:
    # private copy of the checkout per shard
    ROOT = f"/tmp/sqlglot{SHARD}"
    if not os.path.exists(ROOT):
        shutil.copytree("/opt/sqlglot", ROOT)
tests = json.load(open(TESTS))["tests"]
num = {t["id"]: t["number"] for t in tests}
by_file = {}
for t in tests:
    by_file.setdefault(t["file"], []).append(t["number"])
kinds = json.load(open(os.path.join(SET, "kinds.json")))
ans_p = os.path.join(SET, "answers.json")
done = json.load(open(ans_p))["commits"] if os.path.exists(ans_p) else {}
if NSHARDS > 1:
    ans_p = os.path.join(SET, f"answers.{SHARD}.json")
ans = json.load(open(ans_p))["commits"] if os.path.exists(ans_p) else {}

PLUGIN = '''
import json, os
FAILED = set()
FILES = set()
def pytest_runtest_logreport(report):
    if report.failed:
        FAILED.add(report.nodeid)
def pytest_collectreport(report):
    if report.failed:
        FILES.add(report.nodeid)
def pytest_sessionfinish(session):
    if os.environ.get("PYTEST_XDIST_WORKER"):
        tag = os.environ["PYTEST_XDIST_WORKER"]
    else:
        tag = "main"
    with open(os.environ["LABEL_OUT"] + "." + tag, "w") as f:
        json.dump({"failed": sorted(FAILED), "files": sorted(FILES)}, f)
'''
LP = f"/tmp/lp{SHARD}"
os.makedirs(LP, exist_ok=True)
open(f"{LP}/label_plugin.py", "w").write(PLUGIN)
OUTP = f"/tmp/label_out{SHARD}"


def apply(path, patch):
    old = open(path).read().splitlines(True)
    out = []
    i = 0
    lines = patch.splitlines(True)
    k = 0
    while k < len(lines) and not lines[k].startswith("@@"):
        k += 1
    while k < len(lines):
        h = lines[k]
        a = int(h.split()[1][1:].split(",")[0])
        start = a - 1 if "," not in h.split()[1] or h.split()[1].split(",")[1] != "0" else a
        out.extend(old[i:start])
        i = start
        k += 1
        while k < len(lines) and not lines[k].startswith("@@"):
            t = lines[k]
            if t.startswith(" "):
                out.append(old[i])
                i += 1
            elif t.startswith("-"):
                i += 1
            elif t.startswith("+"):
                out.append(t[1:])
            k += 1
    out.extend(old[i:])
    return "".join(out)


for k, cid in enumerate(sorted(kinds)):
    if cid in ans or cid in done or k % NSHARDS != SHARD:
        continue
    patch = open(os.path.join(SET, "commits", cid + ".patch")).read()
    multi = os.path.join(SET, "new", cid + ".json")
    if os.path.exists(multi):
        edits = json.load(open(multi))
    else:
        path = None
        for ln in patch.splitlines():
            if ln.startswith("+++ b/"):
                path = ln[6:]
                break
        newp = os.path.join(SET, "new", cid)
        content = open(newp).read() if os.path.exists(newp) else apply(os.path.join(ROOT, path), patch)
        edits = [{"path": path, "content": content}]
    origs = {e["path"]: open(os.path.join(ROOT, e["path"])).read() for e in edits}
    full = os.path.join(ROOT, edits[0]["path"])
    orig = origs[edits[0]["path"]]
    try:
        for e in edits:
            open(os.path.join(ROOT, e["path"]), "w").write(e["content"])
        for f in os.listdir("/tmp"):
            if f.startswith(os.path.basename(OUTP) + "."):
                os.remove(os.path.join("/tmp", f))
        env = dict(os.environ, SKIP_INTEGRATION="0", PYTHONPATH=f"{ROOT}:{LP}", LABEL_OUT=OUTP)
        subprocess.run(
            [sys.executable, "-m", "pytest", "-p", "label_plugin", "-p", "no:cacheprovider",
             "-n", os.environ.get("LABEL_WORKERS", "8"), "-q",
             "--timeout", "300", "tests"],
            cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        failed = set()
        for f in os.listdir("/tmp"):
            if f.startswith(os.path.basename(OUTP) + "."):
                d = json.load(open(os.path.join("/tmp", f)))
                for nid in d["failed"]:
                    if nid in num:
                        failed.add(num[nid])
                for fid in d["files"]:
                    for n in by_file.get(fid, []):
                        failed.add(n)
        ans[cid] = {"failing": sorted(failed), "kind": kinds[cid]}
        print(cid, kinds[cid], len(failed), flush=True)
    finally:
        for p_, o_ in origs.items():
            open(os.path.join(ROOT, p_), "w").write(o_)
            shutil.rmtree(os.path.join(ROOT, os.path.dirname(p_), "__pycache__"), ignore_errors=True)
    json.dump({"commits": ans}, open(ans_p, "w"))
