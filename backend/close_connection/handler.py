"""AWS Lambda handler for WebSocket disconnection cleanup.

Finds the job that owns a disconnecting WebSocket connection and clears the connection
attributes from that job record. Disconnects are idempotent: a connection with no job, or whose
job has since been attached to a newer connection, is a successful no-op.
"""

import json
import os
from typing import Any, Dict

import boto3
from botocore.exceptions import ClientError

from logging_config import StructuredLoggerAdapter, configure_logger

logger = configure_logger(__name__)

# GSI on the JOBS table with connectionId as its partition key. Required for the lookup below;
# a full table scan per disconnect would not scale.
CONNECTION_ID_INDEX_NAME = "connectionId-index"


class JobNotFoundError(Exception):
    """Raised when no job record is associated with a WebSocket connectionId."""


class ConnectionIndexIntegrityError(Exception):
    """Raised when the connectionId index does not resolve to exactly one usable job record.

    Covers both several jobs sharing one connectionId and a matching record without a jobId.
    Both indicate corrupted connection bookkeeping, so the caller reports a server error.
    """


def _response(status_code: int, body: Dict[str, Any]) -> Dict[str, Any]:
    """Build an API-Gateway-style JSON response.

    Args:
        status_code: HTTP status code.
        body: Response body as a dictionary.

    Returns:
        API Gateway response dict with statusCode, headers, and body as JSON string.
    """
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }


def _find_job_id_by_connection_id(
    dynamodb_resource: Any,
    connection_id: str,
    jobs_table_name: str,
) -> str:
    """Resolve the jobId that currently owns a WebSocket connection.

    Queries the `connectionId-index` GSI. The GSI is eventually consistent, so the result can
    briefly lag a concurrent reconnect; the conditional REMOVE in `_remove_connection_from_dynamodb`
    guards against acting on such a stale result.

    Args:
        dynamodb_resource: DynamoDB resource instance.
        connection_id: The WebSocket connection ID to search for.
        jobs_table_name: Name of the DynamoDB JOBS table.

    Returns:
        The jobId of the single job record holding this connectionId.

    Raises:
        JobNotFoundError: If no job record holds this connectionId.
        ConnectionIndexIntegrityError: If several records hold it, or the matching record has no jobId.
        ClientError: If the DynamoDB query fails.
    """
    table = dynamodb_resource.Table(jobs_table_name)
    response = table.query(
        IndexName=CONNECTION_ID_INDEX_NAME,
        KeyConditionExpression="connectionId = :conn_id",
        ExpressionAttributeValues={":conn_id": connection_id},
    )
    items = response.get("Items", [])
    if not items:
        raise JobNotFoundError(f"No job holds connectionId {connection_id}")
    if len(items) > 1:
        raise ConnectionIndexIntegrityError(
            f"{len(items)} jobs hold connectionId {connection_id}"
        )
    job_id = items[0].get("jobId")
    if not job_id:
        raise ConnectionIndexIntegrityError(
            f"Job holding connectionId {connection_id} has no jobId"
        )
    return job_id


def _is_conditional_check_failed(error: ClientError) -> bool:
    """Tell whether a DynamoDB ClientError is a failed ConditionExpression.

    Args:
        error: The ClientError raised by a DynamoDB write.

    Returns:
        True if the error code is ConditionalCheckFailedException, False otherwise.
    """
    return error.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException"


def _remove_connection_from_dynamodb(
    dynamodb_resource: Any,
    job_id: str,
    connection_id: str,
    jobs_table_name: str,
) -> None:
    """Clear connection attributes from a job record, only if it still holds this connection.

    The condition prevents a stale $disconnect from erasing the connection of a newer reconnect
    to the same job.

    Args:
        dynamodb_resource: DynamoDB resource instance.
        job_id: Job ID whose connection attributes are removed.
        connection_id: The disconnecting connection ID; must match the stored connectionId.
        jobs_table_name: Name of the DynamoDB JOBS table.

    Raises:
        ClientError: If update_item fails. A ConditionalCheckFailedException means the job no
            longer holds this connection and the caller should treat the disconnect as complete.
    """
    table = dynamodb_resource.Table(jobs_table_name)
    table.update_item(
        Key={"jobId": job_id},
        UpdateExpression="REMOVE connectionId, connectedAt",
        ConditionExpression="connectionId = :connectionId",
        ExpressionAttributeValues={":connectionId": connection_id},
    )


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """Handle WebSocket disconnection and cleanup.

    Resolves the job owning the connectionId and clears its connection attributes. Returns 200
    for every well-formed disconnect, including ones with nothing left to clean up, because
    API Gateway ignores the $disconnect response and retries would not change the outcome.

    Args:
        event: Lambda event from API Gateway WebSocket $disconnect route.
        context: Lambda context (unused).

    Returns:
        API Gateway response dict with statusCode, headers, and JSON body. 400 for a missing
        connectionId, 500 for missing configuration, corrupted index data or DynamoDB errors,
        otherwise 200.
    """
    del context

    request_context = (event or {}).get("requestContext") or {}
    connection_id = request_context.get("connectionId")

    if not connection_id or not isinstance(connection_id, str) or not connection_id.strip():
        error_log = StructuredLoggerAdapter(
            logger, {"connectionId": "unknown", "stage": "parse_input"}
        )
        error_log.warning("Missing or empty connectionId")
        return _response(
            400,
            {
                "error": "InvalidRequest",
                "message": "connectionId is required",
            },
        )

    log = StructuredLoggerAdapter(
        logger,
        {"connectionId": connection_id, "stage": "parse_input"},
    )
    log.info("WebSocket disconnection initiated")

    jobs_table_name = os.getenv("JOBS_TABLE_NAME")
    if not jobs_table_name:
        error_log = StructuredLoggerAdapter(
            logger, {"connectionId": connection_id, "stage": "error"}
        )
        error_log.error("Missing JOBS_TABLE_NAME env var")
        return _response(
            500,
            {
                "error": "ServerConfiguration",
                "message": "Failed to initialize handler",
            },
        )

    dynamodb = boto3.resource("dynamodb")  # type: ignore[arg-type]

    try:
        job_id = _find_job_id_by_connection_id(
            dynamodb,
            connection_id,
            jobs_table_name,
        )
    except JobNotFoundError:
        log.info("No job holds this connection; nothing to clean up")
        return _response(
            200,
            {
                "message": "No active job found for this connection",
            },
        )
    except ConnectionIndexIntegrityError as e:
        error_log = StructuredLoggerAdapter(
            logger, {"connectionId": connection_id, "stage": "error"}
        )
        error_log.error(f"Connection index integrity error: {str(e)}")
        return _response(
            500,
            {
                "error": "DatabaseError",
                "message": "Failed to resolve job for connection",
            },
        )
    except ClientError as e:
        error_log = StructuredLoggerAdapter(
            logger, {"connectionId": connection_id, "stage": "error"}
        )
        error_log.error(f"Failed to query jobs table: {str(e)}")
        return _response(
            500,
            {
                "error": "DatabaseError",
                "message": "Failed to query jobs table",
            },
        )

    log = StructuredLoggerAdapter(
        logger,
        {"jobId": job_id, "connectionId": connection_id, "stage": "remove_connection"},
    )
    try:
        _remove_connection_from_dynamodb(
            dynamodb,
            job_id,
            connection_id,
            jobs_table_name,
        )
    except ClientError as e:
        if _is_conditional_check_failed(e):
            log.info("Job no longer holds this connection; skipping removal")
            return _response(
                200,
                {
                    "message": "Connection already closed",
                },
            )
        log.error(f"Failed to remove connection: {str(e)}")
        return _response(
            500,
            {
                "error": "DatabaseError",
                "message": "Failed to remove connection",
            },
        )

    log.info("Connection closed successfully")
    return _response(
        200,
        {
            "message": "Connection closed successfully",
        },
    )
