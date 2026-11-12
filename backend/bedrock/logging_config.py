"""Structured JSON logging configuration for Lambda handlers."""

import logging

from json_formatter import UNKNOWN, JSONFormatter
from structured_logger_adapter import StructuredLoggerAdapter

__all__ = ["UNKNOWN", "JSONFormatter", "StructuredLoggerAdapter", "configure_logger"]


def configure_logger(name: str) -> logging.Logger:
    """Configure and return a logger that emits JSON lines to stderr.

    Existing handlers are removed first, so calling this twice for the same name does not
    duplicate output.

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
