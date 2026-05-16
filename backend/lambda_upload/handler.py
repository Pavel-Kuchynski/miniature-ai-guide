"""AWS Lambda handler that returns four S3 pre-signed upload URLs."""

import json
import os
import uuid
from typing import Any, Dict, List, Tuple

import boto3
from botocore.exceptions import ClientError

s3_client = boto3.client("s3")  # type: ignore[arg-type]


def _response(status_code: int, body: Dict[str, Any]) -> Dict[str, Any]:
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


def _normalize_to_list(value: Any) -> List[str]:
    if value is None:
        return []

    if isinstance(value, list):
        return [str(item) for item in value if item]

    if isinstance(value, str):
        parts = [part.strip() for part in value.split(",")]
        return [part for part in parts if part]

    return [str(value)]


def _parse_event(event: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    """Extract file names and content types from query/body payload."""
    file_names: List[str] = []
    content_types: List[str] = []

    query = event.get("queryStringParameters") or {}
    if isinstance(query, dict):
        file_names = _normalize_to_list(query.get("fileNames") or query.get("fileName"))
        content_types = _normalize_to_list(query.get("contentTypes") or query.get("contentType"))

    raw_body = event.get("body")
    if raw_body:
        try:
            parsed_body = json.loads(raw_body)
            if isinstance(parsed_body, dict):
                body_file_names = _normalize_to_list(parsed_body.get("fileNames") or parsed_body.get("fileName"))
                body_content_types = _normalize_to_list(
                    parsed_body.get("contentTypes") or parsed_body.get("contentType")
                )

                if body_file_names:
                    file_names = body_file_names
                if body_content_types:
                    content_types = body_content_types
        except (TypeError, json.JSONDecodeError):
            pass

    return file_names, content_types


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """Create four pre-signed PUT URLs in one UUID-based folder."""
    del context

    bucket_name = os.getenv("UPLOAD_BUCKET_NAME")
    if not bucket_name:
        return _response(
            500,
            {"error": "Server misconfiguration: UPLOAD_BUCKET_NAME is not set."},
        )

    expires_in = int(os.getenv("UPLOAD_URL_EXPIRES_SECONDS", "900"))
    file_names, content_types = _parse_event(event or {})

    folder_id = str(uuid.uuid4())
    base_prefix = f"uploads/{folder_id}"
    total_files = 4

    upload_items: List[Dict[str, str]] = []

    try:
        for index in range(total_files):
            file_name = file_names[index] if index < len(file_names) else f"file_{index + 1}.bin"
            content_type = (
                content_types[index]
                if index < len(content_types)
                else (content_types[0] if content_types else "application/octet-stream")
            )

            safe_file_name = os.path.basename(file_name)
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
    except ClientError as exc:
        return _response(500, {"error": "Failed to create upload URL.", "details": str(exc)})

    return _response(
        200,
        {
            "bucket": bucket_name,
            "folder": folder_id,
            "prefix": base_prefix,
            "uploadItems": upload_items,
            "expiresIn": expires_in,
        },
    )
