# Python AWS Lambda Unit Testing

## Purpose

This document defines Lambda-specific unit testing rules.

All tests described here are unit tests.

No real AWS services or external infrastructure may be used.

---

# Lambda Handler

A Lambda handler typically has the form:

```python
def lambda_handler(event, context):
    ...
```

Test the handler's observable contract.

Relevant scenarios may include:

* valid event;
* invalid event;
* missing fields;
* successful response;
* expected business error;
* expected dependency error;
* unexpected dependency failure;
* response formatting.

Only test scenarios relevant to the actual Lambda contract.

---

# Lambda Event

Create minimal event fixtures.

Example:

```python
@pytest.fixture
def api_gateway_event():
    return {
        "httpMethod": "GET",
        "path": "/users/123",
        "pathParameters": {
            "id": "123",
        },
    }
```

Do not create enormous fixtures containing every possible API Gateway field.

Include only fields required by the code.

---

# API Gateway Responses

When Lambda is used behind API Gateway, verify relevant response fields.

Example:

```python
def test_handler_returns_200_for_existing_user():
    response = lambda_handler(event, context)

    assert response["statusCode"] == 200
    assert response["body"] == expected_body
```

Test headers only when they are part of the contract.

Do not assert irrelevant implementation details.

---

# Lambda Context

Do not use the real AWS Lambda runtime context.

Create a minimal mock:

```python
@pytest.fixture
def lambda_context():
    context = Mock()
    context.aws_request_id = "test-request-id"
    context.function_name = "test-function"
    return context
```

Only define attributes used by the production code.

---

# AWS SDK

Never call real AWS services in unit tests.

Mock:

* `boto3.client`;
* `boto3.resource`;
* service clients;
* service methods.

Examples:

* `s3_client.get_object`;
* `table.get_item`;
* `table.put_item`;
* `sqs_client.send_message`;
* `secrets_client.get_secret_value`.

---

# Patch Where the Dependency Is Used

Example production code:

```python
# app.py
import boto3

dynamodb = boto3.resource("dynamodb")
```

If testing code that uses `app.dynamodb`, patch:

```python
mocker.patch("app.dynamodb")
```

Do not automatically patch `boto3.resource`.

The correct patch target is determined by where the application looks up the dependency.

---

# AWS Success Responses

Mock only the response data required by the application.

Example:

```python
mocker.patch(
    "app.table.get_item",
    return_value={
        "Item": {
            "id": "123",
            "name": "Alice",
        }
    },
)
```

Do not reproduce the entire AWS response format when the application uses only one field.

---

# AWS Errors

Test expected application behavior when AWS calls fail.

Example:

```python
mocker.patch(
    "app.table.get_item",
    side_effect=ClientError(...),
)
```

Verify the resulting application behavior.

Do not test boto3's implementation.

Do not create tests for every possible AWS exception unless the application handles them differently.

---

# DynamoDB

For DynamoDB unit tests:

* mock table operations;
* provide minimal item structures;
* test missing items;
* test expected conditional failures when relevant;
* test application behavior around DynamoDB errors.

Do not use a real DynamoDB table.

Do not require LocalStack or DynamoDB Local for unit tests.

---

# S3

For S3 unit tests:

* mock `get_object`;
* mock `put_object`;
* mock `head_object`;
* mock expected S3 errors.

Verify application behavior.

Do not use real S3 buckets.

---

# SQS and SNS

Mock message publishing and sending.

Verify:

* correct application behavior;
* relevant payload construction;
* error handling.

Do not send real messages.

---

# Secrets Manager and SSM

Mock secret and parameter retrieval.

Do not access real secrets or parameters.

Test:

* successful retrieval;
* missing secret/parameter when relevant;
* expected dependency failure.

Never place real credentials or secrets in tests.

---

# Environment Variables

Use `monkeypatch`.

Example:

```python
def test_handler_uses_table_name(monkeypatch):
    monkeypatch.setenv(
        "TABLE_NAME",
        "test-table",
    )

    ...
```

Test missing configuration only when the application has explicit behavior for it.

Do not rely on real deployment configuration.

---

# Import-Time Initialization

Be aware of code such as:

```python
dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(
    os.environ["TABLE_NAME"]
)
```

This code executes during import.

Tests may need to:

* patch module-level objects;
* control environment variables before import;
* refactor dependency initialization.

Prefer production code that makes dependencies easy to control.

Do not introduce fragile import manipulation unless necessary.

---

# Lambda Environment and AWS Credentials

Unit tests must not require:

* real AWS credentials;
* an AWS profile;
* access to AWS;
* deployed Lambda configuration.

If importing a module requires AWS configuration, tests must provide controlled values or isolate initialization.

---

# Business Logic

Business logic should be tested independently of AWS.

Example:

```python
def calculate_total(items):
    ...
```

Test:

```python
def test_calculate_total():
    assert calculate_total(items) == expected
```

Do not mock `calculate_total` when the purpose of the test is to verify it.

---

# Thin Handler Pattern

Prefer:

```python
def lambda_handler(event, context):
    data = parse_event(event)
    result = process_request(data)

    return build_response(result)
```

Then test:

* `parse_event` independently;
* `process_request` independently;
* `build_response` independently;
* handler orchestration separately.

Do not duplicate every business logic test through the handler.

---

# Lambda Error Mapping

If the handler converts exceptions to responses, test the mapping.

Example:

```python
def test_handler_returns_404_when_user_not_found():
    ...

    response = lambda_handler(event, context)

    assert response["statusCode"] == 404
```

Test only mappings that are part of the contract.

---

# External HTTP APIs

If Lambda calls an external HTTP API:

* do not make a real request;
* mock the HTTP client or adapter;
* test success;
* test relevant error handling.

Prefer mocking the application's HTTP adapter rather than mocking every internal HTTP library implementation detail.

---

# Time

If Lambda behavior depends on the current time:

* control the time source;
* avoid real clock dependencies;
* verify exact behavior when time matters.

---

# UUIDs

If Lambda generates IDs:

* mock UUID generation when the generated value matters;
* do not assert unpredictable UUIDs.

Example:

```python
mocker.patch(
    "app.uuid4",
    return_value="fixed-id",
)
```

---

# Randomness

Control random values when they affect behavior.

Do not create flaky tests based on uncontrolled randomness.

---

# Handler Test Checklist

For each Lambda handler, consider:

### Input

* [ ] Valid event.
* [ ] Missing required to be input.
* [ ] Invalid input.
* [ ] Malformed input.

### Success

* [ ] Correct result.
* [ ] Correct response structure.
* [ ] Correct status code when applicable.

### Errors

* [ ] Expected business errors.
* [ ] Expected dependency errors.
* [ ] Relevant unexpected failures.

### Dependencies

* [ ] AWS calls are mocked.
* [ ] External APIs are mocked.
* [ ] No real infrastructure is used.

### Runtime

* [ ] Environment variables are controlled.
* [ ] Lambda context is controlled.
* [ ] Time is controlled when relevant.
* [ ] Randomness is controlled when relevant.
