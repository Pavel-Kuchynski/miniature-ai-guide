# close_connection

AWS Lambda function that handles WebSocket client disconnection and cleans up connection metadata from DynamoDB.
When a client disconnects from the WebSocket endpoint, this function removes the associated connection record from the job tracking table for proper connection lifecycle management.

**Python Version:** Requires Python 3.12+ (uses type-hint union syntax).

When a frontend client disconnects from the WebSocket endpoint, this function:
1. Extract `connectionId` from the WebSocket disconnect event context
2. Query the `JOBS_TABLE_NAME` DynamoDB table by `connectionId` to find the associated job record
3. Extract the `jobId` from the query result
4. Remove the connection metadata from the job record in `JOBS_TABLE_NAME`
5. Log all operations (successful and failed) to CloudWatch for debugging and audit trail

## WebSocket Disconnect Event

The payload of the WebSocket disconnect event is as follows:

```json
{
  "requestContext": {
    "connectionId": "<connection_id>",
    "routeKey": "$disconnect",
    "eventType": "DISCONNECT",
    "extendedRequestId": "<extended_request_id>",
    "requestTime": "<request_time>",
    "messageDirection": "IN",
    "stage": "<stage>",
    "disconnectedAt": "<disconnected_at_timestamp>",
    "identity": {
      "sourceIp": "<source_ip>",
      "userAgent": "<user_agent>"
    },
    "requestTimeEpoch": "<request_time_epoch>"
  }
}
```

When a client disconnects from the WebSocket endpoint, the `connectionId` is passed in `requestContext`. The Lambda extracts the `connectionId` and queries DynamoDB to find the associated job record, then uses the found `jobId` to clean up the connection.

## Lambda Handler

- Set Lambda handler to `handler.lambda_handler`.
- Entry point signature: `lambda_handler(event: dict, context) -> dict`, returning an
  API-Gateway-style response (`statusCode`, `headers`, `body` as a JSON string).

## Environment Variables

**Required:**
- `JOBS_TABLE_NAME`: DynamoDB table name containing job and connection records.
- The handler removes connection metadata attributes (`connectionId`, `connectedAt`) from the job's item in this table.
- If unset, the function returns HTTP 500.

**Optional:** None

## Implementation Details

The Lambda performs the following steps:
1. Extracts `connectionId` from `event.requestContext` (required, must be a non-empty string).
2. Validates required environment variables (`JOBS_TABLE_NAME`).
3. Queries the JOBS table using a Global Secondary Index (GSI) on `connectionId` to find the job record:
   - Uses the GSI named `connectionId-index` with `connectionId` as the partition key for efficient lookup.
   - If job is not found, log a warning and return 404 Not Found.
   - Returns 500 if multiple jobs are found (data integrity issue) or if the query fails.
4. Extracts the `jobId` from the query result.
5. Removes the connection metadata from the job record using UpdateExpression with a condition check:
   - Ensures the job record still exists before removal (ConditionExpression).
   - `connectionId`: The WebSocket connection ID attribute is removed.
   - `connectedAt`: The connection timestamp attribute is removed.
6. Returns 200 on success, 400 for invalid input, 500 for server/database errors.

Connection data and job data are stored in the same DynamoDB table (`JOBS_TABLE_NAME`), using `jobId` as the partition key.

### Logging & Debugging

All operations are logged to CloudWatch with structured JSON format including:
- `timestamp`: ISO-8601 UTC timestamp
- `level`: INFO, WARNING, or ERROR
- `jobId`: Correlation ID for tracing requests
- `stage`: Lifecycle stage (parse_input, remove_connection, error)
- `message`: Human-readable log message

Error responses do NOT include exception details to prevent information disclosure (AWS account IDs, role names, table names, etc.). Details are logged internally for debugging.

## Response Shapes

All responses are API Gateway-compatible dictionaries with `statusCode`, `headers`, and JSON `body`.

**200 OK** — Connection closed successfully:
```json
{
  "statusCode": 200,
  "headers": {"Content-Type": "application/json"},
  "body": "{\"message\": \"Connection closed successfully\"}"
}
```

**400 Bad Request** — Missing or invalid `connectionId`:
```json
{
  "statusCode": 400,
  "headers": {"Content-Type": "application/json"},
  "body": "{\"error\": \"InvalidRequest\", \"message\": \"connectionId is required\"}"
}
```

**404 Not Found** — No active job found for the connection:
```json
{
  "statusCode": 404,
  "headers": {"Content-Type": "application/json"},
  "body": "{\"error\": \"NotFound\", \"message\": \"No active job found for this connection\"}"
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

## Code Structure

- `handler.py` — Lambda entry point (`lambda_handler`) with DynamoDB operations and event parsing helpers.
  - `_find_job_id_by_connection_id(dynamodb_resource, connection_id, table_name)` — Queries DynamoDB using the `connectionId-index` GSI to efficiently find the associated `jobId` by `connectionId`.
  - `_remove_connection_from_dynamodb(dynamodb_resource, job_id, table_name)` — Removes connection metadata from job record, with existence validation.
  - `_response(status_code, body)` — Builds API Gateway response.
  
  **Note:** The `dynamodb` resource is initialized inside `lambda_handler()` (lazy initialization) rather than at module level. This is a Lambda best practice that allows the handler to initialize only when invoked and enables proper test mocking without requiring AWS credentials/region configuration during test imports.

### DynamoDB Table Structure

The JOBS table requires:
- **Partition Key:** `jobId` (string)
- **Global Secondary Index (GSI):** `connectionId-index`
- **Partition Key:** `connectionId` (string)
- **Purpose:** Enables efficient querying of jobs by WebSocket connection ID for disconnection cleanup.

- `logging_config.py` — Structured JSON logging with correlation IDs.
  - `JSONFormatter` — Formats logs as single-line JSON objects.
  - `StructuredLoggerAdapter` — Injects jobId and stage into every log entry.
  - `configure_logger(name)` — Returns a configured logger instance.

- `tests/test_handler.py` — Comprehensive unit tests using `unittest` and mocked boto3 (20+ test cases).
- `requirements.txt` — Dependencies (boto3, boto3-stubs for DynamoDB).

## Setup & Testing

### Install Dependencies

**Prerequisites:** Python 3.10+, `pip` or `uv`, virtual environment (recommended)

```bash
# from backend/close_connection/

# Install runtime dependencies
pip install -r requirements.txt
```

### Run Tests

```bash
# From backend/close_connection/ directory
python -m unittest discover -s tests

# Run specific test file
python -m unittest tests.test_handler

# Run specific test case
python -m unittest tests.test_handler.TestCloseConnectionHandler.test_successful_disconnection

# Run tests with verbose output
python -m unittest discover -s tests -v
```

## Deployment

- **Region**: `eu-central-1` (Frankfurt)
- **Lambda function name**: `close-connection`
- **API Gateway WebSocket route**: `$disconnect` mapped to this Lambda function.
- **IAM Role**: Lambda execution role with permissions to read/write to `JOBS_TABLE_NAME` and log to CloudWatch.
  - Managed policy: `AWSLambdaBasicExecutionRole` (CloudWatch Logs)
  - Inline policy: `dynamodb:UpdateItem` scoped to the JOBS table only
- **DynamoDB Table**: `JOBS_TABLE_NAME` must exist with:
  - `jobId` as the partition key (string)
  - Global Secondary Index named `connectionId-index` with `connectionId` as the partition key (string), required for efficient connection lookup
- **Environment Variables**: Set `JOBS_TABLE_NAME` in Lambda configuration.
- **Runtime**: Python 3.12
- **Memory**: 128 MB
- **Timeout**: 10 seconds

