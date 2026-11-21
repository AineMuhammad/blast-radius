"""Build the selection map from trace files + the pinned sqlglot source.

usage: python build_map.py <sqlglot_root> <trace_dir> <out_dir>

Output (all plain JSON, read by selector.py with the stdlib only):
  out/index.json   {"tests": [nodeid...], "files": {relpath: "f<n>.json"},
                    "data": {relpath: hex}, "dirs": {relpath: hex},
                    "testfile": {relpath: hex}}
  out/f<n>.json    per python source file:
                    {"g": [hex...],           # distinct test bitsets
                     "m": [gi per line],      # change at line (1-based, index 0 unused); -1 = none known
                     "i": [[prev, next, [[indent, gi]...]] per gap after line a],
                     "s": [lines that are inside multi-line strings],
                     "u": gi}                 # fallback: everything importing this file's users
Bitsets are over the test index in index.json["tests"].
"""

import ast
import glob
import json
import os
import sys
import types
from collections import defaultdict

ROOT, TRACE, OUT = sys.argv[1:4]
ROOT = ROOT.rstrip("/") + "/"


def load_traces():
    tests = {}
    imp = defaultdict(set)
    global import_func_lines, import_types
    for f in sorted(glob.glob(os.path.join(TRACE, "trace_*.json"))):
        d = json.load(open(f))
        for k, v in d["tests"].items():
            tests[k] = v
        for k, v in d["import_lines"].items():
            imp[k].update(v)
        for k, v in d.get("import_func_lines", {}).items():
            import_func_lines[k].update(v)
        import_types.update(d.get("import_types", []))
    nchild = 0
    for f in sorted(glob.glob(os.path.join(TRACE, "child_*.jsonl"))):
        for line in open(f):
            if not line.strip():
                continue
            r = json.loads(line)
            t = tests.get(r["test"])
            if t is None:
                continue
            nchild += 1
            c = t["call"]
            for fn, ls in r["lines"].items():
                c["lines"][fn] = sorted(set(c["lines"].get(fn, [])) | set(ls))
            for k in ("files", "dirs", "classes"):
                c[k] = sorted(set(c[k]) | set(r[k]))
    print("child records", nchild, file=sys.stderr)
    return tests, imp


import_func_lines = defaultdict(set)
import_types = set()  # qualnames (with bases) of sqlglot classes instantiated while importing
tests, import_lines = load_traces()
if len(tests) < 1600:  # real tests only
    sys.exit(f"only {len(tests)} tests traced; refusing to build an incomplete map")
# Each TPC query of the heavy executor tests becomes a pseudo-test ("@tpc:<test>#<i>")
# so every rule below also tells which queries a change can reach. "#setup" holds
# code shared by all queries; "#unmodelled" holds lines the real test runs that no
# query model reaches. A change hitting either needs the real test.
PROFILE = sys.argv[4] if len(sys.argv) > 4 else None
pseudo = {}
if PROFILE and os.path.exists(PROFILE):
    _prof = json.load(open(PROFILE))
    for tid, v in _prof.items():
        if tid not in tests or not isinstance(v, dict):
            continue
        for qi, q in enumerate(v["queries"]):
            pseudo[f"@tpc:{tid}#{qi}"] = {"lines": q["lines"], "classes": q.get("classes", [])}
            if q.get("out") and "plan" in q["out"]:
                # run phase only (PythonExecutor and result pickling): needs a full run
                pseudo[f"@tpc:{tid}#{qi}R"] = {"lines": q["run_lines"], "classes": q["run_classes"]}
        pseudo[f"@tpc:{tid}#setup"] = {"lines": v["setup_lines"], "classes": v.get("setup_classes", [])}
        pseudo[f"@tpc:{tid}#unmodelled"] = {"lines": {}, "classes": []}
ids = sorted(tests) + sorted(pseudo)
IDX = {t: i for i, t in enumerate(ids)}
ALL = (1 << len(ids)) - 1

# --- per-test combined data (call + class-level setup union) ---------------
setup_union = defaultdict(lambda: {"lines": defaultdict(set), "files": set(), "dirs": set(), "classes": set()})
for t, v in tests.items():
    key = t.rsplit("::", 1)[0]
    su = setup_union[key]
    s = v["setup"]
    for f, ls in s["lines"].items():
        su["lines"][f].update(ls)
    su["files"].update(s["files"])
    su["dirs"].update(s["dirs"])
    su["classes"].update(s["classes"])

cov = defaultdict(lambda: defaultdict(int))  # file -> line -> bits
for pid, pv in pseudo.items():
    pb = 1 << IDX[pid]
    for f_, ls in pv["lines"].items():
        for l in ls:
            cov[f_][l] |= pb
data_files = defaultdict(int)
data_dirs = defaultdict(int)
class_users = defaultdict(int)
test_file_bits = defaultdict(int)

for t, v in tests.items():
    b = 1 << IDX[t]
    test_file_bits[t.split("::", 1)[0]] |= b
    su = setup_union[t.rsplit("::", 1)[0]]
    c = v["call"]
    for f, ls in list(c["lines"].items()) + list(su["lines"].items()):
        cf = cov[f]
        for l in ls:
            cf[l] |= b
    for x in set(c["files"]) | su["files"]:
        data_files[x] |= b
    for x in set(c["dirs"]) | su["dirs"]:
        data_dirs[x] |= b
    for x in set(c["classes"]) | su["classes"]:
        class_users[x] |= b

for pid, pv in pseudo.items():
    for x in pv["classes"]:
        class_users[x] |= 1 << IDX[pid]

# lines the real heavy test runs that none of its query models reach
for tid in {p.split("#")[0][5:] for p in pseudo}:
    tb = 1 << IDX[tid]
    qbits = 0
    for p in pseudo:
        if p.startswith(f"@tpc:{tid}#"):
            qbits |= 1 << IDX[p]
    ub = 1 << IDX[f"@tpc:{tid}#unmodelled"]
    n_un = 0
    for f_, lines in cov.items():
        if not f_.startswith("sqlglot/"):
            continue
        for l, b in lines.items():
            if b & tb and not b & qbits:
                lines[l] = b | ub
                n_un += 1
    print(f"{tid.split('::')[-1]}: {n_un} lines not reached by any query model", file=sys.stderr)

# --- static analysis --------------------------------------------------------
def py_files():
    """Every Python file in the checkout (tests also run e.g. sqlglotc/setup.py)."""
    out = []
    for dp, dn, fn in os.walk(ROOT):
        dn[:] = [d for d in dn if d not in (".git", "__pycache__")]
        for f in fn:
            if f.endswith(".py"):
                out.append(os.path.relpath(os.path.join(dp, f), ROOT))
    return sorted(out)


FILES = py_files()
SRC = {f: open(ROOT + f, encoding="utf-8").read() for f in FILES}
TREES = {}
for f in FILES:
    try:
        TREES[f] = ast.parse(SRC[f])
    except SyntaxError:
        pass


def modname(f):
    m = f[:-3].replace("/", ".")
    if m.endswith(".__init__"):
        m = m[: -len(".__init__")]
    return m


# name references: name -> bits of tests that ran code referencing it
def code_objects(co):
    yield co
    for c in co.co_consts:
        if isinstance(c, types.CodeType):
            yield from code_objects(c)


def names_of(co):
    r = set(co.co_names) | set(co.co_freevars)
    for c in co.co_consts:
        if isinstance(c, str) and c.isidentifier():
            r.add(c)
    return r


nameref = defaultdict(int)
IMPNAMES = set()  # names read by function code that ran at import time (metaclasses, factories)
func_bits = {}  # (file, firstlineno, name) -> bits (tests that ran the code object)
for f in FILES:
    if f not in TREES:
        continue
    try:
        top = compile(SRC[f], ROOT + f, "exec")
    except SyntaxError:
        continue
    cf = cov.get(f, {})
    ifl = import_func_lines.get(f, set())
    for co in code_objects(top):
        if not (co.co_flags & 1):
            continue
        bits = 0
        for _, _, ln in co.co_lines():
            if ln is not None:
                bits |= cf.get(ln, 0)
        func_bits[(f, co.co_firstlineno, co.co_name)] = bits
        if bits:
            for n in names_of(co):
                nameref[n] |= bits


BODY_REFS = defaultdict(set)


def _collect_body_refs():
    for f, tree in TREES.items():
        if not f.startswith("sqlglot/"):
            continue

        def walk_body(stmts, qual):
            for st in stmts:
                if isinstance(st, ast.ClassDef):
                    walk_body(st.body, (qual + "." if qual else "") + st.name)
                    for b in st.bases + st.keywords + st.decorator_list:
                        for n in ast.walk(b):
                            _ref(n, f, None)
                    continue
                if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    for d in st.decorator_list + st.args.defaults + st.args.kw_defaults:
                        if d is not None:
                            for n in ast.walk(d):
                                _ref(n, f, None)
                    continue
                own = assigned_names(st) if qual else set()
                for n in ast.walk(st):
                    if isinstance(n, (ast.Lambda, ast.FunctionDef)):
                        continue
                    nm = _refname(n)
                    if nm and nm not in own:
                        BODY_REFS[nm].add((f, qual or None))
                    elif nm:
                        BODY_REFS[nm].add((f, qual))

        walk_body(tree.body, "")


def _refname(n):
    if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
        return n.id
    if isinstance(n, ast.Attribute) and isinstance(n.ctx, ast.Load):
        return n.attr
    if isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value.isidentifier():
        return n.value
    return None


def _ref(n, f, qual):
    nm = _refname(n)
    if nm:
        BODY_REFS[nm].add((f, qual))


def can_narrow(C, names):
    """Class-level data names of C whose readers are all visible to static name analysis."""
    if not names:
        return False
    related = [C] + list(DIALECT_OF.get(C, []))
    for nm in names:
        if nm.startswith("__") or nm in IMPNAMES_META:
            return False
        if nm in IMPNAMES and any(made_at_import(X) for X in related):
            return False
        for (g, q) in BODY_REFS.get(nm, ()):
            if q is None:
                return False
            D = resolve(g, q)
            if D is None or not issubclass(D, C):
                return False
    return True


def _line_names(tree):
    """line -> names referenced on it (attribute names at the attribute's last line)."""
    out = defaultdict(set)
    for n in ast.walk(tree):
        if isinstance(n, ast.Attribute):
            out[n.end_lineno].add(n.attr)
        elif isinstance(n, ast.Name):
            out[n.lineno].add(n.id)
        elif isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value.isidentifier():
            for l in range(n.lineno, n.end_lineno + 1):
                out[l].add(n.value)
    return out


def _func_kinds(tree):
    """line -> True if it belongs to class-creation code (metaclass, __init_subclass__)."""
    meta = {}

    def walk(node, in_meta_cls):
        for ch in ast.iter_child_nodes(node):
            if isinstance(ch, ast.ClassDef):
                is_meta = any(isinstance(b, ast.Name) and b.id in ("type", "ABCMeta") or
                              isinstance(b, ast.Attribute) and b.attr in ("ABCMeta",) for b in ch.bases)
                walk(ch, is_meta)
            elif isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef)):
                m = in_meta_cls or ch.name in ("__new__", "__init_subclass__", "__set_name__", "__prepare__")
                for l in range(ch.lineno, ch.end_lineno + 1):
                    meta[l] = m  # inner functions overwrite below
                walk(ch, in_meta_cls)
            else:
                walk(ch, in_meta_cls)

    walk(tree, False)
    return meta


# names read on lines that actually ran at import time, split by who read them:
# class-creation code derives per-class state from them (never narrowed); other code
# reads them from the few objects that exist at import (narrowed unless such an object
# is an instance of the edited class)
IMPNAMES_META = set()
for _f, _ls in import_func_lines.items():
    if _f in TREES and _f.startswith("sqlglot/"):
        _ln = _line_names(TREES[_f])
        _mk = _func_kinds(TREES[_f])
        for _l in _ls:
            names_here = _ln.get(_l, ())
            IMPNAMES.update(names_here)
            if _mk.get(_l, True):
                IMPNAMES_META.update(names_here)


def _resolve_qual(full):
    parts = full.split(".")
    for i in range(len(parts) - 1, 0, -1):
        try:
            o = importlib.import_module(".".join(parts[:i]))
        except Exception:
            continue
        try:
            for p in parts[i:]:
                o = getattr(o, p)
            return o if isinstance(o, type) else None
        except AttributeError:
            return None
    return None


IMPORT_CLASSES = None


def made_at_import(C):
    """Was an instance of C (or a subclass) created while importing sqlglot?"""
    global IMPORT_CLASSES
    if IMPORT_CLASSES is None:
        IMPORT_CLASSES = [c for c in (_resolve_qual(q) for q in import_types) if c is not None]
    if not import_types:
        return True  # old trace without the data: assume yes
    return any(issubclass(c, C) for c in IMPORT_CLASSES)


def bits_lines(f, a, b):
    cf = cov.get(f)
    if not cf:
        return 0
    r = 0
    for l in range(a, b + 1):
        r |= cf.get(l, 0)
    return r


def header_end(node):
    """Last line of a compound statement's header (before its body)."""
    if isinstance(node, (ast.If, ast.While)):
        return node.test.end_lineno
    if isinstance(node, (ast.For, ast.AsyncFor)):
        return node.iter.end_lineno
    if isinstance(node, (ast.With, ast.AsyncWith)):
        return node.items[-1].context_expr.end_lineno if node.items else node.lineno
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return node.body[0].lineno - 1 if node.body else node.end_lineno
    if hasattr(ast, "Match") and isinstance(node, ast.Match):
        return node.subject.end_lineno
    body = getattr(node, "body", None)
    if body:
        return max(node.lineno, body[0].lineno - 1)
    return node.end_lineno


def assigned_names(node):
    names = set()
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        names.add(node.name)
    elif isinstance(node, (ast.Import, ast.ImportFrom)):
        for a in node.names:
            names.add((a.asname or a.name).split(".")[0])
    else:
        for n in ast.walk(node):
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
                names.add(n.id)
            elif isinstance(n, ast.Attribute) and isinstance(n.ctx, ast.Store):
                names.add(n.attr)
    return names


sys.path.insert(0, ROOT)
import importlib  # noqa: E402

FAMILY = []
DIALECT_OF = defaultdict(list)  # class -> dialect classes using it (or a subclass of it)
try:
    import sqlglot.expressions as _exp
    from sqlglot import generator as _gen, parser as _par, tokens as _tok
    from sqlglot.dialects.dialect import Dialect as _Dialect, Dialects as _Dialects

    FAMILY = [c for c in (getattr(_exp, "Expr", None), getattr(_exp, "Expression", None)) if isinstance(c, type)]
    FAMILY += [_Dialect, _tok.Tokenizer, _par.Parser, _gen.Generator]
    for _d in _Dialects:
        if _d.value:
            try:
                _Dialect.get_or_raise(_d.value)
            except Exception:
                pass
    _dcls = []
    _st = [_Dialect]
    while _st:
        _c = _st.pop()
        _dcls.append(_c)
        _st.extend(_c.__subclasses__())
    for _d in _dcls:
        for _attr in ("tokenizer_class", "parser_class", "generator_class"):
            _k = getattr(_d, _attr, None)
            if isinstance(_k, type):
                for _b in _k.__mro__:
                    DIALECT_OF[_b].append(_d)
except Exception as e:  # pragma: no cover
    print("family resolution failed", e, file=sys.stderr)


def resolve(f, qual):
    if not f.startswith(("sqlglot/", "tests/")):
        return None  # never import scripts such as setup.py
    try:
        o = importlib.import_module(modname(f))
        for part in qual.split("."):
            o = getattr(o, part)
        return o if isinstance(o, type) else None
    except Exception:
        return None


def qn(c):
    return f"{c.__module__}.{c.__qualname__}"


def class_bits(f, qual):
    full = modname(f) + "." + qual
    return class_users.get(full, 0)


def users_ext(C, f, qual):
    """Tests that may observe class-level data of C."""
    b = class_users.get(qn(C), 0) | class_users.get(modname(f) + "." + qual, 0)
    for d in DIALECT_OF.get(C, []):
        b |= class_users.get(qn(d), 0)
    # nested class: owner class users too (e.g. Snowflake.Tokenizer -> Snowflake)
    if "." in qual:
        O = resolve(f, qual.rsplit(".", 1)[0])
        if O is not None:
            b |= users_ext(O, f, qual.rsplit(".", 1)[0])
    return b


def is_family(C):
    return C is not None and any(issubclass(C, F) for F in FAMILY)


def importers_bits(f):
    """Tests in test files that import module f (for tests/ helpers)."""
    mod = modname(f)
    short = mod.split(".")[-1]
    r = test_file_bits.get(f, 0)
    for g in FILES:
        if not g.startswith("tests/") or g == f:
            continue
        s = SRC[g]
        if mod in s or f"import {short}" in s or f".{short} import" in s:
            r |= test_file_bits.get(g, 0)
            # one more level (e.g. test_dialect.Validator users)
            mg = modname(g)
            for h in FILES:
                if h.startswith("tests/") and h != g and mg in SRC[h]:
                    r |= test_file_bits.get(h, 0)
    return r


def analyse(f):
    if not BODY_REFS:
        _collect_body_refs()
    tree = TREES[f]
    n = len(SRC[f].splitlines())
    is_test = f.startswith("tests/")
    M = [None] * (n + 2)  # bits per line
    # stmt spans: each line -> innermost (span, info)
    best = [None] * (n + 2)  # (size, bits)
    gap_blocks = []  # (body_start, body_end, indent, header_bits)

    docstrings = set()
    for nd in ast.walk(tree):
        if isinstance(nd, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and nd.body:
            b0 = nd.body[0]
            if isinstance(b0, ast.Expr) and isinstance(b0.value, ast.Constant) and isinstance(b0.value.value, str):
                docstrings.add(id(b0))
    doc_bits = nameref.get("__doc__", 0) | nameref.get("getdoc", 0)

    def visit(stmts, scope, qual, func_bits_here):
        """scope: 'm' module, 'c' class, 'f' function. func_bits_here: bits of enclosing function."""
        for i, node in enumerate(stmts):
            a = node.lineno
            if getattr(node, "decorator_list", None):
                a = min([a] + [d.lineno for d in node.decorator_list])
            compound = hasattr(node, "body") and isinstance(getattr(node, "body"), list)
            hend = header_end(node) if compound else node.end_lineno
            if id(node) in docstrings:
                # only observable through __doc__
                bits = doc_bits
            elif scope == "f":
                bits = bits_lines(f, a, hend)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    bits |= bits_lines(f, a, node.end_lineno)
            else:
                # import-time statement
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    # signature / default / decorator changes only matter when the function
                    # runs, and running it executes its body
                    bits = bits_lines(f, a, node.end_lineno)
                    if not bits:
                        bits = nameref.get(node.name, 0) if not node.name.startswith("__") else 0
                elif isinstance(node, ast.ClassDef):
                    q = (qual + "." if qual else "") + node.name
                    C = resolve(f, q)
                    bits = class_bits(f, q) | nameref.get(node.name, 0)
                    if is_family(C):
                        bits |= users_ext(C, f, q)
                else:
                    bits = bits_lines(f, a, node.end_lineno)  # lambdas etc inside
                    C = resolve(f, qual) if scope == "c" else None
                    if scope == "c" and is_family(C):
                        names = assigned_names(node)
                        ub = users_ext(C, f, qual)
                        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)) and can_narrow(C, names):
                            rb = 0
                            for nm in names:
                                rb |= nameref.get(nm, 0)
                            ub &= rb
                        bits |= ub
                    else:
                        for nm in assigned_names(node):
                            bits |= nameref.get(nm, 0)
                        if scope == "c":
                            bits |= class_bits(f, qual)
                            if C is not None:
                                bits |= class_users.get(qn(C), 0)
                if is_test:
                    bits |= importers_bits(f) if scope == "m" else test_file_bits.get(f, 0)
            size = hend - a
            if scope == "f":
                span = (a, node.end_lineno) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) else (a, hend)
            else:
                span = None  # import-time: no per-query attribution
            for l in range(a, min(hend, n) + 1):
                if best[l] is None or best[l][0] >= size:
                    best[l] = (size, bits, span)
            if compound:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    fb = bits_lines(f, a, node.end_lineno)
                    sub = ("f", qual, fb)
                    hb = fb
                elif isinstance(node, ast.ClassDef):
                    q = (qual + "." if qual else "") + node.name
                    sub = ("c" if scope != "f" else "f", q, func_bits_here)
                    hb = bits
                else:
                    sub = (scope, qual, func_bits_here)
                    hb = bits
                # all statement lists of the compound node
                lists = []
                for fld in ("body", "orelse", "finalbody"):
                    v = getattr(node, fld, None)
                    if v:
                        lists.append(v)
                for h in getattr(node, "handlers", []) or []:
                    # the except line itself
                    he = h.body[0].lineno - 1 if h.body else h.lineno
                    hbits = bits_lines(f, h.lineno, he) if scope == "f" else bits
                    for l in range(h.lineno, he + 1):
                        if best[l] is None or best[l][0] >= he - h.lineno:
                            best[l] = (he - h.lineno, hbits | bits, (h.lineno, he) if scope == "f" else None)
                    lists.append(h.body)
                for c in getattr(node, "cases", []) or []:
                    lists.append(c.body)
                for lst in lists:
                    gap_blocks.append((lst[0].lineno, lst[-1].end_lineno, lst[0].col_offset, hb))
                    visit(lst, *sub)
                # lines inside the node not owned by a child statement (else:, try:, finally:)
                for l in range(a, min(node.end_lineno, n) + 1):
                    if best[l] is None:
                        best[l] = (node.end_lineno - a, hb, (a, hend) if scope == "f" else None)

    top_bits = importers_bits(f) if is_test else 0
    gap_blocks.append((1, n, 0, top_bits))
    visit(tree.body, "m", "", 0)

    # lambdas / nested defs inside import-time statements: their bodies only matter when called
    src_lines = SRC[f].splitlines()
    func_lines = set()

    def mark(node, in_func):
        for ch in ast.iter_child_nodes(node):
            if isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef)):
                mark(ch, True)
            elif isinstance(ch, ast.Lambda):
                if not in_func:
                    lam.append(ch)
                mark(ch, True)
            else:
                mark(ch, in_func)

    lam = []
    mark(tree, False)
    import re as _re

    closer = _re.compile(r"^[\s,)\]}]*(#.*)?$")
    for node in sorted(lam, key=lambda x: (x.end_lineno - x.lineno), reverse=True):
        lb = bits_lines(f, node.lineno, node.end_lineno)
        for l in range(node.lineno + 1, node.end_lineno + 1):
            if l == node.end_lineno:
                rest = src_lines[l - 1][node.end_col_offset:] if l - 1 < len(src_lines) else ""
                if not closer.match(rest):
                    continue
            if best[l] is not None:
                best[l] = (best[l][0], lb, (node.lineno, node.end_lineno))

    SPAN = [None] * (n + 2)
    for l in range(1, n + 1):
        if best[l] is not None:
            M[l] = best[l][1]
            SPAN[l] = best[l][2]

    # strings spanning lines
    strlines = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, (str, bytes)):
            if node.end_lineno > node.lineno:
                strlines.update(range(node.lineno, node.end_lineno + 1))
        elif isinstance(node, ast.JoinedStr) and node.end_lineno > node.lineno:
            strlines.update(range(node.lineno, node.end_lineno + 1))

    # gaps: insertion after old line a (0..n)
    code_line = [M[l] is not None for l in range(n + 2)]
    gaps = []
    for a in range(0, n + 1):
        p = a
        while p >= 1 and not code_line[p]:
            p -= 1
        q = a + 1
        while q <= n and not code_line[q]:
            q += 1
        prev_b = M[p] if p >= 1 else 0
        next_b = M[q] if q <= n else 0
        encl = []
        for (s, e, ind, hb) in gap_blocks:
            # block that could contain an insertion here
            if s <= a + 1 and a <= e:
                encl.append((ind, hb))
        encl.sort()
        gaps.append((prev_b or 0, next_b or 0, encl))
    return M, gaps, sorted(strlines), SPAN


def to_hex(b):
    return format(b, "x")


os.makedirs(OUT, exist_ok=True)
index = {"tests": ids, "files": {}, "data": {}, "dirs": {}, "testfile": {}, "heavy": {}}
if PROFILE and os.path.exists(PROFILE):
    pdir = os.path.dirname(PROFILE)
    for tid, v in json.load(open(PROFILE)).items():
        if tid in IDX and isinstance(v, dict):
            index["heavy"][tid] = {
                "cpu": [q["cpu"] for q in v["queries"]],
                "cpu_compile": [q.get("cpu_compile") for q in v["queries"]],
                # items that always run when the test is checked (optimizer "rest")
                "always": [i for i, it in enumerate(v.get("items", [])) if it == "rest"],
                "tables": [q["tables"] for q in v["queries"]],
            }
    # calib() seconds measured before profiling started (idle machine, one process)
    _p = json.load(open(PROFILE))
    if _p.get("@idle_unit"):
        index["cpu_unit"] = _p["@idle_unit"]
    sizes = {}
    for ds in ("tpch", "tpcds"):
        d = os.path.join(pdir, ds)
        if os.path.isdir(d):
            for fn in os.listdir(d):
                sizes[f"{ds}/{fn[:-4]}"] = os.path.getsize(os.path.join(d, fn))
    index["table_bytes"] = sizes
for i, f in enumerate(FILES):
    if f not in TREES:
        continue
    M, gaps, strl, SPAN = analyse(f)
    groups = {}

    def gi(b):
        if not b:
            return -1
        if b not in groups:
            groups[b] = len(groups)
        return groups[b]

    m = [gi(b) if b is not None else -1 for b in M[: len(M) - 1]]
    ii = []
    for prev_b, next_b, encl in gaps:
        ii.append([gi(prev_b), gi(next_b), [[ind, gi(hb)] for ind, hb in encl]])
    name = f"f{i}.json"
    with open(os.path.join(OUT, name), "w") as fh:
        json.dump(
            {"g": [to_hex(b) for b, _ in sorted(groups.items(), key=lambda kv: kv[1])], "m": m, "i": ii, "s": strl,
             # lines of statements that run at import time (module / class bodies, not lambdas)
             "t": [l for l in range(1, len(M) - 1) if M[l] is not None and SPAN[l] is None]},
            fh,
            separators=(",", ":"),
        )
    index["files"][f] = name

for k, v in data_files.items():
    index["data"][k] = to_hex(v)
for k, v in data_dirs.items():
    index["dirs"][k] = to_hex(v)
for k, v in test_file_bits.items():
    index["testfile"][k] = to_hex(v)
with open(os.path.join(OUT, "index.json"), "w") as fh:
    json.dump(index, fh, separators=(",", ":"))
print("tests", len(ids), "files", len(index["files"]), file=sys.stderr)
