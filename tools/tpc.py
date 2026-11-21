"""Per-item models of sqlglot's heavy loop tests.

Items: the TPC-H / TPC-DS queries of tests/test_executor.py, and the dialects
of TestGenerator.test_generator_memory (one independent subtest per dialect).

The heavy tests in tests/test_executor.py run every TPC query through sqlglot
and compare against DuckDB. Each query is independent, so a change that only
touches code reached by a few queries can only change those queries.

Build time  (python tpc.py profile <sqlglot_root> <out_dir>)
  For every query, in a fresh process, record the sqlglot lines it executes
  and the outputs the unpatched code produces: the DuckDB SQL that sqlglot
  generates (DuckDB's answer depends only on that) and a digest of the result
  table from sqlglot's executor. Table data is stored pre-parsed.

Run time  (check(...) inside the selector's forked child, patched sources)
  Re-run only the affected queries and compare with the stored outputs.
  Identical outputs mean the test behaves as it did unpatched (it passed);
  anything else, including an exception, counts as a failure.
"""

import hashlib
import json
import os
import pickle
import sys
import time

TESTS = {
    # test id: (dataset, mode)
    "tests/test_executor.py::TestExecutor::test_execute_tpcds": ("tpcds", "execute"),
    "tests/test_executor.py::TestExecutor::test_execute_tpch": ("tpch", "execute"),
    "tests/test_executor.py::TestExecutor::test_optimized_tpch": ("tpch", "optimized"),
    "tests/test_generator.py::TestGenerator::test_generator_memory": ("dialects", "genmem"),
}
FIXTURE = {"tpch": "optimizer/tpc-h/tpc-h.sql", "tpcds": "optimizer/tpc-ds/tpc-ds.sql"}
DATA_DIR = {"tpch": "optimizer/tpc-h/", "tpcds": "optimizer/tpc-ds/"}


def calib():
    """CPU seconds of a fixed pure-Python workload (best of 3).

    Costs are stored relative to this, so estimates follow the speed and load of the
    machine they run on rather than the build machine's. selector.py has the same loop."""
    best = float("inf")
    for _ in range(3):
        t0 = time.process_time()
        x = 0
        for i in range(200000):
            x += i * i
        best = min(best, time.process_time() - t0)
    return best


def _digest(obj):
    return hashlib.sha256(pickle.dumps(obj, protocol=4)).hexdigest()


def _schema(ds):
    from tests import helpers

    return helpers.TPCH_SCHEMA if ds == "tpch" else helpers.TPCDS_SCHEMA


def _queries(ds):
    if ds == "dialects":
        from sqlglot.dialects import Dialects

        return [({}, d.value, None) for d in Dialects]
    from tests.helpers import load_sql_fixture_pairs

    return list(load_sql_fixture_pairs(FIXTURE[ds]))


def _genmem(name):
    """Body of one test_generator_memory subtest (tests/test_generator.py)."""
    import gc
    import tracemalloc

    from sqlglot.dialects import Dialect, Dialects

    dialect = Dialect.get_or_raise(Dialects(name))
    for _ in range(100):
        dialect.generator()
    gc.collect()
    tracemalloc.start()
    try:
        before = tracemalloc.get_traced_memory()[0]
        for _ in range(1000):
            dialect.generator()
        gc.collect()
        after = tracemalloc.get_traced_memory()[0]
    finally:
        tracemalloc.stop()
    if not after - before < 1024:
        raise AssertionError(f"{name}: {after - before} bytes")
    return {"ok": True}


class _Stop(Exception):
    def __init__(self, plan):
        self.plan = plan


def plan_digest(plan):
    """Canonical, content-based digest of a planner.Plan.

    Each step hashes its class, its attributes (expressions by class, args, type and
    comments) and the sorted hashes of the steps it depends on, so the digest does not
    depend on object identity or set iteration order. `dependents` is the inverse of
    `dependencies` and is left out."""
    import enum

    import sqlglot.expressions as exp
    from sqlglot.planner import Step

    Expr = getattr(exp, "Expr", None) or exp.Expression

    def c(v):
        if isinstance(v, Step):
            return ("step", h(v))
        if isinstance(v, Expr):
            args = tuple((k, c(x)) for k, x in sorted(v.args.items()))
            t = v.__dict__.get("_type")
            return ("e", type(v).__qualname__, args, c(t) if t is not None else None, tuple(v.comments or ()))
        if isinstance(v, dict):
            return ("d", tuple(sorted(((repr(c(k)), c(x)) for k, x in v.items()), key=lambda kv: kv[0])))
        if isinstance(v, (set, frozenset)):
            return ("s", tuple(sorted(repr(c(x)) for x in v)))
        if isinstance(v, (list, tuple)):
            return ("l", tuple(c(x) for x in v))
        if isinstance(v, type):
            return ("t", v.__module__, v.__qualname__)
        if isinstance(v, enum.Enum):
            return ("n", type(v).__qualname__, v.name)
        if isinstance(v, (str, int, float, bool, type(None))):
            return ("v", type(v).__name__, repr(v))
        raise TypeError(f"unexpected value in plan: {type(v).__qualname__}")

    memo = {}

    def h(st):
        k = id(st)
        if k not in memo:
            memo[k] = None  # cycle guard (plans are DAGs)
            attrs = tuple((name, c(x)) for name, x in sorted(vars(st).items()) if name != "dependents")
            memo[k] = hashlib.sha256(repr((type(st).__qualname__, attrs)).encode()).hexdigest()
        if memo[k] is None:
            raise ValueError("cycle in plan")
        return memo[k]

    body = (h(plan.root), c(plan.expression), c(plan.ctes))
    return hashlib.sha256(repr(body).encode()).hexdigest()


def run_one(ds, mode, meta, sql, optimized, tables, compile_only=False, phase=None):
    build_marks = phase is not None
    """Mirror what the test does for one query. Returns a comparable record.

    compile_only: stop at the executor's plan (replacing PythonExecutor), for changes that
    only reach code running before it. phase: build-time hook marking front/compile/run.
    """
    from sqlglot import find_tables, parse_one, transpile
    import sqlglot.executor as ex

    phase = phase or (lambda name: None)
    phase("front")
    if mode == "genmem":
        return _genmem(sql)
    if mode == "optimized":
        # test_optimized_tpch: DuckDB runs sqlglot's DuckDB SQL for both statements
        return {"a": transpile(sql, write="duckdb")[0], "b": transpile(optimized, write="duckdb")[0]}

    # test_execute_*: parse in the parent, pickle to a pool worker
    phase("front")
    expression = pickle.loads(pickle.dumps(parse_one(sql)))
    if not meta.get("execute"):
        return {"noexec": True}
    needed = {t.name: tables[t.name] for t in find_tables(expression)}
    duck = transpile(sql, write="duckdb")[0]  # cached_execute: DuckDB's answer depends on it
    if compile_only:
        real = ex.PythonExecutor

        class Stub:
            def __init__(self, *a, **k):
                pass

            def execute(self, plan):
                raise _Stop(plan)

        ex.PythonExecutor = Stub
        try:
            ex.execute(expression, schema=_schema(ds), tables=needed)
            raise RuntimeError("executor stub not reached")
        except _Stop as stop:
            return {"duck": duck, "plan": plan_digest(stop.plan), "tables": sorted(needed)}
        finally:
            ex.PythonExecutor = real
    # build-time phase marks. "compile" is only what runs inside optimize() and Plan(): its
    # whole output is the plan the digest covers. Everything else in execute() (tables and
    # schema preparation, logging, PythonExecutor) and the result pickling is "run", since
    # it can reach the executor outside the plan; a change there needs a full run.
    real_opt, real_plan = ex.optimize, ex.Plan
    captured = []

    def opt(*a, **k):
        phase("compile")
        try:
            return real_opt(*a, **k)
        finally:
            phase("run")

    def plan_(*a, **k):
        phase("compile")
        try:
            p = real_plan(*a, **k)
            captured.append(p)
            return p
        finally:
            phase("run")

    if build_marks:
        ex.optimize, ex.Plan = opt, plan_
    try:
        phase("run")
        table = ex.execute(expression, schema=_schema(ds), tables=needed)
        table = pickle.loads(pickle.dumps(table))  # result comes back to the parent
    finally:
        ex.optimize, ex.Plan = real_opt, real_plan
    phase("front")
    rec = {"duck": duck, "cols": list(table.columns), "rows": _digest(table.rows), "tables": sorted(needed)}
    if captured:
        phase(None)
        rec["plan"] = plan_digest(captured[-1])
    return rec


# ---------------------------------------------------------------- tables
def _read_csv(root, ds, table):
    """Same parsing as TestExecutor.setUpClass."""
    import ast
    import csv
    import gzip

    path = os.path.join(root, "tests", "fixtures", DATA_DIR[ds], f"{table}.csv.gz")
    with gzip.open(path, "rt", newline="") as f:
        reader = csv.reader(f, delimiter="|")
        next(reader)
        rows, ctypes = [], []
        for row in reader:
            if not ctypes:
                for v in row:
                    try:
                        ctypes.append(type(ast.literal_eval(v)))
                    except (ValueError, SyntaxError):
                        ctypes.append(str)
            rows.append(tuple(None if (t is not str and v == "") else t(v) for t, v in zip(ctypes, row)))
    return rows


def load_tables(data_dir, ds, names):
    from sqlglot.executor.table import Table

    schema = _schema(ds)
    out = {}
    for name in names:
        with open(os.path.join(data_dir, ds, name + ".pkl"), "rb") as f:
            rows = pickle.load(f)
        out[name] = Table(columns=schema[name], rows=rows)
    return out


# ---------------------------------------------------------------- run time
def check(data_dir, test_id, indices, budget, compile_only=()):
    """True if every listed query reproduces its stored outputs.

    Queries in compile_only are checked up to the executor's plan."""
    with open(os.path.join(data_dir, "expect.json")) as f:
        prof = json.load(f)[test_id]
    if prof.get("mode") == "optimizer":
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import opt_model

        return opt_model.check(test_id.rsplit("::", 1)[1], prof["items"], indices, prof["expect"])
    ds, mode = TESTS[test_id]
    queries = _queries(ds)
    names = set()
    for i in indices:
        names.update(prof["queries"][i].get("tables", []))
    tables = load_tables(data_dir, ds, sorted(names)) if names else {}
    compile_only = set(compile_only)
    for i in indices:
        meta, sql, optimized = queries[i]
        exp_rec = prof["queries"][i]["out"]
        if i in compile_only and "plan" in (exp_rec or {}):
            rec = run_one(ds, mode, meta, sql, optimized, tables, compile_only=True)
            if rec == {"duck": exp_rec["duck"], "plan": exp_rec["plan"], "tables": exp_rec["tables"]}:
                continue
            if rec.get("duck") != exp_rec["duck"]:
                return False  # DuckDB would compute a different expected answer
            # the plan changed but the test only compares results: run this query fully
            rec = run_one(ds, mode, meta, sql, optimized, tables)
            if {k: v for k, v in rec.items() if k != "plan"} != {k: v for k, v in exp_rec.items() if k != "plan"}:
                return False
        else:
            rec = run_one(ds, mode, meta, sql, optimized, tables)
            if {k: v for k, v in rec.items() if k != "plan"} != {k: v for k, v in exp_rec.items() if k != "plan"}:
                return False
        # no deadline here: the outcome must depend only on outputs. The selector's outer
        # safety kill (sized for every query running fully) still keeps the test.
    return True


# ---------------------------------------------------------------- build time
_types = None


_HOOKED = False


def _hook_classes():
    """Record sqlglot classes instantiated (same families as tools/br_trace.py)."""
    global _HOOKED
    if _HOOKED:
        return
    _HOOKED = True
    import sqlglot.expressions as exp
    from sqlglot import generator, parser, tokens
    from sqlglot.dialects.dialect import Dialect, Dialects

    for d in Dialects:
        if d.value:
            try:
                Dialect.get_or_raise(d.value)
            except Exception:
                pass
    bases = [b for b in (getattr(exp, "Expr", None), getattr(exp, "Expression", None)) if isinstance(b, type)]
    bases += [Dialect, tokens.Tokenizer, parser.Parser, generator.Generator]
    try:
        from sqlglot.schema import Schema

        bases.append(Schema)
    except Exception:
        pass
    seen = set()
    for b in bases:
        stack = [b]
        while stack:
            c = stack.pop()
            stack.extend(c.__subclasses__())
            if "__init__" in c.__dict__ and c not in seen:
                seen.add(c)
                orig = c.__dict__["__init__"]

                def make(orig):
                    def __init__(self, *a, **k):
                        if _types is not None:
                            _types.add(type(self))
                        return orig(self, *a, **k)

                    return __init__

                try:
                    setattr(c, "__init__", make(orig))
                except Exception:
                    pass


def _classes(types):
    out = set()
    for t in types:
        for c in t.__mro__:
            if c.__module__.startswith("sqlglot"):
                out.add(f"{c.__module__}.{c.__qualname__}")
    return sorted(out)


def _trace_task(args):
    root, data_dir, test_id, i = args
    from sys import monitoring as M

    ds, mode = TESTS[test_id]
    prefix = os.path.join(root, "sqlglot") + "/"
    queries = _queries(ds)
    meta, sql, optimized = queries[i]
    schema = _schema(ds)
    tables = load_tables(data_dir, ds, list(schema)) if mode == "execute" and meta.get("execute") else {}
    global _types
    buckets = {k: ({}, set()) for k in ("front", "compile", "run")}
    cur = [None]

    def cb(code, line):
        fn = code.co_filename
        b = cur[0]
        if b is not None and fn.startswith(prefix) and code.co_flags & 1:
            b[0].setdefault(fn[len(root) + 1:], set()).add(line)
        return M.DISABLE

    def phase(name):
        global _types
        cur[0] = buckets[name] if name else None
        _types = cur[0][1] if cur[0] is not None else None
        M.restart_events()

    _hook_classes()
    if M.get_tool(5) is None:
        M.use_tool_id(5, "tpc")
    M.register_callback(5, M.events.LINE, cb)
    M.register_callback(5, M.events.PY_START, lambda code, off: cb(code, code.co_firstlineno))
    t0 = time.process_time()
    M.set_events(5, M.events.LINE | M.events.PY_START)
    try:
        rec = run_one(ds, mode, meta, sql, optimized, tables, phase=phase)
        err = None
    except Exception as e:  # noqa: BLE001
        rec, err = None, repr(e)
    M.set_events(5, 0)
    phase(None)
    lines = {}
    types = set()
    for ls, ts in buckets.values():
        for f, v in ls.items():
            lines.setdefault(f, set()).update(v)
        types |= ts
    run_lines, run_types = buckets["run"]
    traced = time.process_time() - t0
    # untraced timing for the cost model
    t0 = time.process_time()
    if rec is not None:
        run_one(ds, mode, meta, sql, optimized, tables)
    cpu = time.process_time() - t0
    t0 = time.process_time()
    if rec is not None and "plan" in rec:
        run_one(ds, mode, meta, sql, optimized, tables, compile_only=True)
    cpu_compile = time.process_time() - t0 if rec is not None and "plan" in rec else None
    unit = calib()
    cpu, cpu_compile = cpu / unit, (cpu_compile / unit if cpu_compile is not None else None)
    used = []
    if mode == "execute" and meta.get("execute"):
        from sqlglot import find_tables, parse_one

        used = sorted({t.name for t in find_tables(parse_one(sql))})
    return test_id, i, {
        "out": rec,
        "err": err,
        "unit": round(unit, 6),
        "cpu": round(cpu, 4),  # in calib() units
        "cpu_compile": round(cpu_compile, 4) if cpu_compile is not None else None,
        "run_lines": {f: sorted(v) for f, v in run_lines.items()},
        "run_classes": _classes(run_types),
        "traced": round(traced, 4),
        "tables": used,
        "classes": _classes(types),
        "lines": {f: sorted(v) for f, v in lines.items()},
    }


def _setup_task(args):
    """Coverage of the shared part: building Table objects from rows."""
    root, data_dir, ds = args
    from sys import monitoring as M

    prefix = os.path.join(root, "sqlglot") + "/"
    lines = {}

    def cb(code, line):
        fn = code.co_filename
        if fn.startswith(prefix) and code.co_flags & 1:
            lines.setdefault(fn[len(root) + 1:], set()).add(line)
        return M.DISABLE

    global _types
    _hook_classes()
    if M.get_tool(5) is None:
        M.use_tool_id(5, "tpc")
    M.register_callback(5, M.events.LINE, cb)
    M.register_callback(5, M.events.PY_START, lambda code, off: cb(code, code.co_firstlineno))
    _types = set()
    M.set_events(5, M.events.LINE | M.events.PY_START)
    if ds == "dialects":
        _queries(ds)  # iterating Dialects
    else:
        tables = load_tables(data_dir, ds, list(_schema(ds)))
        pickle.loads(pickle.dumps(tables))  # initargs go to the pool workers
        from tests.helpers import load_sql_fixture_pairs

        list(load_sql_fixture_pairs(FIXTURE[ds]))
    M.set_events(5, 0)
    types, _types = _types, None
    out = {f: set(v) for f, v in lines.items()}
    if ds == "dialects":
        # the Dialects enum body decides which subtests exist: changing it needs the real test
        import ast as _ast

        rel = "sqlglot/dialects/dialect.py"
        tree = _ast.parse(open(os.path.join(root, rel)).read())
        for node in tree.body:
            if isinstance(node, _ast.ClassDef) and node.name == "Dialects":
                out.setdefault(rel, set()).update(range(node.lineno, node.end_lineno + 1))
    return ds, ({f: sorted(v) for f, v in out.items()}, [] if ds == "dialects" else _classes(types))


OPT_PREFIX = "tests/test_optimizer.py::TestOptimizer::"


def _opt_task(args):
    """Profile one TestOptimizer method: warm-up run, then a traced run per entry."""
    root, method = args
    from sys import monitoring as M

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import opt_model

    tid = OPT_PREFIX + method
    try:
        opt_model.load_module()  # imports are not part of what a check costs
        t0 = time.process_time()
        warm = opt_model.Harness().run(method)
        total = time.process_time() - t0
    except Exception as e:  # noqa: BLE001
        return tid, None, f"not modelled: {e!r}"
    if warm.failures:
        return tid, None, f"fails under the stand-ins: {sorted(map(str, warm.failures))[:3]}"

    global _types
    _hook_classes()
    prefix = os.path.join(root, "sqlglot") + "/"
    buckets = {}
    cur = [None]

    def cb(code, line):
        b = cur[0]
        if b is not None and code.co_filename.startswith(prefix) and code.co_flags & 1:
            b[0].setdefault(code.co_filename[len(root) + 1:], set()).add(line)
        return M.DISABLE

    def phase(key):
        global _types
        b = buckets.setdefault(key, ({}, set()))
        cur[0] = b
        _types = b[1]
        M.restart_events()

    if M.get_tool(5) is None:
        M.use_tool_id(5, "tpc")
    M.register_callback(5, M.events.LINE, cb)
    M.register_callback(5, M.events.PY_START, lambda code, off: cb(code, code.co_firstlineno))
    phase(opt_model.REST)
    M.set_events(5, M.events.LINE | M.events.PY_START)
    try:
        traced = opt_model.Harness(on_phase=phase).run(method)
    finally:
        M.set_events(5, 0)
        cur[0] = None
        _types = None
    if traced.failures or traced.keys != warm.keys:
        return tid, None, "traced run differs from warm-up run"
    unit = calib()
    items = [list(k) for k in warm.keys] + [opt_model.REST]
    recs = []
    for k in warm.keys + [opt_model.REST]:
        ls, ts = buckets.get(k, ({}, set()))
        cpu = warm.cpu.get(k, 0) if k != opt_model.REST else max(0.0, total - sum(warm.cpu.values()))
        cpu /= unit
        recs.append({
            "out": {"pairs": warm.pairs.get(k, [])},
            "cpu": round(cpu, 4),
            "cpu_compile": None,
            "tables": [],
            "classes": _classes(ts),
            "lines": {f: sorted(v) for f, v in ls.items()},
        })
    expect = {repr(k): v for k, v in warm.pairs.items() if k != opt_model.REST}
    return tid, {"mode": "optimizer", "items": items, "expect": expect, "queries": recs}, None


def _opt_setup_task(root):
    """Coverage of what setUpClass does besides the pool and DuckDB."""
    from sys import monitoring as M

    prefix = os.path.join(root, "sqlglot") + "/"
    lines = {}

    def cb(code, line):
        if code.co_filename.startswith(prefix) and code.co_flags & 1:
            lines.setdefault(code.co_filename[len(root) + 1:], set()).add(line)
        return M.DISABLE

    import sqlglot
    from sqlglot.schema import MappingSchema

    if M.get_tool(5) is None:
        M.use_tool_id(5, "tpc")
    M.register_callback(5, M.events.LINE, cb)
    M.register_callback(5, M.events.PY_START, lambda code, off: cb(code, code.co_firstlineno))
    M.set_events(5, M.events.LINE | M.events.PY_START)
    sqlglot.schema = MappingSchema()
    M.set_events(5, 0)
    return {f: sorted(v) for f, v in lines.items()}


def _selfcheck_task(args):
    """Each model must pass on the unpatched code, all items and every other item."""
    root, out_dir, tid, n, has_plan = args
    try:
        if has_plan:
            # compile-only on every query, full runs on a spread of them
            if not check(out_dir, tid, list(range(n)), 900, compile_only=list(range(n))):
                return tid, False
            return tid, check(out_dir, tid, list(range(0, n, 4)), 900)
        if not check(out_dir, tid, list(range(n)), 900):
            return tid, False
        return tid, check(out_dir, tid, list(range(1, n, 2)), 900)
    except Exception:  # noqa: BLE001
        return tid, False


def _init(root):
    os.chdir(root)
    sys.path.insert(0, root)


def opt_methods(root):
    import subprocess

    out = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0, sys.argv[1]); sys.path.insert(0, sys.argv[2]); import opt_model; "
         "print('\\n'.join(opt_model.methods(sys.argv[1])))",
         root, os.path.dirname(os.path.abspath(__file__))],
        capture_output=True, text=True, cwd=root, check=True,
    )
    return [m for m in out.stdout.split() if m.startswith("test")]


def profile(root, out_dir):
    import multiprocessing as mp

    sys.path.insert(0, root)
    os.chdir(root)
    # calib() on an otherwise idle machine, one process: the reference for run time
    idle_unit = min(calib() for _ in range(15))
    for ds in ("tpch", "tpcds"):
        os.makedirs(os.path.join(out_dir, ds), exist_ok=True)
        for table in _schema(ds):
            with open(os.path.join(out_dir, ds, table + ".pkl"), "wb") as f:
                pickle.dump(_read_csv(root, ds, table), f, protocol=4)
    tasks = []
    for tid, (ds, _) in TESTS.items():
        for i in range(len(_queries(ds))):
            tasks.append((root, out_dir, tid, i))
    ctx = mp.get_context("spawn")
    res = {tid: {"queries": [None] * len(_queries(ds))} for tid, (ds, _) in TESTS.items()}
    with ctx.Pool(4, initializer=_init, initargs=(root,), maxtasksperchild=1) as pool:
        for tid, i, rec in pool.imap_unordered(_trace_task, tasks):
            res[tid]["queries"][i] = rec
        setup = dict(pool.map(_setup_task, [(root, out_dir, ds) for ds in ("tpch", "tpcds", "dialects")]))
        opt_setup = pool.apply(_opt_setup_task, (root,))
    # optimizer per-entry models: off by default. On fresh out-of-sample commits they
    # traded simulated seconds for the same CPU and cost two minutes of build time.
    # Methods share worker processes; code that only runs once per process then shows
    # up as "unmodelled" for later methods, which keeps them conservative.
    opt_res = []
    if os.environ.get("BR_OPT_MODELS") == "1":
        with ctx.Pool(4, initializer=_init, initargs=(root,), maxtasksperchild=12) as pool:
            opt_res = list(pool.imap_unordered(_opt_task, [(root, m) for m in opt_methods(root)]))
    for tid, (ds, _) in TESTS.items():
        res[tid]["setup_lines"], res[tid]["setup_classes"] = setup[ds]
    skipped = []
    for tid, rec, why in opt_res:
        if rec is None:
            skipped.append((tid.rsplit("::", 1)[1], why))
            continue
        rec["setup_lines"], rec["setup_classes"] = opt_setup, []
        res[tid] = rec
    print(f"optimizer methods modelled: {sum(1 for _, r, _ in opt_res if r)}, skipped: {len(skipped)}",
          file=sys.stderr)
    for m, why in skipped[:12]:
        print(f"  skipped {m}: {why[:100]}", file=sys.stderr)
    bad = [(tid, q["err"]) for tid in res for q in res[tid]["queries"] if q.get("err")]
    if bad:
        raise SystemExit(f"TPC profile: unpatched queries failed: {bad[:3]}")
    def write_slim():
        slim = {}
        for tid, v in res.items():
            if not isinstance(v, dict):
                continue
            if v.get("mode") == "optimizer":
                slim[tid] = {"mode": "optimizer", "items": v["items"], "expect": v["expect"]}
            else:
                slim[tid] = {"queries": [{"out": q["out"], "tables": q["tables"]} for q in v["queries"]]}
        with open(os.path.join(out_dir, "expect.json"), "w") as f:
            json.dump(slim, f, separators=(",", ":"))

    # self-check in fresh processes: a model that does not reproduce the unpatched
    # outcome is dropped, so the selector never relies on it
    write_slim()
    jobs = [(root, out_dir, tid, len(v["queries"]),
             any(q.get("out") and "plan" in q["out"] for q in v["queries"])) for tid, v in res.items()]
    with ctx.Pool(4, initializer=_init, initargs=(root,), maxtasksperchild=1) as pool:
        verdicts = dict(pool.imap_unordered(_selfcheck_task, jobs))
    dropped = sorted(t for t, ok in verdicts.items() if not ok)
    for t in dropped:
        del res[t]
    print(f"self-check: {len(verdicts) - len(dropped)} models kept, dropped {[t.rsplit('::', 1)[1] for t in dropped]}",
          file=sys.stderr)
    res["@idle_unit"] = idle_unit
    with open(os.path.join(out_dir, "profile.json"), "w") as f:
        json.dump(res, f, separators=(",", ":"))
    print(f"calibration unit (idle, one process): {idle_unit * 1000:.2f} ms", file=sys.stderr)
    write_slim()
    unit = calib()
    for tid in [t for t in TESTS if t in res]:
        print(tid, "queries", len(res[tid]["queries"]),
              "cpu %.2f s (here)" % (unit * sum(q["cpu"] for q in res[tid]["queries"])), file=sys.stderr)


if __name__ == "__main__":
    if sys.argv[1] == "profile":
        profile(sys.argv[2], sys.argv[3])
