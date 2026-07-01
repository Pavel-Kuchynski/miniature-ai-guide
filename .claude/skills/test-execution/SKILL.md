---
name: test-execution
description: Use this skill whenever code changes are made in this repo (new function, modified logic, bug fix), when the user asks to "run tests," "check if this passes," or "verify the fix," before finalizing any code review, or proactively after writing new code — don't wait to be asked. This project uses Python's built-in unittest (not pytest) — see backend/lambda_upload/tests.
---

# Test Execution Skill

## When to trigger
- Code changes were made (new function, modified logic, bug fix)
- User explicitly asks to "run tests," "check if this passes," "verify the fix"
- Before finalizing any code review — check if existing tests still pass
- After writing new code — proactively run relevant tests, don't wait to be asked

## Key fact: this repo uses `unittest`, not `pytest`

Each backend module is self-contained under `backend/<module_name>/` with its own `tests/` folder (per CLAUDE.md). There is no repo-wide pytest config, no `pytest-json-report`, no `pytest-cov` — don't reach for pytest tooling here. Everything runs via `python -m unittest` from inside the module directory (e.g. `backend/lambda_upload/`), using that module's `.venv/Scripts/python.exe` if configured.

## Step 1: Discover test scope

Before running, determine what to run:
- If reviewing a diff → identify which module(s) changed (e.g. `backend/lambda_upload/handler.py` changed → tests live in `backend/lambda_upload/tests/test_handler.py`).
- If a changed module has no `tests/` folder or no test file covering the changed function → flag this explicitly as a gap, don't skip silently.
- If unsure what tests exist, list them first:
  ```bash
  python -m unittest discover -s tests -v --dry-run
  ```
  (run from inside the module directory, e.g. `backend/lambda_upload/`)

## Step 2: Execution commands

All commands below are run from inside the relevant module directory (e.g. `backend/lambda_upload/`).

Full run for a module:
```bash
python -m unittest discover -s tests
```

Verbose (see each test name + outcome):
```bash
python -m unittest discover -s tests -v
```

Targeted run — single test file:
```bash
python -m unittest tests.test_handler
```

Targeted run — single test case (fast feedback loop while fixing):
```bash
python -m unittest tests.test_handler.TestLambdaUploadHandler.test_generates_four_urls_in_single_uuid_folder
```

Re-running after a fix: re-run the same targeted test case first, then the full module suite once it passes, to catch regressions.

There is no built-in coverage tool wired up in this repo. Only add `coverage.py` if the user explicitly asks for coverage numbers — don't introduce new dev dependencies unprompted.

## Step 3: Parse the output

`unittest` output (with `-v`) gives one line per test: `test_name (module.Class) ... ok|FAIL|ERROR`, followed by tracebacks for failures/errors and a final summary (`Ran N tests in Xs`, `OK` or `FAILED (failures=N, errors=N)`).

For each non-passing test, extract:
- test id (`module.Class.test_name`)
- outcome: `FAIL` (assertion mismatch) vs `ERROR` (unhandled exception/setup failure)
- the traceback

Note test duration only if the run is noticeably slow — unittest doesn't report per-test timing by default, so don't fabricate numbers; if timing matters, note it qualitatively (e.g. "suite took Xs for N tests").

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
- **Ran**: which command(s), from which module directory
- **Result**: `OK` / `N failed, M errors` out of total
- **Failures/Errors**: test id → root cause (one line each, pointing to file:line in the source, not just the test)
- **Coverage gaps**: any changed code with no corresponding test, called out explicitly
- **Next step**: what to fix first (root-cause fixes before flaky/cosmetic issues)

Keep the report terse — this is a status readout, not a narrative.
