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
                if not callable(orig):
                    continue

                def make(orig):
                    def __init__(self, *a, **k):
                        if _cur_types is not None:
                            _cur_types.add(type(self))
                        return orig(self, *a, **k)

                    __init__.__wrapped__ = orig
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
    # dialect classes are lazily loaded through Dialect registry
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


_installed = False
_cur_test = None


def _install():
    global _installed
    if _installed:
        return
    _installed = True
    M.use_tool_id(TOOL, "br_trace")
    M.register_callback(TOOL, M.events.LINE, _line_cb)
    M.set_events(TOOL, M.events.LINE)
    sys.addaudithook(_audit)
    _import_everything()
    _hook_classes()


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
            },
            f,
        )
