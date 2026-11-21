"""pytest plugin: per-test line coverage, file reads and class usage.

Loaded with `-p br_trace` from the sqlglot checkout (cwd = repo root).
Writes one JSON file per xdist worker into $BR_TRACE_OUT.

Recorded per test:
  lines   {file: [line, ...]}  lines executed in function-like code (not module/class bodies)
  files   [path, ...]          non-.py files opened under the repo (fixtures)
  dirs    [path, ...]          directories listed under the repo
  classes [qualname, ...]      sqlglot classes (full MRO) instantiated
Globally:
  import_lines {file: [line]}  lines executed by module / class bodies
"""

import importlib
import json
import os
import pkgutil
import sys

M = sys.monitoring
TOOL = 4
CO_OPTIMIZED = 0x1
ROOT = os.getcwd().rstrip("/") + "/"
OUT = os.environ.get("BR_TRACE_OUT", "/tmp/br_trace")

_cur_lines = None  # dict file -> set(lines) for the running test
_cur_files = None
_cur_dirs = None
_cur_types = None
_import_lines = {}
_import_func_lines = {}  # function code run outside any test (imports, metaclasses, collection)
_import_types = set()  # sqlglot classes instantiated outside any test
_results = {}


def _rel(path):
    if path.startswith(ROOT):
        return path[len(ROOT):]
    return None


def _line_cb(code, line):
    fn = code.co_filename
    if ROOT and fn.startswith(ROOT):
        rel = fn[len(ROOT):]
        if code.co_flags & CO_OPTIMIZED:
            if _cur_lines is not None:
                s = _cur_lines.get(rel)
                if s is None:
                    s = _cur_lines[rel] = set()
                s.add(line)
            else:
                s = _import_func_lines.get(rel)
                if s is None:
                    s = _import_func_lines[rel] = set()
                s.add(line)
        else:
            s = _import_lines.get(rel)
            if s is None:
                s = _import_lines[rel] = set()
            s.add(line)
    return M.DISABLE


def _start_cb(code, offset):
    # LINE events fire when execution moves to a new line, so code on a function's first
    # line (a one-line lambda, `def f(): return x`) would never be seen: record that line
    # when the code object starts.
    return _line_cb(code, code.co_firstlineno)


def _audit(event, args):
    if _cur_files is None:
        return
    if event == "open":
        p = args[0]
        if isinstance(p, int):
            return
        try:
            p = os.fsdecode(p)
        except Exception:
            return
        p = os.path.abspath(p)
        r = _rel(p)
        if r is not None and not r.endswith((".py", ".pyc")) and "__pycache__" not in r:
            _cur_files.add(r)
    elif event in ("os.listdir", "os.scandir", "glob.glob", "os.walk"):
        p = args[0] if args else "."
        try:
            p = os.fsdecode(p) if not isinstance(p, int) else None
        except Exception:
            p = None
        if p is None:
            return
        r = _rel(os.path.abspath(p))
        if r is not None:
            _cur_dirs.add(r)


def _hook_classes():
    """Record the type of every Expression / Dialect / Tokenizer / Parser / Generator built."""
    import sqlglot.expressions as exp
    from sqlglot import dialects, generator, parser, tokens
    from sqlglot.dialects.dialect import Dialect

    bases = []
    for name in ("Expr", "Expression"):
        b = getattr(exp, name, None)
        if isinstance(b, type):
            bases.append(b)
    bases += [Dialect, tokens.Tokenizer, parser.Parser, generator.Generator]
    try:
        from sqlglot.schema import Schema

        bases.append(Schema)
    except Exception:
        pass

    seen = set()
    for b in bases:
        # patch every __init__ defined on the base or any subclass, once
        stack = [b]
        while stack:
            c = stack.pop()
            stack.extend(c.__subclasses__())
            if "__init__" in c.__dict__ and c not in seen:
                seen.add(c)
                orig = c.__dict__["__init__"]
                if not callable(orig) or getattr(orig, "_br_hooked", False):
                    continue

                def make(orig):
                    def __init__(self, *a, **k):
                        (_cur_types if _cur_types is not None else _import_types).add(type(self))
                        return orig(self, *a, **k)

                    __init__.__wrapped__ = orig
                    __init__._br_hooked = True
                    return __init__

                try:
                    setattr(c, "__init__", make(orig))
                except Exception:
                    pass


def _import_everything():
    import sqlglot

    for info in pkgutil.walk_packages(sqlglot.__path__, prefix="sqlglot."):
        if info.name.endswith("__main__"):
            continue
        try:
            importlib.import_module(info.name)
        except Exception:
            pass
    # dialect classes are lazily loaded through Dialect registry; instances made here are
    # the tracer's doing, not sqlglot's import-time behaviour
    snapshot = set(_import_types)
    try:
        from sqlglot.dialects.dialect import Dialects, Dialect

        for d in Dialects:
            if d.value:
                try:
                    Dialect.get_or_raise(d.value)
                except Exception:
                    pass
    except Exception:
        pass
    _import_types.clear()
    _import_types.update(snapshot)


# --- comparison probes -------------------------------------------------------
# Every single comparison in sqlglot is compiled as a probe call on the same line. The
# probe returns the original result and records, per site, whether swapping the operator
# for its partner (is/==, is not/!=, </<=, >/>=) would have given the same result at
# every evaluation seen. A site is keyed by (file, line, column of the operator).
_CMP = {}  # site -> True (equivalent so far) / False (a difference or an unsafe operand)
_ORDER_SAFE = (int, float, str, bytes)


def _safe_order(v):
    t = type(v)
    if t in _ORDER_SAFE or t is bool:
        return True
    if t is tuple:
        return all(type(x) in _ORDER_SAFE or type(x) is bool for x in v)
    return False


def _mark(site, ok):
    if not ok:
        _CMP[site] = False
    elif site not in _CMP:
        _CMP[site] = True


def _p_is(site, a, b):
    r = a is b
    if _CMP.get(site) is not False:
        try:
            e = a == b
            _mark(site, type(e) is bool and e == r)
        except Exception:  # noqa: BLE001
            _mark(site, False)
    return r


def _p_isnot(site, a, b):
    r = a is not b
    if _CMP.get(site) is not False:
        try:
            e = a != b
            _mark(site, type(e) is bool and e == r)
        except Exception:  # noqa: BLE001
            _mark(site, False)
    return r


def _p_eq(site, a, b):
    r = a == b
    if _CMP.get(site) is not False:
        _mark(site, type(r) is bool and r == (a is b))
    return r


def _p_ne(site, a, b):
    r = a != b
    if _CMP.get(site) is not False:
        _mark(site, type(r) is bool and r == (a is not b))
    return r


def _order(op):
    def probe(site, a, b):
        r = op(a, b)
        if _CMP.get(site) is not False:
            try:
                _mark(site, _safe_order(a) and _safe_order(b) and not (a == b) and type(r) is bool)
            except Exception:  # noqa: BLE001
                _mark(site, False)
        return r

    return probe


import operator as _op  # noqa: E402

_PROBES = {"Is": "_p_is", "IsNot": "_p_isnot", "Eq": "_p_eq", "NotEq": "_p_ne",
           "Lt": "_p_lt", "LtE": "_p_le", "Gt": "_p_gt", "GtE": "_p_ge"}


def _install_probes():
    import ast
    import builtins
    import importlib.abc
    import importlib.machinery
    import importlib.util

    for name, fn in (("_p_is", _p_is), ("_p_isnot", _p_isnot), ("_p_eq", _p_eq), ("_p_ne", _p_ne),
                     ("_p_lt", _order(_op.lt)), ("_p_le", _order(_op.le)),
                     ("_p_gt", _order(_op.gt)), ("_p_ge", _order(_op.ge))):
        setattr(builtins, "_br" + name, fn)  # single underscore: no class-body mangling
    base = ROOT + "sqlglot/"

    class Rewrite(ast.NodeTransformer):
        def __init__(self, rel, lines):
            self.rel, self.lines = rel, lines

        def visit_Compare(self, node):
            self.generic_visit(node)
            if len(node.ops) != 1:
                return node
            kind = type(node.ops[0]).__name__
            if kind not in _PROBES:
                return node
            left, right = node.left, node.comparators[0]
            if not (left.end_lineno == right.lineno == node.lineno):
                return node
            col = _op_col(self.lines[node.lineno - 1], left.end_col_offset, right.col_offset)
            if col is None:
                return node
            site = f"{self.rel}:{node.lineno}:{col}"
            call = ast.Call(func=ast.Name(id="_br" + _PROBES[kind], ctx=ast.Load()),
                            args=[ast.Constant(site), left, right], keywords=[])
            return ast.copy_location(call, node)

    class Loader(importlib.machinery.SourceFileLoader):
        def source_to_code(self, data, path, *, _optimize=-1):
            src = data.decode("utf-8") if isinstance(data, bytes) else data
            tree = ast.parse(src, path)
            rel = path[len(ROOT):]
            tree = ast.fix_missing_locations(Rewrite(rel, src.splitlines()).visit(tree))
            return compile(tree, path, "exec", dont_inherit=True)

    class Finder(importlib.abc.MetaPathFinder):
        def find_spec(self, name, path=None, target=None):
            if name != "sqlglot" and not name.startswith("sqlglot."):
                return None
            spec = importlib.machinery.PathFinder.find_spec(name, path)
            if spec is None or not spec.origin or not spec.origin.startswith(base) or not spec.origin.endswith(".py"):
                return spec
            spec.loader = Loader(name, spec.origin)
            return spec

    sys.meta_path.insert(0, Finder())


def _op_col(line, start, end):
    """Column of the comparison operator: the first operator text after the left operand."""
    seg = line[start:end]
    hits = [seg.find(t) for t in ("is", "==", "!=", "<", ">") if seg.find(t) >= 0]
    return start + min(hits) if hits else None


_installed = False
_cur_test = None


def _install():
    global _installed
    if _installed:
        return
    _installed = True
    if os.environ.get("BR_PROBES") == "1":  # offline only: makes the suite ~5x slower
        _install_probes()
    M.use_tool_id(TOOL, "br_trace")
    M.register_callback(TOOL, M.events.LINE, _line_cb)
    M.register_callback(TOOL, M.events.PY_START, _start_cb)
    M.set_events(TOOL, M.events.LINE | M.events.PY_START)
    sys.addaudithook(_audit)
    import sqlglot  # noqa: F401  core modules, so instances made while importing the rest are seen

    _hook_classes()
    _import_everything()
    _hook_classes()  # classes defined by the lazily imported modules


def _child_record(test_id, payload):
    """Runs inside a pool worker: trace unpickling, fn, and result pickling."""
    import pickle

    _install()
    bucket = _new()
    _switch(bucket)
    M.restart_events()
    try:
        fn, args, kwargs = pickle.loads(payload)
        r = fn(*args, **kwargs)
        try:
            pickle.dumps(r)
        except Exception:
            pass
        return r
    finally:
        _switch((None, None, None, None))
        rec = _pack(bucket)
        rec["test"] = test_id
        os.makedirs(OUT, exist_ok=True)
        with open(os.path.join(OUT, f"child_{os.getpid()}.jsonl"), "a") as f:
            f.write(json.dumps(rec) + "\n")
        with open(os.path.join(OUT, f"cmp_{os.getpid()}.json"), "w") as f:
            json.dump(_CMP, f)


def _child_task(test_id, payload):
    return _child_record(test_id, payload)


def _wrap(fn, args, kwargs):
    import functools
    import pickle

    return functools.partial(_child_task, _cur_test, pickle.dumps((fn, args, kwargs)))


def _patch_pools():
    import concurrent.futures as cf
    import functools

    P = cf.ProcessPoolExecutor
    orig_submit = P.submit
    orig_init = P.__init__

    def submit(self, fn, /, *args, **kwargs):
        if _cur_test is not None:
            return orig_submit(self, _wrap(fn, args, kwargs))
        return orig_submit(self, fn, *args, **kwargs)

    def __init__(self, *a, **k):
        init = k.get("initializer")
        if init is not None and _cur_test is not None:
            k["initializer"] = _wrap(init, tuple(k.pop("initargs", ())), {})
        return orig_init(self, *a, **k)

    P.submit = submit
    P.__init__ = __init__


def pytest_configure(config):
    _install()
    _patch_pools()


def _qual(t):
    return f"{t.__module__}.{t.__qualname__}"


import pytest  # noqa: E402


def _new():
    return {}, set(), set(), set()


def _switch(bucket):
    global _cur_lines, _cur_files, _cur_dirs, _cur_types
    _cur_lines, _cur_files, _cur_dirs, _cur_types = bucket


def _pack(bucket):
    lines, files, dirs, types = bucket
    classes = set()
    for t in types:
        for c in t.__mro__:
            if c.__module__.startswith("sqlglot"):
                classes.add(_qual(c))
    return {
        "lines": {f: sorted(s) for f, s in lines.items()},
        "files": sorted(files),
        "dirs": sorted(dirs),
        "classes": sorted(classes),
    }


_buckets = {}


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_setup(item):
    _switch(_buckets[item.nodeid][0])
    yield


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    _switch(_buckets[item.nodeid][1])
    yield
    _switch(_buckets[item.nodeid][0])


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_teardown(item, nextitem):
    _switch(_buckets[item.nodeid][0])
    yield


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    global _cur_test
    _cur_test = item.nodeid
    _buckets[item.nodeid] = (_new(), _new())
    M.restart_events()
    yield
    _switch((None, None, None, None))
    _cur_test = None
    setup, call = _buckets.pop(item.nodeid)
    _results[item.nodeid] = {"setup": _pack(setup), "call": _pack(call)}


def pytest_unconfigure(config):
    os.makedirs(OUT, exist_ok=True)
    wid = os.environ.get("PYTEST_XDIST_WORKER", "main")
    with open(os.path.join(OUT, f"trace_{wid}.json"), "w") as f:
        json.dump(
            {
                "tests": _results,
                "import_lines": {k: sorted(v) for k, v in _import_lines.items()},
                "import_func_lines": {k: sorted(v) for k, v in _import_func_lines.items()},
                "cmp": _CMP,
                "import_types": sorted({_qual(c) for t in _import_types for c in t.__mro__
                                        if c.__module__.startswith("sqlglot")}),
            },
            f,
        )
