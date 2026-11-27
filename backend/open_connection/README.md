# open_connection

AWS Lambda function for the WebSocket `$connect` route. It authenticates the caller with a Cognito
ID token, checks that the caller owns the requested job, and records the WebSocket connection on the
job item so later steps (the result Lambda) can push updates to the client.

## Python Version

- Lambda runtime and CI (`.github/workflows/deploy_open_connection.yml`): Python 3.12.
- The code uses `X | None` annotations, so it requires Python 3.10 or newer.

## Connection Flow

When a client connects to the WebSocket endpoint, the handler:
1. Reads `jobId` from `queryStringParameters` and `connectionId` from `requestContext`. Both must be non-blank (400 otherwise).
2. Validates the environment configuration (500 if any required variable is missing or blank).
3. Reads the ID token from the `token` query parameter and validates it against the Cognito JWKS:
   signature (RS256, key selected by `kid`), issuer, audience, expiry, `at_hash` against the `accessToken` query parameter,
   `token_use` and `sub` (401 on failure; 503 if the JWKS cannot be fetched).
4. Attaches the connection to the job with a single conditional `UpdateItem`:
   the job must exist **and** its stored `userId` must equal the token's `sub`. The handler does not read the job first.
5. Returns 200, or 404 when the condition fails (job missing or owned by another user; the two cases are indistinguishable on purpose).

## WebSocket Connect Event

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
    "token": "<cognito_id_token>",
    "accessToken": "<cognito_access_token>"
  },
  "headers": {}
}
```

The client connects with:
- `jobId` as a query string parameter (required; the job the connection belongs to).
- `token` as a query string parameter (required; a Cognito **ID token** of the job owner).
- `accessToken` as a query string parameter (the Cognito access token issued together with that ID token).
  It is required when the ID token carries an `at_hash` claim, which Cognito adds in the Hosted UI
  authorization code flow. If the ID token has no `at_hash`, `accessToken` is not needed and is ignored.

Both tokens travel in the query string, because browsers cannot set headers on a WebSocket handshake.
Query strings can end up in API Gateway access logs, so those logs must not record query parameters
or must be restricted to operators.

The ID token must satisfy:
- **Signature:** RS256, verified with the JWK whose `kid` matches the token header.
- **Audience:** `aud` equals `COGNITO_CLIENT_ID`. A missing `aud` is rejected.
- **Issuer:** `iss` equals `https://cognito-idp.{COGNITO_REGION}.amazonaws.com/{COGNITO_USER_POOL_ID}`.
- **Expiry:** `exp` is present and in the future.
- **Access token hash:** if `at_hash` is present, `accessToken` is required and its hash (SHA-256, left half,
  base64url) must equal `at_hash`. Checked by python-jose (`jwt.decode(..., access_token=...)`).
- **Token type:** `token_use` equals `"id"`. Access tokens are rejected.
- **Subject:** `sub` is present and non-blank. It is the caller's user ID.

## Entry Point

```python
lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]
```

`context` is unused. The handler returns an API-Gateway-style response (`statusCode`, `headers`, `body` as a JSON string).

## Environment Variables

All four variables are **required**. A missing or whitespace-only value returns HTTP 500 (`ServerConfiguration`) before any token is examined.

- `COGNITO_REGION`: AWS region of the Cognito user pool (e.g. `eu-central-1`). Used for the issuer and the JWKS URL.
- `COGNITO_USER_POOL_ID`: User pool ID (e.g. `eu-central-1_examplePoolId`). This is **not** the client ID.
- `COGNITO_CLIENT_ID`: App client ID (e.g. `4g5h6j7k8l9m0n1o2p3q4r5s6t7u8v9w0`). The token `aud` must equal this value.
- `JOBS_TABLE_NAME`: DynamoDB table holding job records (e.g. `miniature-ai-jobs`). Partition key `jobId` (string).

**Optional:**
- `AWS_REGION`: Not read by this code. boto3 uses it to pick the DynamoDB endpoint if set; otherwise the Lambda runtime region applies.

## Job Ownership

Jobs are created by `upload-confirmation`, which stores the caller's Cognito `sub` as `userId` on the job item.
This handler attaches a connection only when the stored `userId` equals the token's `sub`.

Consequences:
- A job without a `userId` attribute (for example one created before ownership was stored) cannot be connected; it returns 404.
- A caller who knows a `jobId` but does not own the job gets the same 404 as for a nonexistent job.
- The owner is never written by this handler. The job's `userId` is only read by the condition.

## Persistence

The handler updates the job item in `JOBS_TABLE_NAME` with exactly two attributes:
- `connectionId`: WebSocket connection ID from `requestContext`.
- `connectedAt`: Unix timestamp (seconds, UTC) of the connection.

The update is:
```
UpdateExpression:    SET connectionId = :connectionId, connectedAt = :connectedAt
ConditionExpression: attribute_exists(jobId) AND userId = :userId
```

- `attribute_exists(jobId)` stops the update from recreating a job that was deleted between requests, which would leave a partial item behind.
- `userId = :userId` enforces ownership atomically, without a separate read.
- The caller's email, if present in the token, is not stored.

`close_connection` removes these two attributes on `$disconnect`.

## Response Shapes

All responses are API Gateway-compatible dictionaries with `statusCode`, `headers` (`Content-Type: application/json`) and a JSON `body`.
Error bodies never contain exception text, AWS account or role names, table names or tokens.

**200 OK** — Connection attached to a job the caller owns:
```json
{"statusCode": 200, "headers": {"Content-Type": "application/json"},
 "body": "{\"message\": \"Connection established successfully\"}"}
```

**400 Bad Request** — `jobId` missing or blank (`"jobId is required"`), or `connectionId` missing or blank (`"Connection ID is required"`). Error code `InvalidRequest`.

**401 Unauthorized** — `{"error": "Unauthorized", "message": "Invalid or missing JWT token"}`. Returned when:
- the `token` parameter is missing or empty;
- the token is malformed, has no `kid`, or its `kid` is not in the JWKS after the refresh described below;
- the signature, audience, issuer or expiry check fails, or `aud` / `exp` is absent;
- the ID token has `at_hash` and `accessToken` is missing, empty or does not match it;
- `token_use` is not `id`;
- `sub` is missing or blank.

**404 Not Found** — `{"error": "NotFound", "message": "jobId does not exist"}`. Returned when the conditional update fails, i.e. the job does not exist **or** `userId` does not match the caller. The two cases are deliberately indistinguishable.

**500 Internal Server Error**:
- Missing or blank environment variable: `{"error": "ServerConfiguration", "message": "Failed to initialize handler"}`.
- DynamoDB update failed for any reason other than the condition: `{"error": "DatabaseError", "message": "Failed to store connection"}`.

**503 Service Unavailable** — Cognito's JWKS could not be fetched (network error, HTTP error status, non-JSON body or a payload without `keys`):
`{"error": "ServiceUnavailable", "message": "Authentication is temporarily unavailable"}`.
This is a server-side fault, so it is not reported as 401; the client may retry.

## Cognito Key Handling

- The JWKS is cached in memory for 1 hour (`_JWKS_CACHE_TTL`). A failed fetch never overwrites the cached copy, but the stale copy is not used as a fallback: once the TTL has expired and Cognito is unreachable, requests return 503.
- If a token's `kid` is not in the cache, the handler forces one refresh, so a key rotation is picked up without waiting for the TTL.
  Forced refreshes are limited to one per 60 seconds per container (`_JWKS_FORCED_REFRESH_MIN_INTERVAL`), so forged tokens with random `kid` values cannot cause one Cognito request per invocation.
  A refresh that fails (for example Cognito is unreachable) returns 503. A refresh that succeeds but still lacks the `kid` returns 401.
- The JWKS request has a 5-second timeout (`JWKS_REQUEST_TIMEOUT_SECONDS`).
- `_reset_jwks_cache()` clears the cache and the refresh timestamp. Tests call it before each case.

## Logging & Debugging

Every log line is a single JSON object:
- `timestamp`: ISO-8601 UTC timestamp
- `level`: INFO, WARNING or ERROR
- `jobId`: Job correlation ID. `"unknown"` if the request had no valid `jobId`.
- `connectionId`: WebSocket connection ID. `"unknown"` if the request had no valid `connectionId`.
- `stage`: `parse_input`, `authenticate`, `store_connection` or `error`
- `message`: Human-readable text

Tokens (neither the ID token nor the access token) are never logged. JWT rejection reasons are logged at INFO; infrastructure failures (configuration, JWKS, DynamoDB) at ERROR.

## Code Structure

- `handler.py` — Lambda entry point and helpers.
  - `CognitoJWTError` — Token missing or invalid (→ 401).
  - `JwksUnavailableError` — Cognito keys cannot be fetched or parsed (→ 503).
  - `ServerConfigurationError` — Required environment variable missing or blank (→ 500).
  - `ConnectionConfig` — Frozen dataclass of the four required settings.
  - `_response(status_code, body)` — Builds an API Gateway response.
  - `_log_error(job_id, connection_id, message)` — Logs an ERROR with correlation fields.
  - `_extract_job_id(event)` / `_extract_connection_id(event)` — Read the request identifiers.
  - `_extract_jwt_from_query_params(event)` — Reads the `token` query parameter.
  - `_extract_access_token_from_query_params(event)` — Reads the optional `accessToken` query parameter (empty → `None`).
  - `_read_required_env(name)` / `_load_config()` — Read and validate configuration at call time.
  - `_is_forced_refresh_allowed(current_time)` — Enforces the forced-refresh rate limit.
  - `_fetch_cognito_jwks(region, user_pool_id, force_refresh=False)` — Fetches the JWKS, cached with a TTL.
  - `_reset_jwks_cache()` — Clears the JWKS cache (tests).
  - `_find_jwk_by_kid(jwks, kid)` — Finds a key by `kid`.
  - `_get_cognito_public_key(token, region, user_pool_id)` — Selects the JWK for the token, refreshing once on a kid miss.
  - `_validate_cognito_jwt(token, region, user_pool_id, client_id, access_token=None)` — Verifies signature, audience, issuer, expiry, `at_hash` and `token_use`.
  - `_extract_user_id(event, config)` — Authenticates the request and returns `sub`.
  - `_is_conditional_check_failed(error)` — True for a `ConditionalCheckFailedException`.
  - `_store_connection_in_dynamodb(dynamodb_resource, job_id, connection_id, user_id, table_name)` — The conditional update.

  **Note:** The `dynamodb` resource is created inside `lambda_handler()` after all input and configuration checks,
  so tests can patch `boto3.resource` without AWS credentials or region at import time.

- `logging_config.py` — Structured JSON logging. Identical copy of the file in `backend/close_connection`.
  - `JSONFormatter` — Formats logs as single-line JSON objects with `jobId`, `connectionId` and `stage`.
  - `StructuredLoggerAdapter` — Binds those fields to every record it emits.
  - `configure_logger(name)` — Returns a configured logger instance.

- `tests/test_handler.py` — `unittest` suite (36 tests). Test tokens are real RS256 JWTs signed with throwaway keys
  generated at import time and verified by python-jose; the JWKS is served by a patched `requests.get`.
  Covers ownership (attach, owner mismatch, missing job), request validation, configuration errors (500, not 401),
  each token-rejection case, the `at_hash` check against `accessToken` (match, missing, mismatch, no `at_hash`),
  JWKS failures (503), kid refresh and its rate limit, caching, log correlation, and that neither token is ever logged.

- `requirements.txt` — Runtime dependencies:
  - `boto3`, `boto3-stubs[dynamodb]` — DynamoDB access and type stubs.
  - `python-jose[cryptography]>=3.4.0` — JWT verification. The `cryptography` extra also provides the RSA primitives the tests use.
  - `requests` — JWKS fetch.

## Setup & Testing

### Install Dependencies

```bash
# from backend/open_connection/
pip install -r requirements.txt
```

### Run Tests

```bash
# From backend/open_connection/ directory
python -m unittest discover -s tests

# Run specific test file
python -m unittest tests.test_handler

# Run specific test case
python -m unittest tests.test_handler.TestConnectOwnership.test_owned_job_gets_connection_attached

# Run tests with verbose output
python -m unittest discover -s tests -v
```

The CI gate (`deploy_open_connection.yml`) runs the first command after installing `requirements.txt`, so the suite must not depend on any package outside it.

## IAM & Deployment

- **Region**: `eu-central-1` (Frankfurt)
- **Function name**: `open-connection`
- **Runtime**: Python 3.12
- **Handler**: `handler.lambda_handler`
- **Timeout**: 10 seconds is sufficient. The JWKS request is capped at 5 seconds and the DynamoDB update is a single call.
- **Memory**: 256 MB
- **Environment variables**: `COGNITO_REGION`, `COGNITO_USER_POOL_ID`, `COGNITO_CLIENT_ID`, `JOBS_TABLE_NAME` (and optionally `AWS_REGION`).

**API Gateway:** the `$connect` route is mapped to this function.

**IAM role** (execution role):
- `dynamodb:UpdateItem` on the `JOBS_TABLE_NAME` table ARN.
- `logs:CreateLogGroup`, `logs:CreateLogStream`, `logs:PutLogEvents` (CloudWatch Logs).
- No `dynamodb:GetItem` is needed, since the handler does not read the job.
- No Cognito API permissions: keys come from the public JWKS endpoint.

**DynamoDB table:** `JOBS_TABLE_NAME` with partition key `jobId` (string). Jobs must carry a `userId` attribute (written by `upload-confirmation`) to be connectable.

## Security Considerations

### Token Validation

- **Signature (RS256)**: verified against the Cognito JWK with the token's `kid`. This is the primary authenticity check.
- **Audience**: `aud` must equal `COGNITO_CLIENT_ID`, and its presence is required. Tokens issued to another app client are rejected.
- **Issuer**: `iss` must equal the user pool's issuer URL.
- **Expiry**: `exp` must be present and not passed.
- **Access token hash**: if the ID token has `at_hash`, the `accessToken` query parameter must be present and match it.
  The access token is used only for this comparison; it is not otherwise validated and never logged.
- **Token type**: `token_use` must be `id`, so access tokens are rejected.
- **Subject**: `sub` must be present and non-blank.

### Authorization

Authentication (the token is valid) is separate from authorization (the caller may use this job). Authorization is the
`userId = :userId` condition on the job item, so a valid token alone does not grant access to a job.

### JWKS Caching

See "Cognito Key Handling" above. The cache lives in process memory; a cold start always fetches the JWKS.

### Error Responses & Logging

- Error responses carry only a generic error code and message. Details are logged at ERROR or INFO for internal debugging.
- Logs carry `jobId`, `connectionId` and `stage`, never tokens or `Authorization` material. CloudWatch Logs access should be restricted to operators.

### Secrets

No shared secrets are configured. Verification uses Cognito's public keys, so no signing key is stored in the environment.
