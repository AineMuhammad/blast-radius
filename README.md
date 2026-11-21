# blast-radius

Test selection for the RWX Blast Radius track: given a patch against
[sqlglot](https://github.com/tobymao/sqlglot) at `eb11aec`, decide which tests
can fail and spread them across up to eight workers.

## How it works

**Build time** (not charged)

1. Install the labeling environment from `requirements.lock` and fetch sqlglot
   at the pinned commit.
2. Run the full test suite once under a per-test tracer (`tools/br_trace.py`,
   built on `sys.monitoring`). For every test it records:
   - executed source lines, including work done inside `ProcessPoolExecutor`
     workers and during pickling of expressions sent to them;
   - non-Python files the test opens (SQL fixtures, TPC data) and directories it lists;
   - sqlglot classes it instantiates (expressions, dialects, tokenizers, parsers, generators).
3. Compile those traces into a per-file map (`tools/build_map.py`): for each
   source line, the set of tests that a change on that line can affect.
   - Lines inside functions use statement-level coverage.
   - Class-level data (dialect settings, mapping tables, `arg_types`) maps to
     the tests that use the class, its subclasses, or the dialect that owns it.
   - Lambdas inside class-level tables map to the tests that call them.
   - Insertions map to the neighbouring statements and the enclosing block.

**Run time** (charged, standard library only)

- Parse each diff into changed and inserted line ranges.
- Ignore hunks that only touch comments or blank lines (outside string literals).
- Union the precomputed test sets; fixture edits select the tests that read the fixture.
- Pack with longest-processing-time first for every worker count from 1 to 8
  and keep the cheapest.

Selector CPU time is under 0.1 s for 30 commits.

## Layout

| Path | Purpose |
| --- | --- |
| `Dockerfile` | Builds the environment, runs the traced suite, compiles the map |
| `selector.py` | Runtime selector |
| `tools/br_trace.py` | pytest plugin that records per-test coverage |
| `tools/build_map.py` | Turns traces into the line-to-tests map |
| `tools/evaluate.py` | Local scorer with per-kind breakdown against the oracle |
| `tools/mutate.py`, `tools/label.py` | Generate and label extra validation patches |
| `score.py`, `SCORING.md`, `Makefile` | Official scoring kit |

## Usage

```sh
make sample                     # build, run on the sample commits, score
python3 tools/evaluate.py sample sample/answers.json output.jsonl -v
```

## Results

See [CHANGELOG.md](CHANGELOG.md) for each version and its local score.
