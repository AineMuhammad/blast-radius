"""Blast Radius selector: coverage-map based test selection + packing.

Stdlib only. Reads /data/tests.json and /data/commits/*.patch, prints one JSON
line per commit. Map built at image build time lives in MAP_DIR.
"""

import json
import os
import sys

DATA = os.environ.get("BR_DATA", "/data")
MAP_DIR = os.environ.get("BR_MAP", "/app/map")
MAX_WORKERS = 8
WORKER_MS = 2000


def log(*a):
    print(*a, file=sys.stderr)


# ---------------------------------------------------------------- diff parsing
def parse_patch(text):
    """-> list of dicts {old, new, hunks:[(old_start, [(tag, text)])], binary}"""
    files = []
    cur = None
    lines = text.split("\n")
    i = 0
    n = len(lines)
    while i < n:
        ln = lines[i]
        if ln.startswith("diff --git "):
            cur = {"old": None, "new": None, "hunks": [], "binary": False, "hdr": ln}
            files.append(cur)
            i += 1
            continue
        if cur is None:
            i += 1
            continue
        if ln.startswith("--- ") and i + 1 < n and lines[i + 1].startswith("+++ "):
            cur["old"] = _path(ln[4:])
            cur["new"] = _path(lines[i + 1][4:])
            i += 2
            continue
        if ln.startswith("rename from "):
            cur["old"] = ln[len("rename from "):]
        elif ln.startswith("rename to "):
            cur["new"] = ln[len("rename to "):]
        elif ln.startswith("Binary files") or ln.startswith("GIT binary patch"):
            cur["binary"] = True
        elif ln.startswith("@@"):
            # @@ -a,b +c,d @@
            try:
                part = ln.split("@@")[1].strip().split()
                o = part[0][1:].split(",")
                ostart = int(o[0])
                ocount = int(o[1]) if len(o) > 1 else 1
                nn = part[1][1:].split(",")
                ncount = int(nn[1]) if len(nn) > 1 else 1
            except Exception:
                i += 1
                continue
            body = []
            i += 1
            ro, rn = ocount, ncount
            while i < n and (ro > 0 or rn > 0):
                b = lines[i]
                if b.startswith("\\"):
                    i += 1
                    continue
                tag = b[:1] if b else " "
                if tag == "-":
                    ro -= 1
                elif tag == "+":
                    rn -= 1
                elif tag == " ":
                    ro -= 1
                    rn -= 1
                else:
                    break
                body.append((tag, b[1:]))
                i += 1
            cur["hunks"].append((ostart, ocount, body))
            continue
        i += 1
    for f in files:
        if f["old"] is None and f["new"] is None:
            # header only (mode change / binary); derive from diff --git line
            parts = f["hdr"].split(" ")
            if len(parts) >= 4:
                f["old"] = parts[2][2:] if parts[2].startswith("a/") else parts[2]
                f["new"] = parts[3][2:] if parts[3].startswith("b/") else parts[3]
    return files


def _path(p):
    p = p.split("\t")[0].strip()
    if p == "/dev/null":
        return "/dev/null"
    if p.startswith("a/") or p.startswith("b/"):
        return p[2:]
    return p


# ------------------------------------------------------------- code-only text
def code_part(line):
    """Line without comment / trailing space. None if a string is left open."""
    i = 0
    q = None
    L = len(line)
    while i < L:
        c = line[i]
        if q:
            if c == "\\":
                i += 2
                continue
            if line.startswith(q, i):
                i += len(q)
                q = None
                continue
            i += 1
            continue
        if c == "#":
            return line[:i].rstrip()
        if c == '"' or c == "'":
            t = line[i:i + 3]
            q = t if t in ('"""', "'''") else c
            i += len(q)
            continue
        i += 1
    if q:
        return None
    return line.rstrip()


# ------------------------------------------------------------------- the map
class Map:
    def __init__(self, d):
        self.d = d
        with open(os.path.join(d, "index.json")) as f:
            self.index = json.load(f)
        self.cache = {}

    def fmap(self, path):
        name = self.index["files"].get(path)
        if name is None:
            return None
        m = self.cache.get(name)
        if m is None:
            with open(os.path.join(self.d, name)) as f:
                m = json.load(f)
            m["gb"] = [int(h, 16) for h in m["g"]]
            m["sset"] = set(m["s"])
            self.cache[name] = m
        return m


def _hex(h):
    return int(h, 16) if h else 0


def select_py(fm, hunks):
    bits = 0
    G = fm["gb"]
    M = fm["m"]
    I = fm["i"]
    S = fm["sset"]
    nlines = len(M) - 1

    def g(i):
        return G[i] if i >= 0 else 0

    for ostart, ocount, body in hunks:
        # split into change blocks: runs of -/+ lines between context lines
        oline = ostart if ocount > 0 else ostart + 1
        blocks = []
        cur = None
        for tag, txt in body:
            if tag == " ":
                cur = None
                oline += 1
                continue
            if cur is None:
                cur = {"after": oline - 1, "rem": [], "rem_txt": [], "add_txt": []}
                blocks.append(cur)
            if tag == "-":
                cur["rem"].append(oline)
                cur["rem_txt"].append(txt)
                oline += 1
            else:
                cur["add_txt"].append(txt)
        for blk in blocks:
            removed, rem_txt, add_txt = blk["rem"], blk["rem_txt"], blk["add_txt"]
            a = blk["after"]
            in_str = any(l in S for l in removed) or (a in S and (a + 1) in S)
            if not in_str:
                rc = [code_part(t) for t in rem_txt]
                ac = [code_part(t) for t in add_txt]
                if None not in rc and None not in ac:
                    if [c for c in rc if c.strip()] == [c for c in ac if c.strip()]:
                        continue  # only comments / blank lines changed
            rem_code = [l for l in removed if 1 <= l <= nlines and M[l] >= 0]
            for l in removed:
                if 1 <= l <= nlines:
                    bits |= g(M[l])
            if rem_code or not add_txt:
                # replacement: new lines run where the old ones ran
                continue
            # pure insertion (or replacing blank/comment lines) after old line a
            a = max(0, min(a, nlines))
            prev_i, next_i, encl = I[a]
            bits |= g(prev_i) | g(next_i)
            ind = None
            for t in add_txt:
                st = t.strip()
                if st and not st.startswith("#"):
                    ind = len(t) - len(t.lstrip())
                    break
            if ind is None:
                continue
            if prev_i >= 0 and prev_i == next_i and a >= 1 and M[a] == prev_i and a + 1 <= nlines and M[a + 1] == next_i:
                # inside one statement (e.g. a new dict entry): that statement's set
                continue
            chosen = None
            for e_ind, e_gi in encl:
                if e_ind <= ind:
                    chosen = e_gi
            if chosen is not None:
                bits |= g(chosen)
            else:
                for _, e_gi in encl:
                    bits |= g(e_gi)
    return bits


def select(mp, patch_text, info=None):
    """-> bits. info (dict) gets 'files' {path: hunks} and 'local_ok' bool."""
    idx = mp.index
    bits = 0
    if info is not None:
        info["files"] = {}
        info["local_ok"] = True
    for f in parse_patch(patch_text):
        if info is not None:
            o, nw = f["old"], f["new"]
            if (o != nw or o in (None, "/dev/null") or f["binary"] or not o.endswith(".py")
                    or not (o.startswith("sqlglot/") or o.startswith("tests/"))):
                info["local_ok"] = False
            else:
                info["files"][o] = f["hunks"]
        old, new = f["old"], f["new"]
        path = old if old and old != "/dev/null" else new
        if not path or path == "/dev/null":
            continue
        is_new = old == "/dev/null"
        is_del = new == "/dev/null"
        if path.endswith(".py"):
            fm = mp.fmap(path)
            if fm is not None and not is_new:
                if is_del:
                    for gb in fm["gb"]:
                        bits |= gb
                    bits |= _hex(idx["testfile"].get(path))
                else:
                    bits |= select_py(fm, f["hunks"])
                if old != new and new != "/dev/null" and not is_del:
                    # rename: like deleting old
                    for gb in fm["gb"]:
                        bits |= gb
            # new python file: nothing imports it yet; directory listers may see it
            if is_new or (old != new):
                d = os.path.dirname(new if new != "/dev/null" else old)
                bits |= _hex(idx["dirs"].get(d))
        else:
            for p in {old, new} - {"/dev/null", None}:
                bits |= _hex(idx["data"].get(p))
                if is_new or is_del or old != new:
                    bits |= _hex(idx["dirs"].get(os.path.dirname(p)))
    return bits


# ------------------------------------------------------------ local runs
SQLGLOT = os.environ.get("BR_SQLGLOT", "/opt/sqlglot")
LOCAL_OVERHEAD_MS = 500  # fork + importing sqlglot, the test modules and patched sources
LOCAL_FACTOR = 1.4  # local CPU per recorded test second (measured, includes per-test imports)
LOCAL_P_FAIL = float(os.environ.get("BR_PFAIL", "0.5"))  # chance a locally run selection contains a failure (then little is saved)
# stdlib modules sqlglot and unittest import: loaded once in the parent, shared by forks
PRELOAD = ("unittest", "importlib.abc", "importlib.util", "signal", "typing", "re", "enum",
           "datetime", "decimal", "math", "functools", "itertools", "collections", "logging",
           "textwrap", "dataclasses", "copy", "inspect", "string", "heapq", "uuid", "hashlib",
           "contextlib", "abc", "numbers", "fractions", "difflib", "zoneinfo", "random")
_preloaded = False


def _preload():
    global _preloaded
    if _preloaded:
        return
    _preloaded = True
    import importlib

    for m in PRELOAD:
        try:
            importlib.import_module(m)
        except Exception:
            pass
# classes whose fixtures start process pools / databases: never run locally
HEAVY_CLASSES = ("tests/test_optimizer.py::", "tests/test_executor.py::", "tests/test_sqlglotc.py::")


def apply_hunks(text, hunks):
    """Apply parsed hunks to text; None if the context does not match."""
    old = text.split("\n")
    out = []
    i = 0
    for ostart, ocount, body in hunks:
        start = ostart - 1 if ocount > 0 else ostart
        if start < i:
            return None
        out.extend(old[i:start])
        i = start
        for tag, txt in body:
            if tag == " " or tag == "-":
                if i >= len(old) or old[i] != txt:
                    return None
                if tag == " ":
                    out.append(txt)
                i += 1
            else:
                out.append(txt)
    out.extend(old[i:])
    return "\n".join(out)


def _modname(path):
    m = path[:-3].replace("/", ".")
    return m[: -len(".__init__")] if m.endswith(".__init__") else m


def _child_run(patched, test_ids, budgets, wfd):
    import importlib.abc
    import importlib.util
    import signal
    import unittest

    os.environ["SKIP_INTEGRATION"] = "0"
    os.chdir(SQLGLOT)
    sys.path.insert(0, SQLGLOT)
    mods = {_modname(p): (p, src) for p, src in patched.items()}

    class Loader(importlib.abc.SourceLoader):
        def __init__(self, path, src):
            self.path, self.src = path, src

        def get_filename(self, fullname):
            return os.path.join(SQLGLOT, self.path)

        def get_data(self, path):
            if path == os.path.join(SQLGLOT, self.path):
                return self.src.encode("utf-8")
            with open(path, "rb") as f:
                return f.read()

        def path_stats(self, path):
            raise OSError  # never use a cached pyc for patched sources

    class Finder(importlib.abc.MetaPathFinder):
        def find_spec(self, name, path=None, target=None):
            if name in mods:
                p, src = mods[name]
                is_pkg = p.endswith("__init__.py")
                spec = importlib.util.spec_from_file_location(
                    name, os.path.join(SQLGLOT, p), loader=Loader(p, src),
                    submodule_search_locations=[os.path.dirname(os.path.join(SQLGLOT, p))] if is_pkg else None,
                )
                return spec
            return None

    sys.meta_path.insert(0, Finder())

    failed = set()
    dotted = {}
    suite = unittest.TestSuite()
    loader = unittest.TestLoader()
    for tid in test_ids:
        f, rest = tid.split("::", 1)
        name = _modname(f) + "." + rest.replace("::", ".")
        dotted[name] = tid
        try:
            suite.addTests(loader.loadTestsFromName(name))
        except BaseException:
            failed.add(tid)
    by_class = {}
    for name, tid in dotted.items():
        by_class.setdefault(name.rsplit(".", 1)[0], []).append(tid)

    class Timeout(BaseException):
        pass

    def on_alarm(signum, frame):
        raise Timeout()

    signal.signal(signal.SIGALRM, on_alarm)

    def mark(test):
        tid = getattr(test, "id", lambda: "")()
        if tid in dotted:
            failed.add(dotted[tid])
            return
        # subtests / class or module fixtures
        base = getattr(test, "test_case", None)
        if base is not None and base.id() in dotted:
            failed.add(dotted[base.id()])
            return
        desc = str(test)
        hit = False
        for cls, tids in by_class.items():
            if cls in desc or cls.rsplit(".", 1)[0] in desc:
                failed.update(tids)
                hit = True
        if not hit:
            failed.update(dotted.values())

    class Result(unittest.TestResult):
        def startTest(self, test):
            super().startTest(test)
            b = budgets.get(dotted.get(test.id()), 5.0)
            signal.setitimer(signal.ITIMER_REAL, b)

        def stopTest(self, test):
            signal.setitimer(signal.ITIMER_REAL, 0)
            super().stopTest(test)

        def addError(self, test, err):
            mark(test)

        def addFailure(self, test, err):
            mark(test)

        def addSubTest(self, test, subtest, err):
            if err is not None:
                mark(test)

        def addUnexpectedSuccess(self, test):
            mark(test)

    devnull = open(os.devnull, "w")
    sys.stdout = devnull
    sys.stderr = devnull
    res = Result()
    try:
        suite.run(res)
    except BaseException:
        failed.update(dotted.values())
    os.write(wfd, json.dumps(sorted(failed)).encode())


def run_local(patched, test_ids, budgets, wall_limit):
    """Run tests on the patched tree in a forked child. -> set of failing ids, or None."""
    import select as _select
    import signal
    import time

    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        code = 0
        try:
            os.close(r)
            _child_run(patched, test_ids, budgets, w)
        except BaseException:
            code = 1
        finally:
            os._exit(code)
    os.close(w)
    chunks = []
    deadline = time.monotonic() + wall_limit
    ok = True
    while True:
        left = deadline - time.monotonic()
        if left <= 0:
            ok = False
            break
        rl, _, _ = _select.select([r], [], [], left)
        if not rl:
            ok = False
            break
        b = os.read(r, 65536)
        if not b:
            break
        chunks.append(b)
    os.close(r)
    if not ok:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    _, status = os.waitpid(pid, 0)
    if not ok or status != 0:
        return None
    try:
        return set(json.loads(b"".join(chunks).decode()))
    except Exception:
        return None


def pack_cost(ms_list):
    if not ms_list:
        return 0
    ms_list = sorted(ms_list, reverse=True)
    best = None
    total = sum(ms_list)
    for k in range(1, min(MAX_WORKERS, len(ms_list)) + 1):
        loads = [0] * k
        for m in ms_list:
            loads[loads.index(min(loads))] += m
        c = max(loads) + WORKER_MS * k
        best = c if best is None else min(best, c)
    return best


def plan_local(nums, ms, idof):
    """Choose the locally-run subset minimizing expected cost (assuming tests pass)."""
    elig = sorted((n for n in nums if not idof[n].startswith(HEAVY_CLASSES)), key=lambda n: ms[n])
    if not elig:
        return []
    base = pack_cost([ms[n] for n in nums])
    best, best_k = 0, 0  # best expected saving
    rest_sorted = sorted((ms[n] for n in nums), reverse=True)
    acc = 0
    elig_set = set()
    for k, n in enumerate(elig, 1):
        acc += ms[n]
        elig_set.add(n)
        if k < len(elig) and ms[elig[k]] == ms[n]:
            continue
        remaining = [ms[m] for m in nums if m not in elig_set]
        cpu = LOCAL_OVERHEAD_MS + LOCAL_FACTOR * acc
        # pass: pay cpu + rest; fail: pay cpu and (about) the full schedule
        saving = (1 - LOCAL_P_FAIL) * (base - pack_cost(remaining)) - cpu
        if saving > best + 100:
            best, best_k = saving, k
    return elig[:best_k]


# ---------------------------------------------------------------- packing
def pack(items):
    """items: list of (ms, number). -> list of workers minimizing max + 2s*k (LPT per k)."""
    if not items:
        return []
    items = sorted(items, key=lambda x: -x[0])
    total = sum(ms for ms, _ in items)
    best = None
    for k in range(1, MAX_WORKERS + 1):
        if k > len(items):
            break
        lb = max(items[0][0], (total + k - 1) // k) + WORKER_MS * k
        if best is not None and lb >= best[0]:
            continue
        loads = [0] * k
        ws = [[] for _ in range(k)]
        for ms, num in items:
            j = loads.index(min(loads))
            loads[j] += ms
            ws[j].append(num)
        cost = max(loads) + WORKER_MS * k
        if best is None or cost < best[0]:
            best = (cost, ws)
    return [w for w in best[1] if w]


def refine_local(info, nums, ms, idof):
    L = plan_local(nums, ms, idof)
    if not L:
        return nums
    patched = {}
    for path, hunks in info["files"].items():
        with open(os.path.join(SQLGLOT, path), encoding="utf-8") as f:
            new = apply_hunks(f.read(), hunks)
        if new is None:
            return nums
        patched[path] = new
    _preload()
    ids = [idof[n] for n in L]
    budgets = {idof[n]: max(3.0, 5 * ms[n] / 1000 + 2.0) for n in L}
    wall = 20 + sum(budgets.values())
    import resource

    c0 = resource.getrusage(resource.RUSAGE_CHILDREN)
    failed = run_local(patched, ids, budgets, wall)
    c1 = resource.getrusage(resource.RUSAGE_CHILDREN)
    log(f"local cpu {(c1.ru_utime + c1.ru_stime - c0.ru_utime - c0.ru_stime):.3f}s, "
        f"recorded {sum(ms[n] for n in L) / 1000:.3f}s")
    if failed is None:
        return nums
    keep = set(nums) - set(L)
    num_of = {idof[n]: n for n in L}
    keep.update(num_of[t] for t in failed if t in num_of)
    log(f"local: ran {len(L)} tests, {len(failed)} failed, kept {len(keep)} of {len(nums)}")
    return keep


def main():
    with open(os.path.join(DATA, "tests.json")) as f:
        tj = json.load(f)
    tests = tj["tests"]
    by_id = {t["id"]: t["number"] for t in tests}
    ms = {t["number"]: round(t["seconds"] * 1000) for t in tests}
    mp = Map(MAP_DIR)
    ids = mp.index["tests"]
    bit2num = [by_id.get(t) for t in ids]
    known = set(n for n in bit2num if n is not None)
    always = [t["number"] for t in tests if t["number"] not in known]
    if always:
        log(f"{len(always)} tests without trace data, always selected")

    idof = {t["number"]: t["id"] for t in tests}
    local_enabled = os.environ.get("BR_LOCAL", "1") == "1" and os.path.isdir(SQLGLOT)
    cdir = os.path.join(DATA, "commits")
    out = []
    for name in sorted(os.listdir(cdir)):
        if not name.endswith(".patch"):
            continue
        cid = name[: -len(".patch")]
        try:
            with open(os.path.join(cdir, name), encoding="utf-8", errors="replace") as f:
                text = f.read()
            info = {}
            bits = select(mp, text, info)
            nums = set(always)
            i = 0
            while bits:
                if bits & 1:
                    nm = bit2num[i]
                    if nm is not None:
                        nums.add(nm)
                bits >>= 1
                i += 1
        except Exception as e:  # never fail: run everything
            log(f"{cid}: error {e!r}, selecting all")
            nums = set(ms)
        if local_enabled and nums and info.get("local_ok"):
            try:
                nums = refine_local(info, nums, ms, idof)
            except Exception as e:
                log(f"{cid}: local run skipped ({e!r})")
        workers = pack([(ms[n], n) for n in nums])
        out.append(json.dumps({"commit": cid, "workers": workers}, separators=(",", ":")))
    sys.stdout.write("\n".join(out) + ("\n" if out else ""))


if __name__ == "__main__":
    main()
