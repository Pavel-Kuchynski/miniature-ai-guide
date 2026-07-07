"""Structured JSON logging configuration for Lambda handlers."""

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Tuple


class JSONFormatter(logging.Formatter):
    """Custom formatter that outputs structured JSON logs.

    Formats each log record as a single-line JSON object with:
    - timestamp (ISO-8601, UTC)
    - level (INFO/WARNING/ERROR)
    - jobId (correlation ID for tracing)
    - stage (lifecycle stage: parse_input, list_s3, put_item, error, etc.)
    - message (human-readable log text)
    """

    def format(self, record: logging.LogRecord) -> str:
        """Format log record as JSON with required fields.

        Args:
            record: The log record to format.

        Returns:
            JSON-formatted log line as a string.
        """
        dt = datetime.fromtimestamp(record.created, tz=timezone.utc)
        log_obj: Dict[str, Any] = {
            "timestamp": dt.isoformat(),
            "level": record.levelname,
            "jobId": record.__dict__.get("jobId", "unknown"),
            "stage": record.__dict__.get("stage", "unknown"),
            "message": record.getMessage(),
        }
        return json.dumps(log_obj)


class StructuredLoggerAdapter(logging.LoggerAdapter):
    """Logger adapter that injects jobId and stage into every log.

    Used to carry jobId and stage as context so they are automatically
    included in every log emitted through this adapter instance.
    """

    def process(
        self, msg: str, kwargs: Dict[str, Any]
    ) -> Tuple[str, Dict[str, Any]]:
        """Inject extra fields into the log record.

        Args:
            msg: The log message.
            kwargs: Keyword arguments passed to the logger.

        Returns:
            Tuple of (msg, kwargs) with extra fields injected.
        """
        if "extra" not in kwargs:
            kwargs["extra"] = {}
        kwargs["extra"]["jobId"] = self.extra.get("jobId", "unknown")
        kwargs["extra"]["stage"] = self.extra.get("stage", "unknown")
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
