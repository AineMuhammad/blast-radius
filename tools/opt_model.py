"""Per-entry model of the TestOptimizer tests (tests/test_optimizer.py).

The real test class starts a spawn process pool and a DuckDB connection in
setUpClass, and most of its tests push fixture entries through the pool with
check_file. Here a test method runs for real, in-process, with three stand-ins:

  pool    synchronous; still pickles arguments and results as the pool does
  conn    records the SQL it would send to DuckDB instead of running it
  subTest skips fixture entries outside the selected set

Everything the method does outside check_file entries (its "rest") always runs.
For entries with `execute`, DuckDB would compare the results of two generated
SQL strings; identical strings give identical results, so the strings are
compared with those of the unpatched code. Methods that need real DuckDB
results fail under the stand-ins at build time and are not modelled.
"""

import contextlib
import importlib
import os
import pickle
import sys
import time
from concurrent.futures import Future

REST = "rest"


class _Skip(Exception):
    pass


class _Tok:
    """What the stand-in DuckDB connection returns: just the SQL it was given."""

    def __init__(self, sql):
        self.sql = sql

    def df(self):
        return self

    def fetchall(self):
        return self

    def fetchdf(self):
        return self

    def __eq__(self, other):  # comparing real results is not modelled
        raise _Unmodelled("result comparison")

    __hash__ = object.__hash__


class _Unmodelled(Exception):
    pass


def _raise_unmodelled(what):
    def f(*a, **k):
        raise _Unmodelled(what)

    return f


def load_module():
    """Import tests.test_optimizer with stand-in duckdb / pandas modules.

    The model never sends anything to DuckDB, so the real libraries are not needed;
    a method that does use them directly raises _Unmodelled (at build time too, where
    the same stand-ins are used) and is left unmodelled."""
    import types

    if "tests.test_optimizer" not in sys.modules:
        if "duckdb" not in sys.modules:
            d = types.ModuleType("duckdb")
            d.connect = _raise_unmodelled("duckdb.connect")
            sys.modules["duckdb"] = d
        if "pandas" not in sys.modules:
            pd = types.ModuleType("pandas")
            pdt = types.ModuleType("pandas.testing")
            pdt.assert_frame_equal = _raise_unmodelled("assert_frame_equal")
            pd.testing = pdt
            sys.modules["pandas"] = pd
            sys.modules["pandas.testing"] = pdt
    return importlib.import_module("tests.test_optimizer")


class Harness:
    def __init__(self, selected=None, on_phase=None):
        self.selected = selected  # set of entry keys, or None for all
        self.on_phase = on_phase or (lambda key: None)
        self.failures = set()
        self.pairs = {}
        self.keys = []
        self.cpu = {}
        self.call = -1
        self.pending = None
        self.submitted = set()
        self.cur = REST

    # -- stand-ins ------------------------------------------------------------
    def load_pairs(self, orig):
        h = self

        def load(filename):
            h.call += 1
            for i, (meta, sql, expected) in enumerate(orig(filename), start=1):
                h.pending = meta.get("title") or f"{i}, {sql}"
                yield meta, sql, expected

        return load

    def submit(self, fn, *args, **kwargs):
        key = (self.call, self.pending)
        fut = Future()
        if self.selected is not None and key not in self.selected:
            fut.set_exception(_Skip())
            return fut
        self.submitted.add(key)
        self.keys.append(key)
        prev = self._switch(key)
        t0 = time.process_time()
        try:
            f2, a2, k2 = pickle.loads(pickle.dumps((fn, args, kwargs)))
            r = f2(*a2, **k2)
            fut.set_result(pickle.loads(pickle.dumps(r)))
        except Exception as e:  # noqa: BLE001  (the pool hands exceptions to the future)
            fut.set_exception(e)
        finally:
            self.cpu[key] = self.cpu.get(key, 0) + time.process_time() - t0
            self._switch(prev)
        return fut

    def _switch(self, key):
        prev = self.cur
        self.cur = key
        self.on_phase(key)
        return prev

    @contextlib.contextmanager
    def subtest(self, msg=None, **params):
        title = msg
        if isinstance(title, str) and title.startswith("(execute) "):
            title = title[len("(execute) "):]
        key = (self.call, title)
        if key not in self.submitted:
            key = self.cur  # a subTest outside check_file entries
        prev = self._switch(key)
        t0 = time.process_time()
        try:
            yield
        except _Skip:
            pass
        except _Unmodelled:
            raise
        except Exception:  # noqa: BLE001  (unittest's subTest records and continues)
            self.failures.add(key)
        finally:
            if key != REST:
                self.cpu[key] = self.cpu.get(key, 0) + time.process_time() - t0
            self._switch(prev)

    def frame_equal(self, a, b, *args, **kwargs):
        if not (isinstance(a, _Tok) and isinstance(b, _Tok)):
            raise _Unmodelled("frame comparison of real data")
        self.pairs.setdefault(self.cur, []).append((a.sql, b.sql))

    # -- running a method -----------------------------------------------------
    def run(self, method):
        import sqlglot
        from sqlglot.schema import MappingSchema

        mod = load_module()
        cls = mod.TestOptimizer
        h = self

        class Pool:
            def submit(self, fn, *a, **k):
                return h.submit(fn, *a, **k)

            def shutdown(self, *a, **k):
                pass

        class Conn:
            def execute(self, sql, *a, **k):
                return _Tok(sql)

            def close(self):
                pass

        saved = {n: getattr(mod, n) for n in ("load_sql_fixture_pairs", "assert_frame_equal") if hasattr(mod, n)}
        mod.load_sql_fixture_pairs = self.load_pairs(saved["load_sql_fixture_pairs"])
        if "assert_frame_equal" in saved:
            mod.assert_frame_equal = self.frame_equal
        cls.pool, cls.conn = Pool(), Conn()
        sqlglot.schema = MappingSchema()  # what setUpClass does besides the pool and DuckDB
        try:
            case = cls(methodName=method)
            case.subTest = self.subtest
            case.setUp()
            try:
                getattr(case, method)()
            finally:
                case.tearDown()
        except _Unmodelled:
            raise
        except Exception:  # noqa: BLE001
            self.failures.add(REST)
        finally:
            for n, v in saved.items():
                setattr(mod, n, v)
        return self


def methods(root):
    """Test methods of TestOptimizer, as listed by unittest."""
    import unittest

    sys.path.insert(0, root)
    mod = load_module()
    return unittest.TestLoader().getTestCaseNames(mod.TestOptimizer)


def check(method, items, selected_idx, expect):
    """True if the method passes with the selected entries, matching unpatched DuckDB SQL."""
    keys = {tuple(items[i]) for i in selected_idx if items[i] != REST}
    h = Harness(selected=keys).run(method)
    if h.failures:
        return False
    for k in keys:
        # order-insensitive: entries sharing a title share a key, and results arrive in
        # completion order
        if sorted(map(tuple, h.pairs.get(k, []))) != sorted(map(tuple, expect.get(repr(k), []))):
            return False
    return True
