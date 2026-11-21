"""Per-query model of sqlglot's TPC-H / TPC-DS executor tests.

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
}
FIXTURE = {"tpch": "optimizer/tpc-h/tpc-h.sql", "tpcds": "optimizer/tpc-ds/tpc-ds.sql"}
DATA_DIR = {"tpch": "optimizer/tpc-h/", "tpcds": "optimizer/tpc-ds/"}


def _digest(obj):
    return hashlib.sha256(pickle.dumps(obj, protocol=4)).hexdigest()


def _schema(ds):
    from tests import helpers

    return helpers.TPCH_SCHEMA if ds == "tpch" else helpers.TPCDS_SCHEMA


def _queries(ds):
    from tests.helpers import load_sql_fixture_pairs

    return list(load_sql_fixture_pairs(FIXTURE[ds]))


class _Stop(Exception):
    def __init__(self, plan):
        self.plan = plan


def plan_digest(plan):
    """Canonical digest of a planner.Plan: every step attribute and every expression's
    class, args, type and comments, with steps numbered in a deterministic walk."""
    import sqlglot.expressions as exp
    from sqlglot.planner import Step

    Expr = getattr(exp, "Expr", None) or exp.Expression
    ids = {}
    order = []
    stack = [plan.root]
    while stack:
        st = stack.pop()
        if id(st) in ids:
            continue
        ids[id(st)] = len(order)
        order.append(st)
        deps = sorted(st.dependencies, key=lambda d: (str(d.name), type(d).__name__))
        stack.extend(reversed(deps))

    def c(v):
        if isinstance(v, Step):
            return ("step", ids.get(id(v), -1))
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
        if isinstance(v, (str, int, float, bool, type(None))):
            return ("v", repr(v))
        return ("o", type(v).__qualname__, repr(sorted(vars(v).items())) if hasattr(v, "__dict__") else repr(v))

    steps = tuple((type(st).__qualname__, tuple((k, c(x)) for k, x in sorted(vars(st).items())))
                  for st in order)
    body = (c(plan.expression), c(plan.ctes), steps)
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
            return {"duck": duck, "plan": plan_digest(stop.plan)}
        finally:
            ex.PythonExecutor = real
    # build-time phase marks: everything compile_only also runs is "compile";
    # building and running PythonExecutor and pickling the result is "run"
    real_px = ex.PythonExecutor
    captured = []

    class Marked:
        def __init__(self, *a, **k):
            phase("run")
            self._real = real_px(*a, **k)

        def execute(self, plan):
            captured.append(plan)
            return self._real.execute(plan)

    if build_marks:
        ex.PythonExecutor = Marked
    try:
        phase("compile")
        table = ex.execute(expression, schema=_schema(ds), tables=needed)
        phase("run")
        table = pickle.loads(pickle.dumps(table))  # result comes back to the parent
    finally:
        ex.PythonExecutor = real_px
    phase("front")
    rec = {"duck": duck, "cols": list(table.columns), "rows": _digest(table.rows)}
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
    ds, mode = TESTS[test_id]
    with open(os.path.join(data_dir, "expect.json")) as f:
        prof = json.load(f)[test_id]
    queries = _queries(ds)
    names = set()
    for i in indices:
        names.update(prof["queries"][i].get("tables", []))
    tables = load_tables(data_dir, ds, sorted(names)) if names else {}
    deadline = time.monotonic() + budget
    compile_only = set(compile_only)
    for i in indices:
        meta, sql, optimized = queries[i]
        exp_rec = prof["queries"][i]["out"]
        if i in compile_only and "plan" in (exp_rec or {}):
            rec = run_one(ds, mode, meta, sql, optimized, tables, compile_only=True)
            if rec != {"duck": exp_rec["duck"], "plan": exp_rec["plan"]}:
                return False
        else:
            rec = run_one(ds, mode, meta, sql, optimized, tables)
            if {k: v for k, v in rec.items() if k != "plan"} != {k: v for k, v in exp_rec.items() if k != "plan"}:
                return False
        if time.monotonic() > deadline:
            return False
    return True


# ---------------------------------------------------------------- build time
_types = None


def _hook_classes():
    """Record sqlglot classes instantiated (same families as tools/br_trace.py)."""
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
    M.use_tool_id(5, "tpc")
    M.register_callback(5, M.events.LINE, cb)
    t0 = time.process_time()
    M.set_events(5, M.events.LINE)
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
    used = []
    if mode == "execute" and meta.get("execute"):
        from sqlglot import find_tables, parse_one

        used = sorted({t.name for t in find_tables(parse_one(sql))})
    return test_id, i, {
        "out": rec,
        "err": err,
        "cpu": round(cpu, 4),
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
    M.use_tool_id(5, "tpc")
    M.register_callback(5, M.events.LINE, cb)
    _types = set()
    M.set_events(5, M.events.LINE)
    tables = load_tables(data_dir, ds, list(_schema(ds)))
    pickle.loads(pickle.dumps(tables))  # initargs go to the pool workers
    from tests.helpers import load_sql_fixture_pairs

    list(load_sql_fixture_pairs(FIXTURE[ds]))
    M.set_events(5, 0)
    types, _types = _types, None
    return ds, ({f: sorted(v) for f, v in lines.items()}, _classes(types))


def _init(root):
    os.chdir(root)
    sys.path.insert(0, root)


def profile(root, out_dir):
    import multiprocessing as mp

    sys.path.insert(0, root)
    os.chdir(root)
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
        setup = dict(pool.map(_setup_task, [(root, out_dir, ds) for ds in ("tpch", "tpcds")]))
    for tid, (ds, _) in TESTS.items():
        res[tid]["setup_lines"], res[tid]["setup_classes"] = setup[ds]
    bad = [(tid, q["err"]) for tid in res for q in res[tid]["queries"] if q["err"]]
    if bad:
        raise SystemExit(f"TPC profile: unpatched queries failed: {bad[:3]}")
    with open(os.path.join(out_dir, "profile.json"), "w") as f:
        json.dump(res, f, separators=(",", ":"))
    # slim copy for run time: expected outputs and tables per query
    slim = {tid: {"queries": [{"out": q["out"], "tables": q["tables"]} for q in v["queries"]]} for tid, v in res.items()}
    with open(os.path.join(out_dir, "expect.json"), "w") as f:
        json.dump(slim, f, separators=(",", ":"))
    for tid in res:
        print(tid, "queries", len(res[tid]["queries"]),
              "cpu %.2f s" % sum(q["cpu"] for q in res[tid]["queries"]), file=sys.stderr)


if __name__ == "__main__":
    if sys.argv[1] == "profile":
        profile(sys.argv[2], sys.argv[3])
