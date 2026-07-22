# General Python Unit Testing Rules

## Purpose

This document contains detailed rules for writing maintainable Python unit tests.

The default framework is `pytest`.

These rules apply to unit tests used by Python AWS Lambda projects.

---

# Test Structure

## Arrange, Act, Assert

Prefer a clear three-phase structure:

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

Do not interleave setup, execution, and assertions unnecessarily.

For trivial tests, comments are optional.

---

# Test Naming

Test names should describe:

* the scenario;
* the expected behavior.

Preferred patterns:

```text
test_<behavior>_<expected_result>
test_<function>_returns_<result>_when_<condition>
test_<function>_raises_<exception>_when_<condition>
```

Examples:

```python
def test_returns_empty_list_when_user_has_no_orders():
    ...


def test_raises_value_error_when_amount_is_negative():
    ...


def test_uses_cached_value_when_cache_contains_key():
    ...
```

Avoid:

```python
def test_works():
    ...


def test_case_1():
    ...


def test_function():
    ...
```

---

# One Logical Behavior Per Test

A test should verify one logical scenario.

Good:

```python
def test_returns_user_when_user_exists():
    ...


def test_returns_none_when_user_does_not_exist():
    ...
```

Avoid combining unrelated scenarios into one test.

Multiple assertions are allowed when they validate one outcome:

```python
def test_user_is_created_with_default_values():
    user = create_user("alice")

    assert user.name == "alice"
    assert user.status == "active"
    assert user.is_verified is False
```

---

# Assertions

Use precise assertions.

Prefer:

```python
assert result == expected
```

or:

```python
assert response["statusCode"] == 200
assert response["body"] == expected_body
```

Avoid weak assertions:

```python
assert result
```

when the exact expected result is known.

Avoid assertions that can pass accidentally.

Prefer structured assertions over string matching when structured data is available.

---

# Exceptions

Use `pytest.raises`.

```python
def test_raises_value_error_for_negative_amount():
    with pytest.raises(ValueError):
        calculate(-1)
```

When the error message is part of the contract:

```python
with pytest.raises(
    ValueError,
    match="amount must be positive",
):
    calculate(-1)
```

Do not assert exact messages when they are merely implementation details.

Always use the narrowest meaningful exception type.

---

# Parametrization

Use parametrization for equivalent scenarios.

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

Good use cases:

* validation rules;
* boundary values;
* multiple equivalent inputs;
* known input/output pairs.

Avoid parameterization when each case has different business meaning.

---

# Fixtures

Use fixtures for reusable setup.

Good fixture candidates:

* common input data;
* common objects;
* reusable dependencies;
* Lambda event templates;
* Lambda context;
* temporary resources.

Keep fixtures:

* small;
* explicit;
* predictable;
* easy to understand.

Avoid deeply nested fixture dependencies.

A test should not require navigating through many fixture layers to understand its scenario.

---

# Test Data

Use minimal, realistic data.

Prefer:

```python
user = User(
    id="123",
    name="Alice",
    email="alice@example.com",
)
```

over:

```python
user = User(
    id="abc",
    name="foo",
    email="x@y.com",
)
```

Domain-specific data improves readability.

Do not construct large objects when only one or two fields matter.

Use factories when object construction becomes repetitive.

---

# Determinism

Tests must produce stable results.

Avoid uncontrolled dependencies on:

* current time;
* randomness;
* UUIDs;
* environment variables;
* global state;
* external services.

Control these dependencies with:

* dependency injection;
* mocks;
* `monkeypatch`;
* deterministic fixtures.

---

# Test Independence

Tests must not depend on:

* another test;
* execution order;
* shared mutable state;
* previous test results.

Each test must establish its own required state.

Avoid global mutable test state.

---

# Mocking

Mock external boundaries.

Typical candidates:

* AWS SDK clients;
* external HTTP clients;
* external service adapters;
* clocks;
* UUID generators;
* random generators.

Do not mock pure business logic unless necessary.

The more application logic is replaced by mocks, the less behavior the test actually verifies.

---

# Mock Where Used

Patch the dependency where the production module looks it up.

For example:

```python
# app.py
from payment import charge
```

Test:

```python
mocker.patch("app.charge")
```

rather than automatically patching:

```python
mocker.patch("payment.charge")
```

The correct target depends on the import structure.

Always inspect the production code before choosing a patch target.

---

# Avoid Deep Mock Chains

Avoid:

```python
mock.client.return_value.session.return_value.response.return_value.json.return_value
```

Deep chains:

* obscure the behavior;
* couple tests to implementation;
* are difficult to maintain.

Prefer:

* mocking a direct dependency;
* a small fake;
* a simple fixture.

---

# Fakes

Use a fake when a dependency has meaningful behavior that is easier to represent with a small in-memory implementation.

Example:

```python
class FakeUserRepository:
    def __init__(self, users):
        self.users = users

    def get(self, user_id):
        return self.users.get(user_id)
```

Fakes are often preferable to complex mock configurations.

Do not build large fake frameworks for simple tests.

---

# Time

If behavior depends on time, control the time source.

Do not use real time when exact values affect the result.

Prefer:

* injecting a clock;
* patching the time dependency;
* using a deterministic fixture.

---

# Randomness and UUIDs

If randomness affects behavior, control it.

When the generated value itself matters, mock it.

Example:

```python
mocker.patch(
    "app.uuid4",
    return_value="fixed-id",
)
```

Do not assert unpredictable generated values directly.

---

# Environment Variables

Use `monkeypatch` to control environment variables.

Example:

```python
def test_uses_configured_table(monkeypatch):
    monkeypatch.setenv(
        "TABLE_NAME",
        "test-table",
    )

    ...
```

Restore environment state automatically through pytest fixtures.

Do not depend on a developer's shell environment.

---

# Coverage

Coverage is a signal, not the goal.

Do not add meaningless tests solely to increase coverage.

Prioritize:

* business-critical behavior;
* complex logic;
* validation;
* error handling;
* boundary conditions;
* security-sensitive behavior;
* regression cases.

A high coverage percentage does not guarantee useful tests.

---

# Readability

A test should be understandable without reading the implementation.

The reader should be able to identify:

1. what is being tested;
2. what scenario is being tested;
3. what the expected result is.

If a test requires excessive setup or mocking, consider whether:

* the production code is too coupled;
* a fixture should be introduced;
* a fake would be clearer;
* the test should target a different public interface.

---

# Test Failure Quality

A failing test should clearly communicate what broke.

Prefer:

```python
assert response["statusCode"] == 404
```

over:

```python
assert response
```

Prefer structured assertions over opaque snapshots when possible.

Do not write tests that pass even when important behavior is broken.

---

# Production Code Changes

Do not modify production code solely to make a bad test pass.

If testing reveals that code is difficult to test:

1. determine whether the design is unnecessarily coupled;
2. consider a small refactor;
3. preserve existing behavior;
4. add tests around the public contract.

Keep testability improvements focused and justified.
