"""AWS Lambda handler for managing WebSocket disconnection and cleanup."""

import json
import os
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


def _find_job_id_by_connection_id(
    dynamodb_resource: Any,
    connection_id: str,
    jobs_table_name: str,
) -> str:
    """Query DynamoDB to find jobId by connectionId.

    Uses the Global Secondary Index (GSI) on connectionId to efficiently
    query the JOBS table for a job record where the connectionId attribute
    matches the provided connection_id. Returns the jobId if found.

    Args:
        dynamodb_resource: DynamoDB resource instance.
        connection_id: The WebSocket connection ID to search for.
        jobs_table_name: Name of the DynamoDB JOBS table.

    Returns:
        The jobId associated with the given connectionId.

    Raises:
        ValueError: If no job is found, multiple jobs are found, or jobId
            is missing from the job record.
    """
    table = dynamodb_resource.Table(jobs_table_name)
    response = table.query(
        IndexName="connectionId-index",
        KeyConditionExpression="connectionId = :conn_id",
        ExpressionAttributeValues={":conn_id": connection_id},
    )
    log = StructuredLoggerAdapter(
        logger,
        {"connectionId": connection_id or "unknown", "stage": "parse_input"},
    )
    items = response.get("Items", [])
    if not items:
        log.warning("No job found for connectionId")
        raise ValueError("No job found for this connection")
    if len(items) > 1:
        log.warning("Multiple jobs found for connectionId")
        raise ValueError("Multiple jobs found for this connection")
    job_id = items[0].get("jobId")
    if not job_id:
        log.warning("Job found but jobId is missing for connectionId")
        raise ValueError("jobId is missing from job record")

    return job_id




def _remove_connection_from_dynamodb(
    dynamodb_resource: Any,
    job_id: str,
    jobs_table_name: str,
) -> None:
    """Remove connection metadata from job record in DynamoDB.

    Clears connectionId and related connection-tracking attributes from
    the job's item in JOBS_TABLE_NAME, identified by jobId. Ensures the
    job record exists before attempting removal.

    Args:
        dynamodb_resource: DynamoDB resource instance.
        job_id: Job ID associated with the connection.
        jobs_table_name: Name of the DynamoDB JOBS table.

    Raises:
        ClientError: If DynamoDB update_item fails or job doesn't exist.
    """
    table = dynamodb_resource.Table(jobs_table_name)
    table.update_item(
        Key={"jobId": job_id},
        UpdateExpression="REMOVE connectionId, connectedAt",
        ConditionExpression="attribute_exists(jobId)",
    )


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """Handle WebSocket disconnection and cleanup.

    Extracts connectionId from the WebSocket event, queries DynamoDB to
    find the associated jobId, and removes connection metadata from the
    job record.

    Args:
        event: Lambda event from API Gateway WebSocket $disconnect route.
        context: Lambda context (unused).

    Returns:
        API Gateway response dict with statusCode, headers, and JSON body.
    """
    del context

    request_context = event.get("requestContext") or {}
    connection_id = request_context.get("connectionId")

    if not connection_id:
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

    dynamodb = boto3.resource("dynamodb")  # type: ignore[arg-type]

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

    try:
        job_id = _find_job_id_by_connection_id(
            dynamodb,
            connection_id,
            jobs_table_name,
        )
    except ValueError:
        error_log = StructuredLoggerAdapter(
            logger,
            {"connectionId": connection_id, "stage": "error"},
        )
        error_log.warning("No active job found for this connection")
        return _response(
            404,
            {
                "error": "NotFound",
                "message": "No active job found for this connection",
            },
        )
    except ClientError as e:
        error_log = StructuredLoggerAdapter(
            logger,
            {"connectionId": connection_id, "stage": "error"},
        )
        error_log.error(f"Failed to query jobs table: {str(e)}")
        return _response(
            500,
            {
                "error": "DatabaseError",
                "message": "Failed to query jobs table",
            },
        )

    try:
        _remove_connection_from_dynamodb(
            dynamodb,
            job_id,
            jobs_table_name,
        )
    except ClientError as e:
        error_log = StructuredLoggerAdapter(
            logger,
            {"jobId": job_id, "connectionId": connection_id,
             "stage": "error"},
        )
        error_code = e.response.get("Error", {}).get("Code")
        if error_code == "ConditionalCheckFailedException":
            error_log.info(f"Job no longer exists: {job_id}")
            return _response(
                400,
                {
                    "error": "NotFound",
                    "message": "Job not found",
                },
            )
        error_log.error(f"Failed to remove connection: {str(e)}")
        return _response(
            500,
            {
                "error": "DatabaseError",
                "message": "Failed to remove connection",
            },
        )
    log = StructuredLoggerAdapter(
        logger, {"jobId": job_id, "connectionId": connection_id,
                 "stage": "remove_connection"}
    )
    log.info("Connection closed successfully")
    return _response(
        200,
        {
            "message": "Connection closed successfully",
        },
    )
