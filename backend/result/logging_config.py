"""Structured JSON logging configuration for Lambda handlers."""

import logging

from json_formatter import UNKNOWN, JSONFormatter
from structured_logger_adapter import StructuredLoggerAdapter

__all__ = ["UNKNOWN", "JSONFormatter", "StructuredLoggerAdapter", "configure_logger"]


def configure_logger(name: str) -> logging.Logger:
    """Configure and return a logger with JSON formatting."""
    logger = logging.getLogger(name)
    logger.handlers.clear()
    _handler = logging.StreamHandler()
    _handler.setFormatter(JSONFormatter())
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)
    return logger
