"""AWS Lambda handler for the start-job Lambda.

Initiates the guide creation workflow by validating job state, confirming 4 images
are present in S3, updating job status to IN_PROGRESS, and triggering the guide
creation process via SQS. Implements the full orchestration flow: parse jobId,
check DynamoDB job status, list uploaded images, validate count, update job status,
and send SQS message. Helper functions: `parse_job_id`, `list_uploaded_images`,
`get_job_status`, `update_job_item`, `trigger_guide_creation`, and response builders.
"""
import datetime
import json
import logging
import os
from typing import Any, Dict, List, Optional, Tuple

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)


def parse_job_id(event: Dict[str, Any]) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    """Parse and validate `jobId` from the URL path parameters.

    Args:
        event: Lambda event dict. Expected to contain a `pathParameters` key with a
            `jobId` field (e.g., `{"pathParameters": {"jobId": "<uuid>"}}`).

    Returns:
        A `(job_id, error_response)` tuple. On success, `job_id` is the trimmed,
        non-empty job id string and `error_response` is `None`. On failure, `job_id`
        is `None` and `error_response` is a `400` API-Gateway-style response dict.
    """
    path_parameters = event.get("pathParameters") or {}
    job_id = path_parameters.get("jobId")
    if isinstance(job_id, str) and job_id.strip():
        return job_id.strip(), None

    return None, _invalid_request_response("jobId is required")


def list_uploaded_images(job_id: str) -> List[str]:
    """List a job's uploaded reference images in S3.

    Lists all objects under the `uploads/<job_id>/` prefix in the `UPLOAD_BUCKET_NAME`
    bucket, since the frontend chooses arbitrary file names and this Lambda must
    independently verify what actually landed in S3 rather than trusting the caller's
    report. Zero-byte objects (S3 "folder marker" placeholders) and the prefix key
    itself are excluded, since they aren't real uploaded images.

    Args:
        job_id: The job id whose upload prefix (`uploads/<job_id>/`) should be listed.

    Returns:
        `s3://<bucket>/<key>` URLs for each uploaded image, sorted lexicographically by
        key. The list may contain fewer or more than 4 entries.

    Raises:
        botocore.exceptions.ClientError: Propagated unchanged if S3 listing fails (e.g.
            throttling, access denied, bucket not found), for the caller to handle.
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


def get_job_status(job_id: str) -> Optional[str]:
    """Query DynamoDB for a job's status.

    Args:
        job_id: The job id to look up.

    Returns:
        The job's status string (e.g., "UPLOADED", "IN_PROGRESS", "SUCCEEDED") if found,
        or `None` if the job does not exist in DynamoDB.

    Raises:
        botocore.exceptions.ClientError: Propagated unchanged if DynamoDB fails (e.g.
            throttling, access denied, table not found).
        KeyError: If `JOBS_TABLE_NAME` environment variable is not set.
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

    job_status = item.get("jobStatus", {}).get("S")
    return job_status


def update_job_item(job_id: str) -> None:
    """Update a job's status to IN_PROGRESS in DynamoDB.

    Uses a conditional expression to ensure the job exists and is currently in the
    "UPLOADED" status before updating.

    Args:
        job_id: The job id to update.

    Returns:
        None on success.

    Raises:
        botocore.exceptions.ClientError: Propagated unchanged on any DynamoDB error
            (e.g. job not found, status not "UPLOADED", access denied, throttling).
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
            ":status": {"S": "IN_PROGRESS"},
            ":now": {"S": datetime.datetime.now(datetime.timezone.utc).isoformat()},
            ":expected": {"S": "UPLOADED"},
        },
    )


def trigger_guide_creation(job_id: str) -> None:
    """Trigger guide creation by sending a message to SQS.

    Args:
        job_id: The job id to include in the SQS message.

    Returns:
        None on success.

    Raises:
        botocore.exceptions.ClientError: Propagated unchanged if SQS fails (e.g.
            queue not found, access denied, throttling).
        KeyError: If `GUIDE_CREATION_QUEUE_URL` environment variable is not set.
    """
    queue_url = os.environ["GUIDE_CREATION_QUEUE_URL"]
    sqs_client = boto3.client("sqs")

    sqs_client.send_message(
        QueueUrl=queue_url,
        MessageBody=json.dumps({"jobId": job_id}),
    )


def _invalid_request_response(message: str) -> Dict[str, Any]:
    """Build the standard `400 InvalidRequest` API-Gateway-style error response."""
    return {
        "statusCode": 400,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({"error": "InvalidRequest", "message": message}),
    }


def _not_found_response(message: str) -> Dict[str, Any]:
    """Build the standard `404 NotFound` API-Gateway-style error response."""
    return {
        "statusCode": 404,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({"error": "NotFound", "message": message}),
    }


def _conflict_response(message: str) -> Dict[str, Any]:
    """Build the standard `409 Conflict` API-Gateway-style error response."""
    return {
        "statusCode": 409,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({"error": "Conflict", "message": message}),
    }


def _unprocessable_entity_response(job_id: str, image_count: int) -> Dict[str, Any]:
    """Build the standard `422 UnprocessableEntity` API-Gateway-style error response."""
    return {
        "statusCode": 422,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({
            "error": "InvalidImageCount",
            "message": f"Exactly 4 images are required for jobId {job_id}",
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


def _success_response(job_id: str) -> Dict[str, Any]:
    """Build a successful API-Gateway-style response."""
    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({
            "jobId": job_id,
            "jobStatus": "IN_PROGRESS",
        }),
    }


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """Orchestrate job startup: validate, check status, verify images, trigger workflow.

    Implements the full flow: parse jobId from path parameters, check job exists and is
    in "UPLOADED" status, list uploaded images and validate exactly 4 are present,
    update job status to "IN_PROGRESS", send message to SQS queue, and return
    appropriate response status codes (200 for success, 400/404/409/422/500 for errors).

    Args:
        event: Lambda event dict with a `pathParameters` field containing `jobId`
            (e.g., `{"pathParameters": {"jobId": "<uuid>"}}`).
        context: Lambda context object (unused).

    Returns:
        An API-Gateway-style response dict:
        - 200: job started successfully (status updated to IN_PROGRESS, SQS triggered).
        - 400: invalid/missing jobId in path parameters.
        - 404: job not found in DynamoDB.
        - 409: job status is not "UPLOADED".
        - 422: uploaded image count is not exactly 4.
        - 500: S3, DynamoDB, or SQS failure.
    """
    del context

    job_id, error_response = parse_job_id(event)
    if error_response is not None:
        return error_response

    try:
        job_status = get_job_status(job_id)
    except ClientError as error:
        logger.error("DynamoDB get failed for jobId=%r: %s", job_id, error)
        return _internal_error_response("Failed to check job status.")
    except KeyError as error:
        logger.error("Server misconfiguration: missing environment variable %s", error)
        return _internal_error_response(
            "Server misconfiguration: missing DynamoDB table name."
        )

    if job_status is None:
        return _not_found_response("jobId does not exist")

    if job_status != "UPLOADED":
        return _conflict_response(
            "jobId is already in progress or completed"
        )

    try:
        image_urls = list_uploaded_images(job_id)
    except ClientError as error:
        logger.error("S3 list failed for jobId=%r: %s", job_id, error)
        return _internal_error_response("Failed to list uploaded images.")
    except KeyError as error:
        logger.error("Server misconfiguration: missing environment variable %s", error)
        return _internal_error_response(
            "Server misconfiguration: missing S3 bucket name."
        )

    if len(image_urls) != 4:
        return _unprocessable_entity_response(job_id, len(image_urls))

    try:
        update_job_item(job_id)
    except ClientError as error:
        if error.response["Error"]["Code"] == "ConditionalCheckFailedException":
            logger.info(
                "Job %s was already updated to IN_PROGRESS (race condition handled).",
                job_id,
            )
        else:
            logger.error("DynamoDB update failed for jobId=%r: %s", job_id, error)
            return _internal_error_response("Failed to record job.")
    except KeyError as error:
        logger.error("Server misconfiguration: missing environment variable %s", error)
        return _internal_error_response(
            "Server misconfiguration: missing DynamoDB table name."
        )

    try:
        trigger_guide_creation(job_id)
    except ClientError as error:
        logger.error("SQS send failed for jobId=%r: %s", job_id, error)
        return _internal_error_response("Failed to trigger guide creation.")
    except KeyError as error:
        logger.error("Server misconfiguration: missing environment variable %s", error)
        return _internal_error_response(
            "Server misconfiguration: missing SQS queue URL."
        )

    return _success_response(job_id)

