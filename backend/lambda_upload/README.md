# lambda_upload

AWS Lambda function that creates four pre-signed S3 `PUT` upload URLs — one call per generation
request, all four files grouped under a single UUID-based `uploads/<uuid>/` prefix so downstream
processing can find the four reference images for one job together.

## Lambda Handler

- Set Lambda handler to `handler.lambda_handler`.
- Entry point signature: `lambda_handler(event: dict, context) -> dict`, returning an
  API-Gateway-style response (`statusCode`, `headers`, `body` as a JSON string).

## Environment Variables

- `UPLOAD_BUCKET_NAME` (required)
  - S3 bucket name where files will be uploaded.
  - Example: `my-app-upload-bucket`
  - If unset, the function returns HTTP 500.

- `UPLOAD_URL_EXPIRES_SECONDS` (optional)
  - Pre-signed URL lifetime in seconds.
  - Default: `900` (15 minutes).
  - Must be an integer greater than `0` and no more than `604800` (S3's 7-day max for
    presigned URLs). If the value is missing/invalid or out of range, the function returns
    HTTP 500.
  - Example: `600`

## Request input

File names and content types can be supplied via query string parameters or a JSON body,
either as the singular (`fileName` / `contentType`) or plural (`fileNames` / `contentTypes`,
list or comma-separated string) form. When both a query parameter and a body value are
present for the same field, **the body value takes precedence**.

The function always returns exactly 4 upload items, regardless of how many names/types were
supplied:
- Missing file names fall back to `file_1.bin`, `file_2.bin`, etc. (by position).
- Missing content types fall back to the first content type provided, or
  `application/octet-stream` if none was provided.
- Extra names/types beyond the first 4 are ignored.

Supplied file names are sanitized before being used in the S3 object key: only
`[A-Za-z0-9._-]` characters are kept (others replaced with `_`), the name is reduced to its
base name (path components stripped), and it's capped at 255 characters. An empty, `.`, or
`..` result falls back to `file_<n>.bin`.

## Response shape

On success (HTTP 200):

```json
{
  "bucket": "my-app-upload-bucket",
  "folder": "5f2c...-uuid",
  "prefix": "uploads/5f2c...-uuid",
  "uploadItems": [
    {"uploadUrl": "...", "key": "uploads/5f2c.../file_1.bin", "fileName": "file_1.bin", "contentType": "application/octet-stream"}
  ],
  "expiresIn": 900
}
```

On failure (HTTP 500), the body is `{"error": "<message>"}`. Internal exception details are
never included in the response; failures are logged server-side via structured logging
instead.

## Logging

The handler emits structured (JSON) logs to CloudWatch for observability and debugging.
Each log line is a JSON object with:

- `timestamp`: ISO-8601 UTC timestamp
- `level`: `INFO`, `WARNING`, or `ERROR`
- `jobId`: Request correlation ID (extracted from `jobId` query param or body field; defaults to `"unknown"` if not provided)
- `stage`: Lifecycle stage (one of: `parse_input`, `put_item`, `error`)
- `message`: Human-readable log text

**Example logs** (one per line):

```json
{"timestamp": "2026-07-06T10:30:00.123456+00:00", "level": "INFO", "jobId": "unknown", "stage": "parse_input", "message": "Starting upload handler"}
{"timestamp": "2026-07-06T10:30:00.124567+00:00", "level": "INFO", "jobId": "abc-def-ghi", "stage": "parse_input", "message": "Successfully parsed input event"}
{"timestamp": "2026-07-06T10:30:00.125678+00:00", "level": "INFO", "jobId": "abc-def-ghi", "stage": "put_item", "message": "Successfully generated 4 presigned upload URLs"}
```

Logs can be filtered by `jobId` in CloudWatch to trace the entire request lifecycle.

## Development

### Installation

**Prerequisites:** Python 3.10+, `pip` or `uv`, virtual environment (recommended)

```bash
# from backend/lambda_upload/

# Install runtime dependencies
pip install -r requirements.txt

# Install development dependencies (for testing)
pip install -r requirements-dev.txt
```

Dev dependencies include: **pytest** (7.0+), **pytest-cov** (4.0+), **moto** (5.0+ with S3/DynamoDB support)

### Testing

No real AWS credentials or services needed — tests use **moto** to mock S3 and DynamoDB.

#### Quick start

```bash
cd backend/lambda_upload
pip install -r requirements-dev.txt
pytest -v
```

Expected output: `93 passed, 2 skipped` (2 skipped tests are placeholders for future features).

#### Running tests

```bash
# Run all tests
pytest

# Run with verbose output
pytest -v

# Run a specific test file
pytest tests/test_input_validation.py

# Run a specific test case
pytest tests/test_handler_integration.py::TestPresignedUrlGeneration::test_generates_four_presigned_urls_on_success

# Run with coverage report
pytest --cov=handler --cov-report=html

# Run with coverage in terminal
pytest --cov=handler --cov-report=term-missing

# Run tests matching a pattern
pytest -k "test_missing"

# Run tests with print output
pytest -s
```

Verify test setup without pytest:
```bash
python verify_tests.py
```

#### Test organization

Tests are organized by responsibility:

- **`tests/test_input_validation.py`** — Request parsing, file name sanitization, parameter precedence (query string vs JSON body), content type parsing, jobId extraction, expires-in validation
- **`tests/test_handler_integration.py`** — Full end-to-end handler flow, presigned URL generation (HTTP 200, 500), error handling, CORS headers
- **`tests/test_logging.py`** — Structured JSON logging format, timestamp ISO-8601 UTC format, jobId/stage context injection, error logging (logs do not contain presigned URLs)
- **`tests/test_s3_listing.py`** — Placeholder for future S3 image listing validation (TASK-03, HTTP 422) — currently skipped
- **`tests/test_dynamodb_write.py`** — Placeholder for future DynamoDB job record creation (TASK-04, HTTP 201) — currently skipped
- **`tests/conftest.py`** — Shared pytest fixtures: `aws_credentials`, `sample_job_id`, `sample_event`, `sample_upload_confirmation_event`, `sample_job_record`

#### Mocking & Fixtures

**Environment variables** — use `monkeypatch` fixture:
```python
def test_with_env_vars(monkeypatch):
    monkeypatch.setenv("UPLOAD_BUCKET_NAME", "test-bucket")
    monkeypatch.delenv("UPLOAD_URL_EXPIRES_SECONDS", raising=False)
```

**S3 client mocking** — use `patch.object()`:
```python
from unittest.mock import patch

def test_s3_presigned_url(monkeypatch):
    monkeypatch.setenv("UPLOAD_BUCKET_NAME", "test-bucket")
    with patch.object(handler.s3_client, "generate_presigned_url", 
                      return_value="https://example.com/presigned-url"):
        response = handler.lambda_handler({}, None)
        assert response["statusCode"] == 200
```

**Boto3 errors** — mock with `side_effect`:
```python
def test_s3_error_handling(monkeypatch):
    monkeypatch.setenv("UPLOAD_BUCKET_NAME", "test-bucket")
    with patch("handler.s3_client") as mock_s3:
        from botocore.exceptions import ClientError
        mock_s3.generate_presigned_url.side_effect = ClientError(
            {"Error": {"Code": "AccessDenied"}}, "GeneratePresignedUrl"
        )
        response = handler.lambda_handler({}, None)
        assert response["statusCode"] == 500
```

#### Coverage expectations

**Currently Tested** ✓ (implemented):
- HTTP 200: Presigned URL generation, all 4 URLs grouped under same UUID, custom/default expiration
- HTTP 500: Missing env vars, invalid config, S3 client errors
- Request parsing: query strings, JSON body, body precedence, singular/plural parameters, list/scalar/comma-separated formats, Base64-encoded bodies
- Input handling: file name sanitization (unsafe chars, path traversal, length limits), jobId extraction from query/body
- Structured logging: JSON format, required fields, timestamp format, jobId/stage injection, stage transitions, error logging, security (no presigned URLs in logs)

**Future Coverage** (TASK-03, TASK-04, TASK-05):
- HTTP 201: DynamoDB job record creation for new jobs
- HTTP 200: Duplicate uploads (idempotent)
- HTTP 422: S3 image listing validation (exactly 4 images, filters zero-byte markers, pagination)
- HTTP 400: Reserved for future validation errors

#### Common tasks

**Add a new test:**
1. Identify the appropriate test file
2. Add a test method with Arrange/Act/Assert pattern:
   ```python
   def test_my_feature(self, fixture_name):
       """One-line description."""
       # Arrange
       input_data = ...
       # Act
       result = function_under_test(input_data)
       # Assert
       assert result == expected_value
   ```
3. Run the test: `pytest tests/test_file.py::TestClass::test_my_feature -v`
4. Verify coverage: `pytest --cov=handler --cov-report=term-missing`

**Debug a failing test:**
1. Run in isolation with verbose output: `pytest tests/test_file.py::TestClass::test_name -vv`
2. Check assertion error and traceback
3. Add print statements or use `pdb`: `import pdb; pdb.set_trace()`
4. Run with output: `pytest -s`

**Update a test after handler changes:**
1. Run full suite: `pytest -v`
2. For each failure:
   - Did handler behavior change? Update the test's assertion.
   - Is the test outdated? Update to match new behavior.
   - Is handler behavior wrong? Fix the handler, not the test.
3. Re-run: `pytest tests/test_file.py::TestClass::test_name -v`
4. Once all pass, run full suite: `pytest`

#### Continuous Integration

Tests designed to run in CI/CD with zero AWS credentials:
- `moto` mocks all AWS service calls
- No buckets, tables, or credentials required
- Tests run in seconds (no network I/O)

CI command:
```bash
cd backend/lambda_upload
pip install -r requirements-dev.txt
pytest --cov=handler
```

Expected: All tests pass (2 skipped), coverage >= 85% for `handler.py`

#### Troubleshooting

**Import error: `No module named 'handler'`** — Run pytest from `backend/lambda_upload/`:
```bash
cd backend/lambda_upload
pytest
```

**ModuleNotFoundError: `No module named 'moto'`** — Install dev dependencies:
```bash
pip install -r requirements-dev.txt
```

**Test fails with `UPLOAD_BUCKET_NAME not set`** — Ensure test uses `monkeypatch`:
```python
def test_something(monkeypatch):
    monkeypatch.setenv("UPLOAD_BUCKET_NAME", "test-bucket")
```

**Flaky tests (pass/fail randomly)** — Mock time/randomness:
```python
from unittest.mock import patch

def test_something():
    with patch("handler.uuid.uuid4") as mock_uuid:
        mock_uuid.return_value = uuid.UUID("11111111-1111-1111-1111-111111111111")
        # ... test code ...
```

#### Future enhancements

Once TASK-03 and TASK-04 are implemented:
1. **test_s3_listing.py** will be activated for image presence validation
2. **test_dynamodb_write.py** will be activated for job record creation
3. **test_handler_integration.py** expanded to test 422 and 201 responses
4. New section for full upload confirmation flow (TASK-05)

## Deployment (AWS)

Deployed manually via the AWS Console (no IaC yet — see
`docs/lambda_upload_deployment_plan.md` for the intended CDK-based setup and open decisions).

- **Region**: `eu-central-1` (Frankfurt)
- **S3 bucket**: `miniature-ai-guide-uploads-dev`
  - Block all public access enabled; no versioning.
- **Lambda function**: `lambda-upload`
  - Runtime: Python 3.12
  - Handler: `handler.lambda_handler`
  - Memory: 128 MB, Timeout: 10 sec
  - Environment variable: `UPLOAD_BUCKET_NAME=miniature-ai-guide-uploads-dev`
    (`UPLOAD_URL_EXPIRES_SECONDS` left unset, uses the default of 900s)
- **IAM execution role**: `lambda-upload-execution-role`
  - Managed policy: `AWSLambdaBasicExecutionRole` (CloudWatch Logs)
  - Inline policy `lambda-upload-s3-put-policy`: `s3:PutObject` scoped to
    `arn:aws:s3:::miniature-ai-guide-uploads-dev/uploads/*` only
