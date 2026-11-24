# Start Job Lambda

API Gateway Lambda that starts guide creation for an uploaded job. It validates the request
(`parse_job_id`, `parse_instruction`), checks the job status in DynamoDB (`get_job_status`),
verifies the uploaded images in S3 (`list_uploaded_images`), moves the job to `IN_PROGRESS`
(`update_job_item`) and sends the paint message to SQS (`trigger_guide_creation`).

## Module layout

Each class lives in its own module (same layout as `backend/paint`). The Lambda root is on
`sys.path`, so modules use flat imports (`from job_status import JobStatus`).

| Module | Contents |
|---|---|
| `handler.py` | `lambda_handler`, request parsing, AWS helpers, response builders |
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

## Request contract
The request must include a valid `jobId` in the URL path parameter that corresponds to an existing job in DynamoDB with the status `UPLOADED`.

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
1. **Parse & validate `jobId` from path parameters** via `parse_job_id(event)`.
    - Returns `400` if jobId is missing from path parameters, invalid, or empty.
2. **JobStatus check** via `get_job_status(job_id)`.
    - Returns `404` if jobId does not exist in DynamoDB.
    - Returns `409` if the job status is anything other than `UPLOADED`.
3. **Validate exactly 1 image** are present.
    - Returns `422` if the count is not exactly 1.
4. **Update job status to IN_PROGRESS** via `update_job_item(job_id, instruction)`.
    - Returns `409` if the conditional write fails because a concurrent request already
      started the job after step 2. No SQS message is sent, so the job is painted once.
    - Returns `500` if DynamoDB update fails for any other reason.
5. **Trigger guide creation** via `trigger_guide_creation(job_id)`. This step will be executed by sending a message to an SQS queue that will be processed by SQS consumers.
    - Returns `500` if the guide creation trigger fails. Before returning, the job is moved
      back to `UPLOADED` via `revert_job_item` so the client can retry (otherwise every retry
      would get `409` and the job would stay `IN_PROGRESS` forever).
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
**404 Not Found** (jobId does not exist):
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
**500 Internal Server Error** (S3 or DynamoDB or SQS failure):
```json
{
  "statusCode": 500,
  "headers": {"Content-Type": "application/json"},
  "body": "{\"error\": \"InternalError\", \"message\": \"Failed to list uploaded images.\"}"
}
```
or
```json
{
  "statusCode": 500,
  "headers": {"Content-Type": "application/json"},
  "body": "{\"error\": \"InternalError\", \"message\": \"Failed to record job.\"}"
}
```
or
```json
{
  "statusCode": 500,
  "headers": {"Content-Type": "application/json"},
  "body": "{\"error\": \"InternalError\", \"message\": \"Failed to trigger guide creation.\"}"
}
```

## `parse_job_id(event) -> (job_id, error_response)`

Parses and validates `jobId` from the URL path parameters in the event object.

- Returns `(job_id, None)` when `event["pathParameters"]["jobId"]` is a present,
  non-empty string after trimming whitespace (the returned `job_id` is trimmed).
- Returns `(None, error_response)` on any of the following, where `error_response` is a
  `400` API-Gateway-style response with body `{"error": "InvalidRequest", "message": "jobId is required"}`:
    - `pathParameters` key is missing from the event or is `None`
    - `jobId` key is missing from `pathParameters`
    - `jobId` is not a string
    - `jobId` is an empty string or whitespace-only
- `jobId` is **not** validated as a UUID — any non-empty string is accepted at this
  layer; downstream S3/DynamoDB lookups will fail naturally for a bogus id.

## `parse_instruction(event) -> (instruction, error_response)`

Parses the optional `instruction` from the JSON `body`. Returns `(text, None)` with trimmed text,
`(None, None)` when the body/field is missing, null or blank, and `(None, 400_response)` for a
malformed body or non-string `instruction`.

## `get_job_status(job_id) -> str | None`

Queries DynamoDB to retrieve a job's current status.

- Reads the table name from the `JOBS_TABLE_NAME` environment variable at call time.
- Uses `dynamodb.get_item` with `ConsistentRead=True` to fetch the job item.
- Returns the job's status string (e.g., `"UPLOADED"`, `"IN_PROGRESS"`, `"PAINTED"`) if found.
- Returns `None` if the job does not exist in DynamoDB.
- Any `botocore.exceptions.ClientError` raised by DynamoDB (throttling, access denied, table not found, etc.) propagates unchanged; the caller is responsible for turning that into a `500` response.

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
  list (which may have fewer or more than 1 entry) so `lambda_handler` can apply that check
  and return `422`.
- Any `botocore.exceptions.ClientError` raised by S3 (throttling, access denied, bucket
  not found, etc.) propagates unchanged; the caller is responsible for turning that into a
  `500` response.

## `update_job_item(job_id, instruction=None) -> None`

Update the job item in DynamoDB to set the status to `IN_PROGRESS`. If `instruction` is non-empty it is saved in the `instruction` attribute in the same conditional write; otherwise the attribute is not touched.

- Reads the table name from the `JOBS_TABLE_NAME` environment variable at call time.
- Uses `dynamodb.update_item` with a conditional expression to ensure the job exists and is in the `UPLOADED` state before updating.
- Returns `None` on success.
- Raises `botocore.exceptions.ClientError` on failure (e.g., job not found, conditional check failed, race condition), which the caller is responsible for handling and converting to appropriate responses.
- Note: If two requests try to update the same job concurrently, the second one raises a `ClientError` with code `ConditionalCheckFailedException`. `lambda_handler` returns `409` for it and does not send an SQS message, because the winning request already did.

## `trigger_guide_creation(job_id) -> None`
Triggers the guide creation process by sending a message to an SQS queue.
- Reads the SQS queue URL from the `PAINT_QUEUE_URL` environment variable at call time.
- Uses `sqs.send_message` to send a message containing the `jobId`.
- Returns `None` on success.
- Raises `botocore.exceptions.ClientError` on failure (e.g., queue not found, access denied, etc.), which the caller is responsible for handling and converting to a `500` response.

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
- Raises `botocore.exceptions.ClientError` (including `ConditionalCheckFailedException`) and `KeyError` for a missing `JOBS_TABLE_NAME`.
- `lambda_handler` calls it through `_release_job`, which never raises. A lost condition is logged as a warning (stage `revert_status`); any other failure is logged with stage `revert_status_error`, the job stays `IN_PROGRESS`, and the original `500` is returned unchanged.
- The revert runs only for a `ClientError` from SQS or a missing `PAINT_QUEUE_URL`. In both cases the message was definitely not enqueued, so a retry cannot paint the job twice.

## Logging

Every log line is one JSON object with `timestamp`, `level`, `jobId`, `stage` and `message`,
plus `exception` (traceback) for caught AWS failures. Stages: `parse_input`, `orchestrate`,
`get_status`, `list_images`, `validate_images`, `update_status`, `trigger_creation`,
`revert_status`, `revert_status_error`, `complete`. Exception messages and resource names stay in logs and never reach the response.

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