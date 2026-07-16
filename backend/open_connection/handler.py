"""AWS Lambda handler for managing WebSocket connection lifecycle."""

import json
import os
from datetime import datetime, timezone
from typing import Any, Dict

import boto3
import jwt
from botocore.exceptions import ClientError

from logging_config import StructuredLoggerAdapter, configure_logger

logger = configure_logger(__name__)


class JWTError(Exception):
    """Raised when JWT token extraction or decoding fails."""

    pass


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


def _log_error(job_id: str, message: str) -> None:
    """Log an error with structured logging context.

    Args:
        job_id: Job ID for correlation.
        message: Error message to log.
    """
    error_log = StructuredLoggerAdapter(logger, {"jobId": job_id, "stage": "error"})
    error_log.error(message)


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


def _extract_jwt_from_header(event: Dict[str, Any]) -> str:
    """Extract JWT token from Authorization header.

    Args:
        event: Lambda event from API Gateway WebSocket.

    Returns:
        JWT token string.

    Raises:
        JWTError: If Authorization header is missing or invalid.
    """
    headers = event.get("headers") or {}
    auth_header = headers.get("Authorization") or ""

    if not auth_header:
        raise JWTError("Missing Authorization header")

    parts = auth_header.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise JWTError("Invalid Authorization header format")

    return parts[1]


def _decode_jwt_token(token: str, jwt_secret: str | None) -> Dict[str, Any]:
    """Decode and verify JWT token.

    Supports both HS256 (symmetric) and RS256 (asymmetric) algorithms.
    Algorithm is extracted from the JWT header.

    Args:
        token: JWT token string.
        jwt_secret: Secret key for verification, or None to skip verification.

    Returns:
        Decoded JWT claims as a dictionary.

    Raises:
        JWTError: If JWT_SECRET_KEY is required but missing in production,
                  if token is invalid, expired, or cannot be decoded.
    """
    try:
        # Check if JWT_SECRET_KEY is required in production
        if not jwt_secret:
            env = os.getenv("ENVIRONMENT", "development")
            if env == "production":
                raise JWTError(
                    "JWT_SECRET_KEY environment variable is required in production"
                )
            logger.warning(
                "JWT signature verification disabled — JWT_SECRET_KEY not configured"
            )

        # Extract algorithm from JWT header to support HS256 and RS256
        # Default to HS256 if header extraction fails (will be caught by decode error)
        try:
            header = jwt.get_unverified_header(token)
            algorithm = header.get("alg", "HS256")
        except (jwt.DecodeError, jwt.InvalidTokenError):
            # Token is malformed; let decode() handle it
            algorithm = "HS256"

        # Decode with or without signature verification
        if jwt_secret:
            decoded = jwt.decode(token, jwt_secret, algorithms=[algorithm])
        else:
            decoded = jwt.decode(
                token, options={"verify_signature": False}, algorithms=[algorithm]
            )
        return decoded
    except jwt.InvalidTokenError as e:
        raise JWTError(f"Invalid JWT token: {str(e)}")
    except JWTError:
        raise
    except Exception as e:
        raise JWTError(f"Failed to decode JWT token: {str(e)}")


def _extract_user_info(event: Dict[str, Any]) -> Dict[str, str]:
    """Extract user information from JWT token in Authorization header.

    Extracts the JWT token from the Authorization: Bearer header, decodes it,
    and returns the userId (from 'sub' claim) and email. Supports verification
    using JWT_SECRET_KEY environment variable (optional).

    The 'sub' (subject) claim is required and must be non-empty; it represents
    the user ID and is critical for user tracking.

    Args:
        event: Lambda event from API Gateway WebSocket.

    Returns:
        Dictionary with 'userId' and 'email' keys extracted from JWT claims.

    Raises:
        JWTError: If JWT token is missing, malformed, cannot be decoded,
                  or if the 'sub' claim is missing or empty.
    """
    token = _extract_jwt_from_header(event)
    jwt_secret = os.getenv("JWT_SECRET_KEY")
    claims = _decode_jwt_token(token, jwt_secret)

    # Validate that 'sub' claim is present and non-empty (required for user tracking)
    user_id = claims.get("sub", "").strip()
    if not user_id:
        raise JWTError("JWT 'sub' claim (user ID) is required and cannot be empty")

    return {
        "userId": user_id,
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

    log = StructuredLoggerAdapter(logger, {"jobId": job_id, "stage": "extract_user"})
    try:
        user_info = _extract_user_info(event or {})
    except JWTError as e:
        log.info(f"JWT authentication failed: {str(e)}")
        return _response(401, {
            "error": "Unauthorized",
            "message": "Invalid or missing JWT token",
        })

    jobs_table_name = os.getenv("JOBS_TABLE_NAME")
    if not jobs_table_name:
        _log_error(job_id, "Missing JOBS_TABLE_NAME env var")
        return _response(500, {
            "error": "ServerConfiguration",
            "message": "Failed to initialize handler",
        })

    log = StructuredLoggerAdapter(logger, {"jobId": job_id, "stage": "validate_job"})
    try:
        job_exists = _job_exists_in_dynamodb(dynamodb, job_id, jobs_table_name)
    except ClientError as e:
        _log_error(job_id, f"Failed to query JOBS table: {str(e)}")
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
        _log_error(job_id, f"Failed to store connection: {str(e)}")
        return _response(500, {
            "error": "DatabaseError",
            "message": "Failed to store connection",
        })

    log.info("Connection established successfully")
    return _response(200, {
        "message": "Connection established successfully",
    })
