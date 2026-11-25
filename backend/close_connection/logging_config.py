"""Structured JSON logging configuration for Lambda handlers.

Kept byte-identical in `backend/open_connection` and `backend/close_connection`; change both together.
"""

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Tuple

# Correlation fields injected by StructuredLoggerAdapter. "unknown" is emitted when a field is not
# bound to the adapter, so every line has the same schema for CloudWatch Logs Insights queries.
_CONTEXT_FIELDS: Tuple[str, ...] = ("jobId", "connectionId", "stage")


class JSONFormatter(logging.Formatter):
    """Custom formatter that outputs structured JSON logs.

    Formats each log record as a single-line JSON object with:
    - timestamp (ISO-8601, UTC)
    - level (INFO/WARNING/ERROR)
    - jobId (job correlation ID, "unknown" if not bound)
    - connectionId (WebSocket connection ID, "unknown" if not bound)
    - stage (lifecycle stage: parse_input, validate_job, store_connection, error, etc.)
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
            "message": record.getMessage(),
        }
        for field_name in _CONTEXT_FIELDS:
            log_obj[field_name] = record.__dict__.get(field_name, "unknown")
        return json.dumps(log_obj)


class StructuredLoggerAdapter(logging.LoggerAdapter):
    """Logger adapter that injects correlation fields into every log.

    Carries jobId, connectionId and stage as context so they are included in every log
    emitted through this adapter instance. Fields not bound to the adapter are logged as "unknown".
    """

    def process(
        self, msg: str, kwargs: Dict[str, Any]
    ) -> Tuple[str, Dict[str, Any]]:
        """Inject correlation fields into the log record.

        Args:
            msg: The log message.
            kwargs: Keyword arguments passed to the logger.

        Returns:
            Tuple of (msg, kwargs) with correlation fields injected into `extra`.
        """
        if "extra" not in kwargs:
            kwargs["extra"] = {}
        for field_name in _CONTEXT_FIELDS:
            kwargs["extra"][field_name] = self.extra.get(field_name, "unknown")
        return msg, kwargs


def configure_logger(name: str) -> logging.Logger:
    """Configure and return a logger that writes JSON lines to stderr.

    Replaces any handlers previously attached to the logger, so repeated calls (e.g. on module
    reload in tests) do not duplicate output.

    Args:
        name: Logger name, normally `__name__` of the calling module.

    Returns:
        The configured logger, set to INFO level.
    """
    logger = logging.getLogger(name)
    logger.handlers.clear()
    _handler = logging.StreamHandler()
    _handler.setFormatter(JSONFormatter())
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)
    return logger
