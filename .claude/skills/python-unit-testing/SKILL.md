---
name: python-unit-testing
description: This skill defines standards and best practices for writing, reviewing, and maintaining unit tests for Python code. It covers when to use unit tests, test naming, structure, mocking, fixtures, edge cases, regression tests, and test review checklists. The default testing framework is `pytest`.
---
# Python Unit Testing

## Purpose

This skill defines standards and best practices for writing, reviewing, and maintaining unit tests for Python code.

The goal is to help the Python agent produce tests that are:

* reliable;
* deterministic;
* readable;
* maintainable;
* isolated;
* fast;
* focused on observable behavior;
* useful for preventing regressions.

The default testing framework is `pytest`.

---

## When to Use

Use this skill when:

* adding unit tests for new Python code;
* modifying existing unit tests;
* reviewing the quality of unit tests;
* fixing broken or flaky tests;
* adding regression tests for a bug;
* increasing test coverage;
* refactoring code while preserving behavior;
* validating error handling and edge cases.

---

## Primary Framework

Use `pytest` as the default testing framework.

Prefer:

```python
def test_something():
    ...
```

over:

```python
class TestSomething(unittest.TestCase):
    ...
```

Use `unittest` only when:

* the existing project already uses it extensively;
* compatibility with existing infrastructure requires it;
* a specific `unittest` feature is needed.

Do not introduce a second testing framework without a clear reason.

---

## Core Principles

### 1. Test Behavior, Not Implementation

Tests should verify what the code does from the perspective of its contract or public interface.

Prefer:

```python
def test_calculate_total_applies_discount():
    result = calculate_total(price=100, discount=0.1)

    assert result == 90
```

Avoid tests that depend unnecessarily on internal implementation details:

```python
def test_calculate_total_calls_private_helper():
    ...
```

Do not test private implementation details unless they represent critical behavior that cannot reasonably be validated through the public interface.

Tests should survive reasonable refactoring of the implementation.

---

### 2. One Logical Behavior Per Test

Each test should verify one logical behavior or scenario.

Prefer:

```python
def test_user_is_created_with_default_status():
    user = create_user("alice")

    assert user.name == "alice"
    assert user.status == "active"
```

Avoid tests that validate many unrelated behaviors:

```python
def test_user_everything():
    ...
```

A test may contain multiple assertions when all assertions describe the same expected outcome.

---

### 3. Arrange, Act, Assert

Structure tests using:

1. Arrange — prepare inputs and dependencies;
2. Act — execute the behavior under test;
3. Assert — verify the result.

Example:

```python
def test_calculate_total():
    # Arrange
    price = 100
    discount = 0.1

    # Act
    result = calculate_total(price, discount)

    # Assert
    assert result == 90
```

For very small tests, explicit comments are optional.

---

### 4. Tests Must Be Deterministic

A test must produce the same result when run repeatedly under the same conditions.

Avoid uncontrolled dependencies on:

* current time;
* random values;
* network;
* external APIs;
* databases;
* environment variables;
* filesystem state;
* operating system state;
* execution order;
* global mutable state.

If these dependencies are required, isolate them behind an abstraction and control them in the test.

---

### 5. Tests Must Be Independent

Tests must not depend on:

* another test running first;
* shared mutable state;
* execution order;
* data created by another test;
* previous test side effects.

Each test should establish its own required state.

Avoid:

```python
global_user = None
```

or test sequences such as:

```text
test_create_user()
test_update_user()
test_delete_user()
```

where the second test depends on the first.

---

## Test Naming

Test names should clearly describe:

* the condition or scenario;
* the expected result.

Prefer:

```python
def test_returns_empty_list_when_user_has_no_orders():
    ...
```

```python
def test_raises_value_error_when_amount_is_negative():
    ...
```

```python
def test_uses_cached_value_when_cache_contains_key():
    ...
```

Avoid vague names:

```python
def test_user():
    ...
```

```python
def test_works():
    ...
```

```python
def test_case_1():
    ...
```

The test name should help diagnose a failure without immediately opening the test body.

---

## Arrange, Act, Assert Rules

Keep the three phases visually clear.

Prefer:

```python
def test_get_user_returns_user():
    # Arrange
    repository = FakeUserRepository()
    service = UserService(repository)

    # Act
    result = service.get_user(123)

    # Assert
    assert result.id == 123
```

Avoid mixing setup, execution, and assertions repeatedly throughout a test.

If a test becomes too complicated, consider:

* extracting a fixture;
* creating a factory;
* simplifying the test scenario;
* splitting the test.

---

## Parameterization

Use `pytest.mark.parametrize` when multiple inputs verify the same logical behavior.

Prefer:

```python
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0, 0),
        (1, 1),
        (10, 100),
    ],
)
def test_square(value, expected):
    assert square(value) == expected
```

Use parameterization for:

* boundary values;
* equivalent input classes;
* validation rules;
* multiple known examples.

Do not use parameterization when each case represents a fundamentally different behavior and would benefit from a separate descriptive test.

---

## Edge Cases

When writing tests, consider relevant edge cases.

Depending on the code, check:

* empty input;
* `None`;
* zero;
* negative values;
* minimum and maximum values;
* empty collections;
* duplicate values;
* missing keys;
* invalid formats;
* unexpected types;
* boundary conditions;
* exceptionally large inputs.

Do not blindly test every theoretically possible edge case.

Focus on cases that are:

* part of the contract;
* likely to occur;
* historically problematic;
* security-sensitive;
* important for business logic.

---

## Exceptions

Test expected exceptions explicitly.

Prefer:

```python
def test_raises_error_for_negative_amount():
    with pytest.raises(ValueError, match="amount must be positive"):
        calculate_fee(-10)
```

Avoid:

```python
def test_raises_error():
    with pytest.raises(Exception):
        calculate_fee(-10)
```

Catch the narrowest expected exception type.

When useful, validate the error message or relevant attributes.

Do not assert exact error messages when they are implementation details and may change without affecting behavior.

---

## Mocking

Use mocks carefully.

Mock external boundaries and dependencies such as:

* HTTP clients;
* message queues;
* email services;
* payment providers;
* cloud SDKs;
* external databases;
* system clocks;
* random number generators.

Avoid mocking internal implementation details unnecessarily.

Prefer testing real domain logic whenever practical.

Bad:

```python
mock_service.calculate.return_value = 100
```

when the purpose of the test is actually to verify `calculate()` itself.

Better:

```python
def test_order_total_is_calculated_correctly():
    result = calculate_order_total(order)

    assert result == 100
```

Mock the boundary, not the behavior being tested.

---

## Mock Where the Dependency Is Used

Patch the dependency in the module where it is looked up, not necessarily where it was originally defined.

For example, if:

```python
# service.py
from payment import charge
```

then tests should generally patch:

```python
monkeypatch.setattr("service.charge", fake_charge)
```

rather than patching the original `payment.charge`.

The agent should verify the import structure before deciding where to patch.

---

## To Prefer Fakes to Complex Mock Chains

When a dependency has meaningful behavior, prefer a small fake or in-memory implementation over deeply nested mocks.

Avoid:

```python
mock.client.return_value.session.return_value.response.return_value.json.return_value
```

Prefer:

```python
class FakeUserRepository:
    def __init__(self, users):
        self.users = users

    def get(self, user_id):
        return self.users.get(user_id)
```

Fakes are often easier to understand and maintain.

---

## Fixtures

Use pytest fixtures for reusable setup.

Prefer fixtures for:

* common objects;
* test dependencies;
* temporary directories;
* reusable application configuration;
* database setup when integration tests are involved.

Keep fixtures:

* small;
* explicit;
* predictable;
* easy to discover.

Avoid overly complex fixture hierarchies.

Avoid fixtures that hide important test behavior.

A reader should be able to understand the test without navigating through many layers of fixtures.

---

## Test Data

Use realistic but minimal test data.

Prefer:

```python
user = User(
    id=1,
    name="Alice",
    email="alice@example.com",
)
```

Avoid unnecessarily large objects when only one field matters.

Use factories when object construction becomes repetitive.

Test data should make the scenario obvious.

Avoid meaningless data such as:

```python
foo = "abc"
bar = 123
baz = True
```

when domain-specific values would improve readability.

---

## Unit Test Isolation

Unit tests should avoid real external systems.

Do not use real:

* HTTP requests;
* third-party APIs;
* production databases;
* cloud services;
* message brokers;
* email servers.

These belong in integration or end-to-end tests.

A unit test should generally test one unit of behavior in isolation.

---

## Filesystem and Environment

When filesystem interaction is part of the behavior being tested, use temporary resources.

Prefer pytest's `tmp_path` fixture.

Example:

```python
def test_writes_report(tmp_path):
    output_file = tmp_path / "report.txt"

    write_report(output_file, "hello")

    assert output_file.read_text() == "hello"
```

Do not write test artifacts into the repository unless explicitly required.

Avoid depending on the developer's local filesystem.

Use controlled environment variables in tests.

---

## Time and Randomness

Do not let tests depend on the real current time or uncontrolled randomness.

Prefer injecting a clock or patching the time source.

For random behavior, use:

* deterministic seeds when appropriate;
* injected random generators;
* controlled mocks.

Tests should not occasionally fail because of timing or randomness.

---

## Async Code

Use pytest-compatible async testing tools when testing asynchronous code.

Tests should:

* use `async def` when appropriate;
* properly await async functions;
* use async mocks for async dependencies;
* avoid blocking calls inside async tests.

Do not test async code by ignoring or suppressing coroutine warnings.

---

## Regression Tests

When fixing a bug:

1. reproduce the bug with a failing test;
2. implement the fix;
3. verify that the test passes;
4. ensure existing tests still pass.

The regression test should fail against the old behavior and pass against the corrected behavior.

Prefer a regression test that captures the externally observable bug.

---

## Coverage

Coverage is a signal, not the primary goal.

Do not write meaningless tests solely to increase coverage percentage.

Prioritize tests for:

* business-critical logic;
* complex logic;
* error handling;
* boundary conditions;
* security-sensitive behavior;
* historically buggy code.

High coverage does not guarantee good tests.

A small number of meaningful tests is better than many superficial tests.

---

## Test Quality

Every test should answer:

1. What behavior is being tested?
2. What scenario is being tested?
3. What is the expected result?
4. Why would this test fail if the code were broken?

If these questions cannot be answered clearly, simplify or rewrite the test.

---

## Test Failure Diagnostics

Tests should fail for the correct reason.

Avoid overly broad assertions:

```python
assert result
```

when the expected value is known.

Prefer:

```python
assert result.status == "active"
```

Avoid assertions that can pass accidentally.

Bad:

```python
assert "error" in str(result)
```

when a structured error field exists.

Prefer:

```python
assert result.error_code == "INVALID_AMOUNT"
```

Use precise assertions whenever the contract allows it.

---

## Running Tests

After adding or modifying tests, the agent should run the relevant tests.

Preferred workflow:

```bash
pytest path/to/test_file.py
```

Then, when appropriate:

```bash
pytest
```

If the project has configured commands, prefer the project's standard test command.

The agent should not claim that tests pass without actually running them when execution is available.

If tests cannot be executed, clearly state that they were not run.

---

## Test Review Checklist

Before considering a unit test complete, verify:

* [ ] The test name clearly describes the behavior.
* [ ] The test verifies observable behavior.
* [ ] The test is deterministic.
* [ ] The test is independent.
* [ ] The test has a clear Arrange/Act/Assert structure.
* [ ] The test contains meaningful assertions.
* [ ] Exceptions are checked explicitly when relevant.
* [ ] Important edge cases are covered.
* [ ] Mocks are used only where necessary.
* [ ] External systems are isolated.
* [ ] Test data is minimal and readable.
* [ ] Fixtures are not unnecessarily complex.
* [ ] The test fails for the expected reason.
* [ ] The relevant tests have been executed.
* [ ] No unrelated production code was changed without justification.

---

## Agent Workflow

When asked to write unit tests:

### Step 1: Inspect the Code

Understand:

* public interfaces;
* expected behavior;
* dependencies;
* error conditions;
* edge cases;
* existing test conventions.

### Step 2: Inspect Existing Tests

Follow the project's existing conventions when they are reasonable.

Look for:

* test framework;
* fixture patterns;
* factories;
* mocking conventions;
* naming conventions;
* test directory structure.

### Step 3: Identify Scenarios

List the most important scenarios:

* happy path;
* invalid input;
* edge cases;
* error conditions;
* dependency failures;
* regression scenarios.

### Step 4: Write Focused Tests

Create tests that verify behavior rather than implementation.

Use parameterization where appropriate.

Use mocks or fakes only at dependency boundaries.

### Step 5: Run Tests

Run the newly added tests first.

Then run the relevant broader test suite.

### Step 6: Diagnose Failures

If a test fails:

* determine whether the test is wrong;
* determine whether the production code is wrong;
* determine whether the environment is wrong.

Do not modify tests merely to make them pass.

### Step 7: Review

Before finishing, review the tests using the Test Review Checklist.

---

## Anti-Patterns

Avoid:

* testing private methods directly without a strong reason;
* testing implementation details;
* excessive mocking;
* mock chains;
* tests depending on test execution order;
* shared mutable state;
* real network requests in unit tests;
* real external APIs in unit tests;
* arbitrary `sleep()` calls;
* tests depending on the current time;
* tests depending on uncontrolled randomness;
* giant test methods;
* vague test names;
* meaningless assertions;
* assertions on irrelevant implementation details;
* duplicated test setup;
* overly magical fixtures;
* tests written only to increase coverage;
* modifying production code solely to satisfy a poorly designed test;
* changing expected behavior in a test without understanding why it changed.

---

## Definition of Done

A unit test task is complete when:

1. Relevant behavior is covered.
2. Important edge cases are considered.
3. Tests are readable and maintainable.
4. Tests are deterministic and isolated.
5. External dependencies are properly controlled.
6. Tests use precise assertions.
7. Tests follow project conventions.
8. New tests have been executed successfully, when execution is available.
9. Existing relevant tests continue to pass.
10. The agent can explain what each new test protects against.
