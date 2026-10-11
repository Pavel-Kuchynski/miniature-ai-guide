"""AWS Lambda handler for the result Lambda.

Triggered by SQS when the bedrock Lambda finishes a job. Reads the jobId and status from
the message, enriches the bedrock result JSON with the nearest Vallejo paints, generates
presigned download URLs for the result files in the paint S3 bucket, looks up the job's WebSocket connectionId in DynamoDB, and pushes the result to
the client through the API Gateway WebSocket management API.
"""
import csv
import io
import json
import os
from typing import Any, Dict, List, Optional, Tuple

import boto3
from botocore.exceptions import ClientError

from logging_config import configure_logger, StructuredLoggerAdapter

logger = configure_logger(__name__)

COMPLETED_STATUS = "COMPLETED"
RESULT_JSON_NAME = "result.json"
RESULT_IMAGE_NAME = "image_0.png"
VALLEJO_RESULT_JSON_NAME = "result_vallejo.json"
VALLEJO_CSV_KEY = "vallejo/vallejo_colors.csv"
PRESIGNED_URL_TTL_SECONDS = 3600

# Parsed palette kept across warm Lambda invocations; the CSV is static.
_vallejo_palette_cache: Optional[List[Dict[str, Any]]] = None


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


def _parse_hex(value: Any) -> Tuple[int, int, int]:
    """Convert a `RRGGBB` / `#RRGGBB` hex string to an `(r, g, b)` tuple.

    Raises:
        ValueError: If the value is not a 6-digit hex colour.
    """
    if not isinstance(value, str):
        raise ValueError(f"Invalid hex colour: {value!r}")
    digits = value.strip().lstrip("#")
    if len(digits) != 6:
        raise ValueError(f"Invalid hex colour: {value!r}")
    return int(digits[0:2], 16), int(digits[2:4], 16), int(digits[4:6], 16)


def _load_vallejo_palette() -> List[Dict[str, Any]]:
    """Read and parse `vallejo/vallejo_colors.csv` from `STATIC_BUCKET_NAME` (cached).

    Returns:
        Rows with the CSV columns, where `r`, `g` and `b` are converted to `int`.

    Raises:
        botocore.exceptions.ClientError: Propagated on S3 read failure.
        KeyError: If `STATIC_BUCKET_NAME` is not set.
        ValueError: If the CSV is malformed or contains no colours.
    """
    global _vallejo_palette_cache
    if _vallejo_palette_cache is not None:
        return _vallejo_palette_cache

    bucket_name = os.environ["STATIC_BUCKET_NAME"]
    body = boto3.client("s3").get_object(Bucket=bucket_name, Key=VALLEJO_CSV_KEY)["Body"].read()

    palette: List[Dict[str, Any]] = []
    for row in csv.DictReader(io.StringIO(body.decode("utf-8-sig"))):
        try:
            palette.append({**row, "r": int(row["r"]), "g": int(row["g"]), "b": int(row["b"])})
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"Malformed row in {VALLEJO_CSV_KEY}: {row}") from exc
    if not palette:
        raise ValueError(f"{VALLEJO_CSV_KEY} contains no colours")

    _vallejo_palette_cache = palette
    return palette


def find_vallejo_colors(result_json: Dict[str, Any]) -> Dict[str, Any]:
    """Add the nearest Vallejo paint to every colour of the bedrock result JSON.

    The nearest paint is the one with the smallest squared Euclidean distance in RGB
    space to the colour's `paint` hex value. The input is not modified.

    Args:
        result_json: Parsed `result.json` with a `colors` list of `{detail, paint}`.

    Returns:
        A copy of `result_json` where each colour has a `vallejo_color` dict holding
        the palette row (`reference`, `Color`, `name`, `r`, `g`, `b`, `hex`).

    Raises:
        botocore.exceptions.ClientError: Propagated on S3 read failure.
        KeyError: If `STATIC_BUCKET_NAME` is not set.
        ValueError: If `colors` is missing or an entry has an invalid `paint` value.
    """
    colors = result_json.get("colors")
    if not isinstance(colors, list):
        raise ValueError("Result JSON has no 'colors' list")

    palette = _load_vallejo_palette()
    enriched_colors = []
    for color in colors:
        if not isinstance(color, dict):
            raise ValueError(f"Invalid color entry: {color!r}")
        r, g, b = _parse_hex(color.get("paint"))
        nearest = min(
            palette,
            key=lambda row: (row["r"] - r) ** 2 + (row["g"] - g) ** 2 + (row["b"] - b) ** 2,
        )
        enriched_colors.append({**color, "vallejo_color": dict(nearest)})

    return {**result_json, "colors": enriched_colors}


def store_vallejo_result(job_id: str) -> None:
    """Enrich the job's `result.json` with Vallejo colours and store it beside the original.

    Reads `painted_images/<job_id>/result.json` and writes the enriched JSON to
    `painted_images/<job_id>/result_vallejo.json`. The original file is left untouched, so
    the operation is safe to repeat when SQS redelivers the message.

    Raises:
        botocore.exceptions.ClientError: Propagated on S3 read/write failure.
        KeyError: If `PAINT_BUCKET_NAME` or `STATIC_BUCKET_NAME` is not set.
        ValueError: If the result JSON is not valid or cannot be matched.
    """
    bucket_name = os.environ["PAINT_BUCKET_NAME"]
    s3_client = boto3.client("s3")
    prefix = f"painted_images/{job_id}"

    body = s3_client.get_object(Bucket=bucket_name, Key=f"{prefix}/{RESULT_JSON_NAME}")["Body"]
    try:
        result_json = json.loads(body.read())
    except json.JSONDecodeError as exc:
        raise ValueError(f"{RESULT_JSON_NAME} is not valid JSON") from exc
    if not isinstance(result_json, dict):
        raise ValueError(f"{RESULT_JSON_NAME} must contain a JSON object")

    s3_client.put_object(
        Bucket=bucket_name,
        Key=f"{prefix}/{VALLEJO_RESULT_JSON_NAME}",
        Body=json.dumps(find_vallejo_colors(result_json)).encode("utf-8"),
        ContentType="application/json",
    )


def generate_result_urls(job_id: str) -> Dict[str, str]:
    """Generate presigned GET URLs for the bedrock result files of a job.

    Files are expected under `painted_images/<job_id>/` in `PAINT_BUCKET_NAME`.

    Args:
        job_id: The job id used as the S3 key prefix.

    Returns:
        Dict with `resultUrl` (result_vallejo.json) and `imageUrl` (image_0.png).

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

    return {"resultUrl": _presign(VALLEJO_RESULT_JSON_NAME), "imageUrl": _presign(RESULT_IMAGE_NAME)}


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

    Triggered by SQS. For `COMPLETED` jobs the result JSON is first enriched with the
    nearest Vallejo paints (`result_vallejo.json`), then the message contains presigned
    URLs for it and for `image_0.png`; for any other status only `jobId` and `status`
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
            store_vallejo_result(job_id)
        except (ClientError, ValueError) as exc:
            log.error("Failed to add Vallejo colors: %s", exc, extra={"stage": "vallejo"})
            raise
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
