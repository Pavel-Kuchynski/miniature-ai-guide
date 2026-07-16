## Open WebSocket Connection Lambda

This Lambda function manages WebSocket connection lifecycle during the `$connect` route.
It validates that the requested job exists, authenticates the user via JWT token, extracts connection and user metadata, and persists the connection record to DynamoDB for subsequent message routing.

**Python Version:** Requires Python 3.8+ (uses `datetime.timezone.utc` for compatibility).

When a frontend client connects to the WebSocket endpoint, this function:
1. Extracts and validates the JWT token from the `Authorization: Bearer <token>` header
2. Decodes the JWT token and extracts the user ID (`sub` claim) and email
3. Extracts the `jobId` from query string parameters (validated for non-empty string)
4. Verifies the job exists in the `JOBS_TABLE_NAME` DynamoDB table
5. Stores connection metadata (connectionId, user info) in DynamoDB
6. Returns success or appropriate error response
7. Logs all operations (successful and failed) to CloudWatch for debugging and audit trail

## The payload of the WebSocket connection event is as follows:

```json
{
  "requestContext": {
    "connectionId": "<connection_id>",
    "routeKey": "$connect",
    "eventType": "CONNECT",
    "extendedRequestId": "<extended_request_id>",
    "requestTime": "<request_time>",
    "messageDirection": "IN",
    "stage": "<stage>",
    "connectedAt": "<connected_at_timestamp>",
    "identity": {
      "sourceIp": "<source_ip>",
      "userAgent": "<user_agent>"
    },
    "requestTimeEpoch": "<request_time_epoch>"
  },
  "queryStringParameters": {
    "jobId": "<uuid>"
  },
  "headers": {
    "Authorization": "Bearer <jwt_token>"
  }
}
```

The client connects to the WebSocket endpoint with:
- `jobId` as a query string parameter (required, identifies the job/session)
- `Authorization` header with a Bearer JWT token (required, used for user authentication)

The JWT token must contain at least the following claims:
- `sub`: Subject (user ID) — used as `userId` in the connection record
- `email`: User email address — stored for audit and contact purposes

## Entry Point

```python
lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]
```

The handler validates the job ID, checks its existence in DynamoDB, and stores the connection metadata.

## Environment Variables

**Required:**
- `JOBS_TABLE_NAME`: DynamoDB table name containing job records. The handler queries this table to validate the `jobId` before accepting the connection, and also stores connection metadata as attributes in the same table.

**Optional:**
- `JWT_SECRET_KEY`: Secret key for verifying JWT token signature. **Required in production mode** to prevent forged tokens. If not set in production (when `ENVIRONMENT=production`), the handler raises a `JWTError`. In development mode, missing `JWT_SECRET_KEY` logs a warning and allows tokens to be decoded without verification. Supports both HS256 (symmetric, HMAC) and RS256 (asymmetric) algorithms — the algorithm is extracted from the JWT header for proper verification.
- `ENVIRONMENT`: Deployment environment (`production` or `development`). Defaults to `development`. When set to `production`, requires `JWT_SECRET_KEY` to be configured; otherwise, the handler returns HTTP 500.

## Implementation Details

The Lambda performs the following steps:

### 1. Authentication & User Extraction
- Extracts JWT token from `Authorization: Bearer <token>` header (required).
- Decodes the JWT token using PyJWT library.
  - **Production mode** (`ENVIRONMENT=production`): Requires `JWT_SECRET_KEY` to be set; raises error if missing to prevent forged tokens.
  - **Development mode**: If `JWT_SECRET_KEY` is not set, logs a security warning and decodes without signature verification (suitable for testing).
  - Supports both HS256 (symmetric, HMAC) and RS256 (asymmetric) algorithms by extracting the algorithm from the JWT header.
- Extracts user information (`sub` as userId, `email`) from JWT claims.
  - **`sub` claim (user ID) is required and must be non-empty**; returns 401 if missing or empty (critical for user tracking and audit trails).
  - `email` claim is optional; defaults to empty string if missing.
- Returns **401 Unauthorized** if:
  - Authorization header is missing
  - Authorization header format is invalid (not "Bearer <token>")
  - JWT token is malformed or signature verification fails
  - `sub` claim is missing or empty
  - In production: `JWT_SECRET_KEY` is not configured

### 2. Job & Connection Validation
- Extracts `jobId` from `queryStringParameters` (required, validated for non-empty string).
- Extracts `connectionId` from `requestContext` (required, validated for non-empty string).
- Validates required environment variables (`JOBS_TABLE_NAME`).
- Queries `JOBS_TABLE_NAME` to verify the job exists.
- Returns **404 Not Found** if job does not exist.

### 3. Connection Persistence
- If the job exists, stores the connection record in `JOBS_TABLE_NAME` with the following attributes:
  - `jobId`: The job ID (partition key) associated with the connection.
  - `connectionId`: The unique WebSocket connection ID.
  - `connectedAt`: Unix timestamp (seconds) of connection establishment.
  - `userId`: User ID extracted from JWT `sub` claim (always non-empty due to validation in step 1).
  - `email`: User email from JWT `email` claim (empty string if missing).

Connection data and job data are stored in the same DynamoDB table (`JOBS_TABLE_NAME`), using `jobId` as the partition key. The connection metadata attributes are added to the job's existing item.

### Logging & Debugging

All operations are logged to CloudWatch with structured JSON format including:
- `timestamp`: ISO-8601 UTC timestamp
- `level`: INFO, WARNING, or ERROR
- `jobId`: Correlation ID for tracing requests
- `stage`: Lifecycle stage (parse_input, validate_job, store_connection, error)
- `message`: Human-readable log message

Error responses do NOT include exception details to prevent information disclosure (AWS account IDs, role names, table names, etc.). Details are logged internally for debugging.

## Response Shapes

All responses are API Gateway-compatible dictionaries with `statusCode`, `headers`, and JSON `body`.

**200 OK** — Connection established successfully:
```json
{
  "statusCode": 200,
  "headers": {"Content-Type": "application/json"},
  "body": "{\"message\": \"Connection established successfully\"}"
}
```

**401 Unauthorized** — Invalid or missing JWT token:
```json
{
  "statusCode": 401,
  "headers": {"Content-Type": "application/json"},
  "body": "{\"error\": \"Unauthorized\", \"message\": \"Invalid or missing JWT token\"}"
}
```

Triggered when:
- Authorization header is missing or empty
- Authorization header format is invalid (not "Bearer <token>")
- JWT token is malformed
- JWT signature verification fails (if `JWT_SECRET_KEY` is configured)

**400 Bad Request** — Missing or invalid `jobId` or `connectionId`:
```json
{
  "statusCode": 400,
  "headers": {"Content-Type": "application/json"},
  "body": "{\"error\": \"InvalidRequest\", \"message\": \"jobId is required\"}"
}
```

**404 Not Found** — Job ID does not exist in `JOBS_TABLE_NAME`:
```json
{
  "statusCode": 404,
  "headers": {"Content-Type": "application/json"},
  "body": "{\"error\": \"NotFound\", \"message\": \"jobId does not exist\"}"
}
```

**500 Internal Server Error** — Missing environment variables or database errors:
```json
{
  "statusCode": 500,
  "headers": {"Content-Type": "application/json"},
  "body": "{\"error\": \"ServerConfiguration\", \"message\": \"Failed to initialize handler\"}"
}
```

## Setup & Testing

### Install Dependencies
```bash
pip install -r requirements.txt
```

### Run Tests
```bash
# From backend/open_connection/ directory
python -m unittest discover -s tests

# Run specific test file
python -m unittest tests.test_handler

# Run specific test case
python -m unittest tests.test_handler.TestOpenConnectionHandler.test_successful_connection_establishment
```

## Code Structure

- `handler.py` — Lambda entry point (`lambda_handler`) with DynamoDB operations and JWT/event parsing helpers.
  - `JWTError` — Custom exception for JWT extraction and decoding failures.
  - `_response(status_code, body)` — Builds API Gateway response.
  - `_extract_jwt_from_header(event)` — Extracts JWT token from `Authorization: Bearer <token>` header.
  - `_decode_jwt_token(token, jwt_secret)` — Decodes JWT token, optionally verifying signature.
  - `_extract_user_info(event)` — Extracts user metadata (userId from `sub`, email) from JWT token.
  - `_extract_job_id(event)` — Extracts jobId from queryStringParameters.
  - `_extract_connection_id(event)` — Extracts connectionId from requestContext.
  - `_job_exists_in_dynamodb(dynamodb_resource, job_id, table_name)` — Queries JOBS table to validate job.
  - `_store_connection_in_dynamodb(dynamodb_resource, job_id, connection_id, user_info, table_name)` — Stores connection metadata to JOBS table as attributes.
  
  **Note:** The `dynamodb` resource is initialized inside `lambda_handler()` (lazy initialization) rather than at module level. This is a Lambda best practice that allows the handler to initialize only when invoked and enables proper test mocking without requiring AWS credentials/region configuration during test imports.
- `logging_config.py` — Structured JSON logging with correlation IDs (copied from lambda_upload).
  - `JSONFormatter` — Formats logs as single-line JSON objects.
  - `StructuredLoggerAdapter` — Injects jobId and stage into every log entry.
  - `configure_logger(name)` — Returns a configured logger instance.
- `tests/test_handler.py` — Comprehensive unit tests using `unittest` with mocked boto3 and jwt modules, covering:
  - Successful connection establishment with valid JWT
  - Missing or invalid JWT token scenarios (401 responses)
  - Missing jobId or connectionId (400 responses)
  - DynamoDB query and update errors (500 responses)
  - JWT extraction and decoding helper functions
  - Total coverage: 30+ test cases
- `requirements.txt` — Dependencies:
  - `boto3` — AWS SDK for DynamoDB operations
  - `boto3-stubs[dynamodb]` — Type hints for DynamoDB
  - `PyJWT` — JWT token encoding/decoding

## Deployment

- **Region**: `eu-central-1` (Frankfurt)
- **Lambda function name**: `open-connection`
- **API Gateway WebSocket route**: `$connect` mapped to this Lambda function.
- **IAM Role**: Lambda execution role with permissions to read/write to `JOBS_TABLE_NAME` and log to CloudWatch.
- **DynamoDB Table**: `JOBS_TABLE_NAME` must exist with `jobId` as the partition key (string).
- **Environment Variables**: Set `JOBS_TABLE_NAME` in Lambda configuration.

