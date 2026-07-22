# Unit Testing Examples and Anti-Patterns

## Purpose

This document provides examples of preferred and discouraged patterns for Python AWS Lambda unit tests.

---

# Example 1: Simple Lambda Success Test

## Preferred

```python
def test_handler_returns_200_for_valid_request(mocker):
    mocker.patch(
        "app.get_user",
        return_value={
            "id": "123",
            "name": "Alice",
        },
    )

    response = lambda_handler(
        {
            "pathParameters": {
                "id": "123",
            },
        },
        Mock(),
    )

    assert response["statusCode"] == 200
```

The test:

* mocks the external dependency;
* executes real application logic;
* verifies observable behavior.

---

# Example 2: Invalid Input

## Preferred

```python
def test_handler_returns_400_when_user_id_is_missing():
    event = {
        "pathParameters": {},
    }

    response = lambda_handler(
        event,
        Mock(),
    )

    assert response["statusCode"] == 400
```

The test focuses on the Lambda contract.

---

# Example 3: Dependency Failure

## Preferred

```python
def test_handler_returns_500_when_repository_fails(mocker):
    mocker.patch(
        "app.repository.get_user",
        side_effect=RuntimeError("database unavailable"),
    )

    response = lambda_handler(
        event,
        Mock(),
    )

    assert response["statusCode"] == 500
```

The test verifies how the application handles the failure.

---

# Example 4: Parametrized Validation

## Preferred

```python
@pytest.mark.parametrize(
    "user_id",
    [
        "",
        None,
        "invalid",
    ],
)
def test_handler_rejects_invalid_user_id(user_id):
    event = {
        "pathParameters": {
            "id": user_id,
        },
    }

    response = lambda_handler(
        event,
        Mock(),
    )

    assert response["statusCode"] == 400
```

Use parametrization when all cases represent the same behavior.

---

# Example 5: Testing Business Logic Separately

Production:

```python
def calculate_total(items):
    return sum(item["price"] for item in items)
```

Test:

```python
def test_calculate_total_sums_item_prices():
    items = [
        {"price": 10},
        {"price": 20},
    ]

    result = calculate_total(items)

    assert result == 30
```

Do not mock `calculate_total` when testing `calculate_total`.

---

# Example 6: Mocking AWS

Production:

```python
def get_user(user_id):
    response = table.get_item(
        Key={"id": user_id},
    )

    return response.get("Item")
```

Test:

```python
def test_get_user_returns_item(mocker):
    mocker.patch(
        "app.table.get_item",
        return_value={
            "Item": {
                "id": "123",
            },
        },
    )

    result = get_user("123")

    assert result == {
        "id": "123",
    }
```

No real DynamoDB is used.

---

# Example 7: Environment Variable

```python
def test_handler_uses_configured_table(monkeypatch):
    monkeypatch.setenv(
        "TABLE_NAME",
        "test-table",
    )

    ...
```

The test controls its own environment.

---

# Example 8: Mocking UUID

```python
def test_create_user_uses_generated_id(mocker):
    mocker.patch(
        "app.uuid4",
        return_value="fixed-id",
    )

    result = create_user("Alice")

    assert result["id"] == "fixed-id"
```

The test does not depend on random UUID generation.

---

# Example 9: Testing Exceptions

```python
def test_process_request_raises_value_error_for_negative_amount():
    with pytest.raises(ValueError):
        process_request(
            {
                "amount": -1,
            },
        )
```

Use the narrowest meaningful exception.

---

# Anti-Pattern 1: Testing AWS

## Bad

```python
def test_real_dynamodb():
    table.put_item(
        Item={
            "id": "123",
        },
    )

    response = table.get_item(
        Key={
            "id": "123",
        },
    )

    assert response["Item"]["id"] == "123"
```

Why it is bad:

* uses real infrastructure;
* requires AWS credentials;
* is slow;
* can fail due to network or infrastructure;
* is not a unit test.

---

# Anti-Pattern 2: Excessive Mocking

## Bad

```python
def test_handler(mocker):
    mocker.patch(
        "app.parse_event",
        return_value=data,
    )
    mocker.patch(
        "app.validate",
        return_value=True,
    )
    mocker.patch(
        "app.process",
        return_value=result,
    )
    mocker.patch(
        "app.build_response",
        return_value=response,
    )

    result = lambda_handler(event, context)

    assert result == response
```

Why it is bad:

The test replaces nearly all application behavior.

It verifies little beyond the fact that mocked functions return mocked values.

---

# Anti-Pattern 3: Deep Mock Chain

## Bad

```python
mock.client.return_value.session.return_value.response.return_value.json.return_value = {
    "id": "123",
}
```

Why it is bad:

* tightly coupled to implementation;
* difficult to read;
* difficult to maintain;
* fragile during refactoring.

Prefer mocking the direct dependency.

---

# Anti-Pattern 4: Vague Assertions

## Bad

```python
assert response
```

Why it is bad:

The test may pass even when important behavior is broken.

Prefer:

```python
assert response["statusCode"] == 200
```

---

# Anti-Pattern 5: Testing Implementation Details

## Bad

```python
def test_handler_calls_private_helper(mocker):
    helper = mocker.patch(
        "app._private_helper",
    )

    lambda_handler(event, context)

    helper.assert_called_once()
```

Why it is bad:

The test may break after a harmless refactor.

If the helper is not part of the public contract, test the observable behavior instead.

---

# Anti-Pattern 6: Shared Mutable State

## Bad

```python
users = []

def test_create_user():
    users.append("alice")


def test_user_exists():
    assert "alice" in users
```

Why it is bad:

* tests depend on execution order;
* tests are not independent;
* failures are difficult to diagnose.

Each test should establish its own state.

---

# Anti-Pattern 7: Testing Through the Handler Only

## Bad

If a Lambda contains complex business logic:

```python
def lambda_handler(event, context):
    # 200 lines of business logic
    ...
```

and all tests invoke only:

```python
lambda_handler(event, context)
```

Why it is bad:

* tests become large;
* setup becomes complicated;
* failures are harder to diagnose;
* business logic cannot be tested in isolation.

Prefer extracting business logic into small functions and testing those directly.

---

# Anti-Pattern 8: Testing Every Possible Error

## Bad

Creating dozens of tests for every theoretical exception from every dependency.

Why it is bad:

* increases maintenance cost;
* provides little value;
* tests implementation rather than contract.

Test errors that have meaningful application behavior.

---

# Anti-Pattern 9: Coverage-Driven Tests

## Bad

Adding tests that execute lines without meaningful assertions:

```python
def test_execute_line():
    function()
```

Why it is bad:

Code coverage increases, but confidence does not.

Every test should protect meaningful behavior.

---

# Anti-Pattern 10: Real Time

## Bad

```python
def test_token_is_valid():
    token = create_token()

    time.sleep(2)

    assert is_valid(token)
```

Why it is bad:

* slow;
* flaky;
* dependent on timing.

Control time instead.

---

# Anti-Pattern 11: Real Randomness

## Bad

```python
def test_random_value():
    assert generate_value() == 42
```

Why it is bad:

The test may randomly fail.

Control randomness or mock the generator when the exact value matters.

---

# Preferred Test Design

A good Lambda unit test should generally have this shape:

```python
def test_handler_returns_expected_response(mocker):
    # Arrange
    mocker.patch(
        "app.external_dependency",
        return_value=expected_dependency_result,
    )

    event = build_test_event()

    # Act
    response = lambda_handler(
        event,
        Mock(),
    )

    # Assert
    assert response["statusCode"] == 200
    assert response["body"] == expected_body
```

The test should clearly communicate:

* what input was provided;
* what dependencies were controlled;
* what behavior was executed;
* what result was expected.

---

# Final Review

Before committing a test, ask:

1. Does this test verify behavior?
2. Is the scenario clear from the name?
3. Is the test deterministic?
4. Is the test independent?
5. Does it call any real external service?
6. Are mocks limited to external boundaries?
7. Would the test survive a reasonable refactor?
8. Does it have meaningful assertions?
9. Does it cover a behavior worth protecting?
10. Was the test actually executed?
