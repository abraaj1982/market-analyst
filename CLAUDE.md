# Contributing to this repository

## Non-negotiable rules

1. **No claims of a fixed accuracy rate.** Not "90% accurate" or anything like
   it — not in code, not in the interface, not in the docs.
2. **The confidence score is pure arithmetic.** No language model produces a
   number. Anything the system prints must be re-derivable from
   `docs/SCORING.md`.
3. **No automated trade execution.** The system analyses and alerts.
4. **Closed candles only.** Any code reading an unfinished bar is a bug.
5. **`NOT_EVALUATED` is not `PASSED`.** A gate that did not run is not a gate
   that cleared.
6. **Missing data is never assumed.** `None` and `0` are different statements,
   and the engines must treat them differently: a missing input lowers an
   engine's own quality rather than being filled in.

## Adding an engine

- Subclass `Engine` and implement `_run(ctx) -> EngineResult` as a pure function
  (no I/O, no state)
- Accumulate evidence through `ScoreBuilder` so every reason carries a number
- Declare `applies_to()` explicitly when it does not apply (asset class, market,
  missing extras)
- Add its weight to `settings.yaml` under `weights.base` plus any multipliers
- Add a test proving its direction on hand-built data, and a test proving it
  stands aside safely when data is missing

## Changing the maths

- Update `docs/SCORING.md` in the same commit
- Add or amend a test in `tests/unit/test_scoring.py`
- If you change the calibration, include the measurement the decision rests on

## Performance

The pipeline runs on every symbol every cycle and once per replayed bar in a
backtest, so hot paths matter:

- Prefer vectorised numpy/pandas over per-bar Python loops
- Never use `rolling().apply()` when only the last value is read — that is what
  `percentile_of_last` exists for
- Bound the history an engine examines; unbounded windows are both slower and
  usually less correct
- When optimising, prove the fast path produces **identical** output, not
  similar output

## Style

- Comments explain **why**, not **what**; document the traps you avoided
- All interface text is English; identifiers are English
- The sentiment lexicon carries both English and Arabic terms — local
  announcements are frequently Arabic even though the interface is not
- `ruff check src tests scripts` and `pytest` must both pass before any commit
