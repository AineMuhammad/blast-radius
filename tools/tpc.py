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
    # fixture-driven: one item per (fixture entry, dialect), i.e. per subTest
    "tests/test_optimizer.py::TestOptimizer::test_annotate_funcs": ("annotate", "annotate"),
}
ANNOTATE_FIXTURE = "optimizer/annotate_functions.sql"
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


def _annotate_schema():
    """The test_schema literal inside TestOptimizer.test_annotate_funcs, read from its source."""
    import ast as _ast
    import tests.test_optimizer as m

    tree = _ast.parse(open(m.__file__).read())
    for node in _ast.walk(tree):
        if isinstance(node, _ast.FunctionDef) and node.name == "test_annotate_funcs":
            for st in node.body:
                if isinstance(st, _ast.Assign) and getattr(st.targets[0], "id", None) == "test_schema":
                    return _ast.literal_eval(st.value)
    raise RuntimeError("test_schema not found in test_annotate_funcs")


def _queries(ds):
    if ds == "annotate":
        from tests.helpers import load_sql_fixture_pairs

        out = []
        for i, (meta, sql, expected) in enumerate(load_sql_fixture_pairs(ANNOTATE_FIXTURE), start=1):
            for dialect in (meta.get("dialect") or "").split(", "):
                out.append(({}, (f"SELECT {sql} FROM tbl", dialect, expected), None))
        return out
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


_ANNOTATE_SCHEMA = []


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
            # None and [] arguments are absent (args.get() gives None either way; pickling
            # drops them), as in expr_digest
            args = tuple((k, c(x)) for k, x in sorted(v.args.items()) if x is not None and x != [])
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


def expr_digest(e):
    """Digest of an expression: class, args, type, comments and meta, recursively."""
    import enum

    import sqlglot.expressions as exp

    Expr = getattr(exp, "Expr", None) or exp.Expression

    def c(v):
        if isinstance(v, Expr):
            t = v.__dict__.get("_type")
            # None and [] arguments are absent arguments (args.get() returns None either way,
            # and sqlglot's own pickling drops them), so they are left out
            args = tuple((k, c(x)) for k, x in sorted(v.args.items()) if x is not None and x != [])
            return ("e", type(v).__qualname__, args,
                    c(t) if t is not None else None, tuple(v.comments or ()),
                    c(v.__dict__.get("_meta")) if v.__dict__.get("_meta") else None)
        if isinstance(v, dict):
            return ("d", tuple(sorted(((repr(c(k)), c(x)) for k, x in v.items()), key=lambda kv: kv[0])))
        if isinstance(v, (list, tuple)):
            return ("l", tuple(c(x) for x in v))
        if isinstance(v, (set, frozenset)):
            return ("s", tuple(sorted(repr(c(x)) for x in v)))
        if isinstance(v, enum.Enum):
            return ("n", type(v).__qualname__, v.name)
        if isinstance(v, type):
            return ("t", v.__module__, v.__qualname__)
        if isinstance(v, (str, int, float, bool, type(None))):
            return ("v", type(v).__name__, repr(v))
        raise TypeError(f"unexpected value in expression: {type(v).__qualname__}")

    return hashlib.sha256(repr(c(e)).encode()).hexdigest()


def rule_kwargs(rule, ds):
    """The keyword arguments optimize() hands this rule when execute() calls it."""
    import inspect

    from sqlglot.schema import ensure_schema

    possible = {
        "db": None, "catalog": None, "schema": ensure_schema(_schema(ds)), "dialect": None,
        "sql": None, "isolate_tables": True, "quote_identifiers": False,
        "leave_tables_isolated": True,
    }
    return {p: possible[p] for p in inspect.getfullargspec(rule).args if p in possible}


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
    if mode == "annotate":
        # one subTest of test_annotate_funcs, through the test module's own helpers
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import opt_model

        mod = opt_model.load_module()
        from sqlglot import exp

        q, dialect, expected = sql
        schema = _ANNOTATE_SCHEMA[0] if _ANNOTATE_SCHEMA else _ANNOTATE_SCHEMA.append(_annotate_schema()) or _ANNOTATE_SCHEMA[0]
        result = mod.parse_and_optimize(mod.annotate_functions, q, dialect, schema=schema, dialect=dialect)
        if result.type.sql(dialect) != exp.DataType.build(expected, dialect=dialect).sql(dialect):
            raise AssertionError("annotate_funcs entry differs")
        return {"ok": True}
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
    # build-time phase marks. Two things flow from execute() into the executor: the plan
    # (digested) and the table mapping built by ensure_tables (not digested). So "run" is
    # ensure_tables, PythonExecutor and the result pickling; everything else execute()
    # does is "compile", which a compile-only check really executes with patched code.
    real_et, real_px = ex.ensure_tables, ex.PythonExecutor

    def et(*a, **k):
        phase("run")
        try:
            return real_et(*a, **k)
        finally:
            phase("compile")

    class Marked:
        def __init__(self, *a, **k):
            phase("run")
            self._real = real_px(*a, **k)

        def execute(self, plan):
            captured.append(plan)
            return self._real.execute(plan)

    captured = []
    if build_marks:
        ex.ensure_tables, ex.PythonExecutor = et, Marked
    try:
        phase("compile")
        table = ex.execute(expression, schema=_schema(ds), tables=needed)
        phase("run")
        table = pickle.loads(pickle.dumps(table))  # result comes back to the parent
    finally:
        ex.ensure_tables, ex.PythonExecutor = real_et, real_px
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
MAX_FULL_FALLBACKS = 3  # plan changed -> full run of that query, at most this many per check


def resume_rules(data_dir, ds, i, rules, expected):
    """Re-run the given optimizer rules from their saved inputs with the patched code.
    True if every output matches the unpatched one (then optimize() returns the same)."""
    from sqlglot.optimizer.optimizer import RULES

    with open(os.path.join(data_dir, "snap", f"{i}.pkl"), "rb") as f:
        inputs = pickle.load(f)
    for k in sorted(rules):
        if k not in inputs or str(k) not in expected:
            return False
        r = RULES[k]
        if expr_digest(r(pickle.loads(inputs[k]), **rule_kwargs(r, ds))) != expected[str(k)]:
            return False
    return True


def check(data_dir, test_id, indices, budget, compile_only=(), resume=None, reuse=()):
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
    resume = resume or {}
    fallbacks = 0
    for i in indices:
        meta, sql, optimized = queries[i]
        exp_rec = prof["queries"][i]["out"]
        if i in compile_only and i in resume and prof["queries"][i].get("rules_out"):
            try:
                if resume_rules(data_dir, ds, i, resume[i], prof["queries"][i]["rules_out"]):
                    continue  # the changed rules give the same outputs: same plan
            except Exception:  # noqa: BLE001  (fall through to the full compile check)
                pass
        if i in compile_only and "plan" in (exp_rec or {}):
            rec = run_one(ds, mode, meta, sql, optimized, tables, compile_only=True)
            if rec == {"duck": exp_rec["duck"], "plan": exp_rec["plan"], "tables": exp_rec["tables"]}:
                continue
            if rec.get("duck") != exp_rec["duck"]:
                return False  # DuckDB would compute a different expected answer
            # the plan changed but the test only compares results: run this query fully,
            # for a few queries only (a change that alters many plans is cheaper to schedule)
            fallbacks += 1
            if fallbacks > MAX_FULL_FALLBACKS:
                return False
            rec = run_one(ds, mode, meta, sql, optimized, tables)
            if {k: v for k, v in rec.items() if k != "plan"} != {k: v for k, v in exp_rec.items() if k != "plan"}:
                return False
        else:
            rec = None
            opt_p = os.path.join(data_dir, "snap", f"{i}.opt.pkl")
            if i in reuse and os.path.exists(opt_p):
                # the change reaches no code run before the executor (no glue, no rule):
                # optimize() gets the same arguments and runs the same code, so its saved
                # output is used; the stand-in calls the real optimize on any other input
                import sqlglot.executor as ex

                with open(opt_p, "rb") as f:
                    saved = pickle.load(f)
                real = ex.optimize
                ex.optimize = _Reuse(real, tuple(saved["key"]), saved["blob"])
                try:
                    rec = run_one(ds, mode, meta, sql, optimized, tables)
                finally:
                    ex.optimize = real
            if rec is None:
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


# class-attribute reads per query, like tools/br_trace.py: UPPERCASE scalar attributes of
# sqlglot's main classes record "@attr:<defining class>:<name>" into the current bucket
_ABUCKET = [None]
_WATCH_DONE = False


class _Watch:
    __slots__ = ("value", "key")

    def __init__(self, value, key):
        self.value, self.key = value, key

    def __get__(self, obj, owner=None):
        b = _ABUCKET[0]
        if b is not None and self.key not in b:
            b[self.key] = {0}
        return self.value


def _install_watchers():
    global _WATCH_DONE
    if _WATCH_DONE:
        return
    _WATCH_DONE = True
    import sqlglot.expressions as exp
    from sqlglot import generator, parser, tokens
    from sqlglot.dialects.dialect import Dialect

    bases = [b for b in (getattr(exp, "Expr", None), getattr(exp, "Expression", None)) if isinstance(b, type)]
    bases += [Dialect, tokens.Tokenizer, parser.Parser, generator.Generator]
    seen, stack = set(), list(bases)
    scalars = (bool, int, float, str, type(None))
    while stack:
        c = stack.pop()
        if c in seen:
            continue
        seen.add(c)
        stack.extend(c.__subclasses__())
        if not c.__module__.startswith("sqlglot"):
            continue
        for name, value in list(c.__dict__.items()):
            if name.isupper() and type(value) in scalars:
                try:
                    setattr(c, name, _Watch(value, f"@attr:{c.__module__}.{c.__qualname__}:{name}"))
                except (AttributeError, TypeError):
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
        _ABUCKET[0] = cur[0][0] if cur[0] is not None else None
        M.restart_events()

    _hook_classes()
    _install_watchers()
    if M.get_tool(5) is None:
        M.use_tool_id(5, "tpc")
    M.register_callback(5, M.events.LINE, cb)
    M.register_callback(5, M.events.PY_START, lambda code, off: cb(code, code.co_firstlineno))

    # optimizer rules: each one's coverage goes to its own bucket "rule:<k>"
    from sqlglot.optimizer.optimizer import RULES

    rule_codes = {r.__code__: k for k, r in enumerate(RULES)}
    stack = []
    names_of_bucket = {id(b): n for n, b in buckets.items()}

    def rule_start(code, off):
        k = rule_codes.get(code)
        if k is not None and cur[0] is not None:
            stack.append(cur[0])
            phase(f"rule:{k}")
        return None

    def rule_return(code, off, val):
        if rule_codes.get(code) is not None and stack:
            b = stack.pop()
            cur[0] = b
            global _types
            _types = b[1]
            _ABUCKET[0] = b[0]
            M.restart_events()
        return None

    _orig_phase = phase

    def phase(name):  # noqa: F811  (buckets are created on demand for rules)
        if name and name not in buckets:
            buckets[name] = ({}, set())
        _orig_phase(name)

    if M.get_tool(3) is None:
        M.use_tool_id(3, "tpc-rules")
    M.register_callback(3, M.events.PY_START, rule_start)
    M.register_callback(3, M.events.PY_RETURN, rule_return)
    for code in rule_codes:
        M.set_local_events(3, code, M.events.PY_START | M.events.PY_RETURN)
    t0 = time.process_time()
    M.set_events(5, M.events.LINE | M.events.PY_START)
    try:
        rec = run_one(ds, mode, meta, sql, optimized, tables, phase=phase)
        err = None
    except Exception as e:  # noqa: BLE001
        rec, err = None, repr(e)
    M.set_events(5, 0)
    for code in rule_codes:
        M.set_local_events(3, code, 0)
    phase(None)
    lines = {}
    types = set()
    for ls, ts in buckets.values():
        for f, v in ls.items():
            lines.setdefault(f, set()).update(v)
        types |= ts
    run_lines, run_types = buckets["run"]
    if mode == "execute" and meta.get("execute"):
        # execute()'s own body decides what reaches the executor (e.g. its arguments), and
        # a compile-only check replaces the executor: any edit there needs a full run
        import ast as _ast

        rel = "sqlglot/executor/__init__.py"
        tree = _ast.parse(open(os.path.join(root, rel)).read())
        for node in tree.body:
            if isinstance(node, _ast.FunctionDef) and node.name == "execute":
                run_lines.setdefault(rel, set()).update(range(node.lineno, node.end_lineno + 1))
                lines.setdefault(rel, set()).update(range(node.lineno, node.end_lineno + 1))
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
    snap = None
    if mode == "execute" and meta.get("execute"):
        from sqlglot import find_tables, parse_one

        used = sorted({t.name for t in find_tables(parse_one(sql))})
        if rec is not None and "plan" in rec:
            snap = _rule_snapshots(ds, mode, meta, sql, optimized, tables, data_dir, i, unit)
            _save_optimized(ds, mode, meta, sql, optimized, tables, data_dir, i, rec)
    rule_lines = {k: {f: sorted(v) for f, v in b[0].items()} for k, b in buckets.items() if k.startswith("rule:")}
    rule_classes = {k: _classes(b[1]) for k, b in buckets.items() if k.startswith("rule:")}
    glue = {}
    for k in ("front", "compile"):
        for f, v in buckets[k][0].items():
            glue.setdefault(f, set()).update(v)
    glue_classes = _classes(buckets["front"][1] | buckets["compile"][1])
    return test_id, i, {
        "out": rec,
        "err": err,
        "unit": round(unit, 6),
        "cpu": round(cpu, 4),  # in calib() units
        "cpu_compile": round(cpu_compile, 4) if cpu_compile is not None else None,
        "run_lines": {f: sorted(v) for f, v in run_lines.items()},
        "run_classes": _classes(run_types),
        "glue_lines": {f: sorted(v) for f, v in glue.items()},
        "glue_classes": glue_classes,
        "rule_lines": rule_lines,
        "rule_classes": rule_classes,
        "rules": snap,
        "traced": round(traced, 4),
        "tables": used,
        "classes": _classes(types),
        "lines": {f: sorted(v) for f, v in lines.items()},
    }


def _opt_key(args, kwargs):
    """Digest of optimize()'s arguments as execute() passes them: (expression, schema)."""
    if len(args) != 2 or any(not isinstance(v, (str, bool, type(None))) for v in kwargs.values()):
        return None
    expr, schema = args
    mapping = getattr(schema, "mapping", None)
    if mapping is None:
        return None
    return (expr_digest(expr), type(schema).__qualname__,
            json.dumps(mapping, sort_keys=True, default=repr), repr(sorted(kwargs.items())),
            _dialect_key(getattr(schema, "dialect", None)), repr(getattr(schema, "normalize", None)))


def _dialect_key(d):
    # the dialect object's default repr holds its address: use its class and settings
    if d is None:
        return None
    return (type(d).__module__, type(d).__qualname__, repr(getattr(d, "normalization_strategy", None)))


def _exact_dumps(obj):
    """Pickle an expression tree with its full object state (types, meta, comments), not
    sqlglot's compact __reduce__ form, so the copy is identical to the original."""
    import copyreg
    import io

    import sqlglot.expressions as exp

    Expr = getattr(exp, "Expr", None) or exp.Expression

    class P(pickle.Pickler):
        def reducer_override(self, o):
            if isinstance(o, Expr):
                slots = {n: getattr(o, n) for n in copyreg._slotnames(type(o)) if hasattr(o, n)}
                d = getattr(o, "__dict__", None)
                return copyreg.__newobj__, (type(o),), (dict(d) if d is not None else None, slots or None)
            return NotImplemented

    old = sys.getrecursionlimit()
    sys.setrecursionlimit(max(old, 100000))
    try:
        buf = io.BytesIO()
        P(buf, protocol=4).dump(obj)
        return buf.getvalue()
    finally:
        sys.setrecursionlimit(old)


def _exact_loads(blob):
    old = sys.getrecursionlimit()
    sys.setrecursionlimit(max(old, 100000))
    try:
        return pickle.loads(blob)
    finally:
        sys.setrecursionlimit(old)


class _Reuse:
    """Stand-in for executor.optimize: returns the saved optimizer output when called with
    exactly the arguments it was saved for, and calls the real optimize otherwise."""

    def __init__(self, real, key, blob):
        self.real, self.key, self.blob, self.hits = real, key, blob, 0

    def __call__(self, *args, **kwargs):
        if self.key is not None and _opt_key(args, kwargs) == self.key:
            self.hits += 1
            return _exact_loads(self.blob)
        return self.real(*args, **kwargs)


def _save_optimized(ds, mode, meta, sql, optimized, tables, data_dir, i, rec):
    """Save the query's optimize() output and its argument digest, kept only if a full run
    that reuses it gives exactly the record of the normal run."""
    import sqlglot.executor as ex

    real = ex.optimize
    seen = {}

    def capture(*args, **kwargs):
        out = real(*args, **kwargs)
        if "key" not in seen:
            seen["key"] = _opt_key(args, kwargs)
            seen["blob"] = _exact_dumps(out)
        return out

    try:
        ex.optimize = capture
        run_one(ds, mode, meta, sql, optimized, tables, compile_only=True)
    except Exception:  # noqa: BLE001
        return
    finally:
        ex.optimize = real
    if seen.get("key") is None:
        return
    try:
        rec = run_one(ds, mode, meta, sql, optimized, tables)  # untraced reference run
    except Exception:  # noqa: BLE001
        return
    reuse = _Reuse(real, seen["key"], seen["blob"])
    try:
        ex.optimize = reuse
        again = run_one(ds, mode, meta, sql, optimized, tables)
    except Exception:  # noqa: BLE001
        return
    finally:
        ex.optimize = real
    if reuse.hits != 1 or again != rec:
        if os.environ.get("BR_DEBUG_REUSE"):
            print("reuse rejected", i, reuse.hits, [k for k in set(rec or {}) | set(again or {}) if (rec or {}).get(k) != (again or {}).get(k)], file=sys.stderr)
        return
    os.makedirs(os.path.join(data_dir, "snap"), exist_ok=True)
    with open(os.path.join(data_dir, "snap", f"{i}.opt.pkl"), "wb") as f:
        pickle.dump({"key": seen["key"], "blob": seen["blob"]}, f, protocol=4)


def _rule_snapshots(ds, mode, meta, sql, optimized, tables, data_dir, i, unit):
    """Untraced compile of one query that records every optimizer rule's input (pickled) and
    output digest, then re-runs each rule from its saved input: a rule is resumable only if
    that reproduces its output exactly. Inputs go to <data_dir>/snap/<i>.pkl."""
    from sys import monitoring as M

    from sqlglot.optimizer.optimizer import RULES

    rule_codes = {r.__code__: k for k, r in enumerate(RULES)}
    inputs, outputs, cpu = {}, {}, {}
    depth = {}
    t_in = {}

    def start(code, off):
        k = rule_codes.get(code)
        if k is not None:
            depth[k] = depth.get(k, 0) + 1
            if depth[k] == 1 and k not in inputs:
                frame = sys._getframe(1)
                inputs[k] = pickle.dumps(frame.f_locals[code.co_varnames[0]])
                t_in[k] = time.process_time()
        return None

    def ret(code, off, val):
        k = rule_codes.get(code)
        if k is not None:
            depth[k] -= 1
            if depth[k] == 0 and k not in outputs:
                cpu[k] = time.process_time() - t_in[k]
                outputs[k] = expr_digest(val)
        return None

    if M.get_tool(2) is None:
        M.use_tool_id(2, "tpc-snap")
    M.register_callback(2, M.events.PY_START, start)
    M.register_callback(2, M.events.PY_RETURN, ret)
    for code in rule_codes:
        M.set_local_events(2, code, M.events.PY_START | M.events.PY_RETURN)
    try:
        run_one(ds, mode, meta, sql, optimized, tables, compile_only=True)
    finally:
        for code in rule_codes:
            M.set_local_events(2, code, 0)
    ok = {}
    for k, r in enumerate(RULES):
        if k not in inputs or k not in outputs:
            ok[k] = False
            continue
        try:
            out = r(pickle.loads(inputs[k]), **rule_kwargs(r, ds))
            ok[k] = expr_digest(out) == outputs[k]
        except Exception:  # noqa: BLE001
            ok[k] = False
    os.makedirs(os.path.join(data_dir, "snap"), exist_ok=True)
    with open(os.path.join(data_dir, "snap", f"{i}.pkl"), "wb") as f:
        pickle.dump({k: inputs[k] for k in inputs if ok.get(k)}, f, protocol=4)
    return {str(k): {"ok": ok[k], "out": outputs.get(k), "cpu": round(cpu.get(k, 0) / unit, 4)}
            for k in range(len(RULES))}


def _annotate_chunk_task(args):
    """Profile a chunk of test_annotate_funcs items in one process: run them all untraced
    first (imports, one-off set-up), then trace each item into its own bucket. Code that
    ran only during the warm-up belongs to no item and is flagged as unmodelled."""
    root, data_dir, tid, start, end = args
    from sys import monitoring as M

    ds, mode = TESTS[tid]
    items = _queries(ds)
    for i in range(start, end):  # warm-up
        meta, sql, optimized = items[i]
        try:
            run_one(ds, mode, meta, sql, optimized, {})
        except Exception:  # noqa: BLE001
            pass
    global _types
    _hook_classes()
    _install_watchers()
    prefix = os.path.join(root, "sqlglot") + "/"
    cur = [None]

    def cb(code, line):
        b = cur[0]
        if b is not None and code.co_filename.startswith(prefix) and code.co_flags & 1:
            b[0].setdefault(code.co_filename[len(root) + 1:], set()).add(line)
        return M.DISABLE

    if M.get_tool(5) is None:
        M.use_tool_id(5, "tpc")
    M.register_callback(5, M.events.LINE, cb)
    M.register_callback(5, M.events.PY_START, lambda code, off: cb(code, code.co_firstlineno))
    out = []
    unit = calib()
    for i in range(start, end):
        meta, sql, optimized = items[i]
        bucket = ({}, set())
        cur[0] = bucket
        _types = bucket[1]
        _ABUCKET[0] = bucket[0]
        M.restart_events()
        M.set_events(5, M.events.LINE | M.events.PY_START)
        err = None
        try:
            rec = run_one(ds, mode, meta, sql, optimized, {})
        except Exception as e:  # noqa: BLE001
            rec, err = None, repr(e)
        M.set_events(5, 0)
        cur[0] = None
        _types = None
        _ABUCKET[0] = None
        t0 = time.process_time()
        if rec is not None:
            run_one(ds, mode, meta, sql, optimized, {})
        cpu = (time.process_time() - t0) / unit
        out.append((i, {"out": rec, "err": err, "unit": round(unit, 6), "cpu": round(cpu, 4),
                        "cpu_compile": None, "tables": [], "classes": _classes(bucket[1]),
                        "lines": {f: sorted(v) for f, v in bucket[0].items()}}))
    return tid, out


def _setup_task(args):
    """Coverage of the shared part: building Table objects from rows."""
    root, data_dir, ds = args
    if ds == "annotate":
        return ds, ({}, [])  # shared work is covered by the warm-up / unmodelled flags
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
    chunks = []
    for tid, (ds, mode) in TESTS.items():
        n_items = len(_queries(ds))
        if mode == "annotate":
            step = (n_items + 3) // 4
            chunks += [(root, out_dir, tid, a, min(a + step, n_items)) for a in range(0, n_items, step)]
            continue
        for i in range(n_items):
            tasks.append((root, out_dir, tid, i))
    ctx = mp.get_context("spawn")
    res = {tid: {"queries": [None] * len(_queries(ds))} for tid, (ds, _) in TESTS.items()}
    with ctx.Pool(4, initializer=_init, initargs=(root,), maxtasksperchild=1) as pool:
        for tid, i, rec in pool.imap_unordered(_trace_task, tasks):
            res[tid]["queries"][i] = rec
        for tid, recs in pool.imap_unordered(_annotate_chunk_task, chunks):
            for i, rec in recs:
                res[tid]["queries"][i] = rec
        setup = dict(pool.map(_setup_task, [(root, out_dir, ds) for ds in ("tpch", "tpcds", "dialects", "annotate")]))
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
        if tid in res:
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
    for tid, (ds, mode) in TESTS.items():
        if mode == "annotate" and tid in res and any(q is None or q.get("err") for q in res[tid]["queries"]):
            print(f"{tid}: some items fail under the model; not modelled", file=sys.stderr)
            del res[tid]
    bad = [(tid, q["err"]) for tid in res if not tid.startswith("@") and isinstance(res[tid], dict)
           for q in res[tid].get("queries", []) if q and q.get("err")]
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
                slim[tid] = {"queries": [
                    {"out": q["out"], "tables": q["tables"],
                     **({"rules_out": {k: r["out"] for k, r in q["rules"].items() if r["ok"]}}
                        if q.get("rules") else {})}
                    for q in v["queries"]]}
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
