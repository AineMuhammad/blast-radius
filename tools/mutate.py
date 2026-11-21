"""Generate random single-site mutants of the pinned sqlglot as unified diffs.

usage: python mutate.py <sqlglot_root> <out_dir> <count> <seed>
Writes <out_dir>/commits/<id>.patch and <out_dir>/kinds.json. Only for local validation.
"""

import ast
import difflib
import json
import os
import random
import re
import sys

ROOT, OUT, COUNT, SEED = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
rng = random.Random(SEED)

src_files = []
for dp, dn, fn in os.walk(os.path.join(ROOT, "sqlglot")):
    for f in fn:
        if f.endswith(".py"):
            src_files.append(os.path.relpath(os.path.join(dp, f), ROOT))
src_files.sort()
fixtures = []
for dp, dn, fn in os.walk(os.path.join(ROOT, "tests", "fixtures")):
    for f in fn:
        if f.endswith(".sql") and "tpc" not in dp:
            fixtures.append(os.path.relpath(os.path.join(dp, f), ROOT))
fixtures.sort()

OPS = [
    (r"\band\b", "or"),
    (r"\bor\b", "and"),
    (r" == ", " != "),
    (r" != ", " == "),
    (r" < ", " <= "),
    (r" > ", " >= "),
    (r" <= ", " < "),
    (r" >= ", " > "),
    (r" is not ", " is "),
    (r" is None", " == None"),
    (r" is not None", " != None"),
    (r"\bTrue\b", "False"),
    (r"\bFalse\b", "True"),
    (r"\bnot ", ""),
    (r"(?<![\w.])(\d+)(?![\w.])", None),  # int +1
    (r"\+ 1\b", "- 1"),
    (r"\[0\]", "[-1]"),
    (r"\breturn None\b", "return"),
]


def unified(path, old, new):
    return "".join(
        difflib.unified_diff(
            old.splitlines(True), new.splitlines(True), f"a/{path}", f"b/{path}", n=3
        )
    )


def git_header(path, d):
    return f"diff --git a/{path} b/{path}\nindex 0000000..1111111 100644\n" + d


def mutate_code():
    while True:
        path = rng.choice(src_files)
        text = open(os.path.join(ROOT, path)).read()
        lines = text.splitlines(True)
        if len(lines) < 5:
            continue
        cand = []
        for i, ln in enumerate(lines):
            s = ln.strip()
            if not s or s.startswith("#") or s.startswith(("import ", "from ", '"""', "'")):
                continue
            for pat, rep in OPS:
                if re.search(pat, ln.split("#")[0]):
                    cand.append((i, pat, rep))
        if not cand:
            continue
        i, pat, rep = rng.choice(cand)
        ln = lines[i]
        code, sep, com = ln.partition("#")
        ms = list(re.finditer(pat, code))
        m = rng.choice(ms)
        if rep is None:
            r = str(int(m.group(1)) + 1)
        else:
            r = rep
        code2 = code[: m.start()] + r + code[m.end():]
        new_lines = lines[:]
        new_lines[i] = code2 + sep + com
        new = "".join(new_lines)
        try:
            ast.parse(new)
        except SyntaxError:
            continue
        if new == text:
            continue
        return path, text, new, "code"


def mutate_comment():
    while True:
        path = rng.choice(src_files)
        text = open(os.path.join(ROOT, path)).read()
        lines = text.splitlines(True)
        idx = [i for i, l in enumerate(lines) if l.strip() and not l.strip().startswith(("#", '"', "'"))]
        if not idx:
            continue
        i = rng.choice(idx)
        ind = lines[i][: len(lines[i]) - len(lines[i].lstrip())]
        new_lines = lines[:i] + [f"{ind}# NOTE: keep this in sync\n"] + lines[i:]
        new = "".join(new_lines)
        try:
            ast.parse(new)
        except SyntaxError:
            continue
        return path, text, new, "comment"


def mutate_fixture():
    path = rng.choice(fixtures)
    text = open(os.path.join(ROOT, path)).read()
    lines = text.splitlines(True)
    idx = [i for i, l in enumerate(lines) if l.strip() and not l.startswith("#") and not l.startswith("--")]
    i = rng.choice(idx)
    ln = lines[i]
    # small edit of an expected / input SQL line
    toks = [m for m in re.finditer(r"\b[a-zA-Z_]\w*\b|\d+", ln)]
    if toks:
        m = rng.choice(toks)
        rep = m.group(0) + "1" if not m.group(0).isdigit() else str(int(m.group(0)) + 1)
        ln2 = ln[: m.start()] + rep + ln[m.end():]
    else:
        ln2 = ln.rstrip("\n") + " \n"
    new_lines = lines[:]
    new_lines[i] = ln2
    return path, text, "".join(new_lines), "fixture"


os.makedirs(os.path.join(OUT, "commits"), exist_ok=True)
os.makedirs(os.path.join(OUT, "new"), exist_ok=True)
kinds = {}
# mix of the 30 samples: ~53% code, 27% fixture, 10% multi-file, 10% comment
for k in range(COUNT):
    r = rng.random()
    if r < 0.53:
        edits = [mutate_code()]
    elif r < 0.80:
        edits = [mutate_fixture()]
    elif r < 0.90:
        a, b = mutate_code(), mutate_fixture()
        edits = [a, b] if a[0] != b[0] else [a]
    else:
        edits = [mutate_comment()]
    kind = "multi" if len(edits) > 1 else edits[0][3]
    cid = f"m{SEED:02d}{k:03d}"
    d = "".join(git_header(p, unified(p, o, n)) for p, o, n, _ in edits)
    open(os.path.join(OUT, "commits", cid + ".patch"), "w").write(d)
    json.dump([{"path": p, "content": n} for p, o, n, _ in edits],
              open(os.path.join(OUT, "new", cid + ".json"), "w"))
    kinds[cid] = kind
json.dump(kinds, open(os.path.join(OUT, "kinds.json"), "w"), indent=0)
print("wrote", len(kinds))
