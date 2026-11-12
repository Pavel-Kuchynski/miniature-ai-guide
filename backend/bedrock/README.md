# Bedrock Lambda

AWS Lambda that runs after the paint Lambda. Triggered by an SQS message containing a
`jobId`, it sends the painted image and a prompt to Amazon Bedrock, validates the model's JSON
reply, stores it in S3 as `result.json`, notifies the result queue, and marks the job
`COMPLETED`.

## Module layout

| File | Contents |
|---|---|
| `handler.py` | `lambda_handler` entry point, the S3, Bedrock, SQS and DynamoDB helpers, the cached boto3 client factories, and the `_mark_*` / `_process_painted_image` helpers |
| `job_status.py` | `JobStatus` `StrEnum`: `PAINTED`, `COMPLETED`, `FAILED` |
| `json_formatter.py` | `JSONFormatter` and `UNKNOWN = "unknown"` |
| `structured_logger_adapter.py` | `StructuredLoggerAdapter` (adapter context; a per-call `extra` overrides it) |
| `logging_config.py` | `configure_logger(name)`; re-exports `UNKNOWN`, `JSONFormatter` and `StructuredLoggerAdapter` via `__all__` |
| `requirements.txt` | Runtime dependency (`boto3`, pinned) |
| `requirements-dev.txt` | Test tools (`pytest`, `moto`) and S3 type stubs |
| `tests/` | `conftest.py` (sets `sys.path` and a default region), `test_handler.py`, `test_logging_config.py`, `test_job_status.py` |

Modules import each other by flat name (`from job_status import JobStatus`) because the
Lambda root directory is on `sys.path`.

## Environment variables

| Variable | Description |
|---|---|
| `PAINT_BUCKET_NAME` | S3 bucket with painted images (`painted_images/<jobId>/`) and where `result.json` is written |
| `STATIC_BUCKET_NAME` | S3 bucket containing `prompts/miniature-photo-to-rgb-prompt.md` |
| `JOBS_TABLE_NAME` | DynamoDB table for job status tracking |
| `BEDROCK_MODEL_ID` | Optional. Bedrock model or inference profile ID; must support image input via the Converse API (default `global.anthropic.claude-sonnet-4-6`) |
| `BEDROCK_MAX_TOKENS` | Optional. Max output tokens (default `4096`) |
| `BEDROCK_READ_TIMEOUT_SECONDS` | Optional. Bedrock client read timeout in seconds (default `300`) |
| `RESULT_QUEUE_URL` | SQS queue URL notified on completion |

Required variables are read with `os.environ[...]`; an unset one raises `KeyError` at the
point of use. Optional variables are read at call time. A blank value counts as unset. A
non-integer or non-positive numeric value raises `ValueError`.

The paint Lambda sends its trigger message to its `GUIDE_CREATION_QUEUE_URL`, which is the
SQS queue this Lambda is subscribed to.

## `lambda_handler(event, context) -> None`

Orchestration only. Each numbered step is a helper in `handler.py`.

1. `parse_job_id(event)` reads `jobId` from `Records[0].body`. `ValueError` if invalid (DLQ).
2. `get_job_status(job_id)` reads the job. `ValueError` if the job is missing or not
   `PAINTED` (DLQ).
3. `_process_painted_image(job_id)` runs steps 3 to 7:
   1. `download_painted_image_from_s3(job_id)`: exactly one non-empty `.png` under
      `painted_images/<jobId>/` (`result.json` is ignored).
   2. `fetch_prompt_from_s3()`: prompt from `STATIC_BUCKET_NAME`.
   3. `invoke_bedrock(image, prompt)`: Bedrock Converse call with the prompt text and the PNG.
      The raw reply is logged (stage `bedrock_response`) before validation, for diagnostics.
   4. `parse_model_output(text)`: parses the reply as JSON (a markdown code fence is
      tolerated) and validates `{"colors": [{"detail": "<name>", "paint": "<RRGGBB>"}, ...]}`
      (non-empty list, uppercase 6-digit hex, no `#`).
   5. `upload_result_to_s3(job_id, result)`: writes the validated JSON as-is to
      `painted_images/<jobId>/result.json`.
4. `notify_result(job_id)` sends the COMPLETED message to `RESULT_QUEUE_URL`.
5. `_mark_completed(job_id)` moves the job `PAINTED` to `COMPLETED` (conditional write).

A failure in step 3 marks the job `FAILED` and returns without raising. A failure in
step 4 or 5 is logged and returns without raising. The result notification is sent
**before** the `COMPLETED` write; do not reorder these.

### Public functions

| Function | Raises |
|---|---|
| `parse_job_id(event) -> str \| None` | Never raises; returns `None` for any malformed event |
| `download_painted_image_from_s3(job_id) -> bytes` | `ValueError` (zero or several PNGs), `ClientError`, `KeyError` (`PAINT_BUCKET_NAME`) |
| `fetch_prompt_from_s3() -> str` | `ClientError`, `KeyError` (`STATIC_BUCKET_NAME`) |
| `invoke_bedrock(image, prompt) -> str` | `ValueError` (empty reply, invalid numeric setting), `ClientError` |
| `parse_model_output(text) -> dict` | `ValueError` (not JSON, or wrong shape) |
| `upload_result_to_s3(job_id, result) -> str` | `ClientError`, `KeyError` (`PAINT_BUCKET_NAME`) |
| `get_job_status(job_id) -> str \| None` | `ClientError`, `KeyError` (`JOBS_TABLE_NAME`) |
| `update_job_status(job_id, status: JobStatus) -> None` | `ClientError` (including `ConditionalCheckFailedException` when the job is not `PAINTED`), `KeyError` (`JOBS_TABLE_NAME`) |
| `notify_result(job_id) -> None` | `ClientError`, `KeyError` (`RESULT_QUEUE_URL`) |
| `lambda_handler(event, context) -> None` | `ValueError` (bad jobId, unknown job, wrong status), `ClientError` and `KeyError` from the initial status read |

## Failure handling

| Situation | Behavior |
|---|---|
| `jobId` missing or invalid | Logged at `parse_input` with `jobId=unknown`; `ValueError` (DLQ) |
| Job not found, or not `PAINTED` | Logged at `validate_job`; `ValueError` (DLQ) |
| Any error inside `_process_painted_image` (S3, prompt, Bedrock, validation, upload) | Logged at `bedrock_error` with traceback; `_mark_failed` writes `FAILED` (conditional on `PAINTED`); return. No message is sent to the result queue. |
| `FAILED` write fails | Logged at `update_status_failed_error` with traceback; return |
| Result notification fails | Logged at `notify_error` with traceback; return. The job stays `PAINTED`; the `COMPLETED` write is skipped. |
| `COMPLETED` write fails | Logged at `update_status_completed_error` with traceback; return without the success log |
| `KeyError` for a missing env var during the initial status read | Propagates (configuration error, DLQ) |

Swallowed failures log the exception text in `message` and the traceback in `exception`. The
Lambda returns no response body, so nothing is returned to a client.

## AWS clients

Clients are created by two cached factories in `handler.py`, so warm invocations reuse them.

- `_get_client(service_name)` is a `functools.lru_cache` keyed by service name (`s3`,
  `dynamodb`, `sqs`). Helpers call it instead of `boto3.client(...)`.
- `_get_bedrock_client(read_timeout_seconds)` is a separate `lru_cache`, because the
  Bedrock client needs `Config(read_timeout=..., retries=...)`. The timeout comes from
  `BEDROCK_READ_TIMEOUT_SECONDS`, which is read at call time, so it cannot be fixed in the
  shared cache. Keying the cache on the resolved value keeps a warm container on one client
  and builds a new one only if the configured timeout changes. The key is one integer, so
  the cache holds at most a few entries (`maxsize=4`).

Tests call `cache_clear()` on both factories in an autouse fixture.

## Logging

Every log line is one JSON object: `timestamp` (UTC ISO-8601), `level`, `jobId`, `stage`,
`message`, plus `exception` (formatted traceback) when the record has exception info.
Missing `jobId` or `stage` is rendered as `unknown`.

`StructuredLoggerAdapter` carries `jobId` and `stage` for the whole invocation. A per-call
`extra={"stage": ...}` overrides the adapter's value for that record only.

| Stage | Meaning |
|---|---|
| `parse_input` | Unusable SQS message |
| `orchestrate` | Invocation context (the adapter default) |
| `validate_job` | Job not found or not `PAINTED` |
| `download_image`, `fetch_prompt`, `invoke_bedrock`, `upload_result` | Step start |
| `bedrock_response` | Raw model reply, logged before validation |
| `bedrock_error` | Processing failed; job marked `FAILED` |
| `update_status_failed_error`, `notify_error`, `update_status_completed_error` | Swallowed failure in a bookkeeping step (status write or notification) |
| `complete` | Job completed successfully |

## Message formats

Input (SQS body of the paint queue message):

```json
{"jobId": "<uuid>"}
```

Output (result queue message, sent on success only):

```json
{"jobId": "<uuid>", "status": "COMPLETED"}
```

Stored object `painted_images/<jobId>/result.json` (the validated model document):

```json
{"colors": [{"detail": "armor", "paint": "A9A9A9"}]}
```

## Timeouts and retries

- Each Bedrock call makes at most `BEDROCK_MAX_ATTEMPTS` = 2 total attempts (standard
  retry mode, one retry). Each attempt has a read timeout of
  `BEDROCK_READ_TIMEOUT_SECONDS` (default 300 s).
- Worst case for one Bedrock call: 2 x 300 = 600 s, plus S3, SQS and DynamoDB calls and
  retry backoff. Raising the read timeout raises this worst case.
- The Lambda timeout is configured in infrastructure outside this repo. It must be above
  the worst case with a margin (660 s or more) and at most the 900 s Lambda maximum.
- The constants and their budget comment are in `handler.py`.
- The SQS visibility timeout must exceed the Lambda timeout.

## Development

Run from `backend/bedrock/`:

```bash
pip install -r requirements.txt -r requirements-dev.txt
pytest
```

Single file: `pytest tests/test_handler.py`. Single test:
`pytest tests/test_handler.py::TestLambdaHandler::test_happy_path`.

The tests use `moto` for S3 and DynamoDB and `MagicMock` for Bedrock, so they need no AWS
credentials or network access. Use `pytest -p no:cacheprovider` to skip writing `.pytest_cache`.

## Deployment

- **Runtime**: Python 3.12
- **Trigger**: SQS (paint queue)
- **Timeout**: configured outside this repo. See [Timeouts and retries](#timeouts-and-retries).

### Example IAM execution role

Replace the `<...>` placeholders. The trust policy lets Lambda assume the role; the
permissions policy grants only what the handler calls.

Trust policy:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {"Service": "lambda.amazonaws.com"},
      "Action": "sts:AssumeRole"
    }
  ]
}
```

Permissions policy:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "Logs",
      "Effect": "Allow",
      "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
      "Resource": "arn:aws:logs:<region>:<account-id>:log-group:/aws/lambda/<function-name>:*"
    },
    {
      "Sid": "ListPaintedImages",
      "Effect": "Allow",
      "Action": "s3:ListBucket",
      "Resource": "arn:aws:s3:::<paint-bucket>",
      "Condition": {"StringLike": {"s3:prefix": "painted_images/*"}}
    },
    {
      "Sid": "ReadPaintedImagesWriteResult",
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:PutObject"],
      "Resource": "arn:aws:s3:::<paint-bucket>/painted_images/*"
    },
    {
      "Sid": "ReadPrompt",
      "Effect": "Allow",
      "Action": "s3:GetObject",
      "Resource": "arn:aws:s3:::<static-bucket>/prompts/miniature-photo-to-rgb-prompt.md"
    },
    {
      "Sid": "JobsTable",
      "Effect": "Allow",
      "Action": ["dynamodb:GetItem", "dynamodb:UpdateItem"],
      "Resource": "arn:aws:dynamodb:<region>:<account-id>:table/<jobs-table>"
    },
    {
      "Sid": "ConsumePaintQueue",
      "Effect": "Allow",
      "Action": ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"],
      "Resource": "arn:aws:sqs:<region>:<account-id>:<paint-queue>"
    },
    {
      "Sid": "SendResult",
      "Effect": "Allow",
      "Action": "sqs:SendMessage",
      "Resource": "arn:aws:sqs:<region>:<account-id>:<result-queue>"
    },
    {
      "Sid": "InvokeBedrock",
      "Effect": "Allow",
      "Action": "bedrock:InvokeModel",
      "Resource": [
        "arn:aws:bedrock:<region>:<account-id>:inference-profile/global.anthropic.claude-sonnet-4-6",
        "arn:aws:bedrock:::foundation-model/anthropic.claude-sonnet-4-6",
        "arn:aws:bedrock:*::foundation-model/anthropic.claude-sonnet-4-6"
      ]
    }
  ]
}
```

Notes:

- The Converse API is authorized by `bedrock:InvokeModel`. A `global.` inference profile
  also needs access to the underlying foundation model in every region it routes to,
  hence the wildcard-region foundation-model ARN. Update both ARNs if you override
  `BEDROCK_MODEL_ID`.
- Add `kms:Decrypt` / `kms:GenerateDataKey` on the key if the buckets, table or queues use a
  customer-managed KMS key.
