"""AWS Lambda handler for the bedrock Lambda.

Triggered by SQS after the paint Lambda has stored its painted image. Downloads that
PNG from the paint S3 bucket, reads the prompt from the static S3 bucket, sends both to
Amazon Bedrock, stores the model output as `result.json` next to the image, notifies the
result queue, and moves the job from PAINTED to COMPLETED in DynamoDB. On processing
failure the job is marked FAILED.
"""
import datetime
import json
import os
import re
from typing import Any, Dict, List, Optional

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from logging_config import configure_logger, StructuredLoggerAdapter

logger = configure_logger(__name__)

PROMPT_KEY = "prompts/miniature-photo-to-rgb-prompt.md"
PAINTED_STATUS = "PAINTED"
COMPLETED_STATUS = "COMPLETED"
FAILED_STATUS = "FAILED"
DEFAULT_BEDROCK_MODEL_ID = "global.anthropic.claude-sonnet-4-6"
DEFAULT_BEDROCK_MAX_TOKENS = 4096
DEFAULT_BEDROCK_READ_TIMEOUT_SECONDS = 300
HEX_COLOR_PATTERN = re.compile(r"^[0-9A-F]{6}$")


def parse_job_id(event: Dict[str, Any]) -> Optional[str]:
    """Parse and validate `jobId` from the first SQS record.

    Args:
        event: Lambda event dict from an SQS trigger. Expected to contain a `Records`
            list whose first entry has a JSON `body` with a `jobId` field.

    Returns:
        The trimmed, non-empty job id, or `None` if records, JSON or jobId are
        missing or invalid.
    """
    records = event.get("Records") or []
    if not records:
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
    s3_client = boto3.client("s3")

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
    s3_client = boto3.client("s3")

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

    Returns `default` when the variable is unset or blank.

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
    """Return `BEDROCK_MODEL_ID` or the default model when unset or blank."""
    return os.environ.get("BEDROCK_MODEL_ID", "").strip() or DEFAULT_BEDROCK_MODEL_ID


def invoke_bedrock(image: bytes, prompt: str) -> str:
    """Send the prompt and painted image to Amazon Bedrock via the Converse API.

    Args:
        image: PNG bytes of the painted miniature.
        prompt: Prompt text sent alongside the image.

    Returns:
        The concatenated text blocks of the model reply.

    Raises:
        ValueError: If the reply contains no text, or an optional setting is invalid.
        botocore.exceptions.ClientError: Propagated on any Bedrock error.
    """
    model_id = _get_model_id()
    max_tokens = _get_positive_int_env("BEDROCK_MAX_TOKENS", DEFAULT_BEDROCK_MAX_TOKENS)
    read_timeout = _get_positive_int_env(
        "BEDROCK_READ_TIMEOUT_SECONDS", DEFAULT_BEDROCK_READ_TIMEOUT_SECONDS
    )
    # Vision responses can be slow; the botocore default 60s read timeout is too short.
    bedrock_client = boto3.client(
        "bedrock-runtime", config=Config(read_timeout=read_timeout)
    )

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
    s3_client = boto3.client("s3")

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
        The job status string, or `None` if the job does not exist.

    Raises:
        botocore.exceptions.ClientError: Propagated on any DynamoDB error.
        KeyError: If `JOBS_TABLE_NAME` is not set.
    """
    table_name = os.environ["JOBS_TABLE_NAME"]
    dynamodb_client = boto3.client("dynamodb")

    response = dynamodb_client.get_item(
        TableName=table_name,
        Key={"jobId": {"S": job_id}},
        ConsistentRead=True,
    )
    item = response.get("Item")
    if item is None:
        return None
    return item["jobStatus"]["S"]


def update_job_status(job_id: str, status: str) -> None:
    """Move a job from PAINTED to the given status in DynamoDB.

    The write is conditional on the current status being `PAINTED`, so a concurrent
    or repeated invocation cannot overwrite a job that already moved on.

    Args:
        job_id: The job id to update.
        status: New status value (`COMPLETED` or `FAILED`).

    Raises:
        botocore.exceptions.ClientError: Propagated on any DynamoDB error, including
            ConditionalCheckFailedException if the job is not currently PAINTED.
        KeyError: If `JOBS_TABLE_NAME` is not set.
    """
    table_name = os.environ["JOBS_TABLE_NAME"]
    dynamodb_client = boto3.client("dynamodb")

    dynamodb_client.update_item(
        TableName=table_name,
        Key={"jobId": {"S": job_id}},
        UpdateExpression="SET jobStatus = :status, updatedAt = :now",
        ConditionExpression="jobStatus = :expected",
        ExpressionAttributeValues={
            ":status": {"S": status},
            ":now": {"S": datetime.datetime.now(datetime.timezone.utc).isoformat()},
            ":expected": {"S": PAINTED_STATUS},
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
    sqs_client = boto3.client("sqs")

    sqs_client.send_message(
        QueueUrl=queue_url,
        MessageBody=json.dumps({"jobId": job_id, "status": COMPLETED_STATUS}),
    )


def _mark_job_failed(job_id: str, log: StructuredLoggerAdapter) -> None:
    """Best-effort transition of the job to FAILED; failures are only logged."""
    try:
        update_job_status(job_id, FAILED_STATUS)
    except (ClientError, KeyError) as exc:
        log.error(
            "Failed to update job status to FAILED: %s",
            exc,
            extra={"stage": "update_status_failed_error"},
        )


def lambda_handler(event: Dict[str, Any], context: Any) -> None:
    """Orchestrate the Bedrock step: image + prompt -> Bedrock -> result.json -> status.

    Triggered by SQS. Raises ValueError (so SQS routes the message to the DLQ) if the
    jobId is unparsable, the job is unknown, or the job is not in PAINTED status. Any
    failure while downloading, calling Bedrock, validating its output or storing the result marks the job
    FAILED and returns without raising. Failures after the result is stored (SQS
    notification, final status update) are logged and do not raise.

    Args:
        event: SQS Lambda event with `Records[0].body` containing `{"jobId": "<uuid>"}`.
        context: Lambda context object (unused).
    """
    del context

    job_id = parse_job_id(event)
    if job_id is None:
        logger.error(
            "Failed to parse jobId from SQS event",
            extra={"jobId": "unknown", "stage": "parse_input"},
        )
        raise ValueError("Missing or invalid jobId in SQS message")

    log = StructuredLoggerAdapter(logger, {"jobId": job_id, "stage": "orchestrate"})
    log.info("Starting bedrock job")

    job_status = get_job_status(job_id)
    if job_status is None:
        log.error("Job not found", extra={"stage": "validate_job"})
        raise ValueError(f"Job {job_id} not found")
    if job_status != PAINTED_STATUS:
        log.error(
            "Job is not in PAINTED status; current status=%r",
            job_status,
            extra={"stage": "validate_job"},
        )
        raise ValueError(f"Job {job_id} is not in PAINTED status (current: {job_status})")

    try:
        image = download_painted_image_from_s3(job_id)
        prompt = fetch_prompt_from_s3()
        model_reply = invoke_bedrock(image, prompt)
        # Logged before parsing so malformed replies can be diagnosed.
        # A dedicated adapter is used because the adapter's own stage overrides `extra`.
        response_log = StructuredLoggerAdapter(
            logger, {"jobId": job_id, "stage": "bedrock_response"}
        )
        response_log.info("Bedrock response: %s", model_reply)
        result = parse_model_output(model_reply)
        upload_result_to_s3(job_id, result)
    except Exception as exc:  # noqa: BLE001 - deliberate safety boundary for job failure
        log.error("Failed to process painted image: %s", exc, extra={"stage": "bedrock_error"})
        _mark_job_failed(job_id, log)
        return

    try:
        notify_result(job_id)
    except (ClientError, KeyError) as exc:
        log.error("Failed to send result notification: %s", exc, extra={"stage": "notify_error"})
        return

    try:
        update_job_status(job_id, COMPLETED_STATUS)
    except (ClientError, KeyError) as exc:
        log.error(
            "Failed to update job status to COMPLETED: %s",
            exc,
            extra={"stage": "update_status_completed_error"},
        )
        return

    log.info("Successfully completed bedrock job", extra={"stage": "complete"})
