"""AWS Lambda handler for managing WebSocket connection lifecycle."""

import json
import os
from datetime import datetime, timezone
from typing import Any, Dict

import boto3
from botocore.exceptions import ClientError

from logging_config import StructuredLoggerAdapter, configure_logger

logger = configure_logger(__name__)


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


def _extract_job_id(event: Dict[str, Any]) -> str | None:
    """Extract jobId from WebSocket event queryStringParameters.

    Args:
        event: Lambda event from API Gateway WebSocket.

    Returns:
        jobId string if present, None otherwise.
    """
    query_params = event.get("queryStringParameters") or {}
    return query_params.get("jobId")


def _extract_connection_id(event: Dict[str, Any]) -> str | None:
    """Extract connectionId from WebSocket event requestContext.

    Args:
        event: Lambda event from API Gateway WebSocket.

    Returns:
        connectionId string if present, None otherwise.
    """
    request_context = event.get("requestContext") or {}
    return request_context.get("connectionId")


def _extract_user_info(event: Dict[str, Any]) -> Dict[str, str]:
    """Extract user information from authorizer claims.

    Args:
        event: Lambda event from API Gateway WebSocket.

    Returns:
        Dictionary with 'userId' and 'email' keys, empty strings if not found.
    """
    request_context = event.get("requestContext") or {}
    authorizer = request_context.get("authorizer") or {}
    claims = authorizer.get("claims") or {}
    return {
        "userId": claims.get("sub", ""),
        "email": claims.get("email", ""),
    }


def _job_exists_in_dynamodb(
    dynamodb_resource: Any,
    job_id: str,
    table_name: str,
) -> bool:
    """Check if jobId exists in DynamoDB JOBS_TABLE.

    Args:
        dynamodb_resource: DynamoDB resource instance.
        job_id: The job ID to check.
        table_name: Name of the DynamoDB JOBS table.

    Returns:
        True if job exists, False otherwise.

    Raises:
        ClientError: If DynamoDB query fails.
    """
    table = dynamodb_resource.Table(table_name)
    response = table.get_item(Key={"jobId": job_id})
    return "Item" in response


def _store_connection_in_dynamodb(
    dynamodb_resource: Any,
    job_id: str,
    connection_id: str,
    user_info: Dict[str, str],
    jobs_table_name: str,
) -> None:
    """Store connection data in JOBS_TABLE_NAME.

    Connection metadata (connectionId, connectedAt, user info) is stored as
    attributes in the job's existing item, using jobId as the partition key.

    Args:
        dynamodb_resource: DynamoDB resource instance.
        job_id: Job ID associated with connection.
        connection_id: WebSocket connection ID.
        user_info: Dictionary with 'userId' and 'email' keys.
        jobs_table_name: Name of the DynamoDB JOBS table.

    Raises:
        ClientError: If DynamoDB update_item fails.
    """
    table = dynamodb_resource.Table(jobs_table_name)
    table.update_item(
        Key={"jobId": job_id},
        UpdateExpression="SET connectionId = :connectionId, connectedAt = :connectedAt, userId = :userId, email = :email",
        ExpressionAttributeValues={
            ":connectionId": connection_id,
            ":connectedAt": int(datetime.now(timezone.utc).timestamp()),
            ":userId": user_info.get("userId", ""),
            ":email": user_info.get("email", ""),
        }
    )


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """Handle WebSocket connection establishment.

    Validates jobId exists in JOBS table, stores connection metadata in
    DynamoDB, and returns appropriate success/error responses.

    Args:
        event: Lambda event from API Gateway WebSocket $connect route.
        context: Lambda context (unused).

    Returns:
        API Gateway response dict with statusCode, headers, and JSON body.
    """
    del context

    dynamodb = boto3.resource("dynamodb")  # type: ignore[arg-type]

    job_id = _extract_job_id(event or {})
    log = StructuredLoggerAdapter(logger, {"jobId": job_id or "unknown", "stage": "parse_input"})
    log.info("WebSocket connection attempt")

    if not job_id or not isinstance(job_id, str) or len(job_id.strip()) == 0:
        log.info("Missing or empty jobId")
        return _response(400, {
            "error": "InvalidRequest",
            "message": "jobId is required",
        })

    connection_id = _extract_connection_id(event or {})
    if not connection_id or not isinstance(connection_id, str) or len(connection_id.strip()) == 0:
        log.info("Missing or empty connectionId")
        return _response(400, {
            "error": "InvalidRequest",
            "message": "Connection ID is required",
        })

    jobs_table_name = os.getenv("JOBS_TABLE_NAME")
    if not jobs_table_name:
        error_log = StructuredLoggerAdapter(logger, {"jobId": job_id, "stage": "error"})
        error_log.error("Missing JOBS_TABLE_NAME env var")
        return _response(500, {
            "error": "ServerConfiguration",
            "message": "Failed to initialize handler",
        })

    log = StructuredLoggerAdapter(logger, {"jobId": job_id, "stage": "validate_job"})
    try:
        job_exists = _job_exists_in_dynamodb(dynamodb, job_id, jobs_table_name)
    except ClientError as e:
        error_log = StructuredLoggerAdapter(logger, {"jobId": job_id, "stage": "error"})
        error_log.error(f"Failed to query JOBS table: {str(e)}")
        return _response(500, {
            "error": "DatabaseError",
            "message": "Failed to query JOBS table",
        })

    if not job_exists:
        log.info(f"Job not found: {job_id}")
        return _response(404, {
            "error": "NotFound",
            "message": "jobId does not exist",
        })

    user_info = _extract_user_info(event or {})
    log = StructuredLoggerAdapter(logger, {"jobId": job_id, "stage": "store_connection"})

    try:
        _store_connection_in_dynamodb(
            dynamodb,
            job_id,
            connection_id,
            user_info,
            jobs_table_name,
        )
    except ClientError as e:
        error_log = StructuredLoggerAdapter(logger, {"jobId": job_id, "stage": "error"})
        error_log.error(f"Failed to store connection: {str(e)}")
        return _response(500, {
            "error": "DatabaseError",
            "message": "Failed to store connection",
        })

    log.info("Connection established successfully")
    return _response(200, {
        "message": "Connection established successfully",
    })
