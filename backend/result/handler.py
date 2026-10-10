"""AWS Lambda handler for the result Lambda.

Triggered by SQS when the bedrock Lambda finishes a job. Reads the jobId and status from
the message, generates presigned download URLs for the result files in the paint S3
bucket, looks up the job's WebSocket connectionId in DynamoDB, and pushes the result to
the client through the API Gateway WebSocket management API.
"""
import json
import os
from typing import Any, Dict, Optional, Tuple

import boto3
from botocore.exceptions import ClientError

from logging_config import configure_logger, StructuredLoggerAdapter

logger = configure_logger(__name__)

COMPLETED_STATUS = "COMPLETED"
RESULT_JSON_NAME = "result.json"
RESULT_IMAGE_NAME = "image_0.png"
PRESIGNED_URL_TTL_SECONDS = 3600


def parse_message(event: Dict[str, Any]) -> Optional[Tuple[str, str]]:
    """Parse and validate `jobId` and `status` from the first SQS record.

    Args:
        event: Lambda event dict from an SQS trigger.

    Returns:
        Tuple `(job_id, status)` with trimmed non-empty strings, or `None` if the
        records are missing, the body is not valid JSON, or either field is blank.
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
    status = body.get("status")
    if not (isinstance(job_id, str) and job_id.strip()):
        return None
    if not (isinstance(status, str) and status.strip()):
        return None

    return job_id.strip(), status.strip()


def generate_result_urls(job_id: str) -> Dict[str, str]:
    """Generate presigned GET URLs for the bedrock result files of a job.

    Files are expected under `painted_images/<job_id>/` in `PAINT_BUCKET_NAME`.

    Args:
        job_id: The job id used as the S3 key prefix.

    Returns:
        Dict with `resultUrl` (result.json) and `imageUrl` (image_0.png).

    Raises:
        botocore.exceptions.ClientError: Propagated on presigned URL generation failure.
        KeyError: If `PAINT_BUCKET_NAME` environment variable is not set.
    """
    bucket_name = os.environ["PAINT_BUCKET_NAME"]
    s3_client = boto3.client("s3")
    prefix = f"painted_images/{job_id}"

    def _presign(file_name: str) -> str:
        return s3_client.generate_presigned_url(
            "get_object",
            Params={"Bucket": bucket_name, "Key": f"{prefix}/{file_name}"},
            ExpiresIn=PRESIGNED_URL_TTL_SECONDS,
        )

    return {"resultUrl": _presign(RESULT_JSON_NAME), "imageUrl": _presign(RESULT_IMAGE_NAME)}


def get_connection_id(job_id: str) -> Optional[str]:
    """Look up the WebSocket connectionId stored on the job record.

    Args:
        job_id: The job id to look up.

    Returns:
        The connectionId, or `None` if the job has no connection attached
        (e.g. the client already disconnected and close_connection removed it).

    Raises:
        botocore.exceptions.ClientError: Propagated on any DynamoDB error.
        KeyError: If `JOBS_TABLE_NAME` is not set, or the job does not exist.
    """
    table_name = os.environ["JOBS_TABLE_NAME"]
    dynamodb_client = boto3.client("dynamodb")

    response = dynamodb_client.get_item(
        TableName=table_name,
        Key={"jobId": {"S": job_id}},
    )
    if "Item" not in response:
        raise KeyError(f"Job {job_id} not found in DynamoDB")

    connection_id = response["Item"].get("connectionId", {}).get("S")
    return connection_id or None


def _to_management_endpoint(ws_base_url: str) -> str:
    """Convert a client-facing `wss://` base URL to the `https://` management endpoint.

    `VITE_WS_BASE_URL` is shared with the frontend, which connects over `wss://`, while
    the API Gateway management API is called over HTTPS on the same host and stage.
    """
    base = ws_base_url.strip().rstrip("/")
    if base.startswith("wss://"):
        return "https://" + base[len("wss://"):]
    if base.startswith("ws://"):
        return "http://" + base[len("ws://"):]
    return base


def send_to_connection(connection_id: str, message: Dict[str, Any]) -> bool:
    """Post a JSON message to a WebSocket connection.

    Args:
        connection_id: Target API Gateway WebSocket connection id.
        message: JSON-serializable payload.

    Returns:
        `True` if delivered, `False` if the connection no longer exists (HTTP 410).

    Raises:
        botocore.exceptions.ClientError: Propagated on any other API error.
        KeyError: If `VITE_WS_BASE_URL` environment variable is not set.
    """
    endpoint_url = _to_management_endpoint(os.environ["VITE_WS_BASE_URL"])
    client = boto3.client("apigatewaymanagementapi", endpoint_url=endpoint_url)

    try:
        client.post_to_connection(
            ConnectionId=connection_id,
            Data=json.dumps(message).encode("utf-8"),
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "GoneException":
            return False
        raise
    return True


def lambda_handler(event: Dict[str, Any], context: Any) -> None:
    """Deliver a finished job's result to the client over WebSocket.

    Triggered by SQS. For `COMPLETED` jobs the message contains presigned URLs for
    `result.json` and `image_0.png`; for any other status only `jobId` and `status`
    are sent. If the job has no live connection the result is dropped with a warning
    and the message is acknowledged.

    Raises `ValueError` for an unparseable message or unknown job, and re-raises AWS
    errors, so SQS retries / routes the message to the dead-letter queue.

    Args:
        event: SQS Lambda event with `Records[0].body` containing
            `{"jobId": "<uuid>", "status": "COMPLETED"}`.
        context: Lambda context object (unused).
    """
    del context

    parsed = parse_message(event)
    if parsed is None:
        logger.error(
            "Failed to parse jobId/status from SQS event",
            extra={"jobId": "unknown", "stage": "parse_input"},
        )
        raise ValueError("Missing or invalid jobId/status in SQS message")
    job_id, status = parsed

    log = StructuredLoggerAdapter(logger, {"jobId": job_id, "stage": "orchestrate"})
    log.info("Delivering job result, status=%s", status)

    try:
        connection_id = get_connection_id(job_id)
    except KeyError as exc:
        log.error("Cannot look up connection: %s", exc, extra={"stage": "get_connection"})
        raise ValueError(f"Job {job_id} not found") from exc
    except ClientError as exc:
        log.error("DynamoDB error: %s", exc, extra={"stage": "get_connection"})
        raise

    if connection_id is None:
        log.warning("No WebSocket connection for job; skipping", extra={"stage": "get_connection"})
        return

    message: Dict[str, Any] = {"jobId": job_id, "status": status}
    if status == COMPLETED_STATUS:
        try:
            message.update(generate_result_urls(job_id))
        except ClientError as exc:
            log.error("Failed to presign result URLs: %s", exc, extra={"stage": "presign"})
            raise

    try:
        delivered = send_to_connection(connection_id, message)
    except ClientError as exc:
        log.error("Failed to post to connection: %s", exc, extra={"stage": "send"})
        raise

    if not delivered:
        log.warning("WebSocket connection is gone; result not delivered", extra={"stage": "send"})
        return

    log.info("Result delivered to WebSocket connection", extra={"stage": "complete"})
