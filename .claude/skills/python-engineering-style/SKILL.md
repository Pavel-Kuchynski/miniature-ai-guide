---
name: python-engineering-style
description: Use this skill whenever writing, reviewing, refactoring, or extending Python code in this repository, especially AWS Lambda handlers, backend modules, and integrations. Trigger on requests like "write Python code", "add a Lambda", "create a backend module", "review this Python code", "fix this handler", "add logging", "handle errors", or any task that creates or modifies `.py` files. Encodes the repository's conventions for Python structure, naming, logging, error handling, AWS integrations, and maintainability.
---
# Python engineering style
Use this skill for all Python code written or reviewed in this repository.

## Core principles

* Prefer explicit, readable code over clever or compressed code.
* Preserve established repository patterns when modifying existing code.
* Do not rewrite working code merely to match personal preferences.
* Distinguish actual bugs and security issues from stylistic preferences.
* Keep functions focused and independently testable where practical.
* Prefer simple, flat designs over premature abstractions.
* Make production code observable through structured logging.
* Validate external input and configuration at system boundaries.
* Handle errors explicitly and safely.
* Never expose secrets or internal implementation details in logs or API responses.

## Formatting

* Follow PEP 8.
* Use 4-space indentation. Never use tabs.
* Keep lines to approximately 100 characters.
* Use type hints on function signatures, including return types.
* Prefer f-strings.
* Use standard library, third-party, and local import groups.
* Match formatting of nearby existing code.
* Do not introduce Black, Ruff, MyPy, or repository-wide tooling unless explicitly requested.

## Naming

* Functions and variables: `snake_case`.
* Classes and exceptions: `PascalCase`.
* Constants and environment variables: `UPPER_SNAKE_CASE`.
* Private helpers: prefix with `_`.
* Boolean predicates should use names such as `is_`, `has_`, `can_`, or `should_`.
* Use descriptive domain-specific names.
* Keep API/domain terminology consistent inside Python code.

For detailed naming rules, read:
`references/naming.md`

## Module structure

Prefer self-contained backend modules:

```text
backend/<module_name>/
  handler.py
  requirements.txt
  tests/
    test_handler.py
  README.md
```

Do not introduce shared utility packages, repo-wide requirements files, or generic abstractions unless there is a demonstrated need.

Reference implementation: `backend/paint`. When a module grows beyond a single flow:

* one class per module, named after the class in `snake_case`
  (`job_status.py` -> `JobStatus`, `job_record.py` -> `JobRecord`,
  `job_not_found_error.py` -> `JobNotFoundError`);
* use flat imports (`from job_status import JobStatus`), because the Lambda root is on `sys.path`;
* keep logging in `json_formatter.py`, `structured_logger_adapter.py` and `logging_config.py`
  (see `references/logging.md`);
* use `StrEnum` for status values, and a frozen `@dataclass` for a small typed view of a record;
* add `requirements-dev.txt` for test tools and type stubs, and pin runtime dependencies in
  `requirements.txt`;
* document the module layout in the README.

For Lambda modules, prefer this logical order:

1. module docstring;
2. imports;
3. constants and lightweight caches;
4. custom exceptions;
5. response helpers;
6. logging helpers;
7. input extraction;
8. validation/authentication;
9. external-service helpers;
10. persistence helpers;
11. Lambda entry point.

Keep the handler focused on orchestration.

## Lambda conventions

* Entry point: `lambda_handler(event, context)`.
* Return API-Gateway-style responses when applicable.
* Extract parsing and validation into focused helpers.
* Read environment variables at call time when tests need to monkeypatch them.
* Validate required environment variables explicitly.
* Do not silently default required configuration.
* Catch specific exceptions at meaningful boundaries.
* Do not use bare `except:`.
* Name constants for timeouts, retry counts, limits and defaults, and comment *why* the value
  is what it is (budgets, cost, bounds). No magic numbers inline.
* Optional configuration reads `os.environ.get(NAME, DEFAULT_NAME)` with a documented
  `DEFAULT_*` constant; required configuration uses `os.environ[NAME]`.
* For SQS-triggered Lambdas (no API Gateway response), follow `references/sqs-lambda.md`:
  when to raise (message goes to the DLQ) and when to mark the job `FAILED` and return.
* Keep module-level caches explicit, bounded and resettable (see `references/aws.md`).

## Logging

Use the repository's established logging infrastructure: `configure_logger(__name__)` for the
module logger and `StructuredLoggerAdapter` for per-job context (details in
`references/logging.md`). Use lazy `%s` formatting and `log.exception` for caught failures.

Prefer structured logs with useful correlation context such as:

* `jobId`;
* `userId`;
* `connectionId`;
* `stage`.

Log meaningful workflow events and failures.

Never log:

* JWTs;
* access tokens;
* refresh tokens;
* passwords;
* API keys;
* private keys;
* authorization headers.

Do not log complete Lambda events unless explicitly required and known to contain no secrets.

For detailed logging conventions, read:
`references/logging.md`

## Error handling

* Catch the narrowest meaningful exception.
* Translate internal failures into stable external responses.
* Keep diagnostic details in logs rather than API responses.
* Use semantically appropriate HTTP status codes.
* Do not expose stack traces or raw exception messages to clients.
* Use custom exceptions for meaningful domain-specific error boundaries.
* Use broad `Exception` only at deliberate top-level safety boundaries.

* Client-visible failure text for queue messages contains only the exception class name.
* Helpers that must never raise (status writes, notifications in a failure path) catch the
  narrowest exceptions, log, and say so in their docstrings.

For detailed error-handling conventions, read:
`references/error-handling.md` (API Gateway) and `references/sqs-lambda.md` (SQS)

## AWS and external services

Keep AWS and external-service operations behind focused helper functions.

Examples:

```python
_fetch_cognito_jwks(...)
_job_exists_in_dynamodb(...)
_store_connection_in_dynamodb(...)
```

Handle AWS-specific exceptions explicitly.

For DynamoDB:

* use explicit keys;
* use `ConditionExpression` when atomic invariants matter;
* consider race conditions between reads and writes;
* do not assume `update_item` fails when an item does not exist.

For secrets, boto3 client caching, and external API timeouts/retries, see `references/aws.md`.
Reuse boto3 clients through a cached `_get_client(service_name)` factory.

For detailed AWS conventions, read:
`references/aws.md`

## Authentication and authorization

Treat authentication and authorization as separate concerns.

* Authentication establishes who the caller is.
* Authorization establishes whether the caller may access a specific resource.

A valid JWT does not automatically authorize access to a `jobId`, record, or other resource.

Where the domain requires it, verify ownership or permissions explicitly.

Never log authentication credentials.

## Validation

Validate external input at the system boundary.

Check:

* required fields;
* types;
* empty strings;
* malformed values;
* required configuration;
* authentication;
* resource identifiers.

Reject missing and whitespace-only required strings.

Do not silently invent defaults for required values.

## Testing

Testing rules live in the `python-unit-testing` skill. Use it for any test work.

## Documentation

Docstring and README rules live in the `code-documentation` skill. Follow it for every change.

* Explain intent, constraints, and non-obvious WHYs.
* Do not add comments that merely restate code.

## Abstraction discipline

Do not introduce repositories, services, controllers, factories, dependency-injection frameworks, base classes, or generic CRUD layers for a single use case without a concrete reason.

Add abstractions only when they solve demonstrated duplication, complexity, or reuse needs.

## AI agent workflow

Before writing or modifying Python:

1. Inspect the existing module.
2. Inspect nearby tests.
3. Identify established naming and logging patterns.
4. Identify error-response conventions.
5. Identify AWS integration patterns.
6. Preserve existing behavior unless a change is requested.
7. Make the smallest coherent change.
8. Add or update tests for changed behavior.
9. Review the result for naming, logging, errors, security, testability, and accidental behavior changes.

When reviewing existing code:

* identify actual bugs separately from style preferences;
* do not call a style preference a bug;
* preserve intentional existing patterns;
* recommend changes only when they improve correctness, security, maintainability, or consistency.

## Definition of done

Before finalizing Python code, verify:

* naming is consistent;
* functions have clear responsibilities;
* external input is validated;
* configuration is validated;
* exceptions are handled at appropriate boundaries;
* API responses do not leak internals;
* production failures are observable;
* secrets are never logged;
* external services are isolated;
* tests were written or updated following `python-unit-testing`;
* no unnecessary abstraction or dependency was introduced;
* constants for timeouts, retries and limits are named and justified;
* module-level caches are bounded and have a reset helper;
* docstrings and the README follow `code-documentation`;
* existing behavior was preserved unless explicitly changed.
