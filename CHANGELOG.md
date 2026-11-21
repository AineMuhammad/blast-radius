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
