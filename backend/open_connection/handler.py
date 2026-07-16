"""AWS Lambda handler for managing WebSocket connection lifecycle.

Authenticates users via AWS Cognito JWT tokens and manages WebSocket
connection metadata in DynamoDB.
"""

import json
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import boto3
import requests
from botocore.exceptions import ClientError
from jose import JWTError, jwt
from jose.exceptions import JWTClaimsError

from logging_config import StructuredLoggerAdapter, configure_logger

logger = configure_logger(__name__)

# Cache for JWKS keys (kid -> key)
_JWKS_CACHE: Dict[str, Any] = {}
_JWKS_CACHE_TIME: float = 0
_JWKS_CACHE_TTL: int = 3600  # 1 hour


class CognitoJWTError(Exception):
    """Raised when Cognito JWT token validation fails."""

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


def _extract_jwt_from_query_params(event: Dict[str, Any]) -> str:
    """Extract JWT token from query string parameters.

    Args:
        event: Lambda event from API Gateway WebSocket.

    Returns:
        JWT token string.

    Raises:
        CognitoJWTError: If token query parameter is missing or empty.
    """
    query_params = event.get("queryStringParameters") or {}
    token = query_params.get("token") or ""

    if not token:
        raise CognitoJWTError("Missing token query parameter")

    return token


def _fetch_cognito_jwks(region: str, user_pool_id: str) -> Dict[str, Any]:
    """Fetch Cognito JWKS from the public endpoint.

    Implements simple in-memory caching with TTL to reduce requests to the
    Cognito endpoint while allowing key rotation.

    Args:
        region: AWS region (e.g., 'eu-central-1').
        user_pool_id: Cognito user pool ID.

    Returns:
        Dictionary with 'keys' array from Cognito JWKS endpoint.

    Raises:
        CognitoJWTError: If JWKS fetch or parsing fails.
    """
    global _JWKS_CACHE, _JWKS_CACHE_TIME

    current_time = time.time()
    # Return cached keys if they're still valid
    if _JWKS_CACHE and (current_time - _JWKS_CACHE_TIME) < _JWKS_CACHE_TTL:
        return _JWKS_CACHE

    jwks_url = (
        f"https://cognito-idp.{region}.amazonaws.com/{user_pool_id}/"
        ".well-known/jwks.json"
    )

    try:
        response = requests.get(jwks_url, timeout=5)
        response.raise_for_status()
        jwks = response.json()

        _JWKS_CACHE = jwks
        _JWKS_CACHE_TIME = current_time

        return jwks
    except requests.RequestException as e:
        raise CognitoJWTError(
            f"Failed to fetch Cognito JWKS from {jwks_url}: {str(e)}"
        )
    except (ValueError, json.JSONDecodeError) as e:
        raise CognitoJWTError(f"Invalid JSON response from Cognito JWKS: {str(e)}")


def _get_cognito_public_key(token: str, region: str, user_pool_id: str) -> str:
    """Extract the public key from Cognito JWKS matching the token's kid.

    Args:
        token: JWT token string.
        region: AWS region.
        user_pool_id: Cognito user pool ID.

    Returns:
        Public key in PEM format.

    Raises:
        CognitoJWTError: If token header is invalid or matching key not found.
    """
    try:
        # Get token header without verification to extract kid
        header = jwt.get_unverified_header(token)
        kid = header.get("kid")

        if not kid:
            raise CognitoJWTError("JWT token missing 'kid' (key ID) in header")

        # Fetch JWKS
        jwks = _fetch_cognito_jwks(region, user_pool_id)
        keys = jwks.get("keys", [])

        # Find the key matching the token's kid
        for key in keys:
            if key.get("kid") == kid:
                # Construct public key in PEM format for python-jose
                # python-jose expects a dict with the key components
                return key

        raise CognitoJWTError(f"No matching key found for kid '{kid}' in JWKS")

    except JWTError as e:
        raise CognitoJWTError(f"Invalid JWT header: {str(e)}")


def _validate_cognito_jwt(
    token: str, region: str, user_pool_id: str
) -> Dict[str, Any]:
    """Validate and decode a Cognito JWT token.

    Validates:
    - Signature using RS256 algorithm and Cognito JWKS
    - Issuer matches the Cognito user pool
    - Token is not expired (exp claim)
    - Token type is 'id' token

    Args:
        token: JWT token string.
        region: AWS region.
        user_pool_id: Cognito user pool ID.

    Returns:
        Decoded JWT claims as a dictionary.

    Raises:
        CognitoJWTError: If token is invalid, expired, or validation fails.
    """
    try:
        # Get the public key from JWKS
        public_key = _get_cognito_public_key(token, region, user_pool_id)

        # Expected issuer
        expected_issuer = (
            f"https://cognito-idp.{region}.amazonaws.com/{user_pool_id}"
        )

        # Decode and validate token
        # python-jose's jwt.decode expects the key in a specific format for RS256
        decoded = jwt.decode(
            token,
            public_key,
            algorithms=["RS256"],
            options={"verify_aud": False},  # Don't validate audience (varies by client)
        )

        # Validate issuer
        issuer = decoded.get("iss")
        if issuer != expected_issuer:
            raise CognitoJWTError(
                f"Invalid issuer '{issuer}'. Expected '{expected_issuer}'"
            )

        # Validate token type (should be 'id' token)
        token_type = decoded.get("token_use")
        if token_type != "id":
            raise CognitoJWTError(
                f"Invalid token type '{token_type}'. Expected 'id' token"
            )

        # Validate expiration (exp claim must be in future)
        exp = decoded.get("exp")
        if not exp:
            raise CognitoJWTError("Missing 'exp' (expiration) claim in token")

        current_time = int(time.time())
        if current_time > exp:
            raise CognitoJWTError(
                f"Token expired at {datetime.fromtimestamp(exp, tz=timezone.utc).isoformat()}"
            )

        return decoded

    except CognitoJWTError:
        raise
    except JWTClaimsError as e:
        raise CognitoJWTError(f"Invalid token claims: {str(e)}")
    except JWTError as e:
        raise CognitoJWTError(f"Invalid JWT token: {str(e)}")
    except Exception as e:
        raise CognitoJWTError(f"Failed to validate JWT token: {str(e)}")


def _extract_user_info(event: Dict[str, Any]) -> Dict[str, str]:
    """Extract user information from Cognito JWT token in query parameters.

    Extracts the JWT token from the 'token' query parameter, validates it
    against Cognito JWKS, and returns the userId (from 'sub' claim) and email.

    The 'sub' (subject) claim is required and must be non-empty; it represents
    the user ID and is critical for user tracking.

    Args:
        event: Lambda event from API Gateway WebSocket.

    Returns:
        Dictionary with 'userId' and 'email' keys extracted from JWT claims.

    Raises:
        CognitoJWTError: If JWT token is missing, invalid, or validation fails,
                        or if the 'sub' claim is missing or empty.
    """
    # Extract environment variables
    cognito_region = os.getenv("COGNITO_REGION")
    cognito_user_pool_id = os.getenv("COGNITO_USER_POOL_ID")

    if not cognito_region:
        raise CognitoJWTError("Missing COGNITO_REGION environment variable")
    if not cognito_user_pool_id:
        raise CognitoJWTError("Missing COGNITO_USER_POOL_ID environment variable")

    # Extract and validate JWT token
    token = _extract_jwt_from_query_params(event)
    claims = _validate_cognito_jwt(token, cognito_region, cognito_user_pool_id)

    # Validate that 'sub' claim is present and non-empty (required for user tracking)
    user_id = claims.get("sub", "").strip()
    if not user_id:
        raise CognitoJWTError("JWT 'sub' claim (user ID) is required and cannot be empty")

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

    Validates jobId exists in JOBS table, authenticates user via Cognito JWT,
    stores connection metadata in DynamoDB, and returns appropriate success/error
    responses.

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
    except CognitoJWTError as e:
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
