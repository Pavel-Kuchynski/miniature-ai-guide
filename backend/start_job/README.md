# Start Job Lambda

API Gateway Lambda that starts guide creation for an uploaded job. It requires an
authenticated caller, validates the request (`parse_job_id`, `parse_instruction`), reads the
job's status **only for its owner** (`get_owned_job_status`), verifies the uploaded images in
S3 (`list_uploaded_images`), moves the job to `IN_PROGRESS` (`update_job_item`) and sends the
paint message to SQS (`trigger_guide_creation`).

## Module layout

Each class lives in its own module (same layout as `backend/paint`). The Lambda root is on
`sys.path`, so modules use flat imports (`from job_status import JobStatus`).

| Module | Contents |
|---|---|
| `handler.py` | `lambda_handler`, caller identity, request parsing, AWS helpers, response builders |
| `job_status.py` | `JobStatus` enum (`UPLOADED`, `IN_PROGRESS`) |
| `json_formatter.py` | `JSONFormatter` and the `UNKNOWN` placeholder |
| `structured_logger_adapter.py` | `StructuredLoggerAdapter` |
| `logging_config.py` | `configure_logger` (re-exports the two logging classes) |

boto3 clients come from a cached `_get_client(service_name)` factory, so warm invocations
reuse them. Tests reset it with `_get_client.cache_clear()`.

## Environment variables

All are required and read at call time. A missing variable is logged and returns `500`.

| Variable | Description |
|---|---|
| `JOBS_TABLE_NAME` | DynamoDB table for job status tracking |
| `UPLOAD_BUCKET_NAME` | S3 bucket with the uploaded reference images (`uploads/<jobId>/`) |
| `PAINT_QUEUE_URL` | SQS queue consumed by the paint Lambda |

## Authentication and ownership

Every job has an owner: the Cognito `sub` of the caller who confirmed the upload
(`upload-confirmation` writes it as `userId`). API Gateway authenticates the request; this
Lambda reads the verified claims and does not validate tokens itself.

Accepted event shapes (`requestContext.authorizer`):

- REST API Cognito authorizer: `requestContext.authorizer.claims.sub`
- HTTP API JWT authorizer: `requestContext.authorizer.jwt.claims.sub`

The REST `claims` shape is used when present; the JWT shape is consulted only when `claims`
is absent. A `sub` that is missing, blank, or not a string counts as unauthenticated.

A job is visible to its owner only. A missing job and a job owned by someone else produce the
**same** `404`, so the response never reveals whether another user's job exists.

## Request contract

The request must include a `jobId` in the URL path parameter. It must be a **canonical UUID**
(lowercase, hyphenated, 36 characters) that corresponds to a job owned by the caller with the
status `UPLOADED`.

The endpoint is: `POST /jobs/{jobId}/instruction`

Path parameters:
```json
{
  "jobId": "<uuid>"
}
```

Optional JSON body:
```json
{
  "instruction": "Your instruction text here"
}
```
If `instruction` is absent or blank, nothing is stored. Otherwise the trimmed text is saved in the
job's `instruction` attribute in DynamoDB (in the same write that sets `IN_PROGRESS`) and the
paint Lambda prepends it to its prompt. A malformed JSON body, a non-object body, or a
non-string `instruction` returns `400`.

## `lambda_handler(event, context) -> dict`

Entry point that orchestrates the start-job flow:

1. **Authenticate** via `get_caller_user_id(event)`.
   - Returns `401` if no caller identity is present. Nothing else is read.
2. **Parse & validate `jobId` from path parameters** via `parse_job_id(event)`.
   - Returns `400` if `jobId` is missing, blank, or not a canonical UUID.
3. **Parse the optional `instruction`** via `parse_instruction(event)`.
   - Returns `400` for a malformed body or a non-string `instruction`.
4. **Owner-scoped status check** via `get_owned_job_status(job_id, user_id)`.
   - Returns `404` if the job does not exist **or** is owned by another user (or has no `userId`).
   - Returns `409` if the job status is anything other than `UPLOADED`.
   - Returns `500` if the DynamoDB read fails (`ClientError` or transport error).
5. **Validate exactly 1 image** via `list_uploaded_images(job_id)`.
   - Returns `500` if the S3 listing fails (`ClientError` or transport error).
   - Returns `422` if the count is not exactly 1.
6. **Update job status to IN_PROGRESS** via `update_job_item(job_id, user_id, instruction)`.
   The conditional write requires `jobStatus = UPLOADED` **and** `userId = <caller>`.
   - Returns `409` if the conditional write fails because a concurrent request already started
     the job after step 4. No SQS message is sent, so the job is painted once.
   - Returns `500` on any other DynamoDB error or transport error. The job is **not** reverted
     (see *Known limitations*).
7. **Trigger guide creation** via `trigger_guide_creation(job_id)`, which sends a message to
   the paint SQS queue.
   - On a `ClientError`, a `BotoCoreError` (transport error, e.g. a read timeout) or a missing
     `PAINT_QUEUE_URL`, the job is first reverted to `UPLOADED` via `revert_job_item`, so the
     client's retry is not stuck on `409`. Then the handler returns `500`.
   - Returns `200` if the guide creation was successfully triggered.

## Response shapes
**200 OK** (guide creation triggered):
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
or, for a present value that is not a canonical UUID:
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
**404 Not Found** (job does not exist, or is owned by another user; the two are indistinguishable):
```json
{
  "statusCode": 404,
  "headers": {"Content-Type": "application/json"},
  "body": "{\"error\": \"NotFound\", \"message\": \"jobId does not exist\"}"
}
```
**409 Conflict** (job is no longer `UPLOADED`, including a lost concurrent start):
```json
{
  "statusCode": 409,
  "headers": {"Content-Type": "application/json"},
  "body": "{\"error\": \"Conflict\", \"message\": \"jobId is already in progress or completed\"}"
}
```
**422 Unprocessable Entity** (image count != 1):
```json
{
  "statusCode": 422,
  "headers": {"Content-Type": "application/json"},
  "body": "{\"error\": \"InvalidImageCount\", \"message\": \"Exactly 1 image is required for jobId <uuid>\", \"imageCount\": 0}"
}
```
**500 Internal Server Error** (S3, DynamoDB or SQS failure, including transport errors). The
message is one of the fixed strings below; it never contains an exception message:
```json
{
  "statusCode": 500,
  "headers": {"Content-Type": "application/json"},
  "body": "{\"error\": \"InternalError\", \"message\": \"Failed to list uploaded images.\"}"
}
```
Other fixed 500 messages: `Failed to check job status.`, `Failed to record job.`,
`Failed to trigger guide creation.`, and the configuration messages
`Server misconfiguration: missing DynamoDB table name.`,
`Server misconfiguration: missing S3 bucket name.` and
`Server misconfiguration: missing SQS queue URL.`

### Error table

| Status | `error` | When |
|---|---|---|
| 200 | — | Job moved to `IN_PROGRESS` and the paint message was sent. |
| 400 | `InvalidRequest` | Missing, blank or non-UUID `jobId`; malformed body or `instruction`. |
| 401 | `Unauthorized` | No authenticated caller identity in the event. |
| 404 | `NotFound` | Job missing, or owned by another user (identical response). |
| 409 | `Conflict` | Job not `UPLOADED`, or a concurrent request won the conditional update. |
| 422 | `InvalidImageCount` | Uploaded image count is not `EXPECTED_IMAGE_COUNT`. |
| 500 | `InternalError` | S3/DynamoDB/SQS failure (including transport errors) or missing configuration. |

## `parse_job_id(event) -> (job_id, error_response)`

Parses and validates `jobId` from the URL path parameters in the event object.

- Returns `(job_id, None)` when `event["pathParameters"]["jobId"]` is a canonical UUID after
  trimming surrounding whitespace.
- Returns `(None, error_response)` with a `400` response when:
  - `pathParameters` is missing or `None`, or `jobId` is missing, not a string, or blank
    (message `jobId is required`);
  - `jobId` is not a canonical UUID (message `jobId must be a canonical UUID`). Uppercase
    letters, braces, a `urn:uuid:` prefix, a hyphen-less form, and any `/` are all rejected.

## `get_caller_user_id(event) -> str | None`

Returns the trimmed, non-empty Cognito `sub` from the authorizer claims (see
*Authentication and ownership*). Returns `None` when there are no claims, or when `sub` is
missing, blank, or not a string. It never raises for malformed `requestContext` values.

## `parse_instruction(event) -> (instruction, error_response)`

Parses the optional `instruction` from the JSON `body`. Returns `(text, None)` with trimmed text,
`(None, None)` when the body/field is missing, null or blank, and `(None, 400_response)` for a
malformed body or non-string `instruction`.

## `get_owned_job_status(job_id, user_id) -> str | None`

Reads a job's status from DynamoDB, only when the caller owns the job.

- Reads the table name from the `JOBS_TABLE_NAME` environment variable at call time.
- Uses `dynamodb.get_item` with `ConsistentRead=True`.
- Returns the raw `jobStatus` string (e.g. `"UPLOADED"`, `"IN_PROGRESS"`, `"PAINTED"`) when the
  item exists and its `userId` equals `user_id`.
- Returns `None` when the item does not exist, is owned by another user, or has no `userId`.
  The causes are deliberately indistinguishable to the caller.
- Any `botocore.exceptions.ClientError` or `BotoCoreError` propagates unchanged; the caller
  returns `500`.

## `list_uploaded_images(job_id) -> list[str]`

Lists the objects a client has uploaded to S3 for a given job, so the handler can
independently verify presence rather than trusting the frontend's report (file names
under a job's upload prefix are arbitrary, chosen by the frontend).

- Uses `s3.list_objects_v2` (via a paginator, so it's correct for >1000 objects under a
  prefix) against the `UPLOAD_BUCKET_NAME` bucket, listing everything under
  `uploads/<job_id>/`.
- Excludes zero-byte "folder marker" objects and the prefix key itself — only real
  uploaded files are returned.
- Returns `s3://<bucket>/<key>` URLs, sorted lexicographically by key. Sorting is only for
  deterministic output (logs/tests); order has no downstream meaning.
- Does **not** enforce the "exactly 1 image" business rule itself. It returns the raw
  list so `lambda_handler` can apply that check and return `422`.
- Any `ClientError` (S3 error) or `BotoCoreError` (transport error) propagates unchanged; the
  caller returns `500`.

## `update_job_item(job_id, user_id, instruction=None) -> None`

Moves the job to `IN_PROGRESS` in DynamoDB, for its owner only.

- Reads the table name from the `JOBS_TABLE_NAME` environment variable at call time.
- Uses `dynamodb.update_item` with `ConditionExpression = "jobStatus = :expected AND userId = :uid"`.
  The write therefore fails when the job is missing, not `UPLOADED`, or not owned by `user_id`.
- If `instruction` is non-empty it is saved in the `instruction` attribute in the same write;
  otherwise the attribute is not touched.
- Raises `botocore.exceptions.ClientError` on failure. `ConditionalCheckFailedException` means
  `lambda_handler` returns `409` and sends no SQS message, because the winning request already did.
- Raises `botocore.exceptions.BotoCoreError` on a transport failure. The write may already have
  been applied; `lambda_handler` returns `500` and does not revert (see *Known limitations*).

## `trigger_guide_creation(job_id) -> None`
Triggers the guide creation process by sending a message to an SQS queue.
- Reads the SQS queue URL from the `PAINT_QUEUE_URL` environment variable at call time.
- Uses `sqs.send_message` to send a message containing the `jobId`.
- Returns `None` on success.
- Raises `botocore.exceptions.ClientError` when SQS rejects the request (the message was not
  enqueued) and `botocore.exceptions.BotoCoreError` on a transport failure (the message may or
  may not have been enqueued). `lambda_handler` handles both the same way: revert and `500`.

### Message Format
The message sent to the SQS queue will be a JSON object with the following structure:
```json
{
    "jobId": "<uuid>"
}
```

## `revert_job_item(job_id, instruction=None) -> None`
Undoes `update_job_item` after the SQS send fails, so the job does not get stuck in `IN_PROGRESS`.
- Sets `jobStatus` back to `UPLOADED` and refreshes `updatedAt`, conditional on `jobStatus = IN_PROGRESS`, so it never overwrites a status another writer set in the meantime.
- Removes `instruction` only when this request stored one, so a retry without an instruction does not reuse it. An instruction it did not write is left alone.
- Raises `botocore.exceptions.ClientError` (including `ConditionalCheckFailedException`), `botocore.exceptions.BotoCoreError`, and `KeyError` for a missing `JOBS_TABLE_NAME`.
- `lambda_handler` calls it through `_release_job`, which never raises. A lost condition is logged as a warning (stage `revert_status`). Any other `ClientError`, any `BotoCoreError`, or a missing variable is logged with stage `revert_status_error`, the job stays `IN_PROGRESS`, and the original `500` is returned unchanged.

### Why an ambiguous SQS timeout is reverted

A `ClientError` from SQS means the message was not enqueued, so a revert cannot cause a second
paint. A `BotoCoreError` (for example a read timeout) is ambiguous: the message may have been
enqueued. The revert is still performed so the client's retry is not blocked by `409`. Duplicate
processing in that case is guarded by the paint Lambda's conditional status check
(`jobStatus = :expected`, see `backend/paint/handler.py`), which rejects a second run once the
job is no longer `IN_PROGRESS`.

## Known limitations

- **Ambiguous DynamoDB update timeout.** If the conditional `update_item` times out on the read,
  the write may have been applied. The handler returns `500` and does not revert, because a
  revert conditional only on `IN_PROGRESS` could undo a state a concurrent request has since won.
  In that case the job can stay `IN_PROGRESS` with no paint message, and retries return `409`.
  Manual cleanup is needed until a reconciliation step exists. The `update_status` stage log
  line is the signal.
- **Jobs created before ownership was recorded** have no `userId`. They are treated as not
  owned (`404`) and cannot be started. They need a data migration if they must stay usable.

## Logging

Every log line is one JSON object with `timestamp`, `level`, `jobId`, `stage` and `message`,
plus `exception` (traceback) for caught AWS failures. Stages: `authenticate`, `parse_input`,
`orchestrate`, `get_status`, `list_images`, `validate_images`, `update_status`,
`trigger_creation`, `revert_status`, `revert_status_error`, `complete`. Client-caused
rejections (`401`, `400`, `404`, `409`, `422`) are logged at `WARNING`; AWS and configuration
failures are logged at `ERROR`. Exception messages and resource names stay in logs and never
reach the response.

## Development

```bash
# from backend/start_job/
pip install -r requirements.txt -r requirements-dev.txt
```

### Run all tests
```bash
pytest
```

### Run a single test file
```bash
pytest tests/test_handler.py
```

### Run a single test case
```bash
pytest tests/test_handler.py::TestParseJobId::test_valid_job_id_from_path_parameters
```

### Run tests with verbose output
```bash
pytest -v
```

## Deployment

- **Region**: `eu-central-1` (Frankfurt)
- **Lambda function name**: `start-job`
- IAM is unchanged by the ownership change: the ownership check and the conditional
  `userId` requirement reuse the existing DynamoDB `GetItem` and `UpdateItem` calls, and no
  new AWS action is added.
