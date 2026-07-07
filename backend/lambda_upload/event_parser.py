"""Event parsing and validation utilities for the upload handler."""

import base64
import binascii
import json
import logging
import os
import re
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# S3 presigned URLs cannot be valid for longer than 7 days.
MAX_EXPIRES_IN_SECONDS = 604800
SAFE_FILE_NAME_PATTERN = re.compile(r"[^A-Za-z0-9._-]")
MAX_FILE_NAME_LENGTH = 255


def parse_expires_in(raw_value: str) -> Optional[int]:
    """Parse and bound-check UPLOAD_URL_EXPIRES_SECONDS; return None if invalid."""
    try:
        expires_in = int(raw_value)
    except ValueError:
        return None

    if expires_in <= 0 or expires_in > MAX_EXPIRES_IN_SECONDS:
        return None

    return expires_in


def sanitize_file_name(file_name: str, index: int) -> str:
    """Sanitize a user-supplied file name for safe use in an S3 key."""
    fallback = f"file_{index + 1}.bin"

    base_name = os.path.basename(file_name.replace("\\", "/"))
    if base_name in ("", ".", ".."):
        return fallback

    sanitized = SAFE_FILE_NAME_PATTERN.sub("_", base_name)[:MAX_FILE_NAME_LENGTH]
    if sanitized in ("", ".", "..") or all(c == "_" for c in sanitized):
        return fallback

    return sanitized


def normalize_to_list(value: Any) -> List[str]:
    """Coerce a query/body value (list, scalar, or comma-separated string) to a list of strings."""
    if value is None:
        return []

    if isinstance(value, list):
        return [str(item) for item in value if item]

    if isinstance(value, str):
        parts = [part.strip() for part in value.split(",")]
        return [part for part in parts if part]

    return [str(value)]


def extract_job_id(event: Dict[str, Any]) -> str:
    """Extract jobId from query params or JSON body.

    Body takes precedence over query string parameters.
    Returns "unknown" if jobId is not found or if parsing fails.

    Args:
        event: API Gateway event dict.

    Returns:
        The extracted jobId string, or "unknown" if not found.
    """
    raw_body = event.get("body")
    if raw_body and event.get("isBase64Encoded"):
        try:
            raw_body = base64.b64decode(raw_body).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError, TypeError):
            return "unknown"

    if raw_body:
        try:
            parsed_body = json.loads(raw_body)
            if isinstance(parsed_body, dict):
                job_id = parsed_body.get("jobId")
                if job_id:
                    return str(job_id)
        except (TypeError, json.JSONDecodeError):
            pass

    query = event.get("queryStringParameters") or {}
    if isinstance(query, dict):
        job_id = query.get("jobId")
        if job_id:
            return str(job_id)

    return "unknown"


def parse_event(event: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    """Extract file names and content types from query/body payload."""
    file_names: List[str] = []
    content_types: List[str] = []

    query = event.get("queryStringParameters") or {}
    if isinstance(query, dict):
        file_names = normalize_to_list(query.get("fileNames") or query.get("fileName"))
        content_types = normalize_to_list(query.get("contentTypes") or query.get("contentType"))

    raw_body = event.get("body")
    if raw_body and event.get("isBase64Encoded"):
        try:
            raw_body = base64.b64decode(raw_body).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError, TypeError) as exc:
            logger.warning("Failed to base64-decode request body, falling back to query params: %s", exc)
            raw_body = None

    if raw_body:
        try:
            parsed_body = json.loads(raw_body)
            if isinstance(parsed_body, dict):
                body_file_names = normalize_to_list(parsed_body.get("fileNames") or parsed_body.get("fileName"))
                body_content_types = normalize_to_list(
                    parsed_body.get("contentTypes") or parsed_body.get("contentType")
                )

                if body_file_names:
                    file_names = body_file_names
                if body_content_types:
                    content_types = body_content_types
        except (TypeError, json.JSONDecodeError) as exc:
            logger.warning("Failed to parse request body as JSON, falling back to query params: %s", exc)

    return file_names, content_types
