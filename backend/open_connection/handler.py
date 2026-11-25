"""AWS Lambda handler for WebSocket connection establishment ($connect).

Authenticates the caller with a Cognito ID token, checks that the caller owns the requested job,
and records the WebSocket connection on the job item so later steps can push updates to it.
"""

import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict

import boto3
import requests
from botocore.exceptions import ClientError
from jose import JOSEError, JWTError, jwt
from jose.exceptions import JWTClaimsError

from logging_config import StructuredLoggerAdapter, configure_logger

logger = configure_logger(__name__)

# Cognito's JWKS changes only on key rotation, so an hour bounds JWKS calls per warm container.
_JWKS_CACHE: Dict[str, Any] = {}
_JWKS_CACHE_TIME: float = 0.0
_JWKS_CACHE_TTL: int = 3600  # 1 hour
# A token whose kid is not cached triggers one forced refresh, so a Cognito key rotation is picked up
# without waiting for the TTL. The minimum interval stops forged tokens with random kids from causing
# one Cognito request per invocation.
_JWKS_LAST_FORCED_REFRESH_TIME: float = 0.0
_JWKS_FORCED_REFRESH_MIN_INTERVAL: int = 60  # seconds
# Short timeout so a slow Cognito answer fails fast as 503 instead of holding the $connect call open.
JWKS_REQUEST_TIMEOUT_SECONDS: int = 5


class CognitoJWTError(Exception):
    """Raised when the presented token is missing or fails validation (mapped to 401)."""


class JwksUnavailableError(Exception):
    """Raised when Cognito's public keys cannot be fetched or parsed (mapped to 503)."""


class ServerConfigurationError(Exception):
    """Raised when a required environment variable is missing or blank (mapped to 500)."""


@dataclass(frozen=True)
class ConnectionConfig:
    """Environment configuration required to handle a $connect request.

    Attributes:
        cognito_region: AWS region of the Cognito user pool.
        cognito_user_pool_id: Cognito user pool ID; used for the issuer and JWKS URL.
        cognito_client_id: Cognito app client ID; the expected `aud` of the ID token.
        jobs_table_name: DynamoDB JOBS table name.
    """

    cognito_region: str
    cognito_user_pool_id: str
    cognito_client_id: str
    jobs_table_name: str


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


def _log_error(job_id: str, connection_id: str, message: str) -> None:
    """Log an error at ERROR level with jobId and connectionId correlation.

    Args:
        job_id: Job ID for correlation.
        connection_id: WebSocket connection ID for correlation.
        message: Error message to log. Must not contain tokens or credentials.
    """
    error_log = StructuredLoggerAdapter(
        logger,
        {"jobId": job_id, "connectionId": connection_id, "stage": "error"},
    )
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


def _read_required_env(name: str) -> str:
    """Read a required environment variable, rejecting unset and blank values.

    Args:
        name: Environment variable name.

    Returns:
        The stripped, non-empty value.

    Raises:
        ServerConfigurationError: If the variable is unset, empty or whitespace-only.
    """
    value = os.environ.get(name, "").strip()
    if not value:
        raise ServerConfigurationError(f"Missing {name} environment variable")
    return value


def _load_config() -> ConnectionConfig:
    """Read and validate all environment configuration for this invocation.

    Read at call time rather than import time so tests can control the environment.

    Returns:
        Validated ConnectionConfig.

    Raises:
        ServerConfigurationError: If any required environment variable is missing or blank.
    """
    return ConnectionConfig(
        cognito_region=_read_required_env("COGNITO_REGION"),
        cognito_user_pool_id=_read_required_env("COGNITO_USER_POOL_ID"),
        cognito_client_id=_read_required_env("COGNITO_CLIENT_ID"),
        jobs_table_name=_read_required_env("JOBS_TABLE_NAME"),
    )


def _is_forced_refresh_allowed(current_time: float) -> bool:
    """Tell whether a JWKS refresh forced by an unknown kid may hit Cognito now.

    Args:
        current_time: Current epoch time in seconds.

    Returns:
        True if at least _JWKS_FORCED_REFRESH_MIN_INTERVAL seconds have passed since the last forced refresh.
    """
    return (current_time - _JWKS_LAST_FORCED_REFRESH_TIME) >= _JWKS_FORCED_REFRESH_MIN_INTERVAL


def _fetch_cognito_jwks(
    region: str,
    user_pool_id: str,
    force_refresh: bool = False,
) -> Dict[str, Any]:
    """Fetch the Cognito JWKS, serving the in-memory cache while it is fresh.

    Args:
        region: AWS region (e.g., 'eu-central-1').
        user_pool_id: Cognito user pool ID.
        force_refresh: If True, bypass the cache and fetch now. Callers must check
            `_is_forced_refresh_allowed` first; this function does not rate-limit.

    Returns:
        Dictionary with a 'keys' list from the Cognito JWKS endpoint.

    Raises:
        JwksUnavailableError: If the request fails, the body is not JSON, or the payload has no 'keys' list.
    """
    global _JWKS_CACHE, _JWKS_CACHE_TIME, _JWKS_LAST_FORCED_REFRESH_TIME

    current_time = time.time()
    if not force_refresh and _JWKS_CACHE and (current_time - _JWKS_CACHE_TIME) < _JWKS_CACHE_TTL:
        return _JWKS_CACHE

    if force_refresh:
        # Recorded before the request so a failing endpoint is also rate-limited.
        _JWKS_LAST_FORCED_REFRESH_TIME = current_time

    jwks_url = (
        f"https://cognito-idp.{region}.amazonaws.com/{user_pool_id}/"
        ".well-known/jwks.json"
    )

    try:
        response = requests.get(jwks_url, timeout=JWKS_REQUEST_TIMEOUT_SECONDS)
        response.raise_for_status()
        jwks = response.json()
    except requests.RequestException as e:
        raise JwksUnavailableError(f"Failed to fetch Cognito JWKS from {jwks_url}: {e}") from e
    except ValueError as e:
        raise JwksUnavailableError(f"Invalid JSON in Cognito JWKS response: {e}") from e

    if not isinstance(jwks, dict) or not isinstance(jwks.get("keys"), list):
        raise JwksUnavailableError("Unexpected Cognito JWKS payload: 'keys' list missing")

    _JWKS_CACHE = jwks
    _JWKS_CACHE_TIME = current_time
    return jwks


def _reset_jwks_cache() -> None:
    """Clear the cached JWKS and the forced-refresh timestamp.

    Intended for tests, which must not share key material or rate-limit state between cases.
    """
    global _JWKS_CACHE, _JWKS_CACHE_TIME, _JWKS_LAST_FORCED_REFRESH_TIME

    _JWKS_CACHE = {}
    _JWKS_CACHE_TIME = 0.0
    _JWKS_LAST_FORCED_REFRESH_TIME = 0.0


def _find_jwk_by_kid(jwks: Dict[str, Any], kid: str) -> Dict[str, Any] | None:
    """Return the JWK whose `kid` matches, or None.

    Args:
        jwks: JWKS payload containing a 'keys' list.
        kid: Key ID from the token header.

    Returns:
        Matching JWK dict, or None if no key has this kid.
    """
    for key in jwks.get("keys", []):
        if isinstance(key, dict) and key.get("kid") == kid:
            return key
    return None


def _get_cognito_public_key(token: str, region: str, user_pool_id: str) -> Dict[str, Any]:
    """Return the JWK matching the token's kid, refreshing the JWKS once if the kid is unknown.

    Args:
        token: JWT token string.
        region: AWS region.
        user_pool_id: Cognito user pool ID.

    Returns:
        JWK dict usable as the key argument of `jwt.decode`.

    Raises:
        CognitoJWTError: If the header is malformed, has no kid, or no key matches even after the
            rate-limited refresh.
        JwksUnavailableError: If the JWKS fetch fails.
    """
    try:
        header = jwt.get_unverified_header(token)
    except JWTError as e:
        raise CognitoJWTError(f"Invalid JWT header: {e}") from e

    kid = header.get("kid")
    if not kid:
        raise CognitoJWTError("JWT token missing 'kid' (key ID) in header")

    key = _find_jwk_by_kid(_fetch_cognito_jwks(region, user_pool_id), kid)
    if key is None and _is_forced_refresh_allowed(time.time()):
        key = _find_jwk_by_kid(
            _fetch_cognito_jwks(region, user_pool_id, force_refresh=True),
            kid,
        )
    if key is None:
        raise CognitoJWTError(f"No matching key found for kid '{kid}' in JWKS")
    return key


def _validate_cognito_jwt(
    token: str,
    region: str,
    user_pool_id: str,
    client_id: str,
) -> Dict[str, Any]:
    """Validate and decode a Cognito ID token.

    Validates:
    - Signature (RS256) against the Cognito JWKS key matching the token's kid
    - Issuer (`iss`) equals the user pool's issuer URL (checked by jose)
    - Audience (`aud`) equals the app client ID (checked by jose; presence checked here,
      because jose skips the audience check when the claim is absent)
    - Expiry (`exp`) is present and not passed (checked by jose)
    - Token type (`token_use`) is 'id'

    Args:
        token: JWT token string.
        region: AWS region.
        user_pool_id: Cognito user pool ID.
        client_id: Cognito app client ID the token must have been issued to.

    Returns:
        Decoded and validated JWT claims.

    Raises:
        CognitoJWTError: If the token is invalid, expired, issued for another audience or issuer,
            or is not an ID token.
        JwksUnavailableError: If the JWKS needed for signature verification cannot be fetched.
    """
    public_key = _get_cognito_public_key(token, region, user_pool_id)
    expected_issuer = f"https://cognito-idp.{region}.amazonaws.com/{user_pool_id}"

    try:
        decoded = jwt.decode(
            token,
            public_key,
            algorithms=["RS256"],
            audience=client_id,
            issuer=expected_issuer,
        )
    except JWTClaimsError as e:
        raise CognitoJWTError(f"Invalid token claims: {e}") from e
    except JOSEError as e:
        # Includes ExpiredSignatureError, a JWTError subclass that jose raises for an expired token.
        raise CognitoJWTError(f"Invalid JWT token: {e}") from e

    if "aud" not in decoded:
        raise CognitoJWTError("Missing 'aud' (audience) claim in token")

    token_type = decoded.get("token_use")
    if token_type != "id":
        raise CognitoJWTError(f"Invalid token type '{token_type}'. Expected 'id' token")

    if "exp" not in decoded:
        raise CognitoJWTError("Missing 'exp' (expiration) claim in token")

    return decoded


def _extract_user_id(event: Dict[str, Any], config: ConnectionConfig) -> str:
    """Authenticate the request and return the caller's Cognito `sub`.

    The `sub` is the owner identifier stored on job records, so the value is returned unchanged
    (not stripped) to keep it byte-equal to the stored `userId`.

    Args:
        event: Lambda event from API Gateway WebSocket.
        config: Validated environment configuration.

    Returns:
        Non-blank `sub` claim of the validated ID token.

    Raises:
        CognitoJWTError: If the token is missing or invalid, or `sub` is missing or blank.
        JwksUnavailableError: If the JWKS needed for validation cannot be fetched.
    """
    token = _extract_jwt_from_query_params(event)
    claims = _validate_cognito_jwt(
        token,
        config.cognito_region,
        config.cognito_user_pool_id,
        config.cognito_client_id,
    )

    user_id = claims.get("sub")
    if not isinstance(user_id, str) or not user_id.strip():
        raise CognitoJWTError("JWT 'sub' claim (user ID) is required and cannot be empty")
    return user_id


def _is_conditional_check_failed(error: ClientError) -> bool:
    """Tell whether a DynamoDB ClientError is a failed ConditionExpression.

    Args:
        error: The ClientError raised by a DynamoDB write.

    Returns:
        True if the error code is ConditionalCheckFailedException, False otherwise.
    """
    return error.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException"


def _store_connection_in_dynamodb(
    dynamodb_resource: Any,
    job_id: str,
    connection_id: str,
    user_id: str,
    jobs_table_name: str,
) -> None:
    """Attach the WebSocket connection to an existing job owned by `user_id`.

    The update never creates an item: `attribute_exists(jobId)` rejects a deleted job, which
    would otherwise be recreated as a partial item. `userId = :userId` rejects a job owned by
    someone else. Both cases raise the same ConditionalCheckFailedException, so callers cannot
    tell them apart.

    Args:
        dynamodb_resource: DynamoDB resource instance.
        job_id: Job ID to attach the connection to.
        connection_id: WebSocket connection ID.
        user_id: Cognito `sub` of the caller, compared against the job's stored `userId`.
        jobs_table_name: Name of the DynamoDB JOBS table.

    Raises:
        ClientError: ConditionalCheckFailedException if the job is missing or not owned by
            `user_id`; any other code if the DynamoDB update fails.
    """
    table = dynamodb_resource.Table(jobs_table_name)
    table.update_item(
        Key={"jobId": job_id},
        UpdateExpression="SET connectionId = :connectionId, connectedAt = :connectedAt",
        ConditionExpression="attribute_exists(jobId) AND userId = :userId",
        ExpressionAttributeValues={
            ":connectionId": connection_id,
            ":connectedAt": int(datetime.now(timezone.utc).timestamp()),
            ":userId": user_id,
        },
    )


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """Handle WebSocket connection establishment.

    Order of checks: request shape (400), server configuration (500), token authentication
    (401 for a bad token, 503 if Cognito's keys are unreachable), then the conditional
    attach of the connection to a job the caller owns (404 if the job is missing or not owned,
    500 on DynamoDB failure).

    Args:
        event: Lambda event from API Gateway WebSocket $connect route.
        context: Lambda context (unused).

    Returns:
        API Gateway response dict with statusCode, headers, and JSON body.
    """
    del context
    event = event or {}

    job_id = _extract_job_id(event)
    connection_id = _extract_connection_id(event)
    log = StructuredLoggerAdapter(
        logger,
        {
            "jobId": job_id or "unknown",
            "connectionId": connection_id or "unknown",
            "stage": "parse_input",
        },
    )
    log.info("WebSocket connection attempt")

    if not job_id or not isinstance(job_id, str) or not job_id.strip():
        log.info("Missing or empty jobId")
        return _response(400, {
            "error": "InvalidRequest",
            "message": "jobId is required",
        })

    if not connection_id or not isinstance(connection_id, str) or not connection_id.strip():
        log.info("Missing or empty connectionId")
        return _response(400, {
            "error": "InvalidRequest",
            "message": "Connection ID is required",
        })

    log = StructuredLoggerAdapter(
        logger,
        {"jobId": job_id, "connectionId": connection_id, "stage": "authenticate"},
    )

    try:
        config = _load_config()
    except ServerConfigurationError as e:
        _log_error(job_id, connection_id, str(e))
        return _response(500, {
            "error": "ServerConfiguration",
            "message": "Failed to initialize handler",
        })

    try:
        user_id = _extract_user_id(event, config)
    except CognitoJWTError as e:
        log.info(f"JWT authentication failed: {str(e)}")
        return _response(401, {
            "error": "Unauthorized",
            "message": "Invalid or missing JWT token",
        })
    except JwksUnavailableError as e:
        _log_error(job_id, connection_id, f"Cognito JWKS unavailable: {str(e)}")
        return _response(503, {
            "error": "ServiceUnavailable",
            "message": "Authentication is temporarily unavailable",
        })

    log = StructuredLoggerAdapter(
        logger,
        {"jobId": job_id, "connectionId": connection_id, "stage": "store_connection"},
    )
    dynamodb = boto3.resource("dynamodb")  # type: ignore[arg-type]

    try:
        _store_connection_in_dynamodb(
            dynamodb,
            job_id,
            connection_id,
            user_id,
            config.jobs_table_name,
        )
    except ClientError as e:
        if _is_conditional_check_failed(e):
            # Same response for a missing job and for a job owned by someone else, so the
            # endpoint does not reveal which jobIds exist.
            log.info("Job not found or not owned by caller")
            return _response(404, {
                "error": "NotFound",
                "message": "jobId does not exist",
            })
        _log_error(job_id, connection_id, f"Failed to store connection: {str(e)}")
        return _response(500, {
            "error": "DatabaseError",
            "message": "Failed to store connection",
        })

    log.info("Connection established successfully")
    return _response(200, {
        "message": "Connection established successfully",
    })
