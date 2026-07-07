"""Tests for structured JSON logging functionality.

Verifies that logs are properly formatted as JSON with all required fields,
and that jobId/stage context is correctly injected throughout the request lifecycle.
"""

import io
import json
import logging
from typing import Any
from unittest.mock import patch

from logging_config import (
    JSONFormatter,
    StructuredLoggerAdapter,
)
from handler import (
    lambda_handler,
    logger,
)


class TestJSONFormatter:
    """Tests for JSONFormatter structured logging formatter."""

    def test_formats_log_as_valid_json(self) -> None:
        """Log is formatted as a valid JSON object."""
        formatter = JSONFormatter()
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="test.py",
            lineno=1,
            msg="Test message",
            args=(),
            exc_info=None,
        )

        formatted = formatter.format(record)

        # Should be valid JSON
        parsed = json.loads(formatted)
        assert isinstance(parsed, dict)

    def test_includes_required_fields(self) -> None:
        """Log includes all required fields: timestamp, level, jobId, stage, message."""
        formatter = JSONFormatter()
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="test.py",
            lineno=1,
            msg="Test message",
            args=(),
            exc_info=None,
            jobId="test-job",
            stage="parse_input",
        )

        formatted = formatter.format(record)
        parsed = json.loads(formatted)

        assert "timestamp" in parsed
        assert "level" in parsed
        assert "jobId" in parsed
        assert "stage" in parsed
        assert "message" in parsed

    def test_timestamp_is_iso8601_utc(self) -> None:
        """Timestamp is in ISO-8601 format with UTC timezone."""
        formatter = JSONFormatter()
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="test.py",
            lineno=1,
            msg="Test message",
            args=(),
            exc_info=None,
        )

        formatted = formatter.format(record)
        parsed = json.loads(formatted)

        timestamp = parsed["timestamp"]
        # Should be ISO-8601 format: YYYY-MM-DDTHH:MM:SS...
        assert "T" in timestamp
        assert "+" in timestamp or "Z" in timestamp or timestamp.endswith("00:00")

    def test_level_field_matches_log_level(self) -> None:
        """Level field matches the log level."""
        formatter = JSONFormatter()

        for level_name, level_num in [("INFO", logging.INFO), ("WARNING", logging.WARNING), ("ERROR", logging.ERROR)]:
            record = logging.LogRecord(
                name="test",
                level=level_num,
                pathname="test.py",
                lineno=1,
                msg="Test",
                args=(),
                exc_info=None,
            )
            formatted = formatter.format(record)
            parsed = json.loads(formatted)
            assert parsed["level"] == level_name

    def test_message_field_contains_log_text(self) -> None:
        """Message field contains the log message text."""
        formatter = JSONFormatter()
        test_msg = "This is the test log message"
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="test.py",
            lineno=1,
            msg=test_msg,
            args=(),
            exc_info=None,
        )

        formatted = formatter.format(record)
        parsed = json.loads(formatted)
        assert parsed["message"] == test_msg

    def test_missing_jobid_defaults_to_unknown(self) -> None:
        """Missing jobId defaults to 'unknown'."""
        formatter = JSONFormatter()
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="test.py",
            lineno=1,
            msg="Test",
            args=(),
            exc_info=None,
        )

        formatted = formatter.format(record)
        parsed = json.loads(formatted)
        assert parsed["jobId"] == "unknown"

    def test_missing_stage_defaults_to_unknown(self) -> None:
        """Missing stage defaults to 'unknown'."""
        formatter = JSONFormatter()
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="test.py",
            lineno=1,
            msg="Test",
            args=(),
            exc_info=None,
        )

        formatted = formatter.format(record)
        parsed = json.loads(formatted)
        assert parsed["stage"] == "unknown"


class TestStructuredLoggerAdapter:
    """Tests for StructuredLoggerAdapter context injection."""

    def test_injects_job_id_into_log(self) -> None:
        """jobId is injected into log records via adapter."""
        base_logger = logging.getLogger("test_adapter")
        adapter = StructuredLoggerAdapter(base_logger, {"jobId": "test-job-123", "stage": "parse_input"})

        # Capture log output
        log_stream = io.StringIO()
        handler = logging.StreamHandler(log_stream)
        handler.setFormatter(JSONFormatter())
        base_logger.handlers = [handler]
        base_logger.setLevel(logging.INFO)

        adapter.info("Test message")

        output = log_stream.getvalue()
        parsed = json.loads(output.strip())
        assert parsed["jobId"] == "test-job-123"

    def test_injects_stage_into_log(self) -> None:
        """stage is injected into log records via adapter."""
        base_logger = logging.getLogger("test_adapter_stage")
        adapter = StructuredLoggerAdapter(base_logger, {"jobId": "test-job", "stage": "put_item"})

        log_stream = io.StringIO()
        handler = logging.StreamHandler(log_stream)
        handler.setFormatter(JSONFormatter())
        base_logger.handlers = [handler]
        base_logger.setLevel(logging.INFO)

        adapter.info("Test message")

        output = log_stream.getvalue()
        parsed = json.loads(output.strip())
        assert parsed["stage"] == "put_item"

    def test_different_adapters_inject_different_values(self) -> None:
        """Different adapters can inject different jobId/stage values."""
        base_logger = logging.getLogger("test_adapter_diff")

        # Create two adapters with different values
        adapter1 = StructuredLoggerAdapter(base_logger, {"jobId": "job-1", "stage": "stage-1"})
        adapter2 = StructuredLoggerAdapter(base_logger, {"jobId": "job-2", "stage": "stage-2"})

        log_stream = io.StringIO()
        handler = logging.StreamHandler(log_stream)
        handler.setFormatter(JSONFormatter())
        base_logger.handlers = [handler]
        base_logger.setLevel(logging.INFO)

        adapter1.info("Message 1")
        adapter2.info("Message 2")

        lines = log_stream.getvalue().strip().split("\n")
        parsed1 = json.loads(lines[0])
        parsed2 = json.loads(lines[1])

        assert parsed1["jobId"] == "job-1"
        assert parsed1["stage"] == "stage-1"
        assert parsed2["jobId"] == "job-2"
        assert parsed2["stage"] == "stage-2"


class TestLoggingInHandler:
    """Tests for logging behavior within the handler execution."""

    def test_handler_logs_on_success(self, monkeypatch: Any) -> None:
        """Successful handler execution produces logs."""
        monkeypatch.setenv("UPLOAD_BUCKET_NAME", "test-bucket")

        log_stream = io.StringIO()
        log_handler = logging.StreamHandler(log_stream)
        log_handler.setFormatter(JSONFormatter())

        # Replace logger handlers temporarily
        original_handlers = logger.handlers[:]
        logger.handlers = [log_handler]
        logger.setLevel(logging.INFO)

        try:
            event = {
                "body": json.dumps({
                    "fileNames": ["a.png", "b.png", "c.png", "d.png"],
                    "contentTypes": ["image/png"] * 4,
                }),
            }

            with patch("handler.s3_client.generate_presigned_url", side_effect=["url1", "url2", "url3", "url4"]):
                lambda_handler(event, None)

            output = log_stream.getvalue()
            lines = output.strip().split("\n")

            # Should have multiple log lines
            assert len(lines) >= 2

            # All should be valid JSON
            for line in lines:
                parsed = json.loads(line)
                assert "timestamp" in parsed
                assert "level" in parsed
                assert "jobId" in parsed
                assert "stage" in parsed
        finally:
            logger.handlers = original_handlers

    def test_handler_logs_error_on_missing_env_var(self, monkeypatch: Any) -> None:
        """Handler logs ERROR level when env var is missing."""
        monkeypatch.delenv("UPLOAD_BUCKET_NAME", raising=False)

        log_stream = io.StringIO()
        log_handler = logging.StreamHandler(log_stream)
        log_handler.setFormatter(JSONFormatter())

        original_handlers = logger.handlers[:]
        logger.handlers = [log_handler]
        logger.setLevel(logging.INFO)

        try:
            lambda_handler({}, None)

            output = log_stream.getvalue()
            lines = output.strip().split("\n")

            # Find error log
            error_logs = [json.loads(line) for line in lines if "error" in line.lower()]
            error_found = any(log["level"] == "ERROR" for log in error_logs)
            assert error_found, "No ERROR level log found"
        finally:
            logger.handlers = original_handlers

    def test_logs_contain_stage_transitions(self, monkeypatch: Any) -> None:
        """Logs show stage transitions during handler execution."""
        monkeypatch.setenv("UPLOAD_BUCKET_NAME", "test-bucket")

        log_stream = io.StringIO()
        log_handler = logging.StreamHandler(log_stream)
        log_handler.setFormatter(JSONFormatter())

        original_handlers = logger.handlers[:]
        logger.handlers = [log_handler]
        logger.setLevel(logging.INFO)

        try:
            event = {
                "body": json.dumps({
                    "fileNames": ["a.png", "b.png", "c.png", "d.png"],
                    "contentTypes": ["image/png"] * 4,
                }),
            }

            with patch("handler.s3_client.generate_presigned_url", side_effect=["url1", "url2", "url3", "url4"]):
                lambda_handler(event, None)

            output = log_stream.getvalue()
            lines = output.strip().split("\n")
            logs = [json.loads(line) for line in lines]

            # Should have parse_input stage
            parse_logs = [log for log in logs if log.get("stage") == "parse_input"]
            assert len(parse_logs) > 0, "No parse_input stage logs found"

            # Should have put_item stage
            put_logs = [log for log in logs if log.get("stage") == "put_item"]
            assert len(put_logs) > 0, "No put_item stage logs found"
        finally:
            logger.handlers = original_handlers

    def test_logs_contain_job_id_when_provided(self, monkeypatch: Any) -> None:
        """Logs contain the provided jobId."""
        monkeypatch.setenv("UPLOAD_BUCKET_NAME", "test-bucket")

        job_id = "test-correlation-id-123"
        log_stream = io.StringIO()
        log_handler = logging.StreamHandler(log_stream)
        log_handler.setFormatter(JSONFormatter())

        original_handlers = logger.handlers[:]
        logger.handlers = [log_handler]
        logger.setLevel(logging.INFO)

        try:
            event = {
                "queryStringParameters": {"jobId": job_id},
                "body": json.dumps({
                    "fileNames": ["a.png", "b.png", "c.png", "d.png"],
                    "contentTypes": ["image/png"] * 4,
                }),
            }

            with patch("handler.s3_client.generate_presigned_url", side_effect=["url1", "url2", "url3", "url4"]):
                lambda_handler(event, None)

            output = log_stream.getvalue()
            lines = output.strip().split("\n")
            logs = [json.loads(line) for line in lines]

            # All logs after the first should have the jobId (first is pre-parse with "unknown")
            for log in logs[1:]:
                assert log["jobId"] == job_id, f"Expected jobId {job_id}, got {log['jobId']}"
        finally:
            logger.handlers = original_handlers

    def test_logs_do_not_contain_presigned_urls(self, monkeypatch: Any) -> None:
        """Logs do not contain actual presigned URLs (for security)."""
        monkeypatch.setenv("UPLOAD_BUCKET_NAME", "test-bucket")

        log_stream = io.StringIO()
        log_handler = logging.StreamHandler(log_stream)
        log_handler.setFormatter(JSONFormatter())

        original_handlers = logger.handlers[:]
        logger.handlers = [log_handler]
        logger.setLevel(logging.INFO)

        try:
            event = {
                "body": json.dumps({
                    "fileNames": ["a.png", "b.png", "c.png", "d.png"],
                    "contentTypes": ["image/png"] * 4,
                }),
            }

            fake_url = "https://s3.example.com/bucket/key?X-Amz-Signature=secret123"
            with patch("handler.s3_client.generate_presigned_url", side_effect=[fake_url] * 4):
                lambda_handler(event, None)

            output = log_stream.getvalue()

            # URL should not appear in logs
            assert "X-Amz-Signature" not in output
            assert "secret123" not in output
        finally:
            logger.handlers = original_handlers
