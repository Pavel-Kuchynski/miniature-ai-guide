"""Structured JSON logging configuration for Lambda handlers."""

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Tuple

UNKNOWN = "unknown"


class JSONFormatter(logging.Formatter):
    """Custom formatter that outputs structured JSON logs.

    Formats each log record as a single-line JSON object with:
    - timestamp (ISO-8601, UTC)
    - level (INFO/WARNING/ERROR)
    - jobId (correlation ID for tracing)
    - stage (lifecycle stage: parse_input, download_images, generate, upload, etc.)
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


class StructuredLoggerAdapter(logging.LoggerAdapter):
    """Logger adapter that injects jobId and stage into every log.

    Used to carry jobId and stage as context so they are automatically
    included in every log emitted through this adapter instance. A per-call
    `extra` overrides the adapter's values for the same key.
    """

    def process(
        self, msg: str, kwargs: Dict[str, Any]
    ) -> Tuple[str, Dict[str, Any]]:
        """Merge the adapter's context into the record's extra fields.

        Args:
            msg: The log message.
            kwargs: Keyword arguments passed to the logger. A per-call `extra` mapping
                takes precedence over the adapter's `jobId`/`stage` for the same key.

        Returns:
            Tuple of (msg, kwargs) where `kwargs["extra"]` holds the merged fields.
        """
        kwargs["extra"] = {**self.extra, **kwargs.get("extra", {})}
        return msg, kwargs


def configure_logger(name: str) -> logging.Logger:
    """Configure and return a logger with JSON formatting."""
    logger = logging.getLogger(name)
    logger.handlers.clear()
    _handler = logging.StreamHandler()
    _handler.setFormatter(JSONFormatter())
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)
    return logger
