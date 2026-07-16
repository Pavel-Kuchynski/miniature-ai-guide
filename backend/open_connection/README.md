## Open WebSocket Connection Lambda

This Lambda function manages WebSocket connection lifecycle during the `$connect` route.
It validates that the requested job exists, authenticates the user via AWS Cognito JWT tokens,
extracts connection and user metadata, and persists the connection record to DynamoDB for
subsequent message routing.

**Python Version:** Requires Python 3.8+ (uses `datetime.timezone.utc` for compatibility).

When a frontend client connects to the WebSocket endpoint, this function:
1. Extracts and validates the JWT token from the `token` query string parameter using Cognito JWKS
2. Validates the JWT signature using RS256 algorithm from Cognito public keys
3. Validates the issuer matches the Cognito user pool
4. Validates the token is not expired (exp claim)
5. Validates the token type is 'id' token (token_use claim)
6. Extracts the user ID (`sub` claim) and email from the validated token
7. Extracts the `jobId` from query string parameters (validated for non-empty string)
8. Verifies the job exists in the `JOBS_TABLE_NAME` DynamoDB table
9. Stores connection metadata (connectionId, user info) in DynamoDB
10. Returns success or appropriate error response
11. Logs all operations (successful and failed) to CloudWatch for debugging and audit trail

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
    "jobId": "<uuid>",
    "token": "<jwt_token>"
  },
  "headers": {}
}
```

The client connects to the WebSocket endpoint with:
- `jobId` as a query string parameter (required, identifies the job/session)
- `token` as a query string parameter with a Cognito JWT token (required, used for user authentication)

The JWT token must be a valid Cognito id token with the following:
- **Signature:** Valid RS256 signature verified against Cognito JWKS endpoint
- **Issuer:** `https://cognito-idp.{COGNITO_REGION}.amazonaws.com/{COGNITO_USER_POOL_ID}`
- **Token Use:** `token_use` claim must be `"id"` (id token, not access token)
- **Expiration:** `exp` claim must be in the future (token not expired)
- **Subject:** `sub` claim must be present and non-empty (user ID)
- **Email:** `email` claim optional but stored if present

## Entry Point

```python
lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]
```

The handler validates the job ID, checks its existence in DynamoDB, and stores the connection metadata.

## Environment Variables

**Required:**
- `COGNITO_REGION`: AWS region of the Cognito user pool (e.g., `eu-central-1`).
  Used to construct the JWKS endpoint URL: `https://cognito-idp.{COGNITO_REGION}.amazonaws.com/{COGNITO_USER_POOL_ID}/.well-known/jwks.json`
- `COGNITO_USER_POOL_ID`: Cognito user pool ID.
  Used to validate the issuer claim and construct the JWKS endpoint URL.
- `JOBS_TABLE_NAME`: DynamoDB table name containing job records. The handler queries this table
  to validate the `jobId` before accepting the connection, and also stores connection metadata as
  attributes in the same table.

**Optional:**
- `AWS_REGION`: AWS region (used for DynamoDB client configuration). If not set, boto3 uses
  the default region from configuration or environment.

## Implementation Details

The Lambda performs the following steps:

### 1. Authentication & User Extraction

- Extracts JWT token from `token` query string parameter (required).
- Fetches Cognito JWKS from the public endpoint using `COGNITO_REGION` and `COGNITO_USER_POOL_ID`.
  - JWKS is cached in memory with 1-hour TTL to reduce API calls while allowing key rotation.
  - Cache is automatically refreshed when expired.
- Validates JWT token using Cognito JWKS:
  - **Signature:** Validates RS256 signature using the matching public key from Cognito JWKS.
    The key is matched by the `kid` (key ID) in the JWT header.
  - **Issuer:** Validates `iss` claim matches `https://cognito-idp.{COGNITO_REGION}.amazonaws.com/{COGNITO_USER_POOL_ID}`
  - **Token Type:** Validates `token_use` claim equals `"id"` (must be an id token, not access token).
  - **Expiration:** Validates `exp` claim is in the future (token not expired).
- Extracts user information (`sub` as userId, `email`) from JWT claims.
  - **`sub` claim (user ID) is required and must be non-empty**; returns 401 if missing or empty
    (critical for user tracking and audit trails).
  - `email` claim is optional; defaults to empty string if missing.
- Returns **401 Unauthorized** if:
  - Token query parameter is missing or empty
  - JWT token is malformed or missing required fields
  - JWT signature verification fails
  - Issuer validation fails
  - Token type is not 'id'
  - Token is expired
  - `sub` claim is missing or empty
  - `COGNITO_REGION` or `COGNITO_USER_POOL_ID` environment variables are missing

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

Connection data and job data are stored in the same DynamoDB table (`JOBS_TABLE_NAME`), using
`jobId` as the partition key. The connection metadata attributes are added to the job's existing item.

### Logging & Debugging

All operations are logged to CloudWatch with structured JSON format including:
- `timestamp`: ISO-8601 UTC timestamp
- `level`: INFO, WARNING, or ERROR
- `jobId`: Correlation ID for tracing requests
- `stage`: Lifecycle stage (parse_input, validate_job, store_connection, error)
- `message`: Human-readable log message

Error responses do NOT include exception details to prevent information disclosure (AWS account IDs,
role names, table names, etc.). Details are logged internally for debugging.

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
- Token query parameter is missing or empty
- JWT token is malformed
- JWT signature verification fails
- Issuer validation fails
- Token type is not 'id'
- Token is expired
- `sub` claim is missing or empty
- Required environment variables are missing

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

- `handler.py` — Lambda entry point (`lambda_handler`) with DynamoDB operations and Cognito JWT validation.
  - `CognitoJWTError` — Custom exception for Cognito JWT validation failures.
  - `_response(status_code, body)` — Builds API Gateway response.
  - `_extract_jwt_from_query_params(event)` — Extracts JWT token from `token` query string parameter.
  - `_fetch_cognito_jwks(region, user_pool_id)` — Fetches Cognito JWKS with in-memory caching.
  - `_get_cognito_public_key(token, region, user_pool_id)` — Extracts public key from JWKS matching token's kid.
  - `_validate_cognito_jwt(token, region, user_pool_id)` — Validates JWT signature, issuer, expiration, and token type.
  - `_extract_user_info(event)` — Extracts user metadata (userId from `sub`, email) from validated Cognito JWT token.
  - `_extract_job_id(event)` — Extracts jobId from queryStringParameters.
  - `_extract_connection_id(event)` — Extracts connectionId from requestContext.
  - `_job_exists_in_dynamodb(dynamodb_resource, job_id, table_name)` — Queries JOBS table to validate job.
  - `_store_connection_in_dynamodb(dynamodb_resource, job_id, connection_id, user_info, table_name)` — Stores connection metadata to JOBS table as attributes.

  **Note:** The `dynamodb` resource is initialized inside `lambda_handler()` (lazy initialization) rather than at module level.
  This is a Lambda best practice that allows the handler to initialize only when invoked and enables proper test mocking
  without requiring AWS credentials/region configuration during test imports.

- `logging_config.py` — Structured JSON logging with correlation IDs.
  - `JSONFormatter` — Formats logs as single-line JSON objects.
  - `StructuredLoggerAdapter` — Injects jobId and stage into every log entry.
  - `configure_logger(name)` — Returns a configured logger instance.

- `tests/test_handler.py` — Comprehensive unit tests using `unittest` with mocked boto3, requests, and jose modules, covering:
  - Successful connection establishment with valid Cognito JWT
  - Missing or invalid JWT token scenarios (401 responses)
  - Missing or expired token scenarios
  - Invalid issuer or token_use validation failures
  - Missing jobId or connectionId (400 responses)
  - DynamoDB query and update errors (500 responses)
  - Cognito JWKS fetching and caching
  - Public key extraction from JWKS
  - Signature validation
  - Total coverage: 50+ test cases

- `requirements.txt` — Dependencies:
  - `boto3` — AWS SDK for DynamoDB operations
  - `boto3-stubs[dynamodb]` — Type hints for DynamoDB
  - `python-jose[cryptography]` — JWT validation with JWKS support (RS256)
  - `requests` — HTTP library for fetching Cognito JWKS

## Deployment

- **Region**: `eu-central-1` (Frankfurt)
- **Lambda function name**: `open-connection`
- **API Gateway WebSocket route**: `$connect` mapped to this Lambda function.
- **IAM Role**: Lambda execution role with permissions to read/write to `JOBS_TABLE_NAME` and log to CloudWatch.
  - No additional S3 or other permissions needed (JWT validation uses public Cognito endpoint).
- **DynamoDB Table**: `JOBS_TABLE_NAME` must exist with `jobId` as the partition key (string).
- **Environment Variables**: Set `COGNITO_REGION`, `COGNITO_USER_POOL_ID`, and `JOBS_TABLE_NAME` in Lambda configuration.

## Security Considerations

- **JWT Validation:** All JWT validation follows Cognito best practices:
  - RS256 signature validated against Cognito public keys
  - Issuer claim validated
  - Token expiration validated
  - Token type validated (id token only)
- **JWKS Caching:** Cognito public keys are cached with 1-hour TTL. Cache is automatically
  refreshed when expired, allowing for key rotation without missing authentication.
- **Error Responses:** Error responses do not leak internal details (table names, AWS account info, etc.).
  Diagnostic details are logged internally only.
- **No Secrets:** Unlike the legacy HS256 implementation, no shared secrets are stored in environment
  variables. All validation uses Cognito's public key infrastructure.
