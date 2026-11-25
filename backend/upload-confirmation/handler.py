"""AWS Lambda handler for the upload-confirmation Lambda.

Confirms that a client-uploaded job's reference images are present in S3 and records the
job in DynamoDB, owned by the authenticated caller. Implements the full orchestration flow:
requires an authenticated caller (Cognito `sub`), validates the jobId as a canonical UUID,
lists uploaded images from S3, validates exactly 1 image is present, writes the job item
(with `userId`) to DynamoDB, and returns appropriate response codes (201 for new, 200 for a
duplicate confirmed by the owner, 400/401/404/422/500 for errors). Helper functions:
`get_caller_user_id`, `parse_job_id`, `list_uploaded_images`, `put_job_item`, and response
builders (`_unauthorized_response`, `_invalid_request_response`, `_not_found_response`,
`_missing_images_response`, `_internal_error_response`, `_success_response`).
"""

import base64
import binascii
import datetime
import json
import logging
import os
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

import boto3
from botocore.exceptions import BotoCoreError, ClientError

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

EXPECTED_IMAGE_COUNT = 1
JOB_STATUS_UPLOADED = "UPLOADED"
JOB_TTL_SECONDS = 7 * 24 * 60 * 60


def get_caller_user_id(event: Dict[str, Any]) -> str | None:
    """Return the authenticated caller's Cognito `sub`, or `None` when it is unavailable.

    The `sub` is the job owner identity. A REST API Cognito authorizer exposes the claims at
    `requestContext.authorizer.claims`; an HTTP API JWT authorizer nests them at
    `requestContext.authorizer.jwt.claims`. The REST shape is used when `claims` is present;
    the JWT shape is only consulted when it is absent.

    Args:
        event: API Gateway event dict.

    Returns:
        The trimmed, non-empty `sub` claim. `None` when there are no authorizer claims, or
        when `sub` is missing, blank or not a string.
    """
    authorizer = _child_mapping(_child_mapping(event, "requestContext"), "authorizer")
    claims = authorizer.get("claims")
    if claims is None:
        claims = _child_mapping(authorizer, "jwt").get("claims")
    if not isinstance(claims, dict):
        return None

    sub = claims.get("sub")
    if not isinstance(sub, str):
        return None
    return sub.strip() or None


def _child_mapping(parent: Any, key: str) -> Dict[str, Any]:
    """Return `parent[key]` when `parent` is a dict and that value is a dict, else `{}`."""
    value = parent.get(key) if isinstance(parent, dict) else None
    return value if isinstance(value, dict) else {}


def parse_job_id(event: Dict[str, Any]) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    """Parse and validate `jobId` from the event.

    The first non-blank string found in the top-level `jobId`, then `queryStringParameters`,
    then a JSON body (base64-decoded when `isBase64Encoded`) is used. The value must be a
    canonical UUID: it is embedded in the S3 prefix `uploads/<jobId>/`, so anything else
    (e.g. a value containing "/") is rejected here rather than reaching S3 or DynamoDB.

    Args:
        event: Lambda event dict. Expected to contain a top-level `jobId` key with a
            canonical UUID string value (e.g., `{"jobId": "<uuid>"}`).

    Returns:
        A `(job_id, error_response)` tuple. On success, `job_id` is the trimmed canonical
        UUID and `error_response` is `None`. On failure, `job_id` is `None` and
        `error_response` is a `400` API-Gateway-style response dict: `jobId is required`
        when no non-blank value is present, `jobId must be a canonical UUID` otherwise.
    """
    job_id = _extract_job_id(event)
    if job_id is None:
        return None, _invalid_request_response("jobId is required")
    if not _is_canonical_uuid(job_id):
        return None, _invalid_request_response("jobId must be a canonical UUID")
    return job_id, None


def _extract_job_id(event: Dict[str, Any]) -> Optional[str]:
    """Return the first non-blank `jobId` string from the event, trimmed, or `None`.

    Sources are checked in order: top-level `jobId`, `queryStringParameters`, then the JSON
    body. Malformed base64 or JSON is logged and treated as "no value" so the next source is
    tried.

    Args:
        event: Lambda event dict.

    Returns:
        The trimmed `jobId` string, or `None` when no source provides a non-blank string.
    """
    job_id = event.get("jobId")
    if isinstance(job_id, str) and job_id.strip():
        return job_id.strip()

    query = event.get("queryStringParameters") or {}
    if isinstance(query, dict):
        job_id = query.get("jobId")
        if isinstance(job_id, str) and job_id.strip():
            return job_id.strip()

    raw_body = event.get("body")
    if raw_body and event.get("isBase64Encoded"):
        try:
            raw_body = base64.b64decode(raw_body).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError, TypeError) as exc:
            logger.warning(
                "Failed to base64-decode request body, falling back to query params: %s", exc
            )
            raw_body = None

    if raw_body:
        try:
            parsed_body = json.loads(raw_body)
            if isinstance(parsed_body, dict):
                job_id = parsed_body.get("jobId")
                if isinstance(job_id, str) and job_id.strip():
                    return job_id.strip()
        except (TypeError, json.JSONDecodeError) as exc:
            logger.warning("Failed to parse JSON body, falling back to query params: %s", exc)

    return None


def _is_canonical_uuid(value: str) -> bool:
    """Return `True` when `value` is a UUID in canonical lowercase hyphenated form.

    `uuid.UUID` is lenient (it accepts uppercase, braces, a `urn:uuid:` prefix and no
    hyphens). Round-tripping through `str()` rejects every spelling other than the one
    `lambda_upload` generates, so one job cannot be addressed by several different keys.

    Args:
        value: The candidate string.

    Returns:
        `True` if `value` is a canonical UUID, otherwise `False`.
    """
    try:
        return str(uuid.UUID(value)) == value
    except ValueError:
        return False


def _invalid_request_response(message: str) -> Dict[str, Any]:
    """Build the standard `400 InvalidRequest` API-Gateway-style error response."""
    return {
        "statusCode": 400,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({"error": "InvalidRequest", "message": message}),
    }


def _unauthorized_response() -> Dict[str, Any]:
    """Build the standard `401 Unauthorized` API-Gateway-style error response."""
    return {
        "statusCode": 401,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({"error": "Unauthorized", "message": "Authentication is required."}),
    }


def _not_found_response() -> Dict[str, Any]:
    """Build the `404 NotFound` response.

    Used both when the job does not exist and when it is owned by another user, so the
    response never reveals whether a `jobId` exists for someone else.
    """
    return {
        "statusCode": 404,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({"error": "NotFound", "message": "jobId does not exist"}),
    }


def list_uploaded_images(job_id: str) -> List[str]:
    """List a job's uploaded reference images in S3.

    Lists all objects under the `uploads/<job_id>/` prefix in the `UPLOAD_BUCKET_NAME`
    bucket, since the frontend chooses arbitrary file names and this Lambda must
    independently verify what actually landed in S3 rather than trusting the caller's
    report. Zero-byte objects (S3 "folder marker" placeholders) and the prefix key itself
    are excluded, since they aren't real uploaded images.

    This function does not enforce the "exactly 1 image" business rule — it only returns
    the raw list of what is present, so callers can apply that check themselves.

    Args:
        job_id: The job id whose upload prefix (`uploads/<job_id>/`) should be listed. Must
            already be a validated canonical UUID (see `parse_job_id`).

    Returns:
        `s3://<bucket>/<key>` URLs for each uploaded image, sorted lexicographically by
        key. The list may contain fewer or more than 1 entry.

    Raises:
        botocore.exceptions.ClientError: Propagated unchanged if S3 listing fails (e.g.
            throttling, access denied, bucket not found), for the caller to handle.
        botocore.exceptions.BotoCoreError: Propagated unchanged for transport failures
            (e.g. connect or read timeouts), for the caller to handle.
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

    return [f"s3://{bucket_name}/{key}" for key in sorted(keys)]


def put_job_item(
    job_id: str, image_urls: List[str], user_id: str
) -> Tuple[bool, Dict[str, Any]]:
    """Create the job item in DynamoDB, owned by `user_id`, idempotently.

    Attempts an optimistic `PutItem` with `attribute_not_exists(jobId)` so the common,
    happy-path call (a job's first confirmation) avoids the extra round trip of a
    preceding read. A second confirmation for the same `jobId` collides on that condition;
    the existing item is then fetched with `GetItem` and returned unchanged, so retries are
    safe and never overwrite the original write (including its `userId`).

    Args:
        job_id: The job id to create the item for (used as the table's partition key).
        image_urls: The `s3://` URLs of the job's uploaded reference images, written as-is
            into the item's `imageUrls` list.
        user_id: The authenticated caller's Cognito `sub`, stored as the item's `userId`
            owner attribute.

    Returns:
        A `(created, item)` tuple. `created` is `True` and `item` is the item just written
        when this call performed the initial creation. `created` is `False` and `item` is
        the pre-existing item when a previous call already created it; the caller must
        compare its `userId` with the caller before revealing its state.

    Raises:
        botocore.exceptions.ClientError: Propagated unchanged for any DynamoDB error other
            than the initial `PutItem`'s conditional check failing (e.g. throttling, access
            denied, table not found).
        botocore.exceptions.BotoCoreError: Propagated unchanged for transport failures
            (e.g. connect or read timeouts).
        RuntimeError: If the conditional `PutItem` fails but the fallback `GetItem` finds no
            existing item — an unrecoverable race condition, since the item must exist for
            the condition to have failed.
        KeyError: If `JOBS_TABLE_NAME` environment variable is not set.
    """
    table_name = os.environ["JOBS_TABLE_NAME"]
    dynamodb_client = boto3.client("dynamodb")

    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    ttl = int(time.time()) + JOB_TTL_SECONDS

    item = {
        "jobId": {"S": job_id},
        "userId": {"S": user_id},
        "imageUrls": {"L": [{"S": url} for url in image_urls]},
        "jobStatus": {"S": JOB_STATUS_UPLOADED},
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


def _success_response(status_code: int, job_id: str, job_status: str) -> Dict[str, Any]:
    """Build a successful API-Gateway-style response with `jobId` and the real `jobStatus`."""
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({
            "jobId": job_id,
            "jobStatus": job_status,
        }),
    }


def _missing_images_response(image_count: int) -> Dict[str, Any]:
    """Build the standard `422 MissingImages` API-Gateway-style error response."""
    return {
        "statusCode": 422,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({
            "error": "MissingImages",
            "message": f"Expected {EXPECTED_IMAGE_COUNT} image, found {image_count}",
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


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """Orchestrate authentication, validation, S3 checking, and the DynamoDB write.

    Implements the full flow: require the caller's Cognito `sub`, parse the jobId, list
    uploaded images, validate the count is exactly 1, write the job item owned by the
    caller, and return the response. A duplicate confirmation is answered only for the
    owner: other callers get the same `404` as for a missing job.

    Args:
        event: API Gateway event with a `jobId` (top-level, query string or JSON body) and
            authorizer claims at `requestContext.authorizer` (REST `claims` or HTTP
            `jwt.claims`; see `get_caller_user_id`).
        context: Lambda context object (unused).

    Returns:
        An API-Gateway-style response dict:
        - 201: new job created, owned by the caller.
        - 200: duplicate confirmation by the owner; `jobStatus` is the job's current status.
        - 400: missing or non-canonical-UUID `jobId`.
        - 401: no authenticated caller in the event.
        - 404: the job is owned by another user (a missing job is never reported here).
        - 422: uploaded image count is not `EXPECTED_IMAGE_COUNT`.
        - 500: S3 or DynamoDB failure, or missing configuration.
    """
    del context

    user_id = get_caller_user_id(event)
    if user_id is None:
        logger.warning("Rejected upload confirmation: no authenticated caller")
        return _unauthorized_response()

    job_id, error_response = parse_job_id(event)
    if error_response is not None:
        return error_response

    try:
        image_urls = list_uploaded_images(job_id)
    except (ClientError, BotoCoreError) as error:
        logger.error("S3 list failed for jobId=%r: %s", job_id, error)
        return _internal_error_response("Failed to list uploaded images.")
    except KeyError as error:
        logger.error("Server misconfiguration: missing environment variable %s", error)
        return _internal_error_response("Server misconfiguration: missing S3 bucket name.")

    if len(image_urls) != EXPECTED_IMAGE_COUNT:
        return _missing_images_response(len(image_urls))

    try:
        created, item = put_job_item(job_id, image_urls, user_id)
    except (ClientError, BotoCoreError) as error:
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

    if created:
        return _success_response(201, job_id, JOB_STATUS_UPLOADED)

    if item.get("userId", {}).get("S") != user_id:
        logger.warning(
            "Duplicate confirmation for jobId=%r by a caller who does not own it; returning 404",
            job_id,
        )
        return _not_found_response()

    # The duplicate path reports the stored status: the job may have moved on since upload.
    job_status = item.get("jobStatus", {}).get("S")
    if job_status is None:
        logger.error("Stored job item for jobId=%r has no jobStatus", job_id)
        return _internal_error_response("Failed to record job.")

    return _success_response(200, job_id, job_status)
