## Open WebSocket Connection Lambda

This Lambda function manages WebSocket connection lifecycle during the `$connect` route.
It validates that the requested job exists, extracts connection and user metadata, and persists the connection record to DynamoDB for subsequent message routing.

**Python Version:** Requires Python 3.8+ (uses `datetime.timezone.utc` for compatibility).

When a frontend client connects to the WebSocket endpoint, this function:
1. Extracts the `jobId` from query string parameters (validated for non-empty string)
2. Verifies the job exists in the `JOBS_TABLE_NAME` DynamoDB table
3. Returns success or appropriate error response
4. Logs all operations (successful and failed) to CloudWatch for debugging and audit trail

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
    "connectedAt": <connected_at_timestamp>,
    "identity": {
      "sourceIp": "<source_ip>",
      "userAgent": "<user_agent>"
    },
    "authorizer": {
      "claims": {
        "sub": "cognito-user-id",
        "email": "user@example.com"
      }
    },
    "requestTimeEpoch": <request_time_epoch>
  },
  "queryStringParameters": {
    "jobId": "<uuid>"
  }
}
```

When a client connects to the WebSocket endpoint, the `jobId` is passed as a query string parameter. The lambda function extracts this `jobId` and uses it to manage the connection.

## Entry Point

```python
lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]
```

The handler validates the job ID, checks its existence in DynamoDB, and stores the connection metadata.

## Environment Variables

**Required:**
- `JOBS_TABLE_NAME`: DynamoDB table name containing job records. The handler queries this table to validate the `jobId` before accepting the connection, and also stores connection metadata as attributes in the same table.

**Optional:** None

## Implementation Details

The Lambda performs the following steps:
1. Extracts `jobId` from `queryStringParameters` (required, validated for non-empty string).
2. Extracts `connectionId` from `requestContext` (required, validated for non-empty string).
3. Extracts user information (`sub`, `email`) from `requestContext.authorizer.claims` (optional).
4. Validates required environment variables (`JOBS_TABLE_NAME`).
5. Queries `JOBS_TABLE_NAME` to verify the job exists.
6. If the job exists, stores the connection record in `JOBS_TABLE_NAME` with the following attributes:
   - `jobId`: The job ID (partition key) associated with the connection.
   - `connectionId`: The unique WebSocket connection ID.
   - `connectedAt`: Unix timestamp (seconds) of connection establishment.
   - `sub`: Cognito user ID from authorizer claims (empty string if missing).
   - `email`: User email from authorizer claims (empty string if missing).

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
  "body": "{\"error\": \"ServerConfiguration\", \"message\": \"JOBS_TABLE_NAME environment variable is not set\"}"
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

- `handler.py` — Lambda entry point (`lambda_handler`) with DynamoDB operations and event parsing helpers.
  - `_extract_job_id(event)` — Extracts jobId from queryStringParameters.
  - `_extract_connection_id(event)` — Extracts connectionId from requestContext.
  - `_extract_user_info(event)` — Extracts user metadata (sub, email) from authorizer claims.
  - `_job_exists_in_dynamodb(job_id, table_name)` — Queries JOBS table to validate job.
  - `_store_connection_in_dynamodb(...)` — Stores connection metadata to JOBS table as attributes.
  - `_response(status_code, body)` — Builds API Gateway response.
- `logging_config.py` — Structured JSON logging with correlation IDs (copied from lambda_upload).
  - `JSONFormatter` — Formats logs as single-line JSON objects.
  - `StructuredLoggerAdapter` — Injects jobId and stage into every log entry.
  - `configure_logger(name)` — Returns a configured logger instance.
- `tests/test_handler.py` — Comprehensive unit tests using `unittest` and mocked boto3 (20 test cases).
- `requirements.txt` — Dependencies (boto3, boto3-stubs for DynamoDB).



