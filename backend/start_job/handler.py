"""AWS Lambda handler for the start-job Lambda.

Starts guide creation for a job behind API Gateway. The flow requires an authenticated
caller (Cognito `sub`), parses `jobId` (path, must be a canonical UUID) and an optional
`instruction` (JSON body), checks in DynamoDB that the job is owned by the caller and is
`UPLOADED`, verifies in S3 that exactly `EXPECTED_IMAGE_COUNT` images were uploaded, moves
the job to `IN_PROGRESS` with a conditional write on status and owner, and sends
`{"jobId": ...}` to the paint SQS queue. If that send fails, the job is reverted to
`UPLOADED` so the client can retry.
"""
import datetime
import functools
import json
import os
import uuid
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

import boto3
from botocore.exceptions import BotoCoreError, ClientError

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


def _unauthorized_response() -> Dict[str, Any]:
    """Build the standard `401 Unauthorized` API-Gateway-style error response."""
    return _json_response(
        401, {"error": "Unauthorized", "message": "Authentication is required."}
    )


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


def _child_mapping(parent: Any, key: str) -> Dict[str, Any]:
    """Return `parent[key]` when `parent` is a dict and that value is a dict, else `{}`."""
    value = parent.get(key) if isinstance(parent, dict) else None
    return value if isinstance(value, dict) else {}


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


def parse_job_id(event: Dict[str, Any]) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    """Parse and validate `jobId` from the URL path parameters.

    Args:
        event: Lambda event dict. Expected to contain a `pathParameters` key with a
            `jobId` field that is a canonical UUID (e.g.,
            `{"pathParameters": {"jobId": "<uuid>"}}`).

    Returns:
        A `(job_id, error_response)` tuple. On success, `job_id` is the trimmed canonical
        UUID and `error_response` is `None`. On failure, `job_id` is `None` and
        `error_response` is a `400` API-Gateway-style response dict: `jobId is required`
        when the value is missing, not a string, or blank; `jobId must be a canonical UUID`
        otherwise.
    """
    path_parameters = event.get("pathParameters") or {}
    job_id = path_parameters.get("jobId")
    if not isinstance(job_id, str) or not job_id.strip():
        return None, _invalid_request_response("jobId is required")

    job_id = job_id.strip()
    if not _is_canonical_uuid(job_id):
        return None, _invalid_request_response("jobId must be a canonical UUID")
    return job_id, None


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
        botocore.exceptions.BotoCoreError: Propagated unchanged for transport failures
            (e.g. connect or read timeouts), for the caller to handle.
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
        botocore.exceptions.ClientError: Propagated unchanged if SQS rejects the request
            (e.g. queue not found, access denied, throttling). The message was not enqueued.
        botocore.exceptions.BotoCoreError: Propagated unchanged for transport failures
            (e.g. connect or read timeouts). The message may or may not have been enqueued.
        KeyError: If `PAINT_QUEUE_URL` environment variable is not set.
    """
    queue_url = os.environ["PAINT_QUEUE_URL"]
    sqs_client: "SQSClient" = _get_client("sqs")

    sqs_client.send_message(QueueUrl=queue_url, MessageBody=json.dumps({"jobId": job_id}))


def get_owned_job_status(job_id: str, user_id: str) -> str | None:
    """Read a job's status from DynamoDB, but only when the caller owns the job.

    The read is strongly consistent because the handler decides whether to start the job
    based on it. The owner check happens here so no caller can learn another user's job
    status. The decision is still re-checked atomically by `update_job_item`.

    Args:
        job_id: The job id to look up.
        user_id: The caller's Cognito `sub`, compared with the item's `userId`.

    Returns:
        The raw `jobStatus` string (e.g. "UPLOADED", "IN_PROGRESS", "PAINTED") when the job
        exists and its `userId` equals `user_id`. `None` when the job does not exist, or
        when it is owned by another user or has no `userId` (a job created before ownership
        was recorded). The two causes are deliberately indistinguishable to the caller. The
        value is not converted to `JobStatus` because later steps write statuses this Lambda
        does not model.

    Raises:
        botocore.exceptions.ClientError: Propagated unchanged if DynamoDB fails (e.g.
            throttling, access denied, table not found).
        botocore.exceptions.BotoCoreError: Propagated unchanged for transport failures
            (e.g. connect or read timeouts).
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
    if item.get("userId", {}).get("S") != user_id:
        return None

    return item.get("jobStatus", {}).get("S")


def update_job_item(job_id: str, user_id: str, instruction: Optional[str] = None) -> None:
    """Move a job from `UPLOADED` to `IN_PROGRESS` in DynamoDB, for its owner only.

    The write is conditional on `jobStatus = UPLOADED` and `userId = user_id`, so it fails
    when the job does not exist, when a concurrent request already started it, and when the
    caller is not the owner. When `instruction` is provided it is stored in the same atomic
    write; otherwise the `instruction` attribute is left untouched.

    Args:
        job_id: The job id to update.
        user_id: The caller's Cognito `sub` that must match the item's `userId`.
        instruction: Optional user instruction text to store on the job item.

    Raises:
        botocore.exceptions.ClientError: Propagated unchanged on any DynamoDB error.
            The code is `ConditionalCheckFailedException` when the job is missing, is no
            longer `UPLOADED`, or is not owned by `user_id`.
        botocore.exceptions.BotoCoreError: Propagated unchanged for transport failures
            (e.g. connect or read timeouts). The write may have been applied.
        KeyError: If `JOBS_TABLE_NAME` environment variable is not set.
    """
    table_name = os.environ["JOBS_TABLE_NAME"]
    dynamodb_client: "DynamoDBClient" = _get_client("dynamodb")

    update_expression = "SET jobStatus = :status, updatedAt = :now"
    expression_values = {
        ":status": {"S": JobStatus.IN_PROGRESS.value},
        ":now": {"S": datetime.datetime.now(datetime.timezone.utc).isoformat()},
        ":expected": {"S": JobStatus.UPLOADED.value},
        ":uid": {"S": user_id},
    }
    if instruction:
        update_expression += ", instruction = :instruction"
        expression_values[":instruction"] = {"S": instruction}

    dynamodb_client.update_item(
        TableName=table_name,
        Key={"jobId": {"S": job_id}},
        UpdateExpression=update_expression,
        ConditionExpression="jobStatus = :expected AND userId = :uid",
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
        botocore.exceptions.BotoCoreError: Propagated unchanged for transport failures.
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
    except BotoCoreError:
        log.exception(
            "Transport failure reverting job to UPLOADED; job stays IN_PROGRESS",
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
    """Start guide creation for an uploaded job owned by the authenticated caller.

    Each step's `ClientError` or `BotoCoreError` (AWS or transport failure) and `KeyError`
    (missing environment variable) is logged separately and turned into a `500` with a fixed
    message. A job that is missing or owned by another user gets the same `404`.

    Args:
        event: API Gateway event with `pathParameters.jobId` (a canonical UUID), authorizer
            claims at `requestContext.authorizer` (REST `claims` or HTTP `jwt.claims`; see
            `get_caller_user_id`) and an optional JSON `body` containing `instruction`.
        context: Lambda context object (unused).

    Returns:
        An API-Gateway-style response dict:
        - 200: job moved to `IN_PROGRESS` and the paint message was sent.
        - 400: missing, blank or non-UUID `jobId`, or malformed body/`instruction`.
        - 401: no authenticated caller in the event.
        - 404: job not found in DynamoDB, or owned by another user.
        - 409: job is not `UPLOADED`, including when a concurrent request started it
          between the status check and the conditional update (no SQS message is sent).
        - 422: uploaded image count is not `EXPECTED_IMAGE_COUNT`.
        - 500: S3, DynamoDB, or SQS failure, or missing configuration. When the SQS send
          fails, the job is first reverted to `UPLOADED` so the client can retry.
    """
    del context

    user_id = get_caller_user_id(event)
    if user_id is None:
        logger.warning(
            "Rejected request: no authenticated caller", extra={"stage": "authenticate"}
        )
        return _unauthorized_response()

    job_id, error_response = parse_job_id(event)
    if error_response is not None:
        logger.warning("Invalid request: missing or invalid jobId", extra={"stage": "parse_input"})
        return error_response

    log = StructuredLoggerAdapter(logger, {"jobId": job_id, "stage": "orchestrate"})

    instruction, error_response = parse_instruction(event)
    if error_response is not None:
        log.warning("Invalid request: bad instruction", extra={"stage": "parse_input"})
        return error_response

    log.info("Starting job")

    try:
        job_status = get_owned_job_status(job_id, user_id)
    except (ClientError, BotoCoreError):
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
        log.warning("Job not found or not owned by caller", extra={"stage": "get_status"})
        return _not_found_response("jobId does not exist")

    if job_status != JobStatus.UPLOADED:
        log.warning("Job is not UPLOADED: status=%r", job_status, extra={"stage": "get_status"})
        return _conflict_response()

    try:
        image_urls = list_uploaded_images(job_id)
    except (ClientError, BotoCoreError):
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
        log.warning(
            "Invalid image count: %d (expected %d)",
            len(image_urls),
            EXPECTED_IMAGE_COUNT,
            extra={"stage": "validate_images"},
        )
        return _unprocessable_entity_response(job_id, len(image_urls))

    try:
        update_job_item(job_id, user_id, instruction)
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
    except BotoCoreError:
        # Not reverted: the write may have landed, and a revert conditional only on
        # IN_PROGRESS could undo a state a concurrent request has since won. The job may
        # stay IN_PROGRESS without a paint message; see the README.
        log.exception(
            "DynamoDB update failed with a transport error; job state is unknown",
            extra={"stage": "update_status"},
        )
        return _internal_error_response("Failed to record job.")
    except KeyError as error:
        log.error(
            "Server misconfiguration: missing environment variable %s",
            error,
            extra={"stage": "update_status"},
        )
        return _internal_error_response("Server misconfiguration: missing DynamoDB table name.")

    # A ClientError from SQS means the message was not enqueued, so reverting cannot paint the
    # job twice. A BotoCoreError (e.g. a read timeout) is ambiguous: the message may have been
    # enqueued. Reverting anyway lets the client retry, and the paint Lambda's conditional
    # `jobStatus = IN_PROGRESS` check guards against processing the job twice.
    try:
        trigger_guide_creation(job_id)
    except (ClientError, BotoCoreError):
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
