"""Tests for the structured logging helpers."""

import json
import logging
import sys

from logging_config import StructuredLoggerAdapter, configure_logger


def test_per_call_stage_overrides_adapter_stage(capsys):
    """Regression: the adapter used to overwrite the per-call stage with its own."""
    logger = configure_logger("test_result_logging")
    logger.handlers[0].stream = sys.stdout
    log = StructuredLoggerAdapter(logger, {"jobId": "job-1", "stage": "orchestrate"})

    log.error("boom", extra={"stage": "vallejo"})

    record = json.loads(capsys.readouterr().out)
    assert record["jobId"] == "job-1"
    assert record["stage"] == "vallejo"


def test_exception_logs_include_traceback(capsys):
    logger = configure_logger("test_result_logging_exc")
    logger.handlers[0].stream = sys.stdout
    log = StructuredLoggerAdapter(logger, {"jobId": "job-1", "stage": "orchestrate"})

    try:
        raise ValueError("bad")
    except ValueError:
        log.exception("failed")

    record = json.loads(capsys.readouterr().out)
    assert record["level"] == logging.getLevelName(logging.ERROR)
    assert "ValueError" in record["exception"]
