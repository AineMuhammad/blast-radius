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
