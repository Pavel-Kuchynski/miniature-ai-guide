# upload-confirmation

AWS Lambda function that confirms a job's reference image has finished uploading to S3 and
records the job in DynamoDB, owned by the authenticated caller. Orchestrates caller
identity (`get_caller_user_id`), request validation (`parse_job_id`), the S3 presence check
(`list_uploaded_images`) and the owner-aware DynamoDB write (`put_job_item`) into the
complete upload-confirmation flow.

## Module layout

| File | Contents |
|---|---|
| `handler.py` | `lambda_handler`, caller/input parsing, S3 and DynamoDB helpers, response builders |
| `manual_test.py` | Legacy mock-based orchestration script; also collected by pytest |
| `tests/test_handler.py` | Identity, `parse_job_id`, `list_uploaded_images`, `put_job_item`, `lambda_handler` |
| `tests/test_dynamodb_write.py` | `put_job_item` against a moto DynamoDB table |
| `tests/test_s3_listing.py` | `list_uploaded_images` against a moto S3 bucket |

## Environment variables

Both are required and read at call time. A missing variable is logged and returns `500`.

| Variable | Description |
|---|---|
| `UPLOAD_BUCKET_NAME` | S3 bucket holding the uploaded reference images (`uploads/<jobId>/`) |
| `JOBS_TABLE_NAME` | DynamoDB table for job records (partition key `jobId`) |

## Authentication and ownership

The job's owner is the Cognito `sub` of the caller. Authentication is done by API Gateway;
this Lambda reads the verified claims from the event and does not validate tokens itself.

Accepted event shapes (`requestContext.authorizer`):

- REST API Cognito authorizer: `requestContext.authorizer.claims.sub`
- HTTP API JWT authorizer: `requestContext.authorizer.jwt.claims.sub`

The REST `claims` shape is used when present; the JWT shape is consulted only when `claims`
is absent. A `sub` that is missing, blank, or not a string counts as unauthenticated.

## Request contract

`PUT /job` with a JSON body:

```json
{ "jobId": "<uuid>" }
```

`jobId` may also be supplied as a query-string parameter, or in a JSON body (base64-decoded
first when `isBase64Encoded` is true). The first non-blank value found in this order is
used: top-level `jobId`, `queryStringParameters.jobId`, the body.

`jobId` must be a **canonical UUID**: lowercase, hyphenated, 36 characters, the form
`lambda_upload` generates. Uppercase, braces, a `urn:uuid:` prefix, a hyphen-less form, or
any other character (such as `/`) is rejected with `400`, because the value becomes part of
the S3 prefix `uploads/<jobId>/`.

## `lambda_handler(event, context) -> dict`

Entry point that orchestrates the upload confirmation flow:

1. **Authenticate** via `get_caller_user_id(event)`.
   - Returns `401` if no caller identity is present. No S3 or DynamoDB call is made.
2. **Parse & validate `jobId`** via `parse_job_id(event)`.
   - Returns `400` if `jobId` is missing, blank, or not a canonical UUID.
3. **List uploaded images** via `list_uploaded_images(job_id)`.
   - Returns `500` if S3 listing fails (a `ClientError` or a transport error such as a timeout).
4. **Validate exactly 1 image** is present.
   - Returns `422` if the count is not exactly 1.
5. **Write job to DynamoDB** via `put_job_item(job_id, image_urls, user_id)`.
   - Returns `500` if the DynamoDB write fails (`ClientError` or transport error).
   - Returns `201` if this is the first confirmation (the caller now owns a new job).
   - On a duplicate (the item already exists):
     - if the stored `userId` differs from the caller, returns `404`. A missing job is never
       reported differently from a job owned by someone else;
     - otherwise returns `200` with the job's **stored** `jobStatus`, which may already be
       `IN_PROGRESS` or later.

Ownership is checked after the S3 listing and the image count, because the owner is only
known from the DynamoDB item. See *Known limitations*.

### Error table

| Status | `error` | When |
|---|---|---|
| 201 | — | New job created, owned by the caller. |
| 200 | — | Duplicate confirmation by the owner. `jobStatus` is the stored status. |
| 400 | `InvalidRequest` | `jobId` missing, blank, or not a canonical UUID. |
| 401 | `Unauthorized` | No authenticated caller identity in the event. |
| 404 | `NotFound` | Duplicate confirmation of a job owned by another user, or a job item without a `userId`. |
| 422 | `MissingImages` | The uploaded image count is not exactly 1. Body includes `imageCount`. |
| 500 | `InternalError` | S3 or DynamoDB failure (including transport errors), a race on the item, or missing environment configuration. |

Client-visible `message` values are fixed strings. Exception text and resource names stay in
the logs only.

## Response shapes

**201 Created** (new job):
```json
{
  "statusCode": 201,
  "headers": {"Content-Type": "application/json"},
  "body": "{\"jobId\": \"<uuid>\", \"jobStatus\": \"UPLOADED\"}"
}
```

**200 OK** (duplicate confirmation by the owner; `jobStatus` is the stored value):
```json
{
  "statusCode": 200,
  "headers": {"Content-Type": "application/json"},
  "body": "{\"jobId\": \"<uuid>\", \"jobStatus\": \"IN_PROGRESS\"}"
}
```

**400 Bad Request** (invalid/missing jobId):
```json
{
  "statusCode": 400,
  "headers": {"Content-Type": "application/json"},
  "body": "{\"error\": \"InvalidRequest\", \"message\": \"jobId is required\"}"
}
```
or, for a value that is present but not a canonical UUID:
```json
{
  "statusCode": 400,
  "headers": {"Content-Type": "application/json"},
  "body": "{\"error\": \"InvalidRequest\", \"message\": \"jobId must be a canonical UUID\"}"
}
```

**401 Unauthorized** (no caller identity):
```json
{
  "statusCode": 401,
  "headers": {"Content-Type": "application/json"},
  "body": "{\"error\": \"Unauthorized\", \"message\": \"Authentication is required.\"}"
}
```

**404 Not Found** (job owned by another user):
```json
{
  "statusCode": 404,
  "headers": {"Content-Type": "application/json"},
  "body": "{\"error\": \"NotFound\", \"message\": \"jobId does not exist\"}"
}
```

**422 Unprocessable Entity** (image count != 1):
```json
{
  "statusCode": 422,
  "headers": {"Content-Type": "application/json"},
  "body": "{\"error\": \"MissingImages\", \"message\": \"Expected 1 image, found N\", \"imageCount\": N}"
}
```

**500 Internal Server Error** (S3 or DynamoDB failure). The message is one of the fixed
strings `Failed to list uploaded images.`, `Failed to record job.`, or
`Failed to record job due to race condition.`, or a configuration message such as
`Server misconfiguration: missing S3 bucket name.`:
```json
{
  "statusCode": 500,
  "headers": {"Content-Type": "application/json"},
  "body": "{\"error\": \"InternalError\", \"message\": \"Failed to list uploaded images.\"}"
}
```

## `get_caller_user_id(event) -> str | None`

Returns the trimmed, non-empty Cognito `sub` from the authorizer claims (see
*Authentication and ownership*). Returns `None` when there are no claims, or when `sub` is
missing, blank, or not a string. It never raises for malformed or absent `requestContext`
values.

## `parse_job_id(event) -> (job_id, error_response)`

Parses and validates `jobId` from the event.

- Returns `(job_id, None)` when the first non-blank `jobId` found (top-level, query string,
  then base64/JSON body) is a canonical UUID. The returned `job_id` is trimmed.
- Returns `(None, error_response)` with a `400` `InvalidRequest` response when:
  - no non-blank `jobId` string is present (message `jobId is required`), or
  - the value is not a canonical UUID (message `jobId must be a canonical UUID`).
- A malformed base64 body or malformed JSON is logged as a warning and treated as "no value",
  so the next source is tried.

## `list_uploaded_images(job_id) -> list[str]`

Lists the objects a client has uploaded to S3 for a given job, so the handler can
independently verify presence rather than trusting the frontend's report (file names under a
job's upload prefix are arbitrary, chosen by the frontend).

- Uses `s3.list_objects_v2` (via a paginator, so it's correct for >1000 objects under a
  prefix) against the `UPLOAD_BUCKET_NAME` bucket, listing everything under
  `uploads/<job_id>/`.
- Excludes zero-byte "folder marker" objects and the prefix key itself.
- Returns `s3://<bucket>/<key>` URLs, sorted lexicographically by key.
- Does **not** enforce the "exactly 1 image" rule; `lambda_handler` applies it and returns `422`.
- Raises `botocore.exceptions.ClientError` (S3 error) and `botocore.exceptions.BotoCoreError`
  (transport error, e.g. timeout) unchanged. The caller turns both into `500`.
- Raises `KeyError` if `UPLOAD_BUCKET_NAME` is not set.

## `put_job_item(job_id, image_urls, user_id) -> (created, item)`

Creates the job item in DynamoDB, owned by `user_id`, idempotently. This Lambda is the only
writer that creates a job item, and two consecutive confirmations for the same `jobId` must
not error and must not overwrite each other.

- Reads the table name from the `JOBS_TABLE_NAME` environment variable at call time.
- Optimistically attempts `PutItem` with `ConditionExpression=attribute_not_exists(jobId)`,
  to save a read on the common/happy path (a job's first confirmation).
- On success, returns `(True, item)` where `item` is the DynamoDB low-level (typed
  attribute) item just written:
  ```
  jobId        (S, PK)
  userId       (S) = the caller's Cognito sub (owner)
  imageUrls    (L of S)
  jobStatus    (S) = "UPLOADED"
  createdAt    (S, ISO-8601, UTC)
  updatedAt    (S, ISO-8601, UTC) — same value as createdAt on initial write
  ttl          (N, epoch seconds) = createdAt + 7 days
  ```
  `connectionId`, `pdfUrl`, and `errorMessage` are left absent (not written as `null` or
  empty string).
- On a `ConditionalCheckFailedException` (a second confirmation for the same `jobId`),
  falls back to a consistent `GetItem` and returns `(False, existing_item)` — the original
  item, unchanged, including its original `userId`. The caller compares `userId` before
  revealing the item's state.
  - If that fallback `GetItem` unexpectedly finds no item, raises `RuntimeError` rather than
    fabricating a response.
- Any other `ClientError` and any `BotoCoreError` propagate unchanged; the caller returns `500`.

## Known limitations

- **Jobs created before ownership was recorded** have no `userId`. They can no longer be
  confirmed by anyone (`404`). They need a data migration if they must stay usable.
- **Image count is visible before ownership is checked.** The S3 listing and the `422` check
  run before the DynamoDB owner check, so a non-owner who confirms a job with the wrong image
  count receives `422` with `imageCount`. Only the count is revealed, not the owner or status.
  Reordering would require a read before the S3 listing.

## Logging

The handler uses the standard `logging` module (logger name `handler`). AWS and configuration
failures are logged at `ERROR`, with the exception attached as the failure detail. A missing
caller identity, a malformed base64/JSON body, and a duplicate confirmation by a non-owner are
logged at `WARNING`. Log lines never reach the response body.

## Development

```bash
# from backend/upload-confirmation/
pip install -r requirements.txt
pip install -r requirements-dev.txt

# run all tests
pytest

# run a single test file
pytest tests/test_handler.py

# run a single test case
pytest tests/test_handler.py::TestLambdaHandler::test_duplicate_confirmation_by_other_user_returns_404_and_keeps_owner
```

## Deployment and IAM

- The execution role is defined outside this repository. The ownership change does not
  require any IAM change: it adds no new AWS calls (the same S3 listing and DynamoDB
  `PutItem`/`GetItem` as before).
- No new environment variables are required. The deploy workflow
  (`.github/workflows/deploy_upload_confirmation.yml`) sets `UPLOAD_BUCKET_NAME` and
  `JOBS_TABLE_NAME`.
