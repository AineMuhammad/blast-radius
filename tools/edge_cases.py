"""Edge-case patches for robustness checks of the selector.

usage: python edge_cases.py <sqlglot_root> <out_dir> <tests.json>
Writes <out_dir>/commits/*.patch, tests.json and expect.json:
  {"<id>": "all" | "none" | "valid"}   (valid = any well-formed answer)
"""

import difflib
import json
import os
import shutil
import sys

ROOT, OUT, TESTS = sys.argv[1:4]
os.makedirs(os.path.join(OUT, "commits"), exist_ok=True)
shutil.copy(TESTS, os.path.join(OUT, "tests.json"))
expect = {}


def src(path):
    with open(os.path.join(ROOT, path), encoding="utf-8") as f:
        return f.read()


def diff(path, old, new, header=True):
    d = "".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True), f"a/{path}", f"b/{path}"))
    return (f"diff --git a/{path} b/{path}\nindex 1111111..2222222 100644\n" if header else "") + d


def write(cid, text, exp, binary=False):
    mode = "wb" if binary else "w"
    with open(os.path.join(OUT, "commits", cid + ".patch"), mode) as f:
        f.write(text)
    expect[cid] = exp


def replace(path, old_s, new_s):
    old = src(path)
    assert old.count(old_s) >= 1, (path, old_s)
    return diff(path, old, old.replace(old_s, new_s, 1))


# 1-3: patches that break importing
write("e01_syntax", replace("sqlglot/dialects/trino.py", "SUPPORTS_USER_DEFINED_TYPES = False",
                            "SUPPORTS_USER_DEFINED_TYPES = = False"), "all")
write("e02_import_nameerror", replace("sqlglot/dialects/trino.py", "SUPPORTS_USER_DEFINED_TYPES = False",
                                      "SUPPORTS_USER_DEFINED_TYPES = UNDEFINED_NAME"), "all")
core = src("sqlglot/expressions/core.py")
first_def = core.index("\ndef ")
write("e03_module_level_error", diff("sqlglot/expressions/core.py", core,
                                     core[:first_def] + "\nraise RuntimeError('boom')\n" + core[first_def:]), "all")
# 4-5: new and deleted modules
write("e04_new_module", "diff --git a/sqlglot/dialects/newd.py b/sqlglot/dialects/newd.py\nnew file mode 100644\n"
      "index 0000000..1111111\n--- /dev/null\n+++ b/sqlglot/dialects/newd.py\n@@ -0,0 +1,2 @@\n+X = 1\n+Y = 2\n",
      "valid")
ec = src("sqlglot/optimizer/eliminate_ctes.py")
write("e05_deleted_module", "diff --git a/sqlglot/optimizer/eliminate_ctes.py b/sqlglot/optimizer/eliminate_ctes.py\n"
      "deleted file mode 100644\nindex 1111111..0000000\n--- a/sqlglot/optimizer/eliminate_ctes.py\n+++ /dev/null\n"
      f"@@ -1,{len(ec.splitlines())} +0,0 @@\n" + "".join("-" + l for l in ec.splitlines(True)), "all")
# 6-8: renames, binary, mode-only
write("e06_rename_fixture", "diff --git a/tests/fixtures/optimizer/eliminate_ctes.sql b/tests/fixtures/optimizer/eliminate_ctes2.sql\n"
      "similarity index 100%\nrename from tests/fixtures/optimizer/eliminate_ctes.sql\n"
      "rename to tests/fixtures/optimizer/eliminate_ctes2.sql\n", "valid")
write("e07_binary", "diff --git a/docs/logo.png b/docs/logo.png\nindex 1111111..2222222 100644\n"
      "Binary files a/docs/logo.png and b/docs/logo.png differ\n", "none")
write("e08_mode_only", "diff --git a/sqlglot/helper.py b/sqlglot/helper.py\nold mode 100644\nnew mode 100755\n", "valid")
# 9-12: encodings and formatting
crlf = replace("sqlglot/generators/clickhouse.py", "values_as_table = True", "values_as_table = False")
write("e09_crlf", crlf.replace("\n", "\r\n"), "valid")
h = src("sqlglot/helper.py")
write("e10_no_newline_eof", diff("sqlglot/helper.py", h, h.rstrip("\n") + "\n# end") + "\n\\ No newline at end of file\n",
      "valid")
write("e11_non_utf8", b"diff --git a/README.md b/README.md\nindex 1111111..2222222 100644\n--- a/README.md\n"
      b"+++ b/README.md\n@@ -1,1 +1,1 @@\n-x\n+\xff\xfe caf\xe9\n", "valid", binary=True)
write("e12_empty", "", "none")
# 13-16: docs, CI, pytest config
write("e13_readme", diff("README.md", src("README.md"), src("README.md") + "\nMore docs.\n"), "none")
write("e14_ci", "diff --git a/.github/workflows/x.yml b/.github/workflows/x.yml\nnew file mode 100644\n"
      "--- /dev/null\n+++ b/.github/workflows/x.yml\n@@ -0,0 +1,1 @@\n+on: push\n", "none")
write("e15_conftest", "diff --git a/tests/conftest.py b/tests/conftest.py\nnew file mode 100644\n--- /dev/null\n"
      "+++ b/tests/conftest.py\n@@ -0,0 +1,2 @@\n+import pytest\n+collect_ignore = ['dialects']\n", "all")
pp = src("pyproject.toml")
write("e16_pyproject_plain", diff("pyproject.toml", pp, pp.replace("\n", "\n# note\n", 1)), "none")
# 17-20: multi-file, bad context, test file, many hunks
multi = (replace("sqlglot/generators/clickhouse.py", "values_as_table = True", "values_as_table = False")
         + diff("tests/fixtures/optimizer/eliminate_ctes.sql", src("tests/fixtures/optimizer/eliminate_ctes.sql"),
                src("tests/fixtures/optimizer/eliminate_ctes.sql") + "\n")
         + diff("README.md", src("README.md"), src("README.md") + "\nx\n"))
write("e17_multi", multi, "valid")
bad = replace("sqlglot/generators/clickhouse.py", "values_as_table = True", "values_as_table = False")
write("e18_bad_context", bad.replace("            values_as_table = any", "            values_as_tablX = any"), "valid")
tm = src("tests/dialects/test_mysql.py")
write("e19_test_file", diff("tests/dialects/test_mysql.py", tm, tm.replace("def test_ddl(self):", "def test_ddl(self):\n        self.assertTrue(True)", 1)), "valid")
p = src("sqlglot/parser.py")
lines = p.splitlines(True)
for i in range(200, len(lines), 400):
    if lines[i].strip() and not lines[i].strip().startswith(("#", '"', "'")):
        lines[i] = lines[i].rstrip("\n") + "  # touched\n"
write("e20_many_hunks", diff("sqlglot/parser.py", p, "".join(lines)), "none")

with open(os.path.join(OUT, "expect.json"), "w") as f:
    json.dump(expect, f, indent=1)
print(len(expect), "edge cases")
