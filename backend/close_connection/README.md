# close_connection

AWS Lambda function that handles WebSocket client disconnection (`$disconnect` route) and removes
the connection metadata from the job record that owned the connection.

When a frontend client disconnects, this function:
1. Extracts `connectionId` from the disconnect event (`requestContext.connectionId`).
2. Queries the `JOBS_TABLE_NAME` table through the `connectionId-index` GSI to find the job that holds this connection.
3. Removes `connectionId` and `connectedAt` from that job record, but only while the record still holds this same `connectionId`.
4. Logs every step to CloudWatch as structured JSON.

## Python Version

- Lambda runtime and CI (`.github/workflows/deploy_close_connection.yml`): Python 3.12.
- The code uses no version-specific syntax; the 3.12 pin is the deployment target, not a language requirement.

## WebSocket Disconnect Event

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

Only `requestContext.connectionId` is read. No query-string parameters are used.

## Lambda Handler

- Handler: `handler.lambda_handler`.
- Signature: `lambda_handler(event: dict, context) -> dict`, returning an API-Gateway-style response
  (`statusCode`, `headers`, `body` as a JSON string). `context` is unused.

## Environment Variables

**Required:**
- `JOBS_TABLE_NAME`: DynamoDB table holding job records and connection metadata. Must have `jobId`
  (string) as partition key and the `connectionId-index` GSI described below.
  If unset, the function returns HTTP 500 without calling DynamoDB.

**Optional:** None.

## Implementation Details

1. Extracts `connectionId` from `event.requestContext`. Must be a non-empty, non-whitespace string, otherwise 400.
2. Validates `JOBS_TABLE_NAME`, otherwise 500.
3. Queries the GSI `connectionId-index` (`connectionId` partition key) for records holding this `connectionId`:
   - **No match:** idempotent no-op. Logs at INFO and returns 200. A disconnect for a connection that
     no longer owns any job (for example a second `$disconnect` delivery, or a connection that was
     superseded by a reconnect) has nothing to clean up.
   - **More than one match:** data-integrity problem. Logs at ERROR, performs no writes, returns 500
     (`DatabaseError`). An operator must repair the job records; the function does not pick one.
   - **Matching record without `jobId`:** same treatment as multiple matches (500).
   - **Query error:** 500 (`DatabaseError`).
4. Removes the connection attributes from the job record with a conditional update:
   - `UpdateExpression`: `REMOVE connectionId, connectedAt`
   - `ConditionExpression`: `connectionId = :connectionId` (the disconnecting connection ID).
   - **Condition fails** (`ConditionalCheckFailedException`): the job was already cleared, or a
     reconnect has attached a new connection since the GSI lookup. This is a stale disconnect. Nothing is
     removed, and the function returns 200 (`Connection already closed`).
   - Other DynamoDB errors: 500 (`DatabaseError`).
5. Returns 200 `Connection closed successfully` on success.

The GSI is eventually consistent, so step 3 can return a stale `jobId` shortly after a reconnect.
The condition in step 4 is what prevents such a stale disconnect from erasing the newer connection.

Connection metadata lives in the same table as job data (`JOBS_TABLE_NAME`), keyed by `jobId`.

### Logging & Debugging

Every log line is a single JSON object with these fields:
- `timestamp`: ISO-8601 UTC timestamp
- `level`: INFO, WARNING, or ERROR
- `jobId`: Job correlation ID. `"unknown"` until the job has been resolved.
- `connectionId`: WebSocket connection ID from the event. Always set once step 1 succeeds.
- `stage`: `parse_input`, `remove_connection`, or `error`
- `message`: Human-readable text

Error responses never include exception details (AWS account IDs, role names, table names). Details are
logged internally.

## Response Shapes

All responses are API Gateway-compatible dictionaries with `statusCode`, `headers`, and a JSON `body`.

**200 OK** — Connection closed (connection attributes removed):
```json
{
  "statusCode": 200,
  "headers": {"Content-Type": "application/json"},
  "body": "{\"message\": \"Connection closed successfully\"}"
}
```

**200 OK** — No job holds this connection (idempotent no-op):
```json
{
  "statusCode": 200,
  "headers": {"Content-Type": "application/json"},
  "body": "{\"message\": \"No active job found for this connection\"}"
}
```

**200 OK** — Stale disconnect; the job no longer holds this connection (no-op):
```json
{
  "statusCode": 200,
  "headers": {"Content-Type": "application/json"},
  "body": "{\"message\": \"Connection already closed\"}"
}
```

**400 Bad Request** — Missing, empty or whitespace-only `connectionId`:
```json
{
  "statusCode": 400,
  "headers": {"Content-Type": "application/json"},
  "body": "{\"error\": \"InvalidRequest\", \"message\": \"connectionId is required\"}"
}
```

**500 Internal Server Error** — Missing `JOBS_TABLE_NAME`:
```json
{
  "statusCode": 500,
  "headers": {"Content-Type": "application/json"},
  "body": "{\"error\": \"ServerConfiguration\", \"message\": \"Failed to initialize handler\"}"
}
```

**500 Internal Server Error** — Corrupted connection index (several jobs or missing `jobId`), GSI query failure, or update failure:
- `{"error": "DatabaseError", "message": "Failed to resolve job for connection"}`
- `{"error": "DatabaseError", "message": "Failed to query jobs table"}`
- `{"error": "DatabaseError", "message": "Failed to remove connection"}`

Because API Gateway ignores the response of a `$disconnect` route, these codes matter for CloudWatch
metrics and logs rather than for the client.

## Code Structure

- `handler.py` — Lambda entry point (`lambda_handler`) with DynamoDB operations and event parsing.
  - `JobNotFoundError` — No job holds the connectionId. Mapped to a 200 no-op.
  - `ConnectionIndexIntegrityError` — The GSI does not resolve to exactly one usable job. Mapped to 500.
  - `CONNECTION_ID_INDEX_NAME` — Name of the GSI (`connectionId-index`).
  - `_response(status_code, body)` — Builds an API Gateway response.
  - `_find_job_id_by_connection_id(dynamodb_resource, connection_id, table_name)` — GSI lookup of the owning `jobId`.
  - `_is_conditional_check_failed(error)` — True for a `ConditionalCheckFailedException`.
  - `_remove_connection_from_dynamodb(dynamodb_resource, job_id, connection_id, table_name)` — Conditional REMOVE of connection attributes.

  **Note:** The `dynamodb` resource is created inside `lambda_handler()` after input and configuration checks,
  so tests can patch `boto3.resource` without AWS credentials or region at import time.

- `logging_config.py` — Structured JSON logging. Identical copy of the file in `backend/open_connection`.
  - `JSONFormatter` — Formats logs as single-line JSON objects with `jobId`, `connectionId` and `stage`.
  - `StructuredLoggerAdapter` — Binds `jobId`, `connectionId` and `stage` to every record it emits.
  - `configure_logger(name)` — Returns a configured logger instance.

- `tests/test_handler.py` — `unittest` suite with mocked `boto3` resources. Covers the handler contract,
  the GSI lookup and integrity checks, the conditional REMOVE (including the stale-disconnect no-op),
  error mapping, and the structured log fields.
- `requirements.txt` — Runtime dependencies: `boto3`, `boto3-stubs[dynamodb]`.

## DynamoDB Table Structure

The JOBS table requires:
- **Partition key:** `jobId` (string).
- **Global secondary index:** `connectionId-index`, partition key `connectionId` (string).
  Projection can be `KEYS_ONLY`, because only `jobId` (the table key, always projected) is read.
  Jobs without a connection have no `connectionId` attribute and are not indexed.

## Setup & Testing

### Install Dependencies

**Prerequisites:** Python 3.12 (matches the Lambda runtime and CI), `pip` or `uv`, a virtual environment (recommended).

```bash
# from backend/close_connection/
pip install -r requirements.txt
```

### Run Tests

```bash
# From backend/close_connection/ directory
python -m unittest discover -s tests

# Run specific test file
python -m unittest tests.test_handler

# Run specific test case
python -m unittest tests.test_handler.TestCloseConnectionHandler.test_stale_disconnect_is_noop_returning_200

# Run tests with verbose output
python -m unittest discover -s tests -v
```

The test suite uses only the standard library and the runtime requirements, so it runs in the CI gate
without extra packages.

## IAM & Deployment

- **Region**: `eu-central-1` (Frankfurt)
- **Lambda function name**: `close-connection`
- **API Gateway WebSocket route**: `$disconnect` mapped to this Lambda function.
- **Runtime**: Python 3.12
- **Memory**: 128 MB
- **Timeout**: 10 seconds
- **Environment variables**: `JOBS_TABLE_NAME`.

**IAM role:** the Lambda execution role needs:
- `AWSLambdaBasicExecutionRole` managed policy (CloudWatch Logs).
- Inline policy, scoped to the JOBS table:
  - `dynamodb:Query` on the index ARN `arn:aws:dynamodb:<region>:<account>:table/<JOBS_TABLE_NAME>/index/connectionId-index`
  - `dynamodb:UpdateItem` on the table ARN `arn:aws:dynamodb:<region>:<account>:table/<JOBS_TABLE_NAME>`

  `dynamodb:Query` is not granted on the table ARN alone; the index ARN must be listed.
  `dynamodb:GetItem` is not used.
