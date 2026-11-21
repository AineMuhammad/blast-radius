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
