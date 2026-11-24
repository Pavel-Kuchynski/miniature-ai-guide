"""AWS Lambda handler for the start-job Lambda.

Starts guide creation for a job behind API Gateway. The flow parses `jobId` (path) and an
optional `instruction` (JSON body), checks in DynamoDB that the job is `UPLOADED`, verifies
in S3 that exactly `EXPECTED_IMAGE_COUNT` images were uploaded, moves the job to
`IN_PROGRESS` with a conditional write, and sends `{"jobId": ...}` to the paint SQS queue.
If that send fails, the job is reverted to `UPLOADED` so the client can retry.
"""
import datetime
import functools
import json
import os
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

import boto3
from botocore.exceptions import ClientError

from job_status import JobStatus
from logging_config import configure_logger, StructuredLoggerAdapter

if TYPE_CHECKING:
    from mypy_boto3_dynamodb import DynamoDBClient
    from mypy_boto3_s3 import S3Client
    from mypy_boto3_sqs import SQSClient

logger = configure_logger(__name__)

# Reference images a job must have before painting starts. The paint Lambda sends exactly
# one image to the model to keep generation cost down, so more or fewer is rejected here.
EXPECTED_IMAGE_COUNT = 1
CONDITIONAL_CHECK_FAILED = "ConditionalCheckFailedException"
JSON_HEADERS = {"Content-Type": "application/json"}


@functools.lru_cache(maxsize=None)
def _get_client(service_name: str) -> Any:
    """Return a boto3 client for a service, creating it at most once per execution context.

    Clients are cached by service name so warm invocations reuse the client instead of
    building a new one for every AWS call. Tests reset the cache with `cache_clear()`.

    Args:
        service_name: boto3 service name, e.g. "s3", "dynamodb" or "sqs".

    Returns:
        A boto3 client for the requested service.
    """
    return boto3.client(service_name)


def _json_response(status_code: int, body: Dict[str, Any]) -> Dict[str, Any]:
    """Build an API-Gateway-style response with a JSON-encoded body."""
    return {"statusCode": status_code, "headers": dict(JSON_HEADERS), "body": json.dumps(body)}


def _invalid_request_response(message: str) -> Dict[str, Any]:
    """Build the standard `400 InvalidRequest` API-Gateway-style error response."""
    return _json_response(400, {"error": "InvalidRequest", "message": message})


def _not_found_response(message: str) -> Dict[str, Any]:
    """Build the standard `404 NotFound` API-Gateway-style error response."""
    return _json_response(404, {"error": "NotFound", "message": message})


def _conflict_response() -> Dict[str, Any]:
    """Build the `409 Conflict` response for a job that is no longer `UPLOADED`."""
    return _json_response(
        409, {"error": "Conflict", "message": "jobId is already in progress or completed"}
    )


def _unprocessable_entity_response(job_id: str, image_count: int) -> Dict[str, Any]:
    """Build the `422 InvalidImageCount` response, echoing the image count actually found."""
    return _json_response(
        422,
        {
            "error": "InvalidImageCount",
            "message": f"Exactly {EXPECTED_IMAGE_COUNT} image is required for jobId {job_id}",
            "imageCount": image_count,
        },
    )


def _internal_error_response(message: str) -> Dict[str, Any]:
    """Build the standard `500 InternalError` response.

    `message` must be a fixed, client-safe text: never an exception message or a resource
    name.
    """
    return _json_response(500, {"error": "InternalError", "message": message})


def _success_response(job_id: str) -> Dict[str, Any]:
    """Build the `200` response confirming the job is now `IN_PROGRESS`."""
    return _json_response(200, {"jobId": job_id, "jobStatus": JobStatus.IN_PROGRESS.value})


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


def parse_instruction(event: Dict[str, Any]) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    """Parse the optional `instruction` field from the JSON request body.

    Args:
        event: Lambda event dict. The optional `body` is a JSON string such as
            `{"instruction": "Paint the cloak red"}`.

    Returns:
        An `(instruction, error_response)` tuple. On success, `instruction` is the
        trimmed text, or `None` when the body, the field, or its trimmed value is
        missing/empty (nothing should be stored). On a malformed body or a non-string
        `instruction`, `instruction` is `None` and `error_response` is a `400` response.
    """
    body_raw = event.get("body")
    if body_raw is None or (isinstance(body_raw, str) and not body_raw.strip()):
        return None, None

    try:
        body = json.loads(body_raw)
    except (json.JSONDecodeError, TypeError):
        return None, _invalid_request_response("Request body must be valid JSON")

    if not isinstance(body, dict):
        return None, _invalid_request_response("Request body must be a JSON object")

    instruction = body.get("instruction")
    if instruction is None:
        return None, None
    if not isinstance(instruction, str):
        return None, _invalid_request_response("instruction must be a string")

    return (instruction.strip() or None), None


def list_uploaded_images(job_id: str) -> List[str]:
    """List a job's uploaded reference images in S3.

    Lists all objects under the `uploads/<job_id>/` prefix in the `UPLOAD_BUCKET_NAME`
    bucket. The frontend chooses arbitrary file names, so this Lambda checks what actually
    landed in S3 instead of trusting the caller's report. Zero-byte objects (S3 "folder
    marker" placeholders) and the prefix key itself are excluded, since they aren't real
    uploaded images.

    Args:
        job_id: The job id whose upload prefix (`uploads/<job_id>/`) should be listed.

    Returns:
        `s3://<bucket>/<key>` URLs for each uploaded image, sorted lexicographically by
        key. The count is not enforced here; the caller applies `EXPECTED_IMAGE_COUNT`.

    Raises:
        botocore.exceptions.ClientError: Propagated unchanged if S3 listing fails (e.g.
            throttling, access denied, bucket not found), for the caller to handle.
        KeyError: If `UPLOAD_BUCKET_NAME` environment variable is not set.
    """
    bucket_name = os.environ["UPLOAD_BUCKET_NAME"]
    prefix = f"uploads/{job_id}/"
    log_extra = {"jobId": job_id, "stage": "list_images"}

    logger.info("Listing uploaded images in bucket=%r", bucket_name, extra=log_extra)

    s3_client: "S3Client" = _get_client("s3")
    paginator = s3_client.get_paginator("list_objects_v2")

    keys: List[str] = []
    for page in paginator.paginate(Bucket=bucket_name, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if key == prefix or obj["Size"] == 0:
                continue
            keys.append(key)

    sorted_keys = sorted(keys)
    logger.info(
        "Found %d uploaded images: %s", len(sorted_keys), sorted_keys, extra=log_extra
    )

    return [f"s3://{bucket_name}/{key}" for key in sorted_keys]


def trigger_guide_creation(job_id: str) -> None:
    """Trigger guide creation by sending `{"jobId": job_id}` to the paint SQS queue.

    Only the id is sent; the consumer reads everything else (instruction, images) itself.

    Args:
        job_id: The job id to include in the SQS message.

    Raises:
        botocore.exceptions.ClientError: Propagated unchanged if SQS fails (e.g.
            queue not found, access denied, throttling).
        KeyError: If `PAINT_QUEUE_URL` environment variable is not set.
    """
    queue_url = os.environ["PAINT_QUEUE_URL"]
    sqs_client: "SQSClient" = _get_client("sqs")

    sqs_client.send_message(QueueUrl=queue_url, MessageBody=json.dumps({"jobId": job_id}))


def get_job_status(job_id: str) -> Optional[str]:
    """Read a job's status from DynamoDB with a strongly consistent read.

    The read is consistent because the handler decides whether to start the job based on
    it. The decision is still re-checked atomically by `update_job_item`.

    Args:
        job_id: The job id to look up.

    Returns:
        The raw `jobStatus` string (e.g. "UPLOADED", "IN_PROGRESS", "PAINTED"), or `None`
        if the job does not exist. The value is not converted to `JobStatus` because later
        steps write statuses this Lambda does not model.

    Raises:
        botocore.exceptions.ClientError: Propagated unchanged if DynamoDB fails (e.g.
            throttling, access denied, table not found).
        KeyError: If `JOBS_TABLE_NAME` environment variable is not set.
    """
    table_name = os.environ["JOBS_TABLE_NAME"]
    dynamodb_client: "DynamoDBClient" = _get_client("dynamodb")

    response = dynamodb_client.get_item(
        TableName=table_name,
        Key={"jobId": {"S": job_id}},
        ConsistentRead=True,
    )

    item = response.get("Item")
    if item is None:
        return None

    return item.get("jobStatus", {}).get("S")


def update_job_item(job_id: str, instruction: Optional[str] = None) -> None:
    """Move a job from `UPLOADED` to `IN_PROGRESS` in DynamoDB.

    The write is conditional on `jobStatus = UPLOADED`, so it fails both when the job does
    not exist and when a concurrent request already started it. When `instruction` is
    provided it is stored in the same atomic write; otherwise the `instruction` attribute
    is left untouched.

    Args:
        job_id: The job id to update.
        instruction: Optional user instruction text to store on the job item.

    Raises:
        botocore.exceptions.ClientError: Propagated unchanged on any DynamoDB error.
            The code is `ConditionalCheckFailedException` when the job is missing or no
            longer `UPLOADED`.
        KeyError: If `JOBS_TABLE_NAME` environment variable is not set.
    """
    table_name = os.environ["JOBS_TABLE_NAME"]
    dynamodb_client: "DynamoDBClient" = _get_client("dynamodb")

    update_expression = "SET jobStatus = :status, updatedAt = :now"
    expression_values = {
        ":status": {"S": JobStatus.IN_PROGRESS.value},
        ":now": {"S": datetime.datetime.now(datetime.timezone.utc).isoformat()},
        ":expected": {"S": JobStatus.UPLOADED.value},
    }
    if instruction:
        update_expression += ", instruction = :instruction"
        expression_values[":instruction"] = {"S": instruction}

    dynamodb_client.update_item(
        TableName=table_name,
        Key={"jobId": {"S": job_id}},
        UpdateExpression=update_expression,
        ConditionExpression="jobStatus = :expected",
        ExpressionAttributeValues=expression_values,
    )


def revert_job_item(job_id: str, instruction: Optional[str] = None) -> None:
    """Move a job back from `IN_PROGRESS` to `UPLOADED` after the paint message failed.

    Undoes `update_job_item` so the client can retry instead of getting `409` forever. The
    write is conditional on `jobStatus = IN_PROGRESS`, so it never overwrites a status that
    another writer set in the meantime. When this request stored an `instruction`, it is
    removed so a retry without an instruction does not reuse it.

    Args:
        job_id: The job id to revert.
        instruction: The instruction passed to `update_job_item`, if any.

    Raises:
        botocore.exceptions.ClientError: Propagated unchanged on any DynamoDB error,
            including `ConditionalCheckFailedException` when the job is no longer
            `IN_PROGRESS`.
        KeyError: If `JOBS_TABLE_NAME` environment variable is not set.
    """
    table_name = os.environ["JOBS_TABLE_NAME"]
    dynamodb_client: "DynamoDBClient" = _get_client("dynamodb")

    update_expression = "SET jobStatus = :status, updatedAt = :now"
    if instruction:
        update_expression += " REMOVE instruction"

    dynamodb_client.update_item(
        TableName=table_name,
        Key={"jobId": {"S": job_id}},
        UpdateExpression=update_expression,
        ConditionExpression="jobStatus = :expected",
        ExpressionAttributeValues={
            ":status": {"S": JobStatus.UPLOADED.value},
            ":now": {"S": datetime.datetime.now(datetime.timezone.utc).isoformat()},
            ":expected": {"S": JobStatus.IN_PROGRESS.value},
        },
    )


def _release_job(job_id: str, instruction: Optional[str], log: StructuredLoggerAdapter) -> None:
    """Revert a job to `UPLOADED` after a failed trigger, logging instead of raising.

    Never raises: it runs on a path that already returns `500`, and a revert failure must
    not replace that response. If the revert fails, the job stays `IN_PROGRESS` and the
    `revert_status_error` log line is the signal for manual cleanup.

    Args:
        job_id: The job id to revert.
        instruction: The instruction stored by `update_job_item`, if any.
        log: Adapter carrying the request's `jobId`.
    """
    try:
        revert_job_item(job_id, instruction)
    except ClientError as error:
        if error.response["Error"]["Code"] == CONDITIONAL_CHECK_FAILED:
            log.warning(
                "Job is no longer IN_PROGRESS; leaving its status unchanged",
                extra={"stage": "revert_status"},
            )
            return
        log.exception(
            "Failed to revert job to UPLOADED; job stays IN_PROGRESS",
            extra={"stage": "revert_status_error"},
        )
        return
    except KeyError as error:
        log.error(
            "Server misconfiguration: missing environment variable %s",
            error,
            extra={"stage": "revert_status_error"},
        )
        return
    log.info("Reverted job to UPLOADED so it can be retried", extra={"stage": "revert_status"})


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """Start guide creation for an uploaded job.

    Each step's `ClientError` (AWS failure) and `KeyError` (missing environment variable)
    are logged separately and turned into a `500` with a fixed message.

    Args:
        event: API Gateway event with `pathParameters.jobId` and an optional JSON `body`
            containing `instruction`.
        context: Lambda context object (unused).

    Returns:
        An API-Gateway-style response dict:
        - 200: job moved to `IN_PROGRESS` and the paint message was sent.
        - 400: missing/blank `jobId`, or malformed body/`instruction`.
        - 404: job not found in DynamoDB.
        - 409: job is not `UPLOADED`, including when a concurrent request started it
          between the status check and the conditional update (no SQS message is sent).
        - 422: uploaded image count is not `EXPECTED_IMAGE_COUNT`.
        - 500: S3, DynamoDB, or SQS failure, or missing configuration. When the SQS send
          fails, the job is first reverted to `UPLOADED` so the client can retry.
    """
    del context

    job_id, error_response = parse_job_id(event)
    if error_response is not None:
        logger.error("Invalid request: missing or invalid jobId", extra={"stage": "parse_input"})
        return error_response

    log = StructuredLoggerAdapter(logger, {"jobId": job_id, "stage": "orchestrate"})

    instruction, error_response = parse_instruction(event)
    if error_response is not None:
        log.error("Invalid request: bad instruction", extra={"stage": "parse_input"})
        return error_response

    log.info("Starting job")

    try:
        job_status = get_job_status(job_id)
    except ClientError:
        log.exception("DynamoDB get failed", extra={"stage": "get_status"})
        return _internal_error_response("Failed to check job status.")
    except KeyError as error:
        log.error(
            "Server misconfiguration: missing environment variable %s",
            error,
            extra={"stage": "get_status"},
        )
        return _internal_error_response("Server misconfiguration: missing DynamoDB table name.")

    if job_status is None:
        log.warning("Job not found", extra={"stage": "get_status"})
        return _not_found_response("jobId does not exist")

    if job_status != JobStatus.UPLOADED:
        log.warning("Job is not UPLOADED: status=%r", job_status, extra={"stage": "get_status"})
        return _conflict_response()

    try:
        image_urls = list_uploaded_images(job_id)
    except ClientError:
        log.exception("S3 list failed", extra={"stage": "list_images"})
        return _internal_error_response("Failed to list uploaded images.")
    except KeyError as error:
        log.error(
            "Server misconfiguration: missing environment variable %s",
            error,
            extra={"stage": "list_images"},
        )
        return _internal_error_response("Server misconfiguration: missing S3 bucket name.")

    if len(image_urls) != EXPECTED_IMAGE_COUNT:
        log.error(
            "Invalid image count: %d (expected %d)",
            len(image_urls),
            EXPECTED_IMAGE_COUNT,
            extra={"stage": "validate_images"},
        )
        return _unprocessable_entity_response(job_id, len(image_urls))

    try:
        update_job_item(job_id, instruction)
    except ClientError as error:
        if error.response["Error"]["Code"] == CONDITIONAL_CHECK_FAILED:
            # Another request moved the job out of UPLOADED after our status read. It owns
            # the paint message, so sending another one would paint the job twice.
            log.warning(
                "Job was started by a concurrent request; not triggering again",
                extra={"stage": "update_status"},
            )
            return _conflict_response()
        log.exception("DynamoDB update failed", extra={"stage": "update_status"})
        return _internal_error_response("Failed to record job.")
    except KeyError as error:
        log.error(
            "Server misconfiguration: missing environment variable %s",
            error,
            extra={"stage": "update_status"},
        )
        return _internal_error_response("Server misconfiguration: missing DynamoDB table name.")

    # Both failures below mean the message was definitely not enqueued (SQS returned an
    # error response, or the call never happened), so reverting cannot cause a double paint.
    try:
        trigger_guide_creation(job_id)
    except ClientError:
        log.exception("SQS send failed", extra={"stage": "trigger_creation"})
        _release_job(job_id, instruction, log)
        return _internal_error_response("Failed to trigger guide creation.")
    except KeyError as error:
        log.error(
            "Server misconfiguration: missing environment variable %s",
            error,
            extra={"stage": "trigger_creation"},
        )
        _release_job(job_id, instruction, log)
        return _internal_error_response("Server misconfiguration: missing SQS queue URL.")

    log.info("Successfully started job", extra={"stage": "complete"})
    return _success_response(job_id)
