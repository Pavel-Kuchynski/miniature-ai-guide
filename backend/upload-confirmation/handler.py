"""AWS Lambda handler for the upload-confirmation Lambda.

Confirms that a client-uploaded job's reference images are present in S3 and records the
job in DynamoDB. Implements the full orchestration flow: validates the jobId, lists
uploaded images from S3, validates exactly 4 images are present, writes the job item to
DynamoDB, and returns appropriate response codes (201 for new, 200 for duplicate,
400/422/500 for errors). Helper functions: `parse_job_id`, `list_uploaded_images`,
`put_job_item`, and response builders (`_invalid_request_response`, `_missing_images_response`,
`_internal_error_response`, `_success_response`).
"""
import base64
import binascii
import datetime
import json
import logging
import os
import time
from typing import Any, Dict, List, Optional, Tuple

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)


def parse_job_id(event: Dict[str, Any]) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    """Parse and validate `jobId` from the event.

    Args:
        event: Lambda event dict. Expected to contain a top-level `jobId` key with a
            string value (e.g., `{"jobId": "<uuid>"}`).

    Returns:
        A `(job_id, error_response)` tuple. On success, `job_id` is the trimmed, non-empty
        job id string and `error_response` is `None`. On failure, `job_id` is `None` and
        `error_response` is a `400` API-Gateway-style response dict.
    """
    job_id = event.get("jobId")
    if isinstance(job_id, str) and job_id.strip():
        return job_id.strip(), None

    query = event.get("queryStringParameters") or {}
    if isinstance(query, dict):
        job_id = query.get("jobId")
        if isinstance(job_id, str) and job_id.strip():
            return job_id.strip(), None

    raw_body = event.get("body")
    if raw_body and event.get("isBase64Encoded"):
        try:
            raw_body = base64.b64decode(raw_body).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError, TypeError) as exc:
            logger.warning("Failed to base64-decode request body, falling back to query params: %s", exc)
            raw_body = None

    if raw_body:
        try:
            parsed_body = json.loads(raw_body)
            if isinstance(parsed_body, dict):
                job_id = parsed_body.get("jobId")
                if isinstance(job_id, str) and job_id.strip():
                    return job_id.strip(), None
        except (TypeError, json.JSONDecodeError) as exc:
            logger.warning("Failed to parse JSON body, falling back to query params: %s", exc)

    return None, _invalid_request_response("jobId is required")


def _invalid_request_response(message: str) -> Dict[str, Any]:
    """Build the standard `400 InvalidRequest` API-Gateway-style error response."""
    return {
        "statusCode": 400,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({"error": "InvalidRequest", "message": message}),
    }


def list_uploaded_images(job_id: str) -> List[str]:
    """List a job's uploaded reference images in S3.

    Lists all objects under the `uploads/<job_id>/` prefix in the `UPLOAD_BUCKET_NAME`
    bucket, since the frontend chooses arbitrary file names and this Lambda must
    independently verify what actually landed in S3 rather than trusting the caller's
    report. Zero-byte objects (S3 "folder marker" placeholders) and the prefix key itself
    are excluded, since they aren't real uploaded images.

    This function does not enforce the "exactly 4 images" business rule — it only returns
    the raw list of what is present, so callers can apply that check themselves.

    Args:
        job_id: The job id whose upload prefix (`uploads/<job_id>/`) should be listed.

    Returns:
        `s3://<bucket>/<key>` URLs for each uploaded image, sorted lexicographically by
        key. The list may contain fewer or more than 4 entries.

    Raises:
        botocore.exceptions.ClientError: Propagated unchanged if S3 listing fails (e.g.
            throttling, access denied, bucket not found), for the caller to handle.
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

    return [f"s3://{bucket_name}/{key}" for key in sorted(keys)]


JOB_TTL_SECONDS = 7 * 24 * 60 * 60


def put_job_item(job_id: str, image_urls: List[str]) -> Tuple[bool, Dict[str, Any]]:
    """Create the job item in DynamoDB, idempotently.

    Attempts an optimistic `PutItem` with `attribute_not_exists(jobId)` so the common,
    happy-path call (a job's first confirmation) avoids the extra round trip of a
    preceding read. A second confirmation for the same `jobId` collides on that condition;
    the existing item is then fetched with `GetItem` and returned unchanged, so retries are
    safe and never overwrite the original write.

    Args:
        job_id: The job id to create the item for (used as the table's partition key).
        image_urls: The `s3://` URLs of the job's uploaded reference images, written as-is
            into the item's `imageUrls` list.

    Returns:
        A `(created, item)` tuple. `created` is `True` and `item` is the item just written
        when this call performed the initial creation. `created` is `False` and `item` is
        the pre-existing item when a previous call already created it.

    Raises:
        botocore.exceptions.ClientError: Propagated unchanged for any DynamoDB error other
            than the initial `PutItem`'s conditional check failing (e.g. throttling, access
            denied, table not found).
        RuntimeError: If the conditional `PutItem` fails but the fallback `GetItem` finds no
            existing item — an unrecoverable race condition, since the item must exist for
            the condition to have failed.
    """
    table_name = os.environ["JOBS_TABLE_NAME"]
    dynamodb_client = boto3.client("dynamodb")

    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    ttl = int(time.time()) + JOB_TTL_SECONDS

    item = {
        "jobId": {"S": job_id},
        "imageUrls": {"L": [{"S": url} for url in image_urls]},
        "jobStatus": {"S": "UPLOADED"},
        "createdAt": {"S": now},
        "updatedAt": {"S": now},
        "ttl": {"N": str(ttl)},
    }

    try:
        dynamodb_client.put_item(
            TableName=table_name,
            Item=item,
            ConditionExpression="attribute_not_exists(jobId)",
        )
    except ClientError as error:
        if error.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise

        response = dynamodb_client.get_item(
            TableName=table_name,
            Key={"jobId": {"S": job_id}},
            ConsistentRead=True,
        )
        existing_item = response.get("Item")
        if existing_item is None:
            raise RuntimeError(
                f"PutItem for jobId={job_id!r} collided on an existing item, but the "
                "fallback GetItem found none."
            ) from error

        return False, existing_item

    return True, item


def _missing_images_response(image_count: int) -> Dict[str, Any]:
    """Build the standard `422 MissingImages` API-Gateway-style error response."""
    return {
        "statusCode": 422,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({
            "error": "MissingImages",
            "message": f"Expected 4 images, found {image_count}",
            "imageCount": image_count,
        }),
    }


def _internal_error_response(message: str) -> Dict[str, Any]:
    """Build the standard `500 InternalError` API-Gateway-style error response."""
    return {
        "statusCode": 500,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({
            "error": "InternalError",
            "message": message,
        }),
    }


def _success_response(
    status_code: int, job_id: str, job_status: str = "UPLOADED"
) -> Dict[str, Any]:
    """Build a successful API-Gateway-style response with jobId and jobStatus."""
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({
            "jobId": job_id,
            "jobStatus": job_status,
        }),
    }


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """Orchestrate validation, S3 checking, and DynamoDB write for upload confirmation.

    Implements the full flow: parse jobId, list uploaded images, validate count is exactly 4,
    write job item to DynamoDB, and return appropriate response status codes (201 for new,
    200 for duplicate, 400/422/500 for errors).

    Args:
        event: Lambda event dict with a top-level `jobId` field (e.g., `{"jobId": "<uuid>"}`).
        context: Lambda context object (unused).

    Returns:
        An API-Gateway-style response dict:
        - 201: new job created successfully.
        - 200: duplicate confirmation of existing job.
        - 400: invalid/missing jobId.
        - 422: wrong number of uploaded images.
        - 500: S3 or DynamoDB failure.
    """
    del context

    job_id, error_response = parse_job_id(event)
    if error_response is not None:
        return error_response

    try:
        image_urls = list_uploaded_images(job_id)
    except ClientError as error:
        logger.error("S3 list failed for jobId=%r: %s", job_id, error)
        return _internal_error_response("Failed to list uploaded images.")
    except KeyError as error:
        logger.error("Server misconfiguration: missing environment variable %s", error)
        return _internal_error_response("Server misconfiguration: missing S3 bucket name.")

    if len(image_urls) != 4:
        return _missing_images_response(len(image_urls))

    try:
        created, _ = put_job_item(job_id, image_urls)
    except ClientError as error:
        logger.error("DynamoDB put failed for jobId=%r: %s", job_id, error)
        return _internal_error_response("Failed to record job.")
    except RuntimeError as error:
        logger.error("DynamoDB race condition for jobId=%r: %s", job_id, error)
        return _internal_error_response("Failed to record job due to race condition.")
    except KeyError as error:
        logger.error("Server misconfiguration: missing environment variable %s", error)
        return _internal_error_response(
            "Server misconfiguration: missing DynamoDB table name."
        )

    status_code = 201 if created else 200
    return _success_response(status_code, job_id)
