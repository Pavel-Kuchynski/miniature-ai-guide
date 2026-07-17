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
  Used to construct the JWKS endpoint URL and validate issuer claims.
  Format: `https://cognito-idp.{COGNITO_REGION}.amazonaws.com/{COGNITO_USER_POOL_ID}/.well-known/jwks.json`
  Example: `eu-central-1`

- `COGNITO_USER_POOL_ID`: Cognito user pool ID (e.g., `eu-central-1_examplePoolId`).
  Used to validate the issuer claim and construct the JWKS endpoint URL.
  Format: `{region}_{random-chars}` (e.g., `eu-central-1_examplePoolId`)
  **Note:** This is NOT the Client ID. Client ID is a UUID-like string; User Pool ID contains the region prefix.

- `COGNITO_CLIENT_ID`: Cognito app client ID (e.g., `4g5h6j7k8l9m0n1o2p3q4r5s6t7u8v9w0`).
  Used to validate the audience claim in the JWT token. Must match the app client used to issue the token.

- `JOBS_TABLE_NAME`: DynamoDB table name containing job records (e.g., `miniature-ai-jobs`).
  The handler queries this table to validate the `jobId` before accepting the connection, and stores
  connection metadata as attributes in the same table.
  **Note:** Table must have `jobId` (string) as the partition key.

**Optional:**
- `AWS_REGION`: AWS region for DynamoDB client configuration (e.g., `eu-central-1`). 
  If not set, boto3 uses the default region from configuration or environment.
  Recommended to set explicitly for consistent behavior across environments.

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

### Lambda Configuration

- **Region**: `eu-central-1` (Frankfurt)
- **Function name**: `open-connection`
- **Runtime**: Python 3.8+
- **Timeout**: Recommended 30 seconds (JWT validation and DynamoDB operations complete quickly)
- **Memory**: Recommended 256 MB (minimum for Python runtime)

### Environment Variables (Required for Lambda Configuration)

Set these in the Lambda function's Environment Variables section:

| Variable | Example Value | Notes |
|----------|---|---|
| `COGNITO_REGION` | `eu-central-1` | Must match your Cognito user pool region |
| `COGNITO_USER_POOL_ID` | `eu-central-1_examplePoolId` | Format: `{region}_{pool_id}`, NOT the Client ID |
| `COGNITO_CLIENT_ID` | `4g5h6j7k8l9m0n1o2p3q4r5s6t7u8v9w0` | Cognito app client ID (used to validate the audience claim in the JWT token) |
| `JOBS_TABLE_NAME` | `miniature-ai-jobs` | DynamoDB table name (must have jobId as partition key) |
| `AWS_REGION` | `eu-central-1` | Optional, recommended for explicit region configuration |

**Example AWS CLI command to update environment variables:**
```bash
aws lambda update-function-configuration \
  --function-name open-connection \
  --environment Variables="{
    COGNITO_REGION=eu-central-1,
    COGNITO_USER_POOL_ID=eu-central-1_examplePoolId,
    COGNITO_CLIENT_ID=4g5h6j7k8l9m0n1o2p3q4r5s6t7u8v9w0,
    JOBS_TABLE_NAME=miniature-ai-jobs,
    AWS_REGION=eu-central-1
  }"
```

### AWS Infrastructure Requirements

- **API Gateway WebSocket**: Route `$connect` mapped to this Lambda function
- **IAM Role**: Lambda execution role with permissions:
  - `dynamodb:GetItem` on `JOBS_TABLE_NAME` (to validate job exists)
  - `dynamodb:UpdateItem` on `JOBS_TABLE_NAME` (to store connection metadata)
  - `logs:CreateLogGroup`, `logs:CreateLogStream`, `logs:PutLogEvents` (CloudWatch logging)
  - No S3, Cognito API, or other permissions needed (uses public Cognito JWKS endpoint)

- **DynamoDB Table**: `JOBS_TABLE_NAME` must exist with:
  - **Partition Key**: `jobId` (string)
  - **Attributes**: Table should have existing job records with at least a `jobId` attribute
  - Connection metadata will be stored as additional attributes on the job item

### Security Best Practices for Deployment

1. **Environment Variables**: Use AWS Lambda Secrets Manager or Systems Manager Parameter Store for sensitive configuration (optional, if needed in future)
2. **IAM Policies**: Follow least-privilege principle; only grant required DynamoDB and CloudWatch permissions
3. **Cognito Settings**: Ensure Cognito app client is configured with appropriate callback URLs for frontend
4. **DynamoDB Encryption**: Enable at-rest encryption on the JOBS_TABLE_NAME (recommended)
5. **CloudWatch Logs**: Retention policy recommended (e.g., 30 days) to manage costs

## Security Considerations

### JWT Validation Approach

The Lambda uses Cognito's public key infrastructure (JWKS) for validation rather than shared secrets:

**Validated Claims:**
- ✅ **RS256 Signature:** Validated against Cognito JWKS public keys (the most critical validation)
- ✅ **Issuer:** Validated against `https://cognito-idp.{COGNITO_REGION}.amazonaws.com/{COGNITO_USER_POOL_ID}`
- ✅ **Expiration:** Token must not be expired (exp claim checked against current time)
- ✅ **Token Type:** `token_use` claim must be `"id"` (id tokens only, rejects access tokens)
- ✅ **Subject (User ID):** `sub` claim must be present and non-empty

**Disabled Validations (by design):**
- ❌ **Audience (aud):** Disabled because the audience varies by Cognito App Client and is not critical for this use case
- ❌ **At-Hash (at_hash):** Disabled because we only receive the ID token, not the access token needed for validation

These disabled validations are explicitly configured with `options={"verify_aud": False, "verify_at_hash": False}` 
in the JWT decode step. This is safe because the signature and issuer validation are sufficient for authentication.

### JWKS Caching

Cognito public keys are cached in memory with a 1-hour TTL:
- Reduces API calls to the Cognito JWKS endpoint
- Automatically refreshed when expired, allowing for Cognito key rotation
- Failed JWKS fetches do not invalidate the cache (resilient to temporary network issues)

### Error Responses & Logging

- **Error Responses:** Error responses do not leak internal details (table names, AWS account info, etc.).
  All errors return generic messages like "Unauthorized" or "Invalid JWT token".
- **Diagnostic Logging:** Detailed error information is logged to CloudWatch for internal debugging and audit trails.
  Logs include: timestamp, jobId correlation, lifecycle stage, and error message.
  CloudWatch logs are protected by IAM and should not be exposed to end users.

### No Shared Secrets

Unlike the legacy HS256 implementation, this Lambda uses Cognito's public key infrastructure.
No JWT_SECRET_KEY or other shared secrets are stored in environment variables, reducing the attack surface
and eliminating the need for secure secret rotation mechanisms.
