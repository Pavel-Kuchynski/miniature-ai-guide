---
name: code-documentation
description: Use this skill whenever writing, modifying, or reviewing Python code in this repository (Lambda handlers, tests, future backend modules), or when touching a backend module's README.md. Trigger on requests like "write a Lambda function", "add a Python module", "fix a bug in handler.py", "review this Python code", or any task that adds/edits/reviews `.py` files. Mandates that every function has a docstring and that each package/handler's README.md is kept up to date with the code, as part of the same change — not a follow-up task.
---

# Code documentation rules for miniature-ai-guide

## Non-negotiable rule

Any change to code in a backend module (`backend/<module_name>/`) MUST be accompanied by documentation updates in the same change. Documentation is part of the change, not optional cleanup or a follow-up task. A code change is not "done" until its docstrings and README are updated to match.

## Function & module docstrings

- Every function must have a docstring — public entry points and private/underscore-prefixed helpers alike. One-line summary is the minimum; add `Args`/`Returns`/`Raises` only when behavior isn't obvious from the name, type hints, and signature (don't redundantly restate types already visible in the signature).
- Every module (e.g. `handler.py`, `test_handler.py`) gets a short module-level docstring describing its purpose.
- This is a targeted exception to the [[code-style]] skill's general "default to no comments" guidance: that guidance still governs inline `#` comments (avoid restating WHAT the code does; only explain non-obvious WHY). Docstrings are required documentation, not comments, and are not covered by the "no comments" default.

## Per-module README.md

Each `backend/<module_name>/` must have a `README.md` that accurately reflects the current code. At minimum, it documents:

- Purpose of the module/handler.
- Entry point signature (e.g. `lambda_handler(event, context) -> dict`).
- Required and optional environment variables, including defaults.
- Request/response shape: input parsing rules, precedence rules (e.g. body overrides query params), error response format.
- How to install dependencies and run tests.

## When code changes, update docs in the same change

- New function, parameter, or env var → add/update its docstring and the relevant README section.
- Changed behavior (defaults, precedence rules, error responses, validation) → update the README immediately, don't defer to a later pass.
- Renamed or removed function/parameter → remove the stale doc reference; don't leave dangling descriptions of code that no longer exists.
- Before considering any code task complete, re-read the affected `README.md` end to end and confirm every claim in it still matches the current code.

## Enforcement in review

`python-code-reviewer` must treat a missing/stale docstring or a README that no longer matches the code as a **Major** finding (same severity tier as a test-coverage gap for documented behavior) — not a 🔵 Nitpick. A PR/change that adds or changes behavior without a corresponding doc update should not receive an "Approve" verdict.
