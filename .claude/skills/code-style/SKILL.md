---
name: code-style
description: Use this skill whenever writing or reviewing Python code in this repository (Lambda handlers, tests, future backend modules). Trigger on requests like "write a Lambda function", "add a Python module", "review this Python code", "how should this be styled", or any task that produces/edits `.py` files. Encodes the Python conventions already established in backend/lambda_upload so new code stays consistent.
---

# Python code style for miniature-ai-guide

## Formatting & tooling

- Follow PEP 8. 4-space indentation, no tabs.
- Line length: keep to ~100 chars (matches existing `handler.py`).
- Use type hints on function signatures, including return types (see `lambda_handler(event: dict, context) -> dict`).
- Prefer f-strings for interpolation.
- No linter/formatter config exists yet in this repo — don't introduce black/ruff/mypy config files unless the user asks. Just match the style already in `backend/lambda_upload/handler.py`.

## Module layout (per backend module)

Each backend module (e.g. `backend/lambda_upload/`) is self-contained:

```
backend/<module_name>/
  handler.py          # entry point + helpers
  requirements.txt     # only this module's deps
  tests/
    test_handler.py
  README.md
```

Do not add a repo-wide `requirements.txt` or shared utils package across Lambda modules — each stays independent, matching the existing pattern.

## Lambda handler conventions

- Entry point function named `lambda_handler(event, context)`, returning an API-Gateway-style dict (`statusCode`, `body` as JSON string, headers if needed).
- Extract small, focused helper functions for parsing event input (e.g. reading query string vs JSON body, merging with precedence rules) rather than inlining logic in `lambda_handler`.
- Read configuration from environment variables at call time (`os.environ`), not at import time, so tests can monkeypatch them per-test. Missing required env vars should fail loudly (e.g. HTTP 500), not silently default.
- Give env vars sane optional defaults only when explicitly documented (e.g. `UPLOAD_URL_EXPIRES_SECONDS` defaults to `900`).
- Avoid bare `except:` — catch specific exceptions (`boto3.exceptions.*`, `KeyError`, `ValueError`) and return a meaningful error response.

## Testing conventions

- Use the standard library `unittest` (not pytest) — matches `tests/test_handler.py`.
- Mock `boto3` clients rather than hitting real AWS in tests.
- Test file per module: `tests/test_handler.py`, test class `Test<Thing>`, methods named `test_<behavior>` describing the scenario (e.g. `test_generates_four_urls_in_single_uuid_folder`).
- Cover: happy path, missing/partial input (falls back to defaults), missing required env var, and precedence rules (body overrides query params) where applicable.
- Run tests with `python -m unittest discover -s tests` from the module directory.

## Naming & structure

- snake_case for functions/variables, PascalCase for classes, UPPER_SNAKE_CASE for module-level constants and env var names.
- Keep helper functions pure where possible (no hidden side effects) so they're easy to unit test in isolation from `lambda_handler`.
- Don't add abstractions (base classes, config objects, plugin systems) for a single Lambda — keep it a flat handler + helpers until a second consumer actually needs shared code.

## Docstrings & comments

- Every function and module must have a docstring — see the [[code-documentation]] skill for the full rule, including the requirement to keep each module's `README.md` in sync with the code on every change.
- Default to no inline `#` comments. Only add one when it explains a non-obvious WHY (e.g. why all 4 files share one UUID folder), never to restate what the code does. This "no comments" default applies to inline comments only, not to docstrings.
