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


CMP_FILE = os.environ.get("BR_CMP", os.path.join(os.path.dirname(os.path.abspath(__file__)), "cmp_equivalent.json"))
CMP_PARTNER = {("is",): ("==",), ("==",): ("is",), ("is", "not"): ("!=",), ("!=",): ("is", "not"),
               ("<",): ("<=",), ("<=",): ("<",), (">",): (">=",), (">=",): (">",)}
_CMP_SITES = None


def _cmp_sites():
    global _CMP_SITES
    if _CMP_SITES is None:
        try:
            with open(CMP_FILE) as f:
                _CMP_SITES = set(json.load(f)["sites"])
        except (OSError, ValueError, KeyError):
            _CMP_SITES = set()
    return _CMP_SITES


def _tokens(line):
    import io
    import tokenize

    out = []
    try:
        for t in tokenize.generate_tokens(io.StringIO(line + "\n").readline):
            if t.type in (tokenize.NL, tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT,
                          tokenize.ENDMARKER, tokenize.COMMENT):
                continue
            out.append((t.string, t.start[1]))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        pass  # an open bracket at the end of the line: the tokens so far are enough
    return out


def equivalent_compare_edit(path, line_no, old, new):
    """True if a one-line edit only swaps a comparison operator for its partner at a site
    where the full suite proved both operators give the same result every time."""
    if not _cmp_sites():
        return False
    a, b = _tokens(old), _tokens(new)
    i = 0
    while i < len(a) and i < len(b) and a[i][0] == b[i][0]:
        i += 1
    j = 0
    while j < len(a) - i and j < len(b) - i and a[-1 - j][0] == b[-1 - j][0]:
        j += 1
    oa = tuple(t for t, _ in a[i:len(a) - j])
    ob = tuple(t for t, _ in b[i:len(b) - j])
    if not oa or CMP_PARTNER.get(oa) != ob:
        return False
    return f"{path}:{line_no}:{a[i][1]}" in _cmp_sites()


def lambda_body_edit(fm, removed, rem_txt, add_txt):
    """Group of the innermost lambda whose body alone a one-line edit changes, else None."""
    if len(removed) != 1 or len(add_txt) != 1:
        return None
    ranges = fm.get("lam", {}).get(str(removed[0]))
    if not ranges:
        return None
    old, new = rem_txt[0], add_txt[0]
    if not (old.isascii() and new.isascii()) or old == new:
        return None
    p = 0
    while p < len(old) and p < len(new) and old[p] == new[p]:
        p += 1
    q = 0
    while q < len(old) - p and q < len(new) - p and old[-1 - q] == new[-1 - q]:
        q += 1
    end = len(old) - q  # old[p:end] was replaced by new[p:len(new) - q]
    if end <= p:
        return None  # pure insertion: not narrowed
    for c0, c1, grp in ranges:  # innermost first
        if c0 <= p and end <= c1:
            return grp
    return None


def select_py(fm, hunks, flags=None, path=None):
    """flags (dict) gets 'import_time': True when a change touches code that runs on import,
    and 'blocks': the test bits of each change block."""
    bits = 0
    block_bits = flags.setdefault("blocks", []) if flags is not None else []
    T = fm.get("tset")
    if T is None:
        T = fm["tset"] = set(fm.get("t", ()))
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
            if flags is not None and (any(l in T for l in removed) or (not removed and (
                    a == 0 or a in T or (a + 1) in T))):
                flags["import_time"] = True
            if (path and len(removed) == 1 and len(add_txt) == 1
                    and equivalent_compare_edit(path, removed[0], rem_txt[0], add_txt[0])):
                if flags is not None:
                    flags["equivalent"] = flags.get("equivalent", 0) + 1
                continue  # proven to behave identically for every test
            rem_code = [l for l in removed if 1 <= l <= nlines and M[l] >= 0]
            bbits = 0
            narrowed = lambda_body_edit(fm, removed, rem_txt, add_txt)
            for l in removed:
                if 1 <= l <= nlines:
                    bbits |= g(narrowed) if narrowed is not None else g(M[l])
            bits |= bbits
            # def / class / decorator lines run at import time and bind a name: an edit there
            # (a rename onto a name found by dispatch, `<key>_sql`, `_parse_<x>`; a different
            # decorator) changes what every user of the enclosing scope resolves, whether or
            # not any test ran the old body
            header_edit = any(t.lstrip().startswith(("def ", "async def ", "class ", "@"))
                              for t in rem_txt + add_txt)
            if (rem_code or not add_txt) and not header_edit:
                # replacement: new lines run where the old ones ran
                block_bits.append(bbits)
                continue
            # pure insertion (or replacing blank/comment lines) after old line a
            a = max(0, min(a, nlines))
            prev_i, next_i, encl = I[a]
            ib = bbits | g(prev_i) | g(next_i)
            ind = None
            for t in add_txt + (rem_txt if header_edit else []):
                st = t.strip()
                if st and not st.startswith("#"):
                    ind = len(t) - len(t.lstrip())
                    break
            inside_one = (prev_i >= 0 and prev_i == next_i and a >= 1 and M[a] == prev_i
                          and a + 1 <= nlines and M[a + 1] == next_i)
            if ind is not None and (header_edit or not inside_one):
                chosen = None
                for e_ind, e_gi in encl:
                    if e_ind <= ind:
                        chosen = e_gi
                if chosen is not None:
                    ib |= g(chosen)
                else:
                    for _, e_gi in encl:
                        ib |= g(e_gi)
            bits |= ib
            block_bits.append(ib)
    return bits


def select(mp, patch_text, info=None, skip=()):
    """-> bits. info (dict) gets 'files' {path: hunks} and 'local_ok' bool."""
    idx = mp.index
    bits = 0
    if info is not None:
        info["files"] = {}
        info["local_ok"] = True
        info["import_check"] = []  # patched modules whose import-time code changed
        info["force_all"] = False
        info["blocks"] = []  # test bits per change block of .py files
        info["data_bits"] = 0  # tests reached through non-Python files
    for f in parse_patch(patch_text):
        if (f["old"] or f["new"]) in skip or f["old"] in skip:
            continue  # compiles to the same program as before
        if info is not None and _affects_test_config(f):
            info["force_all"] = True
        if info is not None:
            o, nw = f["old"], f["new"]
            if o != nw or o in (None, "/dev/null") or f["binary"]:
                info["local_ok"] = False
            elif not o.endswith(".py"):
                # a modified data file: tests that read it are kept, the rest may be checked
                info.setdefault("data_paths", []).append(o)
            elif not (o.startswith("sqlglot/") or o.startswith("tests/")):
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
            if (fm is not None and (is_del or old != new) and info is not None
                    and path.startswith(("sqlglot/", "tests/"))):
                # a module that disappears can break importing sqlglot or the test files
                info["force_all"] = True
            if fm is not None and not is_new:
                if is_del:
                    for gb in fm["gb"]:
                        bits |= gb
                    bits |= _hex(idx["testfile"].get(path))
                else:
                    flags = {}
                    bits |= select_py(fm, f["hunks"], flags, path)
                    if info is not None:
                        info["blocks"].extend(flags.get("blocks", []))
                        if flags.get("import_time"):
                            info["import_check"].append(path)
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
                db = _hex(idx["data"].get(p))
                if is_new or is_del or old != new:
                    db |= _hex(idx["dirs"].get(os.path.dirname(p)))
                bits |= db
                if info is not None:
                    info["data_bits"] |= db
    return bits


CONFIG_FILES = ("pytest.ini", "tox.ini", "setup.cfg", "pyproject.toml")


def _affects_test_config(f):
    """conftest.py anywhere, pytest.ini, or a pytest section of tox.ini/setup.cfg/pyproject.toml.

    These change how every test is collected or run, so the whole suite is selected."""
    for p in (f["old"], f["new"]):
        if not p or p == "/dev/null":
            continue
        base = os.path.basename(p)
        if base == "conftest.py" or base == "pytest.ini":
            return True
        if base in CONFIG_FILES and os.path.dirname(p) in ("", "tests"):
            touched = [t for h in f["hunks"] for tag, t in h[2] if tag in "+-"]
            if any("pytest" in t for t in touched):
                return True
            try:
                with open(os.path.join(SQLGLOT, p), encoding="utf-8") as fh:
                    old = fh.read()
            except OSError:
                old = ""
            if "pytest" in old:
                return True  # file already configures pytest: any edit may matter
    return False


# ------------------------------------------------------------ local runs
SQLGLOT = os.environ.get("BR_SQLGLOT", "/opt/sqlglot")
TPC_DIR = os.environ.get("BR_TPC", "/app/tpcdata")
HERE = os.path.dirname(os.path.abspath(__file__))  # tpc.py lives next to this file
# cost of a full model check per tests.json second of the test: in-process runs measured
# ~0.75 (the recorded time includes the pool's start-up); a constant, so decisions do not
# depend on this machine
TPC_FACTOR = float(os.environ.get("BR_TPC_FACTOR", "0.75"))
TPC_LOAD_MS_PER_MB = 25  # loading pre-parsed TPC tables
TPC_FAIL_FRACTION = float(os.environ.get("BR_TPC_FAIL", "0.1"))  # a broken query stops the check at the first mismatch
# wall-clock budget for all local runs (the batch has 180 s, the final 270 s for ~1.5x commits)
LOCAL_WALL_PER_COMMIT = 2.5
LOCAL_WALL_MAX = 150.0
LOCAL_OVERHEAD_MS = 500  # fork, imports, patched sources; deliberately above the measured 0.2 s
LOCAL_FACTOR = 1.4  # local CPU per recorded test second (measured 1.15; margin pays off)
LOCAL_FAIL_KEEPS_ONE = os.environ.get("BR_FAIL_KEEPS_ONE", "0") == "1"  # tried: no gain, more CPU
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


def _child_run(patched, test_ids, budgets, wfd, tpc_tasks=(), import_mods=()):
    import importlib
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

    # import-time code changed: a module that no longer imports fails every test using it
    for m in import_mods:
        try:
            importlib.import_module(m)
        except BaseException as e:  # noqa: BLE001
            os.write(wfd, json.dumps({"import_error": f"{m}: {e!r}"[:300]}).encode())
            return
    if not test_ids and not tpc_tasks:
        os.write(wfd, json.dumps({"failed": []}).encode())
        return

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
    for tid, indices, budget, compile_only, resume in tpc_tasks:
        try:
            sys.path.insert(0, HERE)
            import tpc

            signal.setitimer(signal.ITIMER_REAL, budget)
            ok = tpc.check(TPC_DIR, tid, indices, budget, compile_only, resume)
        except BaseException:
            ok = False
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
        if not ok:
            failed.add(tid)
    res = Result()
    try:
        suite.run(res)
    except BaseException:
        failed.update(dotted.values())
    os.write(wfd, json.dumps({"failed": sorted(failed)}).encode())


def run_local(patched, test_ids, budgets, wall_limit, tpc_tasks=(), import_mods=()):
    """Run tests on the patched tree in a forked child.

    -> {"failed": set of failing ids} | {"import_error": str} | None (the child failed)."""
    import select as _select
    import signal
    import time

    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        code = 0
        try:
            os.close(r)
            _child_run(patched, test_ids, budgets, w, tpc_tasks, import_mods)
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
        res = json.loads(b"".join(chunks).decode())
    except Exception:
        return None
    if "import_error" in res:
        return res
    return {"failed": set(res["failed"])}


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


def plan_local(nums, ms, idof, extra_cost=None, extra_fail=None, exclude=frozenset()):
    """Choose the locally-run subset maximizing expected saving.

    extra_cost: {test number: local CPU ms} for tests checked by a model (TPC queries)
    instead of being run whole.
    """
    extra_cost = extra_cost or {}
    extra_fail = extra_fail or {}
    cand = []
    fail_cost = {}
    for n in nums:
        if n in exclude:
            continue  # reads a changed data file: only the real run can tell
        if n in extra_cost:
            cand.append((extra_cost[n], n))
            fail_cost[n] = extra_fail.get(n, extra_cost[n])
        elif not idof[n].startswith(HEAVY_CLASSES):
            cand.append((LOCAL_FACTOR * ms[n], n))
            fail_cost[n] = LOCAL_FACTOR * ms[n]
    if not cand:
        return []
    # cheapest local cost per scheduled second first
    cand.sort(key=lambda x: (x[0] / max(ms[x[1]], 1), x[0]))
    base = pack_cost([ms[n] for n in nums])
    best, best_k = 0, 0
    chosen = set()
    acc = acc_fail = 0
    for k, (c, n) in enumerate(cand, 1):
        acc += c
        acc_fail += fail_cost[n]
        chosen.add(n)
        remaining = [ms[m] for m in nums if m not in chosen]
        cpu = LOCAL_OVERHEAD_MS + (1 - LOCAL_P_FAIL) * acc + LOCAL_P_FAIL * acc_fail
        if LOCAL_FAIL_KEEPS_ONE:
            # a run that finds a failure still drops the passing tests: assume it keeps
            # the most expensive one of the locally run tests
            worst = max(ms[m] for m in chosen)
            saving = ((1 - LOCAL_P_FAIL) * (base - pack_cost(remaining))
                      + LOCAL_P_FAIL * (base - pack_cost(remaining + [worst])) - cpu)
        else:
            saving = (1 - LOCAL_P_FAIL) * (base - pack_cost(remaining)) - cpu
        if saving > best + 100:
            best, best_k = saving, k
    return [n for _, n in cand[:best_k]]


# ---------------------------------------------------------------- packing
def pack(items):
    """items: list of (ms, number). -> list of workers minimizing max + 2s*k (LPT per k)."""
    if not items:
        return []
    items = sorted(items, key=lambda x: (-x[0], x[1]))  # ties by test number: deterministic output
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


def tpc_plans(mp, qbits, nums, by_id, ms, allow_resume=False):
    """{test number: (test id, query indices, est ms, compile-only indices)}.

    The estimate is the share of the test's work the selected items represent (a ratio of
    CPU measured in one profiling process, so machine speed and load cancel out) times the
    test's tests.json duration. No timing of this machine enters a decision."""
    out = {}
    heavy = mp.index.get("heavy", {})
    if not heavy or not os.path.isdir(TPC_DIR):
        return out
    for tid, h in heavy.items():
        n = by_id.get(tid)
        if n is None or n not in nums:
            continue
        sel = qbits.get(tid)
        if not sel or sel.get("bad") or not sel.get("q"):
            continue
        q = sorted(sel["q"] | sel["run"] | set(h.get("always", [])))
        cc = h.get("cpu_compile") or [None] * len(h["cpu"])
        compile_only = [i for i in q if i not in sel["run"] and cc[i] is not None]
        full = [i for i in q if i not in compile_only]
        names = set()
        for i in full:
            names.update(h["tables"][i])
        for i in compile_only:
            names.update(h["tables"][i])  # find_tables / ensure_tables still see them
        ds = "tpch" if "tpch" in tid else "tpcds"
        mb = sum(mp.index.get("table_bytes", {}).get(f"{ds}/{t}", 0) for t in names) / 1e6
        whole = sum(c for c in h["cpu"] if c) or 1.0
        # compile-only queries whose change reaches only optimizer rules (no glue) can be
        # checked by re-running those rules from saved inputs
        resume = {}
        hr = h.get("rules") or [None] * len(h["cpu"])
        if allow_resume:
            for i in compile_only:
                ks = sel.get("rules", {}).get(i)
                ok = hr[i] or {}
                if ks and i not in sel.get("glue", set()) and all(str(k) in ok for k in ks):
                    resume[i] = sorted(ks)
        share = (sum(h["cpu"][i] for i in full)
                 + sum(cc[i] for i in compile_only if i not in resume)
                 + sum(sum(hr[i][str(k)] for k in resume[i]) for i in resume)) / whole
        est = TPC_FACTOR * ms[n] * share + TPC_LOAD_MS_PER_MB * mb + 50
        # worst case: every compile-only query also runs fully (its plan changed)
        worst = TPC_FACTOR * ms[n] * sum(h["cpu"][i] for i in q) / whole + TPC_LOAD_MS_PER_MB * mb + 50
        out[n] = (tid, q, est, compile_only, worst, resume)
    return out


def code_fingerprint(co):
    """Digest of a code object and everything nested in it, without line numbers or file
    names: two sources with equal fingerprints compile to the same program."""
    import hashlib
    import types

    h = hashlib.sha256()

    def const(k):
        # frozenset constants (from `x in {...}`) iterate in hash order, which differs
        # between processes: compare them by their sorted elements
        if isinstance(k, (frozenset, set)):
            return ("set", tuple(sorted(repr(const(x)) for x in k)))
        if isinstance(k, tuple):
            return ("tuple", tuple(const(x) for x in k))
        return (type(k).__name__, repr(k))

    def walk(c):
        for part in (c.co_code, c.co_exceptiontable):
            h.update(len(part).to_bytes(4, "little"))
            h.update(part)
        h.update(repr((c.co_name, c.co_qualname, c.co_argcount, c.co_posonlyargcount,
                       c.co_kwonlyargcount, c.co_nlocals, c.co_stacksize, c.co_flags,
                       c.co_names, c.co_varnames, c.co_freevars, c.co_cellvars)).encode())
        for k in c.co_consts:
            if isinstance(k, types.CodeType):
                h.update(b"<code>")
                walk(k)
            else:
                h.update(repr(const(k)).encode())

    walk(co)
    return h.hexdigest()


def patched_sources(info):
    """New full text of every patched .py file. -> (dict, problem or None)."""
    patched = {}
    for path, hunks in info["files"].items():
        try:
            with open(os.path.join(SQLGLOT, path), encoding="utf-8") as f:
                new = apply_hunks(f.read(), hunks)
        except OSError:
            return None, None
        if new is None:
            return None, None  # does not apply to the pinned tree: no local work
        try:
            co = compile(new, path, "exec", dont_inherit=True)
        except (SyntaxError, ValueError) as e:
            return patched, f"{path} does not compile: {e!r}"[:200]
        patched[path] = new
        info.setdefault("fingerprints", {})[path] = code_fingerprint(co)
    return patched, None


# Off: in a fresh out-of-sample check, sentinels that only reach a change statically
# (they import the module) passed while the heavy test failed. Kept for experiments.
SENTINEL = os.environ.get("BR_SENTINEL", "0") == "1"
SENTINEL_HEAVY_MS = 2000  # tests at least this long may be dropped on passing sentinels
SENTINEL_MIN = 3  # cheap tests that must cover each change block reaching the heavy test


def sentinel_plan(info, nums, ms, idof, num_bit, L, tpc, total_bits):
    """Heavy tests that can be dropped if cheaper tests covering the same changes all pass.

    -> {heavy test: set of sentinel tests}. Every change block that reaches the heavy test
    needs SENTINEL_MIN cheap, locally runnable tests reaching the same block."""
    if not SENTINEL or not info.get("local_ok") or any(p.startswith("tests/") for p in info["files"]):
        return {}
    blocks = info.get("blocks", [])
    union = 0
    for b in blocks:
        union |= b
    if (union | info.get("data_bits", 0)) != total_bits:
        return {}  # some selected tests are not explained by a recorded block
    runnable = {n for n in nums if n in tpc or not idof[n].startswith(HEAVY_CLASSES)}
    out = {}
    for h in nums:
        if ms[h] < SENTINEL_HEAVY_MS or h in L:
            continue
        hb = num_bit.get(h)
        if hb is None or (info.get("data_bits", 0) >> hb) & 1:
            continue
        reach = [b for b in blocks if (b >> hb) & 1]
        if not reach:
            continue
        sent = set()
        for b in reach:
            cands = sorted((n for n in runnable if n != h and ms[n] < SENTINEL_HEAVY_MS
                            and num_bit.get(n) is not None and (b >> num_bit[n]) & 1),
                           key=lambda n: ms[n])
            if len(cands) < SENTINEL_MIN:
                sent = None
                break
            sent.update(cands[:max(SENTINEL_MIN, 5)])
        if sent:
            cost = sum(tpc[n][2] if n in tpc else LOCAL_FACTOR * ms[n] for n in sent if n not in L)
            if cost < ms[h]:
                out[h] = sent
    return out


def refine_local(info, nums, ms, idof, tpc=None, patched=None, allow_tests=True, num_bit=None, total_bits=0):
    """-> (selection, import_error or None, estimated ms of local work planned)."""
    tpc = tpc or {}
    if any(p.startswith("tests/") for p in info["files"]):
        tpc = {}  # item models mirror the unpatched test code
    data_bits = info.get("data_bits", 0)
    exclude = {n for n in nums if num_bit and n in num_bit and (data_bits >> num_bit[n]) & 1}
    L = plan_local(nums, ms, idof, {n: v[2] for n, v in tpc.items() if n not in exclude},
                   {n: TPC_FAIL_FRACTION * v[2] for n, v in tpc.items() if n not in exclude},
                   exclude) if allow_tests else []
    sentinels = sentinel_plan(info, nums, ms, idof, num_bit or {}, set(L), tpc, total_bits) if allow_tests else {}
    if sentinels:
        extra = set().union(*sentinels.values()) - set(L)
        L = list(L) + sorted(extra)
    import_mods = [_modname(p) for p in info.get("import_check", []) if p in (patched or {})]
    if not L and not import_mods:
        return nums, None, 0
    if patched is None:
        return nums, None, 0
    _preload()
    est = LOCAL_OVERHEAD_MS + sum(tpc[n][2] if n in tpc else LOCAL_FACTOR * ms[n] for n in L)
    # outer safety kill only: sized for the worst case with a wide margin, so it fires only
    # for a stuck check (and then every test is kept)
    tpc_tasks = [(tpc[n][0], tpc[n][1], 60 + 5 * tpc[n][4] / 1000, tpc[n][3], tpc[n][5]) for n in L if n in tpc]
    ids = [idof[n] for n in L if n not in tpc]
    budgets = {idof[n]: max(10.0, 10 * ms[n] / 1000 + 5.0) for n in L if n not in tpc}  # stuck tests only
    wall = 20 + sum(budgets.values()) + sum(t[2] for t in tpc_tasks)
    import resource

    c0 = resource.getrusage(resource.RUSAGE_CHILDREN)
    res = run_local(patched, ids, budgets, wall, tpc_tasks, import_mods)
    c1 = resource.getrusage(resource.RUSAGE_CHILDREN)
    log(f"local cpu {(c1.ru_utime + c1.ru_stime - c0.ru_utime - c0.ru_stime):.3f}s, "
        f"recorded {sum(ms[n] for n in L) / 1000:.3f}s, estimated {est / 1000:.3f}s, "
        f"models {len(tpc_tasks)}, imports {len(import_mods)}")
    if res is None:
        return nums, None, est  # the child failed or was too slow: keep every test
    if "import_error" in res:
        return nums, res["import_error"], est
    failed = res["failed"]
    keep = set(nums) - set(L)
    num_of = {idof[n]: n for n in L}
    keep.update(num_of[t] for t in failed if t in num_of)
    failed_nums = {num_of[t] for t in failed if t in num_of}
    for h, sent in sentinels.items():
        if sent & failed_nums or not sent <= set(L):
            continue  # a sentinel failed: the change is live, keep the heavy test
        keep.discard(h)
        log(f"sentinel: {len(sent)} covering tests passed, dropping {idof[h]}")
    log(f"local: ran {len(L)} tests ({len(tpc_tasks)} by TPC queries), {len(failed)} failed, "
        f"kept {len(keep)} of {len(nums)}")
    return keep, None, est


def main():
    with open(os.path.join(DATA, "tests.json")) as f:
        tj = json.load(f)
    tests = tj["tests"]
    by_id = {t["id"]: t["number"] for t in tests}
    ms = {t["number"]: round(t["seconds"] * 1000) for t in tests}
    mp = Map(MAP_DIR)
    ids = mp.index["tests"]
    bit2num = [by_id.get(t) for t in ids]
    # pseudo-tests "@tpc:<test>#<query>|setup|unmodelled" -> (test, query index or None)
    pseudo = {}
    for i, t in enumerate(ids):
        if t.startswith("@tpc:"):
            tid, _, q = t[5:].rpartition("#")
            if q.isdigit():
                pseudo[i] = (tid, int(q), False)
            elif q.endswith("R") and q[:-1].isdigit():
                pseudo[i] = (tid, int(q[:-1]), True)  # reaches the run phase
            elif q.endswith("G") and q[:-1].isdigit():
                pseudo[i] = (tid, int(q[:-1]), "G")  # glue: front or compile outside rules
            elif "K" in q and q.split("K")[0].isdigit() and q.split("K")[1].isdigit():
                pseudo[i] = (tid, int(q.split("K")[0]), ("K", int(q.split("K")[1])))
            else:
                pseudo[i] = (tid, None, False)
    known = set(n for n in bit2num if n is not None)
    always = [t["number"] for t in tests if t["number"] not in known]
    if always:
        log(f"{len(always)} tests without trace data, always selected")

    idof = {t["number"]: t["id"] for t in tests}
    num_bit = {n: i for i, n in enumerate(bit2num) if n is not None}

    def decode(bits):
        nums = set(always)
        qbits = {}
        i = 0
        while bits:
            if bits & 1:
                nm = bit2num[i]
                if nm is not None:
                    nums.add(nm)
                elif i in pseudo:
                    tid, q, run = pseudo[i]
                    e = qbits.setdefault(tid, {"q": set(), "run": set(), "bad": False,
                                               "glue": set(), "rules": {}})
                    if q is None:
                        e["bad"] = True
                    elif run == "G":
                        e["glue"].add(q)
                        e["q"].add(q)
                    elif isinstance(run, tuple):
                        e["rules"].setdefault(q, set()).add(run[1])
                        e["q"].add(q)
                    elif run:
                        e["run"].add(q)
                    else:
                        e["q"].add(q)
            bits >>= 1
            i += 1
        return nums, qbits
    import time

    local_enabled = os.environ.get("BR_LOCAL", "1") == "1" and os.path.isdir(SQLGLOT)
    cdir = os.path.join(DATA, "commits")
    out = []
    n_commits = sum(1 for x in os.listdir(cdir) if x.endswith(".patch"))
    # budget for local work in *estimated* CPU (tests.json seconds, recorded check costs):
    # the same batch always gets the same checks, whatever the machine's speed or load
    est_budget = 1000 * min(LOCAL_WALL_MAX, LOCAL_WALL_PER_COMMIT * n_commits)
    est_used = 0
    for name in sorted(os.listdir(cdir)):
        if not name.endswith(".patch"):
            continue
        cid = name[: -len(".patch")]
        info, qbits = {}, {}
        total_bits = 0
        try:
            with open(os.path.join(cdir, name), encoding="utf-8", errors="replace") as f:
                text = f.read()
            bits = select(mp, text, info)
            total_bits = bits
            nums, qbits = decode(bits)
        except Exception as e:  # never fail: run everything
            log(f"{cid}: error {e!r}, selecting all")
            nums = set(ms)
        if info.get("force_all"):
            log(f"{cid}: test configuration or module set changed, selecting all")
            nums = set(ms)
        elif info.get("files"):
            try:
                patched, problem = patched_sources(info)
            except Exception as e:  # noqa: BLE001
                patched, problem = None, None
                log(f"{cid}: could not rebuild patched sources ({e!r})")
            same = {p for p, fp in info.get("fingerprints", {}).items()
                    if not problem and mp.index.get("code", {}).get(p) == fp}
            if same:
                # these files compile to exactly the same program: select as if untouched
                try:
                    info2 = {}
                    bits = select(mp, text, info2, skip=same)
                    total_bits = bits
                    nums, qbits = decode(bits)
                    info2["fingerprints"] = info.get("fingerprints", {})
                    info = info2
                    log(f"{cid}: {sorted(same)} compile to the unpatched program")
                except Exception as e:  # noqa: BLE001
                    log(f"{cid}: re-selection failed ({e!r}), keeping the full selection")
            if problem:
                log(f"{cid}: {problem}; selecting all")
                nums = set(ms)
            elif local_enabled and patched is not None:
                in_budget = est_used < est_budget
                if in_budget or info.get("import_check"):
                    try:
                        nums, import_error, spent = refine_local(
                            info, nums, ms, idof,
                            tpc_plans(mp, qbits, nums, by_id, ms,
                                      allow_resume=not info.get("import_check") and not any(
                                          p == "sqlglot/optimizer/optimizer.py" for p in info.get("files", {}))),
                            patched,
                            allow_tests=bool(nums) and info.get("local_ok") and in_budget,
                            num_bit=num_bit, total_bits=total_bits)
                        est_used += spent
                        if import_error:
                            log(f"{cid}: {import_error}; selecting all")
                            nums = set(ms)
                    except Exception as e:  # noqa: BLE001
                        log(f"{cid}: local run skipped ({e!r})")
        workers = pack([(ms[n], n) for n in nums])
        out.append(json.dumps({"commit": cid, "workers": workers}, separators=(",", ":")))
    sys.stdout.write("\n".join(out) + ("\n" if out else ""))


def fallback():
    """Last resort if main() fails: every commit runs the whole suite (valid output)."""
    with open(os.path.join(DATA, "tests.json")) as f:
        tests = json.load(f)["tests"]
    workers = pack([(round(t["seconds"] * 1000), t["number"]) for t in tests])
    lines = []
    for name in sorted(os.listdir(os.path.join(DATA, "commits"))):
        if name.endswith(".patch"):
            lines.append(json.dumps({"commit": name[: -len(".patch")], "workers": workers}, separators=(",", ":")))
    sys.stdout.write("\n".join(lines) + ("\n" if lines else ""))


if __name__ == "__main__":
    try:
        main()
    except BaseException as e:  # noqa: BLE001  (nothing has been written to stdout yet)
        log(f"selector failed ({e!r}); falling back to the whole suite")
        fallback()
