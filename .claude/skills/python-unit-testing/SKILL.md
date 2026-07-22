---
name: python-unit-testing
description: This skill defines standards and best practices for writing, reviewing, and maintaining unit tests for Python code. It covers when to use unit tests, test naming, structure, mocking, fixtures, edge cases, regression tests, and test review checklists. The default testing framework is `pytest`.
---
# Python Unit Testing

## Purpose
This skill defines standards for writing, reviewing, and maintaining unit tests for Python AWS Lambda functions.

The goal is to produce tests that are:

* deterministic;
* isolated;
* fast;
* readable;
* maintainable;
* focused on observable behavior;
* independent from AWS infrastructure.

The default testing framework is `pytest`.

This skill covers **unit tests only**. Integration and end-to-end tests are out of scope.

For detailed guidance, consult the reference files:

* `references/testing-rules.md` — general unit testing rules;
* `references/lambda-testing.md` — AWS Lambda-specific rules;
* `references/examples.md` — examples and anti-patterns.
---

## When to Use
Use this skill when:

* writing unit tests for a new Lambda;
* adding tests for existing Lambda code;
* modifying or fixing unit tests;
* adding regression tests;
* reviewing unit test quality;
* testing Lambda input validation;
* testing Lambda error handling;
* increasing meaningful test coverage.
---

# Core Rules
## 1. Use pytest
Use `pytest` as the default testing framework.

Prefer:
* `pytest`;
* `pytest.mark.parametrize`;
* pytest fixtures;
* `monkeypatch`;
* `pytest-mock` when available;
* `unittest.mock` when appropriate.

Follow existing project conventions when they are reasonable.

Do not introduce another testing framework without a clear reason.
---

## 2. Unit Tests Only
This skill is strictly for unit tests.

Do not create:

* integration tests;
* end-to-end tests;
* tests against real AWS infrastructure;
* tests against real databases;
* tests against real external APIs.

All external dependencies must be isolated, mocked, or replaced with fakes.
---

## 3. Test Behavior, Not Implementation

Tests should verify observable behavior and public contracts.

Do not test implementation details unless they are explicitly part of the required behavior.

Tests should remain valid after reasonable internal refactoring.

Prefer testing:

```python
response = lambda_handler(event, context)

assert response["statusCode"] == 200
```

over testing internal calls that do not matter to the contract.
---

## 4. Keep Tests Isolated

Every test must be:

* independent;
* deterministic;
* order-independent;
* free from shared mutable state.

A test must not depend on another test running first.

Tests must not depend on:

* real AWS resources;
* local developer configuration;
* persistent data from previous test runs;
* execution order;
* uncontrolled global state.
---

## 5. Arrange, Act, Assert

Structure tests around:

1. Arrange — prepare inputs and dependencies;
2. Act — execute the behavior;
3. Assert — verify the result.

Example:

```python
def test_handler_returns_200_for_valid_request(mocker):
    # Arrange
    mocker.patch(
        "app.repository.get_user",
        return_value={"id": "123"},
    )

    # Act
    response = lambda_handler(event, context)

    # Assert
    assert response["statusCode"] == 200
```

For very small tests, explicit comments are optional.
---

## 6. One Logical Scenario Per Test
Each test should verify one logical behavior.

Prefer separate tests for:
* successful execution;
* invalid input;
* missing required input;
* resource not found;
* expected business error;
* dependency failure.

Multiple assertions are acceptable when they describe the same outcome.
---

## 7. Test Names Must Describe Behavior

Use names that describe the scenario and expected result.

Good:

```python
def test_handler_returns_404_when_user_is_not_found():
    ...
```

Bad:

```python
def test_handler():
    ...
```

The test name should help diagnose a failure without opening the test body.
---

## 8. Use Precise Assertions
Assertions must verify expected behavior precisely.

Prefer:

```python
assert response["statusCode"] == 400
```

over:

```python
assert response
```

Avoid assertions that can pass accidentally.

When possible, assert structured values instead of string representations.
---

## 9. Test Relevant Edge Cases

Consider, when applicable:

* missing fields;
* `None`;
* empty strings;
* empty collections;
* invalid types;
* invalid formats;
* boundary values;
* duplicate values;
* missing optional fields;
* unexpected dependency responses.

Do not add meaningless tests solely to increase coverage.

Prioritize cases that are part of the contract or likely to cause regressions.
---

## 10. Use Parametrization Appropriately
Use `pytest.mark.parametrize` when multiple inputs test the same logical behavior.

Example:

```python
@pytest.mark.parametrize(
    "user_id",
    ["", None, "invalid"],
)
def test_handler_rejects_invalid_user_id(user_id):
    ...
```

Do not use parameterization when scenarios have substantially different meanings or require different assertions.
---

# AWS Lambda Rules

## 11. Never Call Real AWS Services

Unit tests must not access real AWS infrastructure.

Mock or isolate:

* S3;
* DynamoDB;
* SQS;
* SNS;
* EventBridge;
* Secrets Manager;
* SSM Parameter Store;
* Lambda;
* Step Functions;
* other AWS services.

Test how application code behaves when dependencies return expected results or errors.
---

## 12. Test the Lambda Contract
For each Lambda handler, test relevant behavior such as:

* valid input;
* invalid input;
* missing required fields;
* successful response;
* expected business errors;
* expected dependency errors;
* correct response structure;
* correct status codes when applicable.

Do not test AWS itself.
---

## 13. Keep Lambda Handlers Thin

Prefer separating:

* event parsing;
* business logic;
* external dependencies;
* response construction.

Test business logic independently from the Lambda handler when practical.

Handler tests should focus on:

* Lambda-specific behavior;
* input/output contract;
* orchestration;
* error mapping.
---

## 14. Mock External Boundaries

Mock dependencies such as:

* AWS SDK clients;
* external HTTP clients;
* external service adapters;
* time;
* randomness;
* UUID generation.

Do not mock pure business logic unnecessarily.

Prefer mocking the dependency where it is used.
---

## 15. Control Environment Variables
Tests must not depend on the developer's local environment.

Use `monkeypatch` to control Lambda environment variables.

Test missing environment variables only when the application has explicit behavior for them.
---

## 16. Control Time and Randomness

Tests must not depend on:

* real current time;
* uncontrolled random values;
* generated UUIDs.

Inject or mock these dependencies when exact values matter.
---

## 17. Lambda Context

Do not depend on the real Lambda runtime context.

Create a minimal mock or fixture containing only attributes used by the code.

Do not create unnecessarily complete fake context objects.
---

## 18. Avoid Import-Time Test Hacks

If AWS clients or configuration are initialized at module import time, mock them at the correct lookup location.
If import-time initialization makes testing unnecessarily difficult, prefer improving production dependency initialization rather than adding complex test hacks.
---

# Exceptions and Errors

## 19. Assert Specific Exceptions
Use the narrowest meaningful exception type.

Prefer:

```python
with pytest.raises(ValueError):
    process_request(data)
```

Avoid:

```python
with pytest.raises(Exception):
    process_request(data)
```

For Lambda handlers, verify the resulting response when exceptions are converted into Lambda or API responses.
---

## 20. Test Expected Dependency Failures

Test dependency failures that are explicitly part of application behavior.

Examples:

* resource not found;
* expected AWS exception;
* expected external service error;
* dependency validation error.

Do not create tests for every theoretically possible exception.
---

# Regression Tests

When fixing a bug:

1. Reproduce the bug with a failing unit test.
2. Confirm the test fails for the expected reason.
3. Fix the production code.
4. Confirm the test passes.
5. Run relevant existing tests.

The regression test must protect against the specific bug returning.
---

# Test Execution

After adding or modifying tests:

1. Run the new or modified test file.
2. Fix test or production issues.
3. Run related unit tests.
4. Run the full unit test suite when practical.

Example:

```bash
pytest tests/unit/test_handler.py
```

Then:

```bash
pytest tests/unit
```

Never claim that tests pass unless they were actually executed.

If tests cannot be executed, explicitly state this.
---

# Test Review Checklist

Before completing a unit testing task, verify:

* [ ] Tests use pytest conventions.
* [ ] Tests are deterministic.
* [ ] Tests are isolated.
* [ ] No real AWS services are called.
* [ ] External dependencies are controlled.
* [ ] Tests verify behavior rather than implementation details.
* [ ] Test names describe behavior.
* [ ] Assertions are precise.
* [ ] Relevant edge cases are covered.
* [ ] Error handling is tested where relevant.
* [ ] Tests are not unnecessarily complex.
* [ ] Tests follow existing project conventions.
* [ ] Relevant tests were executed.
* [ ] Existing relevant tests still pass.
* [ ] Regression tests exist for bug fixes.
---

# Workflow

When asked to write unit tests:

1. Inspect the Lambda code.
2. Inspect existing tests and project conventions.
3. Identify the Lambda contract.
4. Identify success, validation, error, and edge-case scenarios.
5. Identify external dependencies.
6. Mock only external boundaries.
7. Write focused unit tests.
8. Run the new tests.
9. Run relevant existing tests.
10. Review tests against the checklist.
11. Report what was tested and what was executed.
---

# Reference Files

Read the relevant reference when additional detail is required.

### General testing rules

Read:

`references/testing-rules.md`

Use it for:

* test structure;
* naming;
* assertions;
* fixtures;
* parametrization;
* mocking;
* test data;
* isolation;
* deterministic tests.

### Lambda-specific rules

Read:
`references/lambda-testing.md`

Use it for:

* Lambda handlers;
* events;
* context;
* API Gateway responses;
* AWS SDK clients;
* environment variables;
* module initialization;
* AWS errors.

### Examples

Read:

`references/examples.md`

Use it for:

* good tests;
* bad tests;
* mocking examples;
* Lambda handler examples;
* anti-patterns.
