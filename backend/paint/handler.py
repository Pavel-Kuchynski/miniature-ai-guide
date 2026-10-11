"""AWS Lambda handler for the paint Lambda.

Handles painting of previously uploaded reference images. Triggered by SQS.
Downloads the single reference image from S3, reads the painting prompt from a static S3
bucket (prefixed with the job's optional user instruction from DynamoDB), calls OpenAI to generate a painted image, uploads results to S3 via
presigned PUT URLs, updates job status in DynamoDB, and notifies the next step
via SQS on success. On failure, updates job status to FAILED.
"""
import base64
import datetime
import functools
import io
import json
import logging
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Dict, List, Optional

import boto3
from botocore.exceptions import ClientError
from openai import OpenAI, OpenAIError
from PIL import Image, ImageOps

from logging_config import configure_logger, StructuredLoggerAdapter

if TYPE_CHECKING:
    from mypy_boto3_s3 import S3Client

logger = configure_logger(__name__)

UNKNOWN = "unknown"
# Reference images uploaded per job, and images requested from / expected back from OpenAI.
# They are separate because they describe different contracts (input cardinality, the
# OpenAI `n` parameter, and the output count the handler accepts).
REFERENCE_IMAGE_COUNT = 1
REQUESTED_OUTPUT_COUNT = 1
MAX_IMAGE_SIDE_PX = 1024
OPENAI_OUTPUT_FORMAT = "png"
OPENAI_OUTPUT_QUALITY = "medium"
DEFAULT_OPENAI_MODEL = "gpt-image-1"
DEFAULT_OPENAI_API_KEY_SECRET_NAME = "miniature-guide/openai/api-key"
# Explicit timeouts stop a hung upstream call from silently consuming the Lambda timeout.
# Worst case for one OpenAI call is (OPENAI_MAX_RETRIES + 1) * OPENAI_TIMEOUT_SECONDS plus
# backoff. The Lambda timeout must exceed that, otherwise the job stays IN_PROGRESS.
OPENAI_TIMEOUT_SECONDS = 120.0
OPENAI_MAX_RETRIES = 2
UPLOAD_TIMEOUT_SECONDS = 60.0


class JobNotFoundError(Exception):
    """Raised when no job item exists in the jobs DynamoDB table for a job id."""


class JobStatus(StrEnum):
    """Values of the `jobStatus` attribute on a job item in the jobs DynamoDB table."""

    IN_PROGRESS = "IN_PROGRESS"
    PAINTED = "PAINTED"
    FAILED = "FAILED"


@dataclass(frozen=True)
class JobRecord:
    """Subset of a job item needed by the paint flow.

    Attributes:
        status: The job's current status.
        instruction: Trimmed optional user instruction, or `None` if absent or blank.
    """

    status: JobStatus
    instruction: Optional[str]


@functools.lru_cache(maxsize=None)
def _get_client(service_name: str) -> Any:
    """Return a boto3 client for a service, creating it at most once per execution context.

    Clients are cached by service name so warm invocations reuse the client instead of
    building a new one for every AWS call. Tests reset the cache with `cache_clear()`.

    Args:
        service_name: boto3 service name, e.g. "s3", "dynamodb", "sqs" or "secretsmanager".

    Returns:
        A boto3 client for the requested service.
    """
    return boto3.client(service_name)


def parse_job_id(event: Dict[str, Any]) -> Optional[str]:
    """Parse and validate `jobId` from the first SQS record.

    Args:
        event: Lambda event dict from an SQS trigger. Expected to contain a `Records`
            list with at least one entry whose `body` is a JSON string containing a
            `jobId` field.

    Returns:
        The trimmed, non-empty job id string on success, or `None` on any failure:
        missing or non-list `Records`, a first record that is not an object, invalid
        JSON, a JSON body that is not an object (e.g. a number, list, string or null),
        or a missing, non-string or blank `jobId`.
    """
    records = event.get("Records") or []
    if not isinstance(records, list) or not records or not isinstance(records[0], dict):
        return None

    body_raw = records[0].get("body", "")
    try:
        body = json.loads(body_raw)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(body, dict):
        return None

    job_id = body.get("jobId")
    if isinstance(job_id, str) and job_id.strip():
        return job_id.strip()

    return None


def download_reference_image(job_id: str) -> bytes:
    """Download the reference image for a job from the upload S3 bucket.

    Lists non-empty objects under `uploads/<job_id>/` in the `UPLOAD_BUCKET_NAME`
    bucket (zero-byte folder markers are excluded) and downloads only the first key in
    lexicographic order. Only one image is ever sent to the model, to keep processing
    cost down; any extra objects are ignored with a warning.

    Args:
        job_id: The job id whose upload prefix should be listed and downloaded.

    Returns:
        The raw bytes of the reference image.

    Raises:
        ValueError: If the upload prefix contains no non-empty object.
        botocore.exceptions.ClientError: Propagated on any S3 error.
        KeyError: If `UPLOAD_BUCKET_NAME` environment variable is not set.
    """
    bucket_name = os.environ["UPLOAD_BUCKET_NAME"]
    prefix = f"uploads/{job_id}/"
    s3_client: S3Client = _get_client("s3")

    paginator = s3_client.get_paginator("list_objects_v2")
    keys: List[str] = []
    for page in paginator.paginate(Bucket=bucket_name, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if key == prefix or obj["Size"] == 0:
                continue
            keys.append(key)

    keys = sorted(keys)
    if not keys:
        raise ValueError(f"No reference image found under s3://{bucket_name}/{prefix}")
    if len(keys) > 1:
        logger.warning(
            "Found %d objects under prefix=%r; using only %r",
            len(keys),
            prefix,
            keys[0],
            extra={"jobId": job_id, "stage": "download_images"},
        )
    key = keys[0]
    logger.info(
        "Downloading reference image from bucket=%r key=%r",
        bucket_name,
        key,
        extra={"jobId": job_id, "stage": "download_images"},
    )

    response = s3_client.get_object(Bucket=bucket_name, Key=key)
    return response["Body"].read()


def fetch_prompt_from_s3() -> str:
    """Read the painting prompt text from the static S3 bucket.

    Reads the file `prompts/paint_images_prompt.txt` from `STATIC_BUCKET_NAME`.

    Returns:
        Prompt text decoded as UTF-8.

    Raises:
        botocore.exceptions.ClientError: Propagated on any S3 error.
        KeyError: If `STATIC_BUCKET_NAME` environment variable is not set.
    """
    bucket_name = os.environ["STATIC_BUCKET_NAME"]
    key = "prompts/paint_images_prompt.txt"
    s3_client: S3Client = _get_client("s3")

    logger.info("Fetching painting prompt from bucket=%r key=%r", bucket_name, key, extra={"stage": "fetch_prompt"})
    response = s3_client.get_object(Bucket=bucket_name, Key=key)
    return response["Body"].read().decode("utf-8")


@functools.lru_cache(maxsize=1)
def _fetch_openai_api_key(secret_name: str) -> str:
    """Retrieve the OpenAI API key from AWS Secrets Manager.

    Memoised with `lru_cache(maxsize=1)`, so the key is fetched once per Lambda execution
    context and reused by warm invocations. The cache is keyed by `secret_name`, so a
    different secret triggers a fresh fetch. Failed fetches are not cached.

    Args:
        secret_name: Secrets Manager secret id whose JSON value contains an
            `OPENAI_API_KEY` field.

    Returns:
        The OpenAI API key string.

    Raises:
        botocore.exceptions.ClientError: Propagated on any Secrets Manager error.
        KeyError: If the secret has no 'SecretString' field or its JSON has no
            'OPENAI_API_KEY' field.
        json.JSONDecodeError: If the secret is not valid JSON.
    """
    logger.info("Fetching OpenAI API key from Secrets Manager", extra={"stage": "fetch_secret"})
    secrets_client = _get_client("secretsmanager")

    response = secrets_client.get_secret_value(SecretId=secret_name)
    secret = json.loads(response["SecretString"])
    return secret["OPENAI_API_KEY"]


def _resize_reference_image(image_data: bytes) -> bytes:
    """Downscale a reference image so its longer side is at most `MAX_IMAGE_SIDE_PX`.

    Aspect ratio is preserved and smaller images are never upscaled. The result is
    re-encoded as lossless PNG, and EXIF orientation is applied before resizing so the
    model sees the image upright. Smaller inputs reduce image-token cost.

    Args:
        image_data: Raw bytes of the original reference image (any Pillow-readable format).

    Returns:
        PNG-encoded bytes of the resized image.

    Raises:
        PIL.UnidentifiedImageError: If the bytes are not a readable image.
    """
    with Image.open(io.BytesIO(image_data)) as source:
        image = ImageOps.exif_transpose(source)
        if image.mode not in ("RGB", "RGBA", "L", "LA"):
            image = image.convert("RGBA" if "transparency" in image.info else "RGB")
        image.thumbnail((MAX_IMAGE_SIDE_PX, MAX_IMAGE_SIDE_PX), Image.LANCZOS)
        output = io.BytesIO()
        image.save(output, format="PNG")
    return output.getvalue()


def generate_painted_images(images: List[bytes], prompt: str) -> List[bytes]:
    """Call OpenAI to generate painted images from reference images and a text prompt.

    Calls the OpenAI images edit endpoint with the model named by the `OPENAI_MODEL`
    environment variable (default `gpt-image-1`). The API key is read from the secret
    named by `OPENAI_API_KEY_SECRET_NAME` (default `miniature-guide/openai/api-key`).
    Both variables are read at call time. Each reference image is first downscaled to
    `MAX_IMAGE_SIDE_PX` on its longer side and sent as PNG. Requests exactly
    `REQUESTED_OUTPUT_COUNT` generated images in PNG format at `medium` quality. The client
    waits at most `OPENAI_TIMEOUT_SECONDS` per request and retries up to
    `OPENAI_MAX_RETRIES` times. Returns raw PNG bytes decoded from the base64 response.

    Args:
        images: Reference image bytes to use as context for generation.
        prompt: Text prompt describing the desired painting style.

    Returns:
        List of generated image bytes.

    Raises:
        openai.OpenAIError: Propagated on any API error, including timeouts that persist
            after all retries.
        botocore.exceptions.ClientError: Propagated on Secrets Manager errors.
        json.JSONDecodeError: If the secret is not valid JSON.
        KeyError: If the secret does not contain an 'OPENAI_API_KEY' field.
        PIL.UnidentifiedImageError: If a reference image cannot be decoded.
    """
    api_key = _fetch_openai_api_key(
        os.environ.get("OPENAI_API_KEY_SECRET_NAME", DEFAULT_OPENAI_API_KEY_SECRET_NAME)
    )
    model = os.environ.get("OPENAI_MODEL", DEFAULT_OPENAI_MODEL)
    client = OpenAI(
        api_key=api_key,
        timeout=OPENAI_TIMEOUT_SECONDS,
        max_retries=OPENAI_MAX_RETRIES,
    )

    image_files = [
        (f"image_{i}.png", io.BytesIO(_resize_reference_image(img_data)), "image/png")
        for i, img_data in enumerate(images)
    ]

    logger.info(
        "Calling OpenAI images.edit with %d reference image(s), model=%s, n=%d, format=%s, quality=%s",
        len(image_files),
        model,
        REQUESTED_OUTPUT_COUNT,
        OPENAI_OUTPUT_FORMAT,
        OPENAI_OUTPUT_QUALITY,
        extra={"stage": "generate_images"},
    )

    response = client.images.edit(
        model=model,
        image=image_files,
        prompt=prompt,
        n=REQUESTED_OUTPUT_COUNT,
        output_format=OPENAI_OUTPUT_FORMAT,
        quality=OPENAI_OUTPUT_QUALITY,
    )

    return [base64.b64decode(item.b64_json) for item in response.data]


def _request_presigned_url(s3_client: "S3Client", bucket_name: str, key: str) -> str:
    """Generate a presigned S3 PUT URL for the given object.

    Args:
        s3_client: Boto3 S3 client.
        bucket_name: Target S3 bucket.
        key: Object key to generate the URL for.

    Returns:
        Presigned URL string valid for 5 minutes.
    """
    return s3_client.generate_presigned_url(
        "put_object",
        Params={"Bucket": bucket_name, "Key": key},
        ExpiresIn=300,
    )


def _upload_image_via_presigned_url(presigned_url: str, image_data: bytes) -> None:
    """PUT image bytes to a presigned S3 URL.

    Args:
        presigned_url: Presigned PUT URL for the target S3 object.
        image_data: Raw image bytes to upload.

    The request is bounded by `UPLOAD_TIMEOUT_SECONDS`.

    Raises:
        urllib.error.HTTPError: On non-2xx HTTP responses.
        urllib.error.URLError: On network-level errors, including connect or send timeouts.
        TimeoutError: If waiting for the response exceeds `UPLOAD_TIMEOUT_SECONDS`.
    """
    req = urllib.request.Request(
        presigned_url,
        data=image_data,
        method="PUT",
        headers={"Content-Type": "image/png"},
    )
    with urllib.request.urlopen(req, timeout=UPLOAD_TIMEOUT_SECONDS) as _:
        pass


def upload_painted_images(job_id: str, images: List[bytes]) -> None:
    """Upload generated painted images to the paint S3 bucket via presigned PUT URLs.

    For each image, generates a presigned PUT URL and uploads the image bytes.
    Images are stored under `painted_images/<job_id>/image_<n>.png`.

    Args:
        job_id: The job id used as the S3 key prefix.
        images: List of generated image bytes to upload.

    Raises:
        botocore.exceptions.ClientError: Propagated on presigned URL generation failure.
        urllib.error.HTTPError: Propagated on any upload failure.
        KeyError: If `PAINT_BUCKET_NAME` environment variable is not set.
    """
    bucket_name = os.environ["PAINT_BUCKET_NAME"]
    s3_client: S3Client = _get_client("s3")

    for index, image_data in enumerate(images):
        key = f"painted_images/{job_id}/image_{index}.png"
        presigned_url = _request_presigned_url(s3_client, bucket_name, key)
        logger.info(
            "Uploading painted image %d/%d to key=%r",
            index + 1,
            len(images),
            key,
            extra={"jobId": job_id, "stage": "upload_images"},
        )
        _upload_image_via_presigned_url(presigned_url, image_data)


def update_job_status(job_id: str, status: JobStatus) -> None:
    """Update job status in DynamoDB from IN_PROGRESS to the given status.

    Uses a conditional write requiring the current status to be `JobStatus.IN_PROGRESS`.

    Args:
        job_id: The job id to update.
        status: New status value (`JobStatus.PAINTED` or `JobStatus.FAILED`).

    Raises:
        botocore.exceptions.ClientError: Propagated on any DynamoDB error, including
            ConditionalCheckFailedException if the job is not currently IN_PROGRESS.
        KeyError: If `JOBS_TABLE_NAME` environment variable is not set.
    """
    table_name = os.environ["JOBS_TABLE_NAME"]
    dynamodb_client = _get_client("dynamodb")

    dynamodb_client.update_item(
        TableName=table_name,
        Key={"jobId": {"S": job_id}},
        UpdateExpression="SET jobStatus = :status, updatedAt = :now",
        ConditionExpression="jobStatus = :expected",
        ExpressionAttributeValues={
            ":status": {"S": status.value},
            ":now": {"S": datetime.datetime.now(datetime.timezone.utc).isoformat()},
            ":expected": {"S": JobStatus.IN_PROGRESS.value},
        },
    )


def get_job(job_id: str) -> JobRecord:
    """Read a job's status and optional instruction with one strongly consistent DynamoDB read.

    Status and instruction come from the same GetItem call with `ConsistentRead=True`, so
    they describe the same snapshot and the handler performs a single read per invocation.

    Args:
        job_id: The job id to look up.

    Returns:
        A `JobRecord` with the job's status and its trimmed instruction (`None` when the
        instruction is missing or blank).

    Raises:
        botocore.exceptions.ClientError: Propagated on any DynamoDB error.
        JobNotFoundError: If no item exists for `job_id`.
        ValueError: If the stored `jobStatus` is not a known `JobStatus` value.
        KeyError: If `JOBS_TABLE_NAME` environment variable is not set, or the item has no
            `jobStatus` attribute.
    """
    table_name = os.environ["JOBS_TABLE_NAME"]
    dynamodb_client = _get_client("dynamodb")

    response = dynamodb_client.get_item(
        TableName=table_name,
        Key={"jobId": {"S": job_id}},
        ConsistentRead=True,
    )

    if "Item" not in response:
        raise JobNotFoundError(f"Job {job_id} not found in DynamoDB")

    item = response["Item"]
    status = JobStatus(item["jobStatus"]["S"])
    instruction = item.get("instruction", {}).get("S", "").strip() or None
    return JobRecord(status=status, instruction=instruction)


def build_prompt(prompt: str, instruction: Optional[str]) -> str:
    """Prepend the user instruction to the base prompt, if there is one.

    Args:
        prompt: Base prompt text read from S3.
        instruction: Optional user instruction.

    Returns:
        `"Instruction: <instruction> + Prompt: <prompt>"` when an instruction is given,
        otherwise the base prompt unchanged.
    """
    if not instruction:
        return prompt
    return f"Instruction: {instruction} + Prompt: {prompt}"


def notify_guide_creation(job_id: str) -> None:
    """Send a job-painted notification to the guide creation SQS queue.

    Args:
        job_id: The job id to include in the SQS message.

    Raises:
        botocore.exceptions.ClientError: Propagated on any SQS error.
        KeyError: If `GUIDE_CREATION_QUEUE_URL` environment variable is not set.
    """
    queue_url = os.environ["GUIDE_CREATION_QUEUE_URL"]
    sqs_client = _get_client("sqs")

    sqs_client.send_message(
        QueueUrl=queue_url,
        MessageBody=json.dumps({"jobId": job_id}),
    )


def _paint_reference_image(job_id: str, instruction: Optional[str]) -> List[bytes]:
    """Download the reference image, build its prompt, and generate painted images.

    Args:
        job_id: The job whose reference image should be painted.
        instruction: Optional user instruction from the job item, prepended to the prompt.

    Returns:
        Exactly `REQUESTED_OUTPUT_COUNT` generated image byte strings, as a list because
        `upload_painted_images` takes a list.

    Raises:
        ValueError: If OpenAI does not return exactly `REQUESTED_OUTPUT_COUNT` images.
        botocore.exceptions.ClientError, KeyError, json.JSONDecodeError, openai.OpenAIError,
            PIL.UnidentifiedImageError: Propagated from the download, prompt, secret and
            generation steps. The caller maps them to a FAILED job status.
    """
    reference_image = download_reference_image(job_id)

    prompt = fetch_prompt_from_s3()
    prompt = build_prompt(prompt, instruction)

    generated_images = generate_painted_images([reference_image], prompt)
    if len(generated_images) != REQUESTED_OUTPUT_COUNT:
        raise ValueError(
            f"OpenAI returned {len(generated_images)} images; "
            f"expected {REQUESTED_OUTPUT_COUNT}"
        )
    return generated_images


def _mark_painted(job_id: str) -> bool:
    """Set the job status to PAINTED, logging instead of raising if the update fails.

    Args:
        job_id: The job to mark as painted.

    Returns:
        True if the status was written, False if the DynamoDB update failed (logged).
    """
    try:
        update_job_status(job_id, JobStatus.PAINTED)
    except ClientError as exc:
        logger.exception(
            "Failed to update job status to PAINTED: %s",
            exc,
            extra={"jobId": job_id, "stage": "update_status_painted_error"},
        )
        return False
    return True


def _mark_failed(job_id: str) -> None:
    """Set the job status to FAILED, logging instead of raising if the update fails.

    Args:
        job_id: The job to mark as failed.
    """
    try:
        update_job_status(job_id, JobStatus.FAILED)
    except ClientError as exc:
        logger.exception(
            "Failed to update job status to FAILED: %s",
            exc,
            extra={"jobId": job_id, "stage": "update_status_failed_error"},
        )


def lambda_handler(event: Dict[str, Any], context: Any) -> None:
    """Orchestrate paint job: download images, generate paintings, upload, update status.

    Triggered by SQS. Parses jobId from the SQS message body, reads the job with `get_job`
    and verifies it is IN_PROGRESS, paints the reference image (see `_paint_reference_image`),
    uploads the results to S3 via presigned PUT URLs, updates DynamoDB to PAINTED, and
    notifies the guide creation queue. Any failure while painting or uploading updates job
    status to FAILED and returns without raising.

    Args:
        event: SQS Lambda event with `Records[0].body` containing `{"jobId": "<uuid>"}`.
        context: Lambda context object (unused).

    Raises:
        ValueError: If the jobId is missing or invalid, the job item does not exist
            (`JobNotFoundError` is converted to ValueError so the message goes to the
            dead-letter queue), or the job is not IN_PROGRESS.
        KeyError: If `JOBS_TABLE_NAME` is not set. Configuration errors are not converted.
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
    log.info("Starting paint job")

    try:
        job = get_job(job_id)
    except JobNotFoundError as exc:
        log.exception("Job not found: %s", exc, extra={"stage": "get_job_status"})
        raise ValueError(f"Job {job_id} not found") from exc

    if job.status != JobStatus.IN_PROGRESS:
        log.error(
            "Job is not in IN_PROGRESS status; current status=%r",
            job.status.value,
            extra={"stage": "validate_status"},
        )
        raise ValueError(
            f"Job {job_id} is not in IN_PROGRESS status (current: {job.status.value})"
        )

    try:
        generated_images = _paint_reference_image(job_id, job.instruction)
        upload_painted_images(job_id, generated_images)
    except Exception as exc:
        log.exception("Failed to paint images: %s", exc, extra={"stage": "paint_error"})
        _mark_failed(job_id)
        return

    if not _mark_painted(job_id):
        return

    try:
        notify_guide_creation(job_id)
    except (ClientError, KeyError) as exc:
        log.exception(
            "Failed to send guide creation notification: %s",
            exc,
            extra={"stage": "notify_error"},
        )
        return

    log.info("Successfully completed paint job", extra={"stage": "complete"})
