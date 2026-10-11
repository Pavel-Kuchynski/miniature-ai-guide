# Paint Lambda

AWS Lambda function that handles painting of previously uploaded reference images.
Triggered by an SQS message containing a `jobId`. Downloads the single reference image
from S3, reads a painting prompt, calls OpenAI to generate a painted image, uploads
them to S3 via presigned PUT URLs, updates job status in DynamoDB, and notifies
the guide creation queue on success.

## Environment variables

| Variable | Required | Description |
|---|---|---|
| `UPLOAD_BUCKET_NAME` | yes | S3 bucket containing reference images (`uploads/<jobId>/`) |
| `STATIC_BUCKET_NAME` | yes | S3 bucket containing the painting prompt file (`prompts/paint_images_prompt.txt`) |
| `PAINT_BUCKET_NAME` | yes | S3 bucket where painted images are stored (`painted_images/<jobId>/`) |
| `JOBS_TABLE_NAME` | yes | DynamoDB table for job status tracking |
| `GUIDE_CREATION_QUEUE_URL` | yes | SQS queue URL to notify on successful painting |
| `OPENAI_API_KEY_SECRET_NAME` | no | Secrets Manager secret name for the OpenAI API key. Defaults to `miniature-guide/openai/api-key` (`DEFAULT_OPENAI_API_KEY_SECRET_NAME`). The secret's JSON must have an `OPENAI_API_KEY` field. |
| `OPENAI_MODEL` | no | OpenAI image model name. Defaults to `gpt-image-1` (`DEFAULT_OPENAI_MODEL`). |

The module reads all of these variables at call time, not at import time. A missing
required variable raises `KeyError` at the point it is first needed. The two optional
variables fall back to their defaults when unset.

## `lambda_handler(event, context) -> None`

Entry point triggered by SQS. Orchestrates the full paint flow:

1. **Parse `jobId`** from `Records[0].body` via `parse_job_id(event)`.
   - Raises `ValueError` if the jobId is missing or invalid (routes message to DLQ).
   - Non-object JSON bodies and non-object records are treated as invalid, not as errors
     of a different kind.
2. **Read the job** via `get_job(job_id)`, a single strongly consistent DynamoDB read of
   the item in `JOBS_TABLE_NAME`. It returns a `JobRecord` with the job's `status` and its
   optional `instruction`.
   - If no item exists, `get_job` raises `JobNotFoundError`; `lambda_handler` converts
     it to `ValueError` (routes message to DLQ).
   - If the status is not `JobStatus.IN_PROGRESS`, raises `ValueError` (routes message to DLQ).
   - A missing `JOBS_TABLE_NAME` raises `KeyError` and propagates; it is not reported as
     "job not found".
   - A DynamoDB error during this read propagates to the caller before any status is written.
3. **Paint the reference image** in `_paint_reference_image(job_id, instruction)`:
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
     - The API key comes from the secret named by `OPENAI_API_KEY_SECRET_NAME`. It is cached
       in memory for the lifetime of the execution context, keyed by secret name.
     - The reference image is first downscaled to at most 1024 px on its longer side (aspect
       ratio kept, never upscaled) and re-encoded as PNG by `_resize_reference_image` to reduce cost.
     - Requests `n=REQUESTED_OUTPUT_COUNT` (1), `output_format="png"` and `quality="medium"`.
     - Raises `ValueError` unless OpenAI returns exactly `REQUESTED_OUTPUT_COUNT` (1) images.
       Fewer images and more images are both treated as a failure.
4. **Upload painted images** to `PAINT_BUCKET_NAME/painted_images/<jobId>/image_<n>.png` via
   presigned PUT URLs in `upload_painted_images(job_id, images)`.
5. **Update job status to `PAINTED`** via `_mark_painted(job_id)` (`update_job_status(job_id, JobStatus.PAINTED)`).
6. **Notify guide creation queue** via `notify_guide_creation(job_id)`.

### Failure handling

- Any exception from steps 3–4 is logged with its traceback, the job is set to `FAILED`
  via `_mark_failed(job_id)`, and the function returns without raising (the SQS message is
  consumed). If the `FAILED` write itself fails, that error is logged and the job is left as is.
- If the `PAINTED` write fails (step 5), the error is logged with its traceback, the
  notification is skipped, and the function returns without raising. The job is not set to
  `FAILED` in this case.
- If the notification fails (step 6), the error is logged with its traceback and the function
  returns without raising. The job remains `PAINTED`.

## Job statuses

Job status values are the `JobStatus` string enum: `IN_PROGRESS`, `PAINTED` and `FAILED`.
`get_job` raises `ValueError` if the stored `jobStatus` is not one of these values.

## AWS clients

boto3 clients are created through `_get_client(service_name)`, which caches one client per
service name for the lifetime of the execution context. Warm invocations therefore reuse
the same clients instead of building new ones on every call.

## Timeouts and retries

| Call | Setting | Value |
|---|---|---|
| OpenAI client (`generate_painted_images`) | `timeout` per request | `OPENAI_TIMEOUT_SECONDS = 120.0` |
| OpenAI client (`generate_painted_images`) | `max_retries` | `OPENAI_MAX_RETRIES = 2` |
| Presigned PUT upload (`_upload_image_via_presigned_url`) | `urlopen` timeout | `UPLOAD_TIMEOUT_SECONDS = 60.0` |

These are module constants in `handler.py`, not environment variables. Timeouts from the
OpenAI client propagate as `openai.OpenAIError`, which is handled as a generation failure
(job set to `FAILED`). Upload timeouts are handled the same way.

The Lambda function timeout must exceed the worst case for one generation,
roughly `(OPENAI_MAX_RETRIES + 1) * OPENAI_TIMEOUT_SECONDS` plus retry backoff (about 6 minutes
with the defaults). If the Lambda times out before the handler reaches its `FAILED` write, the
job remains `IN_PROGRESS`. The timeout is set in infrastructure, which is not in this repository.

## Errors and logs

All logging is JSON with `timestamp`, `level`, `jobId`, `stage` and `message`. Failure logs
also include an `exception` field with the formatted traceback. The per-call `stage` on each
log line identifies the failing step (for example `paint_error`, `update_status_painted_error`,
`notify_error`). The value `unknown` is used for `jobId` or `stage` when a record has no such
context (for example when the SQS message cannot be parsed).

## Public functions

| Function | Raises |
|---|---|
| `parse_job_id(event) -> str \| None` | Never for a malformed `Records`, record, or body; returns `None` instead |
| `get_job(job_id) -> JobRecord` | `JobNotFoundError` if the item is missing; `ValueError` if `jobStatus` is not a `JobStatus`; `KeyError` if `JOBS_TABLE_NAME` is unset or `jobStatus` is absent |
| `download_reference_image(job_id) -> bytes` | `ValueError` if no reference image exists; `KeyError` if `UPLOAD_BUCKET_NAME` is unset |
| `fetch_prompt_from_s3() -> str` | `KeyError` if `STATIC_BUCKET_NAME` is unset |
| `build_prompt(prompt, instruction) -> str` | Never |
| `generate_painted_images(images, prompt) -> list[bytes]` | `openai.OpenAIError` (including timeouts after retries); `ClientError` from Secrets Manager; `KeyError` if the secret lacks `OPENAI_API_KEY` |
| `upload_painted_images(job_id, images) -> None` | `KeyError` if `PAINT_BUCKET_NAME` is unset; `urllib.error.URLError`, `urllib.error.HTTPError` or `TimeoutError` on upload failure |
| `update_job_status(job_id, status: JobStatus) -> None` | `ClientError` (including a conditional-check failure if the job is not `IN_PROGRESS`); `KeyError` if `JOBS_TABLE_NAME` is unset |
| `notify_guide_creation(job_id) -> None` | `KeyError` if `GUIDE_CREATION_QUEUE_URL` is unset |

## SQS message format

**Input** (paint SQS queue, the trigger for this Lambda):
```json
{"jobId": "<uuid>"}
```

**Output** (`GUIDE_CREATION_QUEUE_URL`):
```json
{"jobId": "<uuid>"}
```

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

Tests reset the module-level caches (`_get_client`, `_fetch_openai_api_key`) before and
after each test, so cached clients or keys do not leak between tests.

## Deployment

- **Region**: `eu-central-1` (Frankfurt)
- **Lambda function name**: `paint`
- **Runtime**: Python 3.12
- **Trigger**: SQS paint queue. The queue is wired in infrastructure; this code does not read a queue URL.
- **Timeout**: must exceed the worst-case OpenAI call budget described in "Timeouts and retries".
