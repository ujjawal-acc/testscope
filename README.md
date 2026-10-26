# testscope: run only the Apex tests a change can affect

**Agentia™ Headless Hackathon 2026 submission**

## Problem
Copado Agentia helps with deployments inside Salesforce, but the pipeline still runs the whole suite
(`RunLocalTests`) on every change. In the benchmark org (apex-recipes: 142 components, 69 test
classes) about 130 tests already fail, so a new regression is easy to miss and every run is slow.

## What it does
`testscope.py` takes the changed component, walks the dependency graph (who references it,
transitively) and selects only the test classes that can reach it. It then:

1. skips test classes that already fail in a recorded baseline (`baseline_failures.txt`), so only *new* failures are reported,
2. prints a gate verdict (selected tests are enough for the *analysed* dependencies),
3. exports `AGENTIA_APEX_TEST_CLASSES` for the Agentia CLI, or runs the selected tests directly with `--run`.

```
python testscope.py\testscope.py --changed TriggerHandler --baseline-failures baseline_failures.txt --run --target-org devorg2
```

## How it was evaluated
`benchmark.py` seeds a bug into one class, deploys it, runs testscope's selected tests, runs the full
`RunLocalTests` suite, compares the two, then restores the original and redeploys it.
"Caught" = new failing classes (not in the baseline) found by the selected run, out of those found by the full run.

| # | Class | Bug seeded | Caught | Full suite | Selected | Time saved |
|---|-------|-----------|--------|-----------|----------|-----------|
| 1 | TriggerHandler | negated `return` in `isBypassed` | 1/1 | 133.4 s | 28.2 s | 79% |
| 2 | MetadataTriggerService | `if (Trigger.new != null)` inverted (line 62) | **1/4** | 26.4 s | 25.3 s | 4% |
| 3 | FormattingRecipes | `return result.getValueAsString();` changed to `return null;` (line 17) | 1/1 | 57.8 s | 8.4 s | 85% |

All three seeded bugs were detected by the selected run, but across the three cases only 3 of the 6 new
failing classes seen in the full runs were caught. Raw data: `results.csv`. Evidence (diffs, deploy logs,
full-suite JSON, testscope output): `evidence/`.

## Limitations (please read)
- **Case 2 missed 3 classes.** `AccountServiceLayer_Tests`, `InboundEmailHandlerRecipes_tests` and `LDVRecipes_Tests` failed in the full run but not in the selected run. The cause is not yet established: either they were not selected (an analysis gap) or they were selected but behaved differently when run alone. `evidence/bug2_testscope_r1.txt` shows which.
- **Timing is very noisy.** The same full suite took 310.8 s, 133.4 s, 57.8 s and 26.4 s on different runs, so the time-saved column comes from single runs and is indicative only (the 4% in case 2 is within noise). Repeat runs with `python benchmark.py --only 1,2,3 --repeats 3` give medians.
- **Tiny sample.** Three hand-seeded bugs in one org. This shows the idea works on some cases, not that it is generally accurate.
- **Static analysis only.** Dynamic Apex, `Type.forName`, metadata-driven triggers and Flow/config references are not analysed.
- **Baseline blind spot.** A bug inside a class whose tests already fail in the baseline cannot be detected as "new".
- Org flakiness can look like a "new" failure; rerun to confirm.

## Setup
Python 3.9+, Salesforce CLI (`sf`), an authorised org alias, and a `baseline_failures.txt` made from a clean full run.

## Reproduce
```
python benchmark.py --only 1,2,3 --repeats 3
```
Run from the folder containing `apex-recipes/`, `testscope.py/` and `baseline_failures.txt`. It restores every seeded bug with `git restore` and redeploys the original.

## Repo layout
`testscope.py/` (tool) · `benchmark.py` · `baseline_failures.txt` · `results.csv` · `evidence/`
