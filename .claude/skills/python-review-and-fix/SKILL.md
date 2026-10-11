---
name: python-review-and-fix
description: Review Python code with the python-code-reviewer agent, consolidate its findings into a prioritized list, then have the python-developer agent fix them. Focus areas are bad code style, composition and architecture problems, and oversized comment blocks. Use this skill whenever the user asks to review, audit, clean up, or refactor Python files or a backend Lambda module (e.g. "review backend/start_job", "check this handler for problems", "clean up this Python code", "find architecture issues and fix them"), even if they don't mention agents or a findings list.
---
# Python review and fix

Run a review-then-fix pipeline on Python code. One agent finds problems, you turn its output into a clear findings list, and a second agent fixes them. Splitting the roles keeps the reviewer independent of the code's author and gives the user a checkpoint before any file is changed.

## Step 1: Scope the review

Decide which files to review from the user's request: a path, a Lambda module (`backend/<name>/`), or the current diff (`git diff main...HEAD -- '*.py'`). If nothing is specified, default to the Python files changed on the current branch. List the files you will review so the scope is explicit.

## Step 2: Review with `python-code-reviewer`

Launch the `python-code-reviewer` agent via the Agent tool. Give it the file paths, the repo conventions (`CLAUDE.md`, the `python-engineering-style` and `code-documentation` skills), and ask it to focus on three areas, because these are what the user cares about most:

1. **Bad code style**: naming, overly long functions, duplication, magic numbers/strings, inconsistent typing, missing docstrings, deviations from the repo's Python conventions.
2. **Composition and architecture**: mixed responsibilities (e.g. a handler doing parsing, AWS calls, and business logic together), tight coupling, hidden globals, hard-to-test dependencies, wrong abstraction boundaries, logic that belongs in a separate module.
3. **Big comment blocks**: long block comments or multi-paragraph inline comments that narrate what the code does, commented-out code, and stale comments. A comment longer than about 3 lines usually means the code needs a better name, a smaller function, or a docstring instead. Docstrings are fine and required; the target is block comments in function bodies and module headers.

The reviewer should still report correctness or security problems if it sees them, but these three areas drive the report. Ask it to return each finding with location, severity, issue, and a concrete suggestion, plus its verdict and any assumptions.

## Step 3: Build the findings list

Do not forward the raw review. Consolidate it into one numbered list, ordered by severity (Critical, Major, Minor, Nitpick), so the user and the developer agent both work from the same artifact:

```
## Findings
| # | Severity | Category | Location | Issue | Proposed fix |
|---|----------|----------|----------|-------|--------------|
| 1 | Major | Architecture | handler.py:lambda_handler | Handler mixes S3 access and validation | Extract `validate_event()` and `S3Client` wrapper |
```

Categories: `style`, `architecture`, `comments`, `correctness`, `security`, `tests`. Merge duplicates, drop findings that contradict repo conventions, and mark anything you are unsure about as "needs decision". Show the table to the user along with the reviewer's verdict.

## Step 4: Confirm what to fix

Fixing edits files, so check scope before changing anything. Ask the user whether to fix everything, only Critical/Major, or a chosen subset. If the user already said "review and fix" or similar, proceed with all findings except those marked "needs decision", and say so.

## Step 5: Fix with `python-developer`

Launch the `python-developer` agent with the approved findings (the table, not a summary), the file paths, and these constraints:

- Fix only the listed findings. No unrelated refactors, since extra changes make the diff harder to review.
- Keep public behavior and function signatures unless a finding explicitly requires changing them.
- Replace big comment blocks by improving names, extracting functions, or moving the explanation into a docstring. Delete commented-out code.
- Update docstrings and the module `README.md` when behavior or structure changes.
- Run the module's tests (`python -m unittest discover -s tests`) and add or adjust tests for extracted code.
- Report, per finding number, what was changed or why it was skipped.

## Step 6: Verify and report

Run the module's tests yourself. Optionally send the changed files back to `python-code-reviewer` for a single re-check if any Critical or Major finding was fixed. Finish with a short report: findings fixed, findings skipped with reasons, test results, and any remaining follow-ups. If tests fail, say so with the output instead of calling the work done.
