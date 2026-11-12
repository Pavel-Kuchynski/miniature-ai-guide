"""AWS Lambda handler for the bedrock Lambda.

Triggered by SQS after the paint Lambda has stored its painted image. Downloads that PNG
from the paint S3 bucket, reads the prompt from the static S3 bucket, sends both to Amazon
Bedrock, stores the validated model output as `result.json` next to the image, notifies the
result queue, and moves the job from PAINTED to COMPLETED in DynamoDB. On processing failure
the job is marked FAILED and the message is consumed rather than retried.
"""

import datetime
import functools
import json
import os
import re
from typing import TYPE_CHECKING, Any, Dict, List, Optional

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from job_status import JobStatus
from logging_config import UNKNOWN, StructuredLoggerAdapter, configure_logger

if TYPE_CHECKING:
    from mypy_boto3_s3 import S3Client

logger = configure_logger(__name__)

PROMPT_KEY = "prompts/miniature-photo-to-rgb-prompt.md"
DEFAULT_BEDROCK_MODEL_ID = "global.anthropic.claude-sonnet-4-6"
DEFAULT_BEDROCK_MAX_TOKENS = 4096
# Budget: one Bedrock call takes at most BEDROCK_MAX_ATTEMPTS x read timeout (2 x 300 = 600 s
# by default), plus S3, SQS, DynamoDB and retry backoff. The Lambda timeout, set outside this
# repo, must sit above that with a margin (660 s or more) and at most the 900 s Lambda maximum,
# so the read timeout must stay below about 420 s. Raising it raises this worst case.
# Vision replies are slow, so the read timeout is well above botocore's 60 s default.
DEFAULT_BEDROCK_READ_TIMEOUT_SECONDS = 300
# Total Converse attempts per call, initial request included. Retries are pinned because
# botocore's default mode differs from standard mode.
BEDROCK_MAX_ATTEMPTS = 2
HEX_COLOR_PATTERN = re.compile(r"^[0-9A-F]{6}$")


@functools.lru_cache(maxsize=None)
def _get_client(service_name: str) -> Any:
    """Return a boto3 client for a service, creating it at most once per execution context.

    Clients are cached by service name so warm invocations reuse them. Tests reset the cache
    with `cache_clear()`.

    Args:
        service_name: boto3 service name, e.g. "s3", "dynamodb" or "sqs".

    Returns:
        A boto3 client for the requested service.
    """
    return boto3.client(service_name)


@functools.lru_cache(maxsize=4)
def _get_bedrock_client(read_timeout_seconds: int) -> Any:
    """Return a Bedrock Runtime client with the given read timeout, created once per value.

    The read timeout comes from `BEDROCK_READ_TIMEOUT_SECONDS`, which is read at call time, so
    it cannot be baked into the shared `_get_client` cache. Keying the cache on the resolved
    value keeps warm invocations on one client and builds a new one only if the configured
    timeout changes. The key is one integer, so the cache holds at most a few entries. Tests
    reset it with `cache_clear()`.

    Args:
        read_timeout_seconds: Socket read timeout applied to each Converse attempt.

    Returns:
        A boto3 `bedrock-runtime` client configured with `read_timeout_seconds`.
        Retries are pinned to `BEDROCK_MAX_ATTEMPTS` total attempts so the worst case
        stays inside the Lambda timeout budget.
    """
    return boto3.client(
        "bedrock-runtime",
        config=Config(
            read_timeout=read_timeout_seconds,
            # `total_max_attempts` counts the initial call; botocore's `max_attempts` counts
            # retries only, so it would allow one extra attempt beyond the budget.
            retries={"mode": "standard", "total_max_attempts": BEDROCK_MAX_ATTEMPTS},
        ),
    )


def parse_job_id(event: Dict[str, Any]) -> Optional[str]:
    """Parse and validate `jobId` from the first SQS record.

    Args:
        event: Lambda event dict from an SQS trigger. Expected to contain a `Records`
            list whose first entry has a JSON `body` with a `jobId` field.

    Returns:
        The trimmed, non-empty job id, or `None` on any failure: missing or non-list
        `Records`, a first record that is not an object, invalid JSON, a JSON body that
        is not an object, or a missing, non-string or blank `jobId`. Never raises.
    """
    records = event.get("Records") or []
    if not isinstance(records, list) or not records or not isinstance(records[0], dict):
        return None

    try:
        body = json.loads(records[0].get("body", ""))
    except (json.JSONDecodeError, TypeError):
        return None

    if not isinstance(body, dict):
        return None

    job_id = body.get("jobId")
    if isinstance(job_id, str) and job_id.strip():
        return job_id.strip()

    return None


def download_painted_image_from_s3(job_id: str) -> bytes:
    """Download the PNG produced by the paint Lambda for a job.

    Lists `painted_images/<job_id>/` in `PAINT_BUCKET_NAME`, considering only non-empty
    `.png` keys. `result.json` also lives under this prefix after processing, so it is
    deliberately ignored. Exactly one PNG is expected.

    Args:
        job_id: The job id whose painted image should be downloaded.

    Returns:
        Raw bytes of the painted image.

    Raises:
        ValueError: If zero or more than one PNG exists under the prefix.
        botocore.exceptions.ClientError: Propagated on any S3 error.
        KeyError: If `PAINT_BUCKET_NAME` is not set.
    """
    bucket_name = os.environ["PAINT_BUCKET_NAME"]
    prefix = f"painted_images/{job_id}/"
    s3_client: S3Client = _get_client("s3")

    paginator = s3_client.get_paginator("list_objects_v2")
    keys: List[str] = []
    for page in paginator.paginate(Bucket=bucket_name, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if key == prefix or obj["Size"] == 0 or not key.lower().endswith(".png"):
                continue
            keys.append(key)

    if len(keys) != 1:
        raise ValueError(
            f"Expected exactly 1 PNG under s3://{bucket_name}/{prefix}, found {len(keys)}"
        )

    logger.info(
        "Downloading painted image from bucket=%r key=%r",
        bucket_name,
        keys[0],
        extra={"jobId": job_id, "stage": "download_image"},
    )
    response = s3_client.get_object(Bucket=bucket_name, Key=keys[0])
    return response["Body"].read()


def fetch_prompt_from_s3() -> str:
    """Read the Bedrock prompt from the static S3 bucket.

    Returns:
        Prompt text decoded as UTF-8 from `prompts/miniature-photo-to-rgb-prompt.md`.

    Raises:
        botocore.exceptions.ClientError: Propagated on any S3 error.
        KeyError: If `STATIC_BUCKET_NAME` is not set.
    """
    bucket_name = os.environ["STATIC_BUCKET_NAME"]
    s3_client: S3Client = _get_client("s3")

    logger.info(
        "Fetching prompt from bucket=%r key=%r",
        bucket_name,
        PROMPT_KEY,
        extra={"stage": "fetch_prompt"},
    )
    response = s3_client.get_object(Bucket=bucket_name, Key=PROMPT_KEY)
    return response["Body"].read().decode("utf-8")


def _get_positive_int_env(name: str, default: int) -> int:
    """Read an optional positive integer from the environment.

    Args:
        name: Environment variable name.
        default: Value returned when the variable is unset or blank.

    Returns:
        The parsed integer, or `default`.

    Raises:
        ValueError: If the value is set but is not a positive integer.
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{name} must be a positive integer, got {raw!r}") from None
    if value <= 0:
        raise ValueError(f"{name} must be a positive integer, got {raw!r}")
    return value


def _get_model_id() -> str:
    """Return `BEDROCK_MODEL_ID` or the default model when unset or blank.

    Returns:
        The configured Bedrock model or inference profile id.
    """
    return os.environ.get("BEDROCK_MODEL_ID", "").strip() or DEFAULT_BEDROCK_MODEL_ID


def invoke_bedrock(image: bytes, prompt: str) -> str:
    """Send the prompt and painted image to Amazon Bedrock via the Converse API.

    Args:
        image: PNG bytes of the painted miniature.
        prompt: Prompt text sent alongside the image.

    Returns:
        The concatenated text blocks of the model reply.

    Raises:
        ValueError: If the reply contains no text, or `BEDROCK_MAX_TOKENS` or
            `BEDROCK_READ_TIMEOUT_SECONDS` is set to a non-positive or non-integer value.
        botocore.exceptions.ClientError: Propagated on any Bedrock error.
    """
    model_id = _get_model_id()
    max_tokens = _get_positive_int_env("BEDROCK_MAX_TOKENS", DEFAULT_BEDROCK_MAX_TOKENS)
    read_timeout = _get_positive_int_env(
        "BEDROCK_READ_TIMEOUT_SECONDS", DEFAULT_BEDROCK_READ_TIMEOUT_SECONDS
    )
    bedrock_client = _get_bedrock_client(read_timeout)

    logger.info("Calling Bedrock model=%r", model_id, extra={"stage": "invoke_bedrock"})
    response = bedrock_client.converse(
        modelId=model_id,
        messages=[
            {
                "role": "user",
                "content": [
                    {"text": prompt},
                    {"image": {"format": "png", "source": {"bytes": image}}},
                ],
            }
        ],
        inferenceConfig={"maxTokens": max_tokens},
    )

    blocks = response["output"]["message"]["content"]
    text = "".join(block["text"] for block in blocks if "text" in block).strip()
    if not text:
        raise ValueError("Bedrock returned an empty response")
    return text


def parse_model_output(text: str) -> Dict[str, Any]:
    """Parse and validate the JSON document the model was prompted to return.

    Expected shape: `{"colors": [{"detail": "<name>", "paint": "<RRGGBB>"}, ...]}` with
    uppercase 6-digit hex and no `#`. A surrounding markdown code fence is tolerated
    because models sometimes add one despite the prompt.

    Args:
        text: Raw model reply text.

    Returns:
        The validated document.

    Raises:
        ValueError: If the text is not valid JSON or does not match the expected shape.
    """
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", cleaned)

    try:
        document = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Model output is not valid JSON: {exc}") from exc

    colors = document.get("colors") if isinstance(document, dict) else None
    if not isinstance(colors, list) or not colors:
        raise ValueError("Model output must contain a non-empty 'colors' list")

    for entry in colors:
        if not isinstance(entry, dict):
            raise ValueError("Each 'colors' entry must be an object")
        detail = entry.get("detail")
        paint = entry.get("paint")
        if not isinstance(detail, str) or not detail.strip():
            raise ValueError("Each color entry needs a non-empty 'detail'")
        if not isinstance(paint, str) or not HEX_COLOR_PATTERN.match(paint):
            raise ValueError(f"Invalid 'paint' value for detail {detail!r}")

    return document


def upload_result_to_s3(job_id: str, result: Dict[str, Any]) -> str:
    """Store the validated model output as `painted_images/<job_id>/result.json`.

    Args:
        job_id: The job id used in the S3 key.
        result: Parsed model document, written as-is.

    Returns:
        The S3 key written.

    Raises:
        botocore.exceptions.ClientError: Propagated on any S3 error.
        KeyError: If `PAINT_BUCKET_NAME` is not set.
    """
    bucket_name = os.environ["PAINT_BUCKET_NAME"]
    key = f"painted_images/{job_id}/result.json"
    s3_client: S3Client = _get_client("s3")

    logger.info(
        "Uploading result to key=%r", key, extra={"jobId": job_id, "stage": "upload_result"}
    )
    s3_client.put_object(
        Bucket=bucket_name,
        Key=key,
        Body=json.dumps(result).encode("utf-8"),
        ContentType="application/json",
    )
    return key


def get_job_status(job_id: str) -> Optional[str]:
    """Retrieve the job status from DynamoDB.

    Args:
        job_id: The job id to look up.

    Returns:
        The raw `jobStatus` string, or `None` if the job does not exist. Compare against
        `JobStatus` members, which are equal to their string values.

    Raises:
        botocore.exceptions.ClientError: Propagated on any DynamoDB error.
        KeyError: If `JOBS_TABLE_NAME` is not set.
    """
    table_name = os.environ["JOBS_TABLE_NAME"]
    dynamodb_client = _get_client("dynamodb")

    response = dynamodb_client.get_item(
        TableName=table_name,
        Key={"jobId": {"S": job_id}},
        ConsistentRead=True,
    )
    item = response.get("Item")
    if item is None:
        return None
    return item["jobStatus"]["S"]


def update_job_status(job_id: str, status: JobStatus) -> None:
    """Move a job from PAINTED to the given status in DynamoDB.

    The write is conditional on the current status being `JobStatus.PAINTED`, so a concurrent
    or repeated invocation cannot overwrite a job that already moved on.

    Args:
        job_id: The job id to update.
        status: New status value (`JobStatus.COMPLETED` or `JobStatus.FAILED`).

    Raises:
        botocore.exceptions.ClientError: Propagated on any DynamoDB error, including
            ConditionalCheckFailedException if the job is not currently PAINTED.
        KeyError: If `JOBS_TABLE_NAME` is not set.
    """
    table_name = os.environ["JOBS_TABLE_NAME"]
    dynamodb_client = _get_client("dynamodb")

    dynamodb_client.update_item(
        TableName=table_name,
        Key={"jobId": {"S": job_id}},
        UpdateExpression="SET jobStatus = :status, updatedAt = :now",
        ConditionExpression="jobStatus = :expected",
        ExpressionAttributeValues={
            ":status": {"S": str(status)},
            ":now": {"S": datetime.datetime.now(datetime.timezone.utc).isoformat()},
            ":expected": {"S": JobStatus.PAINTED.value},
        },
    )


def notify_result(job_id: str) -> None:
    """Send a COMPLETED notification to the result SQS queue.

    Args:
        job_id: The job id to include in the message.

    Raises:
        botocore.exceptions.ClientError: Propagated on any SQS error.
        KeyError: If `RESULT_QUEUE_URL` is not set.
    """
    queue_url = os.environ["RESULT_QUEUE_URL"]
    sqs_client = _get_client("sqs")

    sqs_client.send_message(
        QueueUrl=queue_url,
        MessageBody=json.dumps({"jobId": job_id, "status": JobStatus.COMPLETED.value}),
    )


def _mark_failed(job_id: str) -> None:
    """Set the job status to FAILED, logging instead of raising if the update fails.

    Never raises for `ClientError` or a missing `JOBS_TABLE_NAME`; both are logged at
    `update_status_failed_error`.

    Args:
        job_id: The job to mark as failed.
    """
    try:
        update_job_status(job_id, JobStatus.FAILED)
    except (ClientError, KeyError) as exc:
        logger.exception(
            "Failed to update job status to FAILED: %s",
            exc,
            extra={"jobId": job_id, "stage": "update_status_failed_error"},
        )


def _mark_completed(job_id: str) -> bool:
    """Set the job status to COMPLETED, logging instead of raising if the update fails.

    Never raises for `ClientError` or a missing `JOBS_TABLE_NAME`; both are logged at
    `update_status_completed_error`.

    Args:
        job_id: The job to mark as completed.

    Returns:
        True if the status was written, False if the DynamoDB update failed (logged).
    """
    try:
        update_job_status(job_id, JobStatus.COMPLETED)
    except (ClientError, KeyError) as exc:
        logger.exception(
            "Failed to update job status to COMPLETED: %s",
            exc,
            extra={"jobId": job_id, "stage": "update_status_completed_error"},
        )
        return False
    return True


def _process_painted_image(job_id: str) -> None:
    """Run the Bedrock step for one job and store its validated result.

    Downloads the painted image and prompt, calls Bedrock, validates the reply, and uploads
    it as `result.json`. The raw reply is logged before validation so malformed output can be
    diagnosed.

    Args:
        job_id: The job to process.

    Raises:
        ValueError: If the painted image is missing or ambiguous, Bedrock returns no text,
            an optional Bedrock setting is invalid, or the reply is not the expected document.
        botocore.exceptions.ClientError: Propagated from S3 or Bedrock.
        KeyError: If `PAINT_BUCKET_NAME` or `STATIC_BUCKET_NAME` is not set.
    """
    image = download_painted_image_from_s3(job_id)
    prompt = fetch_prompt_from_s3()
    model_reply = invoke_bedrock(image, prompt)
    logger.info(
        "Bedrock response: %s",
        model_reply,
        extra={"jobId": job_id, "stage": "bedrock_response"},
    )
    result = parse_model_output(model_reply)
    upload_result_to_s3(job_id, result)


def lambda_handler(event: Dict[str, Any], context: Any) -> None:
    """Orchestrate the Bedrock step: image + prompt -> Bedrock -> result.json -> status.

    Triggered by SQS. Raises ValueError (so SQS routes the message to the DLQ) if the jobId
    is unparsable, the job is unknown, or the job is not in PAINTED status. Any failure while
    processing the image (see `_process_painted_image`) marks the job FAILED and returns
    without raising. Failures after the result is stored (SQS notification, COMPLETED
    status update) are logged and do not raise.

    Args:
        event: SQS Lambda event with `Records[0].body` containing `{"jobId": "<uuid>"}`.
        context: Lambda context object (unused).

    Raises:
        ValueError: If the jobId is missing or invalid, the job item does not exist, or the
            job is not in PAINTED status.
        botocore.exceptions.ClientError: Propagated from the initial DynamoDB status read.
        KeyError: If `JOBS_TABLE_NAME` is not set.
    """
    del context

    job_id = parse_job_id(event)
    if job_id is None:
        logger.error(
            "Failed to parse jobId from SQS event",
            extra={"jobId": UNKNOWN, "stage": "parse_input"},
        )
        raise ValueError("Missing or invalid jobId in SQS message")

    log = StructuredLoggerAdapter(logger, {"jobId": job_id, "stage": "orchestrate"})
    log.info("Starting bedrock job")

    job_status = get_job_status(job_id)
    if job_status is None:
        log.error("Job not found", extra={"stage": "validate_job"})
        raise ValueError(f"Job {job_id} not found")
    if job_status != JobStatus.PAINTED:
        log.error(
            "Job is not in PAINTED status; current status=%r",
            job_status,
            extra={"stage": "validate_job"},
        )
        raise ValueError(f"Job {job_id} is not in PAINTED status (current: {job_status})")

    try:
        _process_painted_image(job_id)
    except Exception as exc:
        log.exception(
            "Failed to process painted image: %s", exc, extra={"stage": "bedrock_error"}
        )
        _mark_failed(job_id)
        return

    try:
        notify_result(job_id)
    except (ClientError, KeyError) as exc:
        log.exception(
            "Failed to send result notification: %s", exc, extra={"stage": "notify_error"}
        )
        return

    if not _mark_completed(job_id):
        return

    log.info("Successfully completed bedrock job", extra={"stage": "complete"})
