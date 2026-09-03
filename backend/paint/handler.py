"""AWS Lambda handler for the paint Lambda.

Handles painting of previously uploaded reference images. Triggered by SQS.
Downloads 4 reference images from S3, reads the painting prompt from a static S3
bucket, calls OpenAI to generate 4 painted images, uploads results to S3 via
presigned PUT URLs, updates job status in DynamoDB, and notifies the next step
via SQS on success. On failure, updates job status to FAILED.
"""
import base64
import datetime
import io
import json
import logging
import os
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

import boto3
from botocore.exceptions import ClientError
from openai import OpenAI, OpenAIError

logger = logging.getLogger(__name__)

EXPECTED_IMAGE_COUNT = 4
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-image-1")
OPENAI_API_KEY_SECRET_NAME = os.environ.get(
    "OPENAI_API_KEY_SECRET_NAME", "miniature-guide/openai/api-key"
)

_SECRETS_CACHE: Dict[str, str] = {}


def parse_job_id(event: Dict[str, Any]) -> Optional[str]:
    """Parse and validate `jobId` from the first SQS record.

    Args:
        event: Lambda event dict from an SQS trigger. Expected to contain a `Records`
            list with at least one entry whose `body` is a JSON string containing a
            `jobId` field.

    Returns:
        The trimmed, non-empty job id string on success, or `None` on any failure
        (missing records, invalid JSON, missing or blank jobId).
    """
    records = event.get("Records") or []
    if not records:
        return None

    body_raw = records[0].get("body", "")
    try:
        body = json.loads(body_raw)
    except (json.JSONDecodeError, TypeError):
        return None

    job_id = body.get("jobId")
    if isinstance(job_id, str) and job_id.strip():
        return job_id.strip()

    return None


def download_images_from_s3(job_id: str) -> List[bytes]:
    """Download all reference images for a job from the upload S3 bucket.

    Lists and downloads all non-empty objects under `uploads/<job_id>/` in the
    `UPLOAD_BUCKET_NAME` bucket. Zero-byte folder markers are excluded.
    Objects are returned in lexicographic key order.

    Args:
        job_id: The job id whose upload prefix should be listed and downloaded.

    Returns:
        List of raw image bytes sorted lexicographically by S3 key.

    Raises:
        botocore.exceptions.ClientError: Propagated on any S3 error.
        KeyError: If `UPLOAD_BUCKET_NAME` environment variable is not set.
    """
    bucket_name = os.environ["UPLOAD_BUCKET_NAME"]
    prefix = f"uploads/{job_id}/"
    s3_client = boto3.client("s3")

    paginator = s3_client.get_paginator("list_objects_v2")
    keys: List[str] = []
    for page in paginator.paginate(Bucket=bucket_name, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if key == prefix or obj["Size"] == 0:
                continue
            keys.append(key)

    keys = sorted(keys)
    logger.info(
        "Downloading %d images for jobId=%r from bucket=%r prefix=%r",
        len(keys),
        job_id,
        bucket_name,
        prefix,
    )

    images: List[bytes] = []
    for key in keys:
        response = s3_client.get_object(Bucket=bucket_name, Key=key)
        images.append(response["Body"].read())

    return images


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
    s3_client = boto3.client("s3")

    logger.info("Fetching painting prompt from bucket=%r key=%r", bucket_name, key)
    response = s3_client.get_object(Bucket=bucket_name, Key=key)
    return response["Body"].read().decode("utf-8")


def _fetch_openai_api_key() -> str:
    """Retrieve the OpenAI API key from AWS Secrets Manager.

    Uses in-memory caching within the Lambda execution context to avoid repeated
    API calls for the same secret during a single invocation.

    Returns:
        The OpenAI API key string.

    Raises:
        botocore.exceptions.ClientError: Propagated on any Secrets Manager error.
        KeyError: If the secret does not contain a 'SecretString' field.
        json.JSONDecodeError: If the secret is not valid JSON.
    """
    if OPENAI_API_KEY_SECRET_NAME in _SECRETS_CACHE:
        logger.info("Using cached OpenAI API key")
        return _SECRETS_CACHE[OPENAI_API_KEY_SECRET_NAME]

    secrets_client = boto3.client("secretsmanager")
    logger.info("Fetching OpenAI API key from Secrets Manager")

    response = secrets_client.get_secret_value(SecretId=OPENAI_API_KEY_SECRET_NAME)
    secret = json.loads(response["SecretString"])
    api_key = secret["api_key"]
    _SECRETS_CACHE[OPENAI_API_KEY_SECRET_NAME] = api_key
    return api_key


def generate_painted_images(images: List[bytes], prompt: str) -> List[bytes]:
    """Call OpenAI to generate painted images from reference images and a text prompt.

    Uses the `gpt-image-1` model via the images edit endpoint. Passes all reference
    images as context and requests exactly `EXPECTED_IMAGE_COUNT` generated images.
    Returns raw image bytes decoded from the base64 response.

    Args:
        images: Reference image bytes to use as context for generation.
        prompt: Text prompt describing the desired painting style.

    Returns:
        List of generated image bytes.

    Raises:
        openai.OpenAIError: Propagated on any API error.
        botocore.exceptions.ClientError: Propagated on Secrets Manager errors.
        json.JSONDecodeError: If the secret is not valid JSON.
        KeyError: If the secret does not contain 'api_key' field.
    """
    api_key = _fetch_openai_api_key()
    client = OpenAI(api_key=api_key)

    image_files = [
        (f"image_{i}.jpg", io.BytesIO(img_data), "image/jpeg")
        for i, img_data in enumerate(images)
    ]

    logger.info(
        "Calling OpenAI images.edit with %d reference images, n=%d",
        len(image_files),
        EXPECTED_IMAGE_COUNT,
    )

    response = client.images.edit(
        model=OPENAI_MODEL,
        image=image_files,
        prompt=prompt,
        n=EXPECTED_IMAGE_COUNT,
    )

    return [base64.b64decode(item.b64_json) for item in response.data]


def _request_presigned_url(s3_client: Any, bucket_name: str, key: str) -> str:
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

    Raises:
        urllib.error.HTTPError: On non-2xx HTTP responses.
        urllib.error.URLError: On network-level errors.
    """
    req = urllib.request.Request(
        presigned_url,
        data=image_data,
        method="PUT",
        headers={"Content-Type": "image/jpeg"},
    )
    with urllib.request.urlopen(req) as _:
        pass


def upload_painted_images(job_id: str, images: List[bytes]) -> None:
    """Upload generated painted images to the paint S3 bucket via presigned PUT URLs.

    For each image, generates a presigned PUT URL and uploads the image bytes.
    Images are stored under `painted_images/<job_id>/image_<n>.jpg`.

    Args:
        job_id: The job id used as the S3 key prefix.
        images: List of generated image bytes to upload.

    Raises:
        botocore.exceptions.ClientError: Propagated on presigned URL generation failure.
        urllib.error.HTTPError: Propagated on any upload failure.
        KeyError: If `PAINT_BUCKET_NAME` environment variable is not set.
    """
    bucket_name = os.environ["PAINT_BUCKET_NAME"]
    s3_client = boto3.client("s3")

    for index, image_data in enumerate(images):
        key = f"painted_images/{job_id}/image_{index}.jpg"
        presigned_url = _request_presigned_url(s3_client, bucket_name, key)
        logger.info(
            "Uploading painted image %d/%d for jobId=%r to key=%r",
            index + 1,
            len(images),
            job_id,
            key,
        )
        _upload_image_via_presigned_url(presigned_url, image_data)


def update_job_status(job_id: str, status: str) -> None:
    """Update job status in DynamoDB from IN_PROGRESS to the given status.

    Uses a conditional write requiring the current status to be `IN_PROGRESS`.

    Args:
        job_id: The job id to update.
        status: New status value (e.g. "PAINTED" or "FAILED").

    Raises:
        botocore.exceptions.ClientError: Propagated on any DynamoDB error, including
            ConditionalCheckFailedException if the job is not currently IN_PROGRESS.
        KeyError: If `JOBS_TABLE_NAME` environment variable is not set.
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
            ":expected": {"S": "IN_PROGRESS"},
        },
    )


def notify_guide_creation(job_id: str) -> None:
    """Send a job-painted notification to the guide creation SQS queue.

    Args:
        job_id: The job id to include in the SQS message.

    Raises:
        botocore.exceptions.ClientError: Propagated on any SQS error.
        KeyError: If `GUIDE_CREATION_QUEUE_URL` environment variable is not set.
    """
    queue_url = os.environ["GUIDE_CREATION_QUEUE_URL"]
    sqs_client = boto3.client("sqs")

    sqs_client.send_message(
        QueueUrl=queue_url,
        MessageBody=json.dumps({"jobId": job_id}),
    )


def lambda_handler(event: Dict[str, Any], context: Any) -> None:
    """Orchestrate paint job: download images, generate paintings, upload, update status.

    Triggered by SQS. Parses jobId from the SQS message body, downloads 4 reference
    images from S3, fetches the painting prompt, calls OpenAI to generate 4 painted
    images, uploads them to S3 via presigned PUT URLs, updates DynamoDB to PAINTED,
    and notifies the guide creation queue. On any failure during image generation or
    upload, updates job status to FAILED instead and returns without raising.

    If the jobId cannot be parsed from the SQS message, raises ValueError so SQS can
    route the message to the dead-letter queue.

    Args:
        event: SQS Lambda event with `Records[0].body` containing `{"jobId": "<uuid>"}`.
        context: Lambda context object (unused).
    """
    del context

    job_id = parse_job_id(event)
    if job_id is None:
        logger.error("Failed to parse jobId from SQS event")
        raise ValueError("Missing or invalid jobId in SQS message")

    logger.info("Starting paint job for jobId=%r", job_id)

    try:
        reference_images = download_images_from_s3(job_id)
        prompt = fetch_prompt_from_s3()
        generated_images = generate_painted_images(reference_images, prompt)

        if len(generated_images) < EXPECTED_IMAGE_COUNT:
            raise ValueError(
                f"OpenAI returned {len(generated_images)} images; "
                f"expected {EXPECTED_IMAGE_COUNT}"
            )

        upload_painted_images(job_id, generated_images)

    except Exception as exc:
        logger.error("Failed to paint images for jobId=%r: %s", job_id, exc)
        try:
            update_job_status(job_id, "FAILED")
        except ClientError as update_exc:
            logger.error(
                "Failed to update job status to FAILED for jobId=%r: %s",
                job_id,
                update_exc,
            )
        return

    try:
        update_job_status(job_id, "PAINTED")
    except ClientError as exc:
        logger.error(
            "Failed to update job status to PAINTED for jobId=%r: %s", job_id, exc
        )
        return

    try:
        notify_guide_creation(job_id)
    except (ClientError, KeyError) as exc:
        logger.error(
            "Failed to send guide creation notification for jobId=%r: %s", job_id, exc
        )
        return

    logger.info("Successfully completed paint job for jobId=%r", job_id)
