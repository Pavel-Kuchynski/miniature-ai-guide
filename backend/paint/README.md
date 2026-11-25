# Paint Lambda

AWS Lambda function that handles painting of previously uploaded reference images.
Triggered by an SQS message containing a `jobId`. Downloads the single reference image
from S3, reads a painting prompt, calls OpenAI to generate a painted image, uploads it to S3
via a presigned PUT URL, moves the job to `PAINTED` in DynamoDB, and sends the guide-creation
message. A painting failure marks the job `FAILED` and tells the client through the result
queue.

## Module layout

Each class lives in its own module. `handler.py` imports the job classes and
`logging_config.py` re-exports the logging classes, so existing imports keep working.

| Module | Contents |
|---|---|
| `handler.py` | `lambda_handler` and the paint flow functions |
| `job_status.py` | `JobStatus` enum |
| `job_record.py` | `JobRecord` dataclass |
| `job_not_found_error.py` | `JobNotFoundError` exception |
| `json_formatter.py` | `JSONFormatter` and the `UNKNOWN` placeholder |
| `structured_logger_adapter.py` | `StructuredLoggerAdapter` |
| `logging_config.py` | `configure_logger` (re-exports `UNKNOWN` and the two logging classes) |

## Environment variables

| Variable | Required | Description |
|---|---|---|
| `UPLOAD_BUCKET_NAME` | yes | S3 bucket containing reference images (`uploads/<jobId>/`) |
| `STATIC_BUCKET_NAME` | yes | S3 bucket containing the painting prompt file (`prompts/paint_images_prompt.txt`) |
| `PAINT_BUCKET_NAME` | yes | S3 bucket where painted images are stored (`painted_images/<jobId>/`) |
| `JOBS_TABLE_NAME` | yes | DynamoDB table for job status tracking |
| `GUIDE_CREATION_QUEUE_URL` | yes | SQS queue URL that receives the guide-creation message on success |
| `RESULT_QUEUE_URL` | yes | SQS queue URL that receives a `FAILED` message when painting fails or guide creation permanently fails |
| `OPENAI_API_KEY_SECRET_NAME` | no | Secrets Manager secret name for the OpenAI API key. Defaults to `miniature-guide/openai/api-key` (`DEFAULT_OPENAI_API_KEY_SECRET_NAME`). The secret's JSON must have an `OPENAI_API_KEY` field. |
| `OPENAI_MODEL` | no | OpenAI image model name. Defaults to `gpt-image-1` (`DEFAULT_OPENAI_MODEL`). |

The module reads all of these variables at call time, not at import time. A missing
required variable raises `KeyError` at the point it is first needed. The two optional
variables fall back to their defaults when unset.

## `lambda_handler(event, context) -> None`

Entry point triggered by SQS. The job's status decides the path.

1. **Parse `jobId`** from `Records[0].body` via `parse_job_id(event)`.
   - Raises `ValueError` if the jobId is missing or invalid (routes message to DLQ).
   - Non-object JSON bodies and non-object records are treated as invalid, not as errors
     of a different kind.
2. **Read the job** via `get_job(job_id)`, a single strongly consistent DynamoDB read of
   the item in `JOBS_TABLE_NAME`. It returns a `JobRecord` with the job's `status` and its
   optional `instruction`.
   - If no item exists, `get_job` raises `JobNotFoundError`; `lambda_handler` converts
     it to `ValueError` (routes message to DLQ).
   - A status of `PAINTED` is a redelivery. Go to step 6 and skip painting.
   - A status other than `IN_PROGRESS` or `PAINTED` raises `ValueError` (routes message to DLQ).
   - A missing `JOBS_TABLE_NAME` raises `KeyError` and propagates; it is not reported as
     "job not found".
   - A DynamoDB error during this read propagates to the caller before any status is written.
3. **Check for a stored image** via `_painted_image_exists(job_id)`, a `HeadObject` on
   `PAINT_BUCKET_NAME/painted_images/<jobId>/image_0.png`. If the image is already there, an
   earlier invocation paid for OpenAI and failed before the PAINTED write. Skip step 4 and go to
   step 5 (stage `skip_paint`). Any S3 error other than "not found" propagates and SQS retries.
4. **Paint the reference image** in `_paint_reference_image(job_id, instruction)`:
   - **Download** the reference image from `UPLOAD_BUCKET_NAME` via `download_reference_image(job_id)`,
     which returns the image as `bytes`. Only the first object under the job prefix is used;
     zero-byte objects are skipped and `ValueError` is raised if none exist.
   - **Fetch the prompt** from `STATIC_BUCKET_NAME/prompts/paint_images_prompt.txt` via
     `fetch_prompt_from_s3()`.
     - If the job has a non-blank `instruction` (saved by the start-job Lambda), then
       `build_prompt(prompt, instruction)` produces `Instruction: <instruction> + Prompt: <prompt>`.
       Without one, the S3 prompt is sent unchanged.
   - **Generate** via OpenAI `images.edit` in `generate_painted_images([image], prompt)`.
     - The model is `OPENAI_MODEL` (default `gpt-image-1`).
     - The API key comes from the secret named by `OPENAI_API_KEY_SECRET_NAME`, via
       `_get_openai_api_key(secret_name)`. It is cached in memory per secret name for
       `OPENAI_API_KEY_CACHE_TTL_SECONDS` (300 s). If OpenAI rejects the key with
       `AuthenticationError` or `PermissionDeniedError`, the key is re-read from Secrets Manager
       (bypassing the cache), the client is rebuilt, and the request is sent once more. See
       "OpenAI API key cache and refresh" below.
     - The reference image is first downscaled to at most 1024 px on its longer side (aspect
       ratio kept, never upscaled) and re-encoded as PNG by `_resize_reference_image` to reduce cost.
     - Requests `n=REQUESTED_OUTPUT_COUNT` (1), `output_format="png"` and `quality="medium"`.
     - Raises `ValueError` unless OpenAI returns exactly `REQUESTED_OUTPUT_COUNT` (1) images.
       Fewer images and more images are both treated as a failure.
   - **Upload painted images** to `PAINT_BUCKET_NAME/painted_images/<jobId>/image_<n>.png` via
     presigned PUT URLs in `upload_painted_images(job_id, images)`.
5. **Mark the job `PAINTED`** with `update_job_status(job_id, JobStatus.PAINTED)`, a conditional
   write requiring the status to be `IN_PROGRESS`. A `ClientError` is logged at
   `update_status_painted_error` and re-raised so SQS retries.
6. **Send the guide-creation message** via `_send_guide_creation(job_id, log)`, which calls
   `notify_guide_creation(job_id)`. Failures are handled as described below.

Steps 3 and 4 are what make a retry safe. A redelivered message never repeats the OpenAI
call once the image is stored, so a failed PAINTED write or a failed guide-creation send
costs no second generation.

## Failure handling

| Situation | Behavior |
|---|---|
| `jobId` missing or invalid | `ValueError` (DLQ). Logged at `parse_input` with `jobId=unknown`. |
| Job not found | `ValueError` (DLQ). Logged at `get_job_status`. |
| Status neither `IN_PROGRESS` nor `PAINTED` | `ValueError` (DLQ). Logged at `validate_status`. |
| DynamoDB error on the initial read | Propagates. SQS retries. No status is written. |
| `KeyError` for a missing env var, or the OpenAI secret has no `OPENAI_API_KEY` | Propagates unchanged. The job is **not** marked `FAILED` and no report is sent. It stays `IN_PROGRESS`, which is a deployment problem, not a painting failure. The message is retried and then goes to the DLQ. |
| `ClientError` or `BotoCoreError` on the stored-image check | Propagates. SQS retries. |
| Any other exception while painting or uploading (step 4) | Logged at `paint_error` with traceback. `_mark_failed` writes `FAILED` (conditional on `IN_PROGRESS`); a failed write is logged at `update_status_failed_error`. `_report_failure` sends `{"jobId", "status": "FAILED", "message": "Painting failed: <ExceptionClassName>"}` to `RESULT_QUEUE_URL`; a failed send is logged at `notify_result_error`. Return without raising. |
| PAINTED write fails (step 5) | Logged at `update_status_painted_error` with traceback, then **re-raised**. The image is already stored, so the retry skips OpenAI. |
| Guide-creation send fails, transient (`ClientError` with any code except the permanent ones below, or `BotoCoreError`) | Logged at `notify_error` with traceback, then **re-raised**. The job stays `PAINTED`, and the retry re-sends only the guide message. |
| Guide-creation send fails, permanent (`ClientError` with code `AWS.SimpleQueueService.NonExistentQueue` or `AccessDenied`) | Logged at `notify_error`. `_mark_failed(job_id, expected_status=PAINTED)` writes `FAILED` from `PAINTED`, and `_report_failure` sends the `FAILED` message to the result queue. Return without raising. A retry cannot fix these codes, so the client is told now. |
| `KeyError` for `GUIDE_CREATION_QUEUE_URL` | Propagates unchanged. The job stays `PAINTED`. |

Redelivery behaviour, by the job's state when the message arrives:

| State on arrival | What runs |
|---|---|
| `IN_PROGRESS`, no stored image | Full paint, upload, PAINTED write, guide message. |
| `IN_PROGRESS`, image already stored | No OpenAI call. PAINTED write, guide message. |
| `PAINTED` | No OpenAI call and no status write. Guide message only. |
| `FAILED` or `COMPLETED` | `ValueError` (DLQ). |

A message that exhausts its SQS receive count goes to the DLQ. The job then stays in whatever
state it reached. The receive-count limit is set in infrastructure, outside this repository.

## Job statuses

Job status values are the `JobStatus` string enum: `IN_PROGRESS`, `PAINTED` and `FAILED`.
`get_job` raises `ValueError` if the stored `jobStatus` is not one of these values.

## AWS clients

boto3 clients are created through `_get_client(service_name)`, which caches one client per
service name for the lifetime of the execution context. Warm invocations therefore reuse
the same clients instead of building new ones on every call.

## OpenAI API key cache and refresh

The OpenAI API key is read through `_get_openai_api_key(secret_name, *, force_refresh=False)`,
which keeps an in-memory cache of `(api_key, time.monotonic() at fetch)` per secret name.

- A cached key is reused while it is younger than `OPENAI_API_KEY_CACHE_TTL_SECONDS = 300`.
  After that, the next call re-reads Secrets Manager. A rotated or revoked key therefore
  stops being used by warm containers within about 5 minutes, without a cold start.
- Failed Secrets Manager reads (and malformed secrets) raise before anything is stored, so
  errors are never cached.
- `generate_painted_images` refreshes the key on demand. On `openai.AuthenticationError`
  or `openai.PermissionDeniedError` only, it logs a warning with `stage=refresh_api_key`
  (the exception class name only, never the key), calls `_get_openai_api_key(...,
  force_refresh=True)`, rebuilds the `OpenAI` client, and resends the request exactly once.
  The retry wraps the same resized PNG bytes in fresh buffers, because the SDK consumes them.
- If the retry also fails, its exception propagates and the job is set to `FAILED`.
- No other error triggers a key re-read or an extra attempt here. Rate limits, 5xx responses
  and timeouts are handled by the SDK retries configured in the next section.

`_clear_openai_api_key_cache()` empties the cache. Tests use it to reset state between cases.

## Timeouts and retries

| Call | Setting | Value |
|---|---|---|
| OpenAI client (`generate_painted_images`) | `timeout` per request | `OPENAI_TIMEOUT_SECONDS = 120.0` |
| OpenAI client (`generate_painted_images`) | `max_retries` | `OPENAI_MAX_RETRIES = 2` |
| OpenAI API key cache (`_get_openai_api_key`) | cache TTL | `OPENAI_API_KEY_CACHE_TTL_SECONDS = 300` |
| Presigned PUT upload (`_upload_image_via_presigned_url`) | `urlopen` timeout | `UPLOAD_TIMEOUT_SECONDS = 60.0` |

These are module constants in `handler.py`, not environment variables. Timeouts from the
OpenAI client propagate as `openai.OpenAIError`, which is handled as a generation failure
(job set to `FAILED`). Upload timeouts are handled the same way.

The Lambda function timeout must exceed the worst case for one generation,
roughly `(OPENAI_MAX_RETRIES + 1) * OPENAI_TIMEOUT_SECONDS` plus retry backoff (about 6 minutes
with the defaults). If the Lambda times out before the handler reaches its `FAILED` write, the
job remains `IN_PROGRESS`. The timeout is set in infrastructure, which is not in this repository.
A later redelivery then takes the stored-image or repaint path described above.

## Errors and logs

All logging is JSON with `timestamp`, `level`, `jobId`, `stage` and `message`. Failure logs
also include an `exception` field with the formatted traceback. The per-call `stage` on each
log line identifies the failing step. Examples are `paint_error`, `update_status_painted_error`,
`notify_error` and `notify_result_error`. A rejected OpenAI key logs a `refresh_api_key` warning
before the single retry. The value `unknown` is used for `jobId` or `stage` when a record has
no such context (for example when the SQS message cannot be parsed).

Stages: `parse_input`, `get_job_status`, `validate_status`, `redelivery`, `skip_paint`,
`paint_error`, `update_status_painted_error`, `update_status_failed_error`, `notify_error`,
`notify_result_error`, `complete`.

## Public functions

| Function | Raises |
|---|---|
| `parse_job_id(event) -> str \| None` | Never for a malformed `Records`, record, or body; returns `None` instead |
| `get_job(job_id) -> JobRecord` | `JobNotFoundError` if the item is missing; `ValueError` if `jobStatus` is not a `JobStatus`; `KeyError` if `JOBS_TABLE_NAME` is unset or `jobStatus` is absent |
| `download_reference_image(job_id) -> bytes` | `ValueError` if no reference image exists; `KeyError` if `UPLOAD_BUCKET_NAME` is unset |
| `fetch_prompt_from_s3() -> str` | `KeyError` if `STATIC_BUCKET_NAME` is unset |
| `build_prompt(prompt, instruction) -> str` | Never |
| `generate_painted_images(images, prompt) -> list[bytes]` | `openai.OpenAIError` (including timeouts after SDK retries, and an auth error that persists after the one key refresh); `ClientError` from Secrets Manager (including a failed forced re-read); `KeyError` if the secret lacks `OPENAI_API_KEY` |
| `_get_openai_api_key(secret_name, *, force_refresh=False) -> str` | `ClientError` from Secrets Manager; `KeyError` if the secret lacks `OPENAI_API_KEY`; `json.JSONDecodeError` if the secret is not JSON |
| `_clear_openai_api_key_cache() -> None` | Never (test helper) |
| `_painted_image_exists(job_id) -> bool` | `ClientError` on any S3 error other than "not found"; `KeyError` if `PAINT_BUCKET_NAME` is unset |
| `upload_painted_images(job_id, images) -> None` | `KeyError` if `PAINT_BUCKET_NAME` is unset; `urllib.error.URLError`, `urllib.error.HTTPError` or `TimeoutError` on upload failure |
| `update_job_status(job_id, status, expected_status=JobStatus.IN_PROGRESS) -> None` | `ClientError` (including a conditional-check failure if the job is not in `expected_status`); `KeyError` if `JOBS_TABLE_NAME` is unset |
| `notify_guide_creation(job_id) -> None` | `ClientError`, `BotoCoreError`; `KeyError` if `GUIDE_CREATION_QUEUE_URL` is unset |
| `notify_result_failure(job_id, reason) -> None` | `ClientError`, `BotoCoreError`; `KeyError` if `RESULT_QUEUE_URL` is unset |
| `lambda_handler(event, context) -> None` | `ValueError` (bad jobId, unknown job, status other than `IN_PROGRESS` or `PAINTED`); `ClientError` and `BotoCoreError` (retried by SQS); `KeyError` (configuration, not reported to the client) |

## SQS message format

**Input** (paint SQS queue, the trigger for this Lambda):
```json
{"jobId": "<uuid>"}
```

**Failure output** (`RESULT_QUEUE_URL`), sent on a painting failure or a permanent guide-creation failure:
```json
{"jobId": "<uuid>", "status": "FAILED", "message": "Painting failed: <ExceptionClassName>"}
```

**Output** (`GUIDE_CREATION_QUEUE_URL`), sent on every successful run, including a redelivery:
```json
{"jobId": "<uuid>"}
```

The guide-creation consumer must treat a repeated message for the same `jobId` as idempotent.

## Required IAM permissions

This change adds one permission the paint role did not need before:

- `s3:GetObject` and `s3:ListBucket` on `painted_images/*` in `PAINT_BUCKET_NAME`, for the
  `HeadObject` in `_painted_image_exists`. `ListBucket` is required: without it S3 answers a
  missing key with `403` instead of `404`, and the handler would fail before painting.

Other permissions are unchanged and are not listed here.

## Development

```bash
# from backend/paint/
pip install -r requirements.txt
pip install -r requirements-dev.txt
```

`requirements-dev.txt` includes `boto3-stubs[s3]`, which provides the `mypy_boto3_s3`
types used under `TYPE_CHECKING` in `handler.py`. The stubs are not needed at runtime.

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
pytest tests/test_handler.py::TestLambdaHandler::test_happy_path_paints_job
```

### Run tests with verbose output
```bash
pytest -v
```

Tests reset the module-level caches before and after each test (`_get_client.cache_clear()`
and `_clear_openai_api_key_cache()`), so cached clients or keys do not leak between tests.
The test classes are plain pytest classes, not `unittest.TestCase` subclasses, so
`python -m unittest discover -s tests` collects zero tests. Use `pytest`.

`TestLambdaRedelivery` runs the real S3, DynamoDB and SQS helpers under moto for the retry
paths. These include the PAINTED-write retry that must not call OpenAI twice.

## Deployment

- **Region**: `eu-central-1` (Frankfurt)
- **Lambda function name**: `paint`
- **Runtime**: Python 3.12
- **Trigger**: SQS paint queue. The queue is wired in infrastructure; this code does not read a queue URL.
- **Timeout**: must exceed the worst-case OpenAI call budget described in "Timeouts and retries".
