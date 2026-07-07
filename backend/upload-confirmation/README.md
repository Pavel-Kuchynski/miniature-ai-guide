# upload-confirmation

AWS Lambda function that confirms a job's 4 reference images have finished uploading to S3
and records the job in DynamoDB. Orchestrates the request validation (`parse_job_id`), S3
presence check (`list_uploaded_images`), and DynamoDB write (`put_job_item`) helpers to
implement the complete upload-confirmation flow.

## Request contract

`PUT /job` with a JSON body:

```json
{ "jobId": "<uuid>" }
```

## `lambda_handler(event, context) -> dict`

Entry point that orchestrates the upload confirmation flow:

1. **Parse & validate `jobId`** via `parse_job_id(event)`.
   - Returns `400` if jobId is missing, invalid, or empty.
2. **List uploaded images** via `list_uploaded_images(job_id)`.
   - Returns `500` if S3 listing fails (e.g. throttling, access denied).
3. **Validate exactly 4 images** are present.
   - Returns `422` if the count is not exactly 4.
4. **Write job to DynamoDB** via `put_job_item(job_id, image_urls)`.
   - Returns `500` if DynamoDB write fails.
   - Returns `201` if this is the first confirmation (new job created).
   - Returns `200` if a prior confirmation already created the job (idempotent duplicate).

### Response shapes

**201 Created** (new job):
```json
{
  "statusCode": 201,
  "headers": {"Content-Type": "application/json"},
  "body": "{\"jobId\": \"<uuid>\", \"jobStatus\": \"UPLOADED\"}"
}
```

**200 OK** (duplicate confirmation):
```json
{
  "statusCode": 200,
  "headers": {"Content-Type": "application/json"},
  "body": "{\"jobId\": \"<uuid>\", \"jobStatus\": \"UPLOADED\"}"
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

**422 Unprocessable Entity** (image count != 4):
```json
{
  "statusCode": 422,
  "headers": {"Content-Type": "application/json"},
  "body": "{\"error\": \"MissingImages\", \"message\": \"Expected 4 images, found N\", \"imageCount\": N}"
}
```

**500 Internal Server Error** (S3 or DynamoDB failure):
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

## `parse_job_id(event) -> (job_id, error_response)`

Parses and validates `jobId` from `event["body"]`.

- Returns `(job_id, None)` when `jobId` is present, a string, and non-empty after
  trimming whitespace (the returned `job_id` is trimmed).
- Returns `(None, error_response)` on any of the following, where `error_response` is a
  `400` API-Gateway-style response with body `{"error": "InvalidRequest", "message": "jobId is required"}`:
  - `body` is missing or empty
  - `body` is not valid JSON
  - `body` is valid JSON but not a JSON object
  - `jobId` is missing, not a string, or empty/whitespace-only
- `jobId` is **not** validated as a UUID — any non-empty string is accepted at this
  layer; downstream S3/DynamoDB lookups will fail naturally for a bogus id.

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
- Does **not** enforce the "exactly 4 images" business rule itself — it returns the raw
  list (which may have fewer or more than 4 entries) so the caller (`lambda_handler`, in a
  later task) can apply that check and return the appropriate error response.
- Any `botocore.exceptions.ClientError` raised by S3 (throttling, access denied, bucket
  not found, etc.) propagates unchanged; the caller is responsible for turning that into a
  `500` response.

## `put_job_item(job_id, image_urls) -> (created, item)`

Creates the job item in DynamoDB, idempotently. This Lambda is the only writer that
creates a job item, and two consecutive confirmations for the same `jobId` must not error
and must not overwrite each other.

- Reads the table name from the `JOBS_TABLE_NAME` environment variable at call time.
- Optimistically attempts `PutItem` with `ConditionExpression=attribute_not_exists(jobId)`,
  to save a read on the common/happy path (a job's first confirmation).
- On success, returns `(True, item)` where `item` is the DynamoDB low-level (typed
  attribute) item just written:
  ```
  jobId        (S, PK)
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
  item, unchanged.
  - If that fallback `GetItem` unexpectedly finds no item (a race-condition edge case),
    raises `RuntimeError` rather than fabricating a response.
- Any other `botocore.exceptions.ClientError` (e.g. throttling, access denied, table not
  found) propagates unchanged; the caller is responsible for turning that into a `500`
  response.

## Development

```bash
# from backend/upload-confirmation/
pip install -r requirements.txt
pip install -r requirements-dev.txt

# run all tests
pytest
```
