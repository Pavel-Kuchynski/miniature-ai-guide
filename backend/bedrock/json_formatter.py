"""JSON log formatter for Lambda handlers."""

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict

UNKNOWN = "unknown"


class JSONFormatter(logging.Formatter):
    """Custom formatter that outputs structured JSON logs.

    Formats each log record as a single-line JSON object with:
    - timestamp (ISO-8601, UTC)
    - level (INFO/WARNING/ERROR)
    - jobId (correlation ID for tracing)
    - stage (lifecycle stage: parse_input, download_image, invoke_bedrock, etc.)
    - message (human-readable log text)
    - exception (formatted traceback; only present when the record has exc_info)
    """

    def format(self, record: logging.LogRecord) -> str:
        """Format log record as JSON with required fields.

        Args:
            record: The log record to format. `jobId` and `stage` are read from the
                record's extra attributes and default to `UNKNOWN` (`"unknown"`) when absent.

        Returns:
            JSON-formatted log line as a string. Non-JSON-serializable values are
            rendered with `str()`.
        """
        dt = datetime.fromtimestamp(record.created, tz=timezone.utc)
        log_obj: Dict[str, Any] = {
            "timestamp": dt.isoformat(),
            "level": record.levelname,
            "jobId": record.__dict__.get("jobId", UNKNOWN),
            "stage": record.__dict__.get("stage", UNKNOWN),
            "message": record.getMessage(),
        }
        if record.exc_info:
            log_obj["exception"] = self.formatException(record.exc_info)
        return json.dumps(log_obj, default=str)
