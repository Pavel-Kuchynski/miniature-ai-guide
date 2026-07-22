# Start Job Lambda

AWS lambda function that starts a job by creating a new guide.
This lambda orchestrates the request validation (`parse_job_id`), DynamoDB status check (`get_job_status`), S3 presence check (`list_uploaded_images`), and job status update (`update_job_item`) helpers to implement the complete start job flow.
JobId is present in DynamoDB and has status `UPLOADED` before this lambda is called.\
Table name is specified in the environment variable `JOBS_TABLE_NAME`.\
SQS queue URL is specified in the environment variable `GUIDE_CREATION_QUEUE_URL`.\
S3 bucket name is specified in the environment variable `UPLOAD_BUCKET_NAME`.

## Request contract
The request must include a valid `jobId` in the URL path parameter that corresponds to an existing job in DynamoDB with the status `UPLOADED`.

The endpoint is: `POST /jobs/{jobId}/instruction`

Path parameters:
```json
{
  "jobId": "<uuid>"
}
```

## `lambda_handler(event, context) -> dict`
Entry point that orchestrates the upload confirmation flow:
1. **Parse & validate `jobId` from path parameters** via `parse_job_id(event)`.
    - Returns `400` if jobId is missing from path parameters, invalid, or empty.
2. **JobStatus check** via `get_job_status(job_id)`.
    - Returns `404` if jobId does not exist in DynamoDB.
    - Returns `409` if job status is `IN_PROGRESS` or `SUCCEEDED`.
3. **Validate exactly 4 images** are present.
    - Returns `422` if the count is not exactly 4.
4. **Update job status to IN_PROGRESS** via `update_job_item(job_id)`.
    - Returns `500` if DynamoDB update fails.
5. **Trigger guide creation** via `trigger_guide_creation(job_id)`. This step will be executed by sending a message to an SQS queue that will be processed by SQS consumers.
    - Returns `500` if the guide creation trigger fails.
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
**409 Conflict** (job status is `IN_PROGRESS` or `SUCCEEDED`):
```json
{
  "statusCode": 409,
  "headers": {"Content-Type": "application/json"},
  "body": "{\"error\": \"Conflict\", \"message\": \"jobId is already in progress or completed\"}"
}
```
**422 Unprocessable Entity** (image count != 4):
```json
{
  "statusCode": 422,
  "headers": {"Content-Type": "application/json"},
  "body": "{\"error\": \"InvalidImageCount\", \"message\": \"Exactly 4 images are required for jobId <uuid>\"}"
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

## `get_job_status(job_id) -> str | None`

Queries DynamoDB to retrieve a job's current status.

- Reads the table name from the `JOBS_TABLE_NAME` environment variable at call time.
- Uses `dynamodb.get_item` with `ConsistentRead=True` to fetch the job item.
- Returns the job's status string (e.g., `"UPLOADED"`, `"IN_PROGRESS"`, `"SUCCEEDED"`) if found.
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
- Does **not** enforce the "exactly 4 images" business rule itself — it returns the raw
  list (which may have fewer or more than 4 entries) so the caller (`lambda_handler`, in a
  later task) can apply that check and return the appropriate error response.
- Any `botocore.exceptions.ClientError` raised by S3 (throttling, access denied, bucket
  not found, etc.) propagates unchanged; the caller is responsible for turning that into a
  `500` response.

## `update_job_item(job_id) -> None`

Update the job item in DynamoDB to set the status to `IN_PROGRESS`.

- Reads the table name from the `JOBS_TABLE_NAME` environment variable at call time.
- Uses `dynamodb.update_item` with a conditional expression to ensure the job exists and is in the `UPLOADED` state before updating.
- Returns `None` on success.
- Raises `botocore.exceptions.ClientError` on failure (e.g., job not found, conditional check failed, race condition), which the caller is responsible for handling and converting to appropriate responses.
- Note: If two requests try to update the same job concurrently, the second one will raise `ConditionalCheckFailedException` (a subtype of `ClientError`). The caller should handle this by treating it as idempotent—the job is already IN_PROGRESS.

## `trigger_guide_creation(job_id) -> None`
Triggers the guide creation process by sending a message to an SQS queue.
- Reads the SQS queue URL from the `GUIDE_CREATION_QUEUE_URL` environment variable at call time.
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

## Development

```bash
# from backend/start_job/
pip install -r requirements.txt
pip install -r requirements-dev.txt
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