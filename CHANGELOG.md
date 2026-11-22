# Changelog

Local score = `make sample` simulated seconds on the 30 sample commits (oracle floor 94.351 s).
Runtime CPU measured in-container on the samples.

## v1 — coverage map
- Build: install requirements.lock, fetch sqlglot @ eb11aec, run the full suite once under a
  sys.monitoring per-test tracer (follows ProcessPoolExecutor tasks into spawn workers,
  records fixture reads, directory listings, and instantiated sqlglot classes).
- Map: per source line -> tests (statement-level widening; class-level data -> tests that
  instantiated the class / subclass / owning dialect; lambda bodies -> their own coverage;
  insertions -> neighbouring statements + enclosing block). Fixture/data files -> tests that opened them.
- Runtime: comment/blank-only hunks are no-ops; replacements use the removed lines' tests;
  LPT packing for every k in 1..8.
- Local: **179.611 s**, 0 misses. CPU ≈ 0.08 s.
- Board (279c3ef): **191.2 s** — 191.051 s simulated, 0.18 s CPU, 0 misses, identical in all 3 runs.

## v2 — narrowing and local runs
- Class-level assignments narrowed by name readers when safe.
- Small, cheap selections executed in-container; only failures are scheduled.
  Run only when the expected saving (50% failure assumption) beats the measured CPU cost.
- Samples: **162.551 s** simulated, 0 misses, CPU 5.5–6.3 s (measured while the machine was busy).
- 25 generated mutants: 82.7 s simulated + 3.8 s CPU, 0 misses (v1: 102.1 s).
- Board (5516f32): **182.9 s**, 0 misses.

## v3 — TPC query models, sharper class-level rules
- TPC executor tests modelled per query (pseudo-tests in the map). A change reaching few
  queries is checked by re-running only those; a change reaching only compile-time code
  (parser, optimizer, planner) is checked by comparing a canonical digest of the execution
  plan and the generated DuckDB SQL with the unpatched outputs, without executing.
  TPC-H tests are checked almost for free.
- Function signature/default/decorator edits select the tests that run the function,
  not every reference to its name.
- Docstring edits select only code that reads `__doc__`.
- Settings read at import time are narrowed unless read by class-creation code or from an
  instance of the edited class made during import (tracer now records those instances).
- Wall-clock cap for local runs.
- Samples: **162.551 s** + 3.7 s CPU, 0 misses (3 identical runs).
- 80 generated mutants: **357.7 s** + 16.7 s CPU, 0 misses (v2: 457.2 s + 5.5 s).
- Clean build: 3.6 min on 4 cores.

## v4 — per-dialect generator-memory model, deterministic plan digest
- test_generator_memory modelled per dialect (33 independent subtests); a change reaching
  some dialects re-runs only those subtests in-container.
- Plan digest is content-based (Merkle over the step DAG), so it no longer depends on object
  identity; compile-only TPC-DS checks now confirm harmless optimizer/parser edits.
- Samples: **142.584 s** + 8.5 s CPU = 151.1 s, 0 misses (3 identical runs).
- 80 generated mutants: 330.7 s + 21.3 s CPU, 0 misses (v3: 357.7 s + 16.7 s).
- Clean build: 4.1 min.
- Board (791a089): **178.6 s** (176.197 s simulated + 2.36 s CPU), 0 misses.

## v5 — hardening for the final
- Patched sources are compiled; when import-time code changes, the patched modules are
  test-imported in a forked child. A syntax error or a failing import selects every test.
- Deleted or renamed modules, `conftest.py`, and pytest sections of config files select
  every test. The selector falls back to the whole suite if it ever fails outright.
- The map covers every Python file the suite runs (e.g. `sqlglotc/setup.py`).
- Profiled costs are normalized by a calibration workload measured on an idle build
  machine, so local-check decisions are deterministic across runs.
- Every item model is self-checked against the unpatched code at build time; failures
  are dropped. Optimizer per-entry models and the sentinel rule are implemented but off:
  on fresh out-of-sample commits the first gained nothing, the second missed once.
- Gates: 20 edge-case patches OK; 0 misses on samples (4 identical runs), 80 + 48 + 60
  labeled mutants; 60 commits in 31 s wall; clean build 4.4 min on 4 cores.
- Samples: **121.130 s** + 24.5 s CPU = 145.6 s. Fresh 48: 264.5 s + 11.0 s.

## v6 — first-line coverage, lambda-body edits
- The tracer also listens to PY_START and records each code object's first line. LINE
  events alone never fire for code on a function's first line, so one-line lambdas and
  one-line functions were invisible to coverage (earlier versions were saved by broad
  fallbacks).
- An edit confined to the body of a lambda in a module/class-level table selects the
  tests that call that lambda (lambdas that run during import are excluded).
- Gates: edge cases OK; 0 misses on samples, 80 + 48 + 60 labeled mutants.
- Samples: 121.130 s + 23.0 s CPU (unchanged selection, safer coverage).

## v7 — proven-equivalent comparison edits
- Offline probe run (tools/br_trace.py, BR_PROBES=1): every single comparison in sqlglot
  is compiled as a probe on the same line that records, at every evaluation of the full
  suite, whether the partner operator (is/==, is not/!=, </<=, >/>=) gives the same
  result (`==` must return a real bool; ordering operands must be plain numbers/strings
  or tuples of them and never equal). 1,328 of 1,885 evaluated sites are proven.
  The map is committed (maps/cmp_equivalent.json); the probe run is too slow for the build.
- At run time a one-line edit that only swaps an operator for its partner at a proven
  site behaves identically for every test, by induction over the original run, so it
  selects nothing.
- Samples: **106.587 s** + 8.4 s CPU = 115.0 s (v6: 144.1 s), 0 misses.
  Fresh 48: 264.5 s simulated, 0 misses.

## v7.1 — decisions independent of timing and load
- Local-check costs come only from tests.json seconds and the share of a test's work the
  selected items represent (a CPU ratio from one profiling process, so speed and load
  cancel out). No calibration, wall-clock budget or measured CPU enters a decision.
- The local-work budget is a sum of these estimates. Safety timeouts remain; each one
  resolves to keeping the tests (a timed-out test counts as failing, a killed child keeps
  the selection, a slow model check counts as a mismatch).
- 80-random set: two runs byte-identical (sha256 7b9d6fb24d2f15bb...). Versus v6: -19.5 s
  from three proven-equivalent `is None` swaps, +21.5 s on m01037, where v6 had timed this
  machine to justify a run-phase TPC-DS check.
- Samples: **106.587 s** + 4.1 s CPU = 110.6 s, byte-identical runs, 0 misses.
  Fresh 48, 80 random, 39 of the 150-set: 0 misses. Edge cases OK.
- Board (23e305c): **177.2 s** (174.19 s simulated + 3.05 s CPU), 0 misses.

## v8 — check outcomes depend only on outputs
- TPC checks have no internal deadline. When a compile-only query's plan differs, that
  query is run fully and its results compared (the test compares results, not plans).
- Safety kills are sized for the worst case with a wide margin (TPC: every query runs
  fully, x5 + 60 s; tests: max(10 s, 10x recorded + 5 s)); when one fires the tests are kept.
- 80-random set: byte-identical output with the machine idle and fully loaded.
- Samples 106.587 s; fresh 48: 248.389 s (was 264.502: m02044 drops TPC-DS, CPU-neutral
  there because every plan changed). 0 misses on samples, fresh, 80 random, 150-set so far.
- Probe branch `probe-no-tpcds` (74603c4): never schedules TPC-DS, for board measurement only.

## v9 — TPC phase fix (soundness), class-attribute reads
- FIX: the compile-only TPC check treated all of `execute()` before the executor as
  compile phase, but `execute()` also builds the table mapping the executor reads, which
  the plan digest does not cover. A change there (150-set m03135: `ensure_tables` returning
  `{}`) passed the check while test_execute_tpcds failed. Compile phase is now only what
  runs inside optimize() and Plan(); everything else in execute() needs a full run, and
  the compile-only check also compares the tables each query hands to execute().
  Affected v3..v8, including 23e305c.
- The tracer records reads of UPPERCASE scalar class attributes (non-data descriptor),
  resolved to the defining class. A class-level assignment of such an attribute selects
  the tests that read that definition, unless class-creation code reads it, an object of
  a related class made at import reads it, or any class/module body references the name.
- Labels: three 150-set commits had spurious failures from timeouts while the machine was
  overloaded; re-labeled on an idle machine (m03051, m03052, m03053). Rule: every reported
  miss is re-labeled on an idle machine before it is trusted.
- Samples 106.391 s + 2.7 s CPU; fresh 248.461 s; 80 random 280.956 s (v8 299.125 s);
  150-set 666.871 s. 0 misses on all sets; edge cases OK; byte-identical runs.

## v9.1 — precise TPC run phase, capped fallbacks
- Run phase = ensure_tables (the table mapping handed to the executor), PythonExecutor,
  result pickling, plus every line of execute()'s own body. The rest of execute() (schema
  preparation, logging) is compile phase, which a compile-only check really executes.
  Targeted tests: ensure_tables returning {} and PythonExecutor(tables={}) both schedule
  test_execute_tpcds.
- At most 3 plan-changed queries per check fall back to a full run; beyond that the test is
  scheduled (deterministic count, no timing). Fresh m02044 had spent 26 s CPU for 16 s.
- Samples 106.391 s + 2.5 s; fresh 264.6 + 10.9 s; 80 random 281.0 + 13.6 s;
  150-set 682.8 + 39.6 s. 0 misses everywhere; byte-identical runs; edge cases OK.
- Probes with a 100 s CPU burn (can never become our best score):
  probe-no-tpcds cf92b43, probe-no-long-tests 7de20ae.
- Board (TPC-DS probe on 23e305c): 417.377 s simulated with 1 miss -> 117.4 s without the
  penalty; TPC-DS costs ~56.8 s on the board (~22 s on the commit that breaks it).

## v9.2 — lambda-edit crash fix
- FIX: the map builder assigned the lambda-body groups after the file's group list had been
  serialized, so their indices pointed past it. Every one-line edit inside a lambda in a
  class/module-level table raised IndexError in the selector, whose safety net then selected
  the whole suite. Since v6. The build now fails if a map refers to a missing group.
- 308 labeled commits: 4 crash fallbacks before, 0 after.
- Samples 110.3 s; fresh 267.2 s (v9.1 280.4); 80 random 301.1 s; 150-set 631.2 s
  (v9.1 739.8). 0 misses everywhere; byte-identical runs; edge cases OK.
- Board: v9 (6c0d7c7) 174.19 + 3.00 s = 177.2; v9.2 (d3a2cbb) 174.19 + 3.14 s = 177.3;
  probe C (98af044) 438.831 s sim, 1 miss -> TPC-DS in category C costs 35.4 s, of which
  ~14 s is unnecessary; one A/B commit wastes 21.45 s.

## v9.3 — run-phase TPC checks
- TPC model checks are costed at 0.75 x the test's tests.json seconds (measured in-process
  ratio; a constant) and a failing check at 0.1 of that (it stops at the first mismatch), so
  a full run-phase check runs when it is cheaper than scheduling the test.
- 80 random 259.5 + 40.8 s (v9.2 281.0 + 21.6); 150-set 577.4 + 55.2 s (v9.2 581.0 + 54.9);
  samples and fresh unchanged. 0 misses; byte-identical runs; edge cases OK.

## v9.4 — attribute reads per TPC query; never-executed files
- The per-query TPC profiler records class-attribute reads like the main tracer, so an
  edit to a dialect flag that TPC queries read gets per-query bits and can be checked.
- Files whose code never runs in the traced suite (sqlglot/__main__.py) select nothing.
- 150-set 540.6 + 50.8 s (v9.3 577.4 + 55.0); other sets unchanged. 0 misses; edge cases
  OK; byte-identical runs.
- v9.4.1: packing breaks ties by test number. v9.4 chose identical tests on every commit
  but could order equal-duration tests differently between runs (set insertion order follows
  per-run string hashing). Now byte-identical on all four labeled sets.
