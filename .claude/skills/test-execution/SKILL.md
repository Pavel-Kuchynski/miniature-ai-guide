---
name: test-execution
description: Use this skill whenever code changes are made in this repo (new function, modified logic, bug fix), when the user asks to "run tests," "check if this passes," or "verify the fix," before finalizing any code review, or proactively after writing new code — don't wait to be asked. Runs the module's own test command (pytest for newer modules such as backend/paint, unittest for older ones such as backend/lambda_upload) and interprets the result. Test writing rules live in python-unit-testing.
---

# Test Execution Skill

This skill is about **running and interpreting** tests. How to write them is in the `python-unit-testing` skill.

## When to trigger
- Code changes were made (new function, modified logic, bug fix)
- User explicitly asks to "run tests," "check if this passes," "verify the fix"
- Before finalizing any code review — check if existing tests still pass
- After writing new code — proactively run relevant tests, don't wait to be asked

## Key fact: the framework depends on the module

Each backend module is self-contained under `backend/<module_name>/` with its own `tests/` folder (per CLAUDE.md). There is no repo-wide test config, so always run from inside the module directory, using that module's `.venv/Scripts/python.exe` if configured.

Detect the framework in this order and stop at the first match:
1. The test command in the module's `README.md`.
2. `pytest` in the module's `requirements-dev.txt`, or a `conftest.py` in `tests/` -> **pytest**.
3. Otherwise -> **unittest**.

Running the wrong runner silently collects zero tests (`python -m unittest` ignores plain pytest classes). Always check that the output reports a non-zero test count, and treat "0 tests" as a failure of the run, not as a pass.

Do not add new dev dependencies (coverage tools, plugins) unless the user asks for them.

## Step 1: Discover test scope

Before running, determine what to run:
- If reviewing a diff -> identify which module(s) changed (e.g. `backend/paint/handler.py` changed -> tests live in `backend/paint/tests/`).
- If a changed module has no `tests/` folder or no test file covering the changed function -> flag this explicitly as a gap, don't skip silently.
- If unsure what tests exist, list them first (from the module directory):
  - pytest: `pytest --collect-only -q`
  - unittest: `python -m unittest discover -s tests -v` (this runs them; use `-v` to see names)

## Step 2: Execution commands

All commands are run from inside the module directory (e.g. `backend/paint/` or `backend/lambda_upload/`).

| Goal | pytest | unittest |
|---|---|---|
| Full module run | `pytest` | `python -m unittest discover -s tests` |
| Verbose | `pytest -v` | `python -m unittest discover -s tests -v` |
| Single file | `pytest tests/test_handler.py` | `python -m unittest tests.test_handler` |
| Single test case | `pytest tests/test_handler.py::TestLambdaHandler::test_happy_path_paints_job` | `python -m unittest tests.test_handler.TestLambdaUploadHandler.test_generates_four_urls_in_single_uuid_folder` |
| Stop at first failure | `pytest -x` | `python -m unittest discover -s tests -f` |

Re-running after a fix: re-run the same targeted test case first, then the full module suite once it passes, to catch regressions.

There is no built-in coverage tool wired up in this repo. Only add `coverage.py` if the user explicitly asks for coverage numbers — don't introduce new dev dependencies unprompted.

## Step 3: Parse the output

Both runners print a summary line: pytest `N passed, M failed, K errors in Xs`; unittest `Ran N tests in Xs` then `OK` or `FAILED (failures=N, errors=N)`. Use `-v` to get one line per test.

For each non-passing test, extract:
- test id (pytest `tests/test_handler.py::Class::test_name`; unittest `module.Class.test_name`)
- outcome: assertion failure (`FAIL` / `F`) vs unhandled exception or setup failure (`ERROR` / `E`)
- the traceback

Report suite duration from the summary line only. Don't fabricate per-test timings.

## Step 4: Interpretation rules (this is where judgment matters)

- **A failing test after a code change is not automatically "the test is outdated."**
  Default assumption: the code broke the contract. Only conclude the test itself is
  wrong if the test's expected behavior contradicts the stated requirements (e.g. CLAUDE.md's documented Lambda behavior).
- **Traceback → root cause, not just re-paste.** Map the exception (e.g., `KeyError: 'UPLOAD_BUCKET_NAME'`) to the specific line in the changed code that likely caused it — don't just show the raw traceback.
- **Missing coverage is not equally important everywhere.** Prioritize, in order:
  1. Uncovered error paths (missing env var → 500, missing required input → fallback)
  2. Uncovered branches (query-string vs JSON-body precedence, singular vs plural param handling)
  3. Uncovered trivial code (simple helpers, constant defaults) — lowest priority, mention but don't block on it
- **Flaky test detection**: if a test fails intermittently across reruns with no code
  change (e.g. depends on `uuid`/time without mocking), flag it as potentially flaky
  rather than treating it as a real regression.
- **New code with no corresponding test is a finding, not a silent gap** — explicitly
  call this out as "Missing test coverage" in the review, don't just skip it. Per CLAUDE.md, new Lambda modules are expected to ship with their own `tests/test_handler.py`.

## Step 5: Report format

Summarize as:
- **Ran**: which command(s), from which module directory, and which framework was detected
- **Result**: passed / failed / errors out of total (must be a non-zero total)
- **Failures/Errors**: test id → root cause (one line each, pointing to file:line in the source, not just the test)
- **Coverage gaps**: any changed code with no corresponding test, called out explicitly
- **Next step**: what to fix first (root-cause fixes before flaky/cosmetic issues)

Keep the report terse — this is a status readout, not a narrative.
