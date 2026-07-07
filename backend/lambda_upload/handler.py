"""AWS Lambda handler that returns four S3 pre-signed upload URLs."""

import json
import logging
import os
import uuid
from typing import Any, Dict, List

import boto3
from botocore.exceptions import ClientError, ParamValidationError

from event_parser import extract_job_id, parse_event, parse_expires_in, sanitize_file_name
from logging_config import StructuredLoggerAdapter, configure_logger

logger = configure_logger(__name__)
s3_client = boto3.client("s3")  # type: ignore[arg-type]


def _response(status_code: int, body: Dict[str, Any]) -> Dict[str, Any]:
    """Build an API-Gateway-style JSON response with CORS headers."""
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "Content-Type,Authorization",
            "Access-Control-Allow-Methods": "OPTIONS,POST",
        },
        "body": json.dumps(body),
    }


def _generate_upload_urls(
    bucket_name: str,
    folder_id: str,
    file_names: List[str],
    content_types: List[str],
    expires_in: int,
    log: StructuredLoggerAdapter,
) -> Dict[str, Any]:
    """Generate four pre-signed S3 PUT URLs under the given folder."""
    base_prefix = f"uploads/{folder_id}"
    upload_items: List[Dict[str, str]] = []

    for index in range(4):
        file_name = file_names[index] if index < len(file_names) else f"file_{index + 1}.bin"
        content_type = (
            content_types[index]
            if index < len(content_types)
            else (content_types[0] if content_types else "application/octet-stream")
        )

        safe_file_name = sanitize_file_name(file_name, index)
        object_key = f"{base_prefix}/{safe_file_name}"

        upload_url = s3_client.generate_presigned_url(
            ClientMethod="put_object",
            Params={
                "Bucket": bucket_name,
                "Key": object_key,
                "ContentType": content_type,
            },
            ExpiresIn=expires_in,
            HttpMethod="PUT",
        )

        upload_items.append(
            {
                "uploadUrl": upload_url,
                "key": object_key,
                "fileName": safe_file_name,
                "contentType": content_type,
            }
        )

    log.info("Successfully generated 4 presigned upload URLs")
    return {
        "bucket": bucket_name,
        "folder": folder_id,
        "prefix": base_prefix,
        "uploadItems": upload_items,
        "expiresIn": expires_in,
    }

def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """Create four pre-signed PUT URLs in one UUID-based folder."""
    del context

    job_id = extract_job_id(event or {})
    log = StructuredLoggerAdapter(logger, {"jobId": job_id, "stage": "parse_input"})
    logger.info("Starting upload handler", extra={"jobId": job_id, "stage": "parse_input"})

    bucket_name = os.getenv("UPLOAD_BUCKET_NAME")
    if not bucket_name:
        error_log = StructuredLoggerAdapter(logger, {"jobId": job_id, "stage": "error"})
        error_log.error("Missing required env var UPLOAD_BUCKET_NAME")
        return _response(500, {"error": "Server misconfiguration: UPLOAD_BUCKET_NAME is not set."})

    expires_in = parse_expires_in(os.getenv("UPLOAD_URL_EXPIRES_SECONDS", "900"))
    if expires_in is None:
        error_log = StructuredLoggerAdapter(logger, {"jobId": job_id, "stage": "error"})
        error_log.error("Invalid UPLOAD_URL_EXPIRES_SECONDS")
        return _response(500, {"error": "Server misconfiguration: UPLOAD_URL_EXPIRES_SECONDS is invalid."})

    file_names, content_types = parse_event(event or {})
    log.info("Successfully parsed input event")

    folder_id = str(uuid.uuid4())
    log = StructuredLoggerAdapter(logger, {"jobId": job_id, "stage": "put_item"})

    try:
        result = _generate_upload_urls(bucket_name, folder_id, file_names, content_types, expires_in, log)
        return _response(200, result)
    except (ClientError, ParamValidationError) as exc:
        error_log = StructuredLoggerAdapter(logger, {"jobId": job_id, "stage": "error"})
        error_log.error(f"Failed to create upload URL: {exc}")
        return _response(500, {"error": "Failed to create upload URL."})
