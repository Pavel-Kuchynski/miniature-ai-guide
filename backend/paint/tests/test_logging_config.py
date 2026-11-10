"""Tests for the structured JSON logging helpers in `logging_config`.

Covers the JSON field set emitted by `JSONFormatter`, the traceback field for records
with exception info, and per-call `extra` values overriding `StructuredLoggerAdapter`
context.
"""

import json
import logging
import sys
import uuid
from typing import Any

import pytest

from logging_config import JSONFormatter, StructuredLoggerAdapter

EXPECTED_FIELDS = {"timestamp", "level", "jobId", "stage", "message"}


def _make_record(
    msg: str = "hello %s",
    args: tuple[Any, ...] = ("world",),
    exc_info: Any = None,
    **extra: Any,
) -> logging.LogRecord:
    """Build a LogRecord directly, attaching `extra` as attributes the way logging does."""
    record = logging.LogRecord(
        name="test_logging_config",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=args,
        exc_info=exc_info,
    )
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def _format_to_dict(record: logging.LogRecord) -> dict[str, Any]:
    """Format a record with JSONFormatter and parse the resulting JSON line."""
    return json.loads(JSONFormatter().format(record))


class TestJSONFormatter:
    """Tests for `JSONFormatter.format`."""

    def test_emits_exactly_the_documented_fields(self) -> None:
        """A record without exc_info should produce only the five documented fields."""
        payload = _format_to_dict(_make_record(jobId="job-1", stage="download_images"))

        assert set(payload) == EXPECTED_FIELDS

    def test_populates_fields_from_record(self) -> None:
        """Level, message, jobId and stage should come from the record."""
        payload = _format_to_dict(_make_record(jobId="job-1", stage="download_images"))

        assert payload["level"] == "INFO"
        assert payload["message"] == "hello world"
        assert payload["jobId"] == "job-1"
        assert payload["stage"] == "download_images"

    def test_timestamp_is_utc_iso_8601(self) -> None:
        """Timestamp should be an ISO-8601 string with a UTC offset."""
        payload = _format_to_dict(_make_record())

        assert payload["timestamp"].endswith("+00:00")

    def test_missing_job_id_and_stage_default_to_unknown(self) -> None:
        """Records without jobId/stage extras should still emit the fields as 'unknown'."""
        payload = _format_to_dict(_make_record())

        assert payload["jobId"] == "unknown"
        assert payload["stage"] == "unknown"

    def test_non_serializable_extra_is_rendered_as_string(self) -> None:
        """Non-JSON-serializable extra values should be rendered with str() instead of raising."""
        job_uuid = uuid.UUID("123e4567-e89b-12d3-a456-426614174000")

        payload = _format_to_dict(_make_record(jobId=job_uuid))

        assert payload["jobId"] == str(job_uuid)

    def test_exception_field_is_absent_without_exc_info(self) -> None:
        """Records without exc_info should not carry an exception field."""
        payload = _format_to_dict(_make_record())

        assert "exception" not in payload

    def test_exception_field_contains_traceback_when_exc_info_set(self) -> None:
        """Records logged with exc_info should include the formatted traceback."""
        try:
            raise ValueError("boom")
        except ValueError:
            exc_info = sys.exc_info()

        payload = _format_to_dict(_make_record(exc_info=exc_info))

        assert payload["exception"].startswith("Traceback (most recent call last):")
        assert payload["exception"].rstrip().endswith("ValueError: boom")
        assert set(payload) == EXPECTED_FIELDS | {"exception"}


class TestStructuredLoggerAdapter:
    """Tests for `StructuredLoggerAdapter.process`."""

    @pytest.fixture
    def captured(self) -> list[logging.LogRecord]:
        """Return a list that receives every record emitted through the test logger."""
        records: list[logging.LogRecord] = []

        class _ListHandler(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append(record)

        logger = logging.getLogger("test_logging_config.adapter")
        logger.handlers = [_ListHandler()]
        logger.setLevel(logging.DEBUG)
        logger.propagate = False
        return records

    def test_adapter_context_is_applied_when_no_per_call_extra(self, captured: list) -> None:
        """Adapter jobId and stage should be attached to records with no per-call extra."""
        adapter = StructuredLoggerAdapter(
            logging.getLogger("test_logging_config.adapter"),
            {"jobId": "job-1", "stage": "orchestrate"},
        )

        adapter.info("Starting paint job")

        [record] = captured
        assert record.jobId == "job-1"
        assert record.stage == "orchestrate"

    def test_per_call_stage_overrides_adapter_stage(self, captured: list) -> None:
        """A per-call stage in extra should win over the adapter's stage."""
        adapter = StructuredLoggerAdapter(
            logging.getLogger("test_logging_config.adapter"),
            {"jobId": "job-1", "stage": "orchestrate"},
        )

        adapter.error("Failed to paint images", extra={"stage": "paint_error"})

        [record] = captured
        assert record.stage == "paint_error"
        assert record.jobId == "job-1"

    def test_per_call_stage_survives_into_json_output(self, captured: list) -> None:
        """The per-call stage should appear in the formatted JSON, not the adapter's stage."""
        adapter = StructuredLoggerAdapter(
            logging.getLogger("test_logging_config.adapter"),
            {"jobId": "job-1", "stage": "orchestrate"},
        )

        adapter.info("Uploading", extra={"stage": "upload_images"})

        payload = _format_to_dict(captured[0])
        assert payload["stage"] == "upload_images"
        assert payload["jobId"] == "job-1"

    def test_exception_logged_through_adapter_includes_traceback(self, captured: list) -> None:
        """`exception()` through the adapter should attach exc_info and an exception field."""
        adapter = StructuredLoggerAdapter(
            logging.getLogger("test_logging_config.adapter"),
            {"jobId": "job-1", "stage": "orchestrate"},
        )

        try:
            raise RuntimeError("upload failed")
        except RuntimeError:
            adapter.exception("Failed to paint images", extra={"stage": "paint_error"})

        payload = _format_to_dict(captured[0])
        assert payload["level"] == "ERROR"
        assert payload["stage"] == "paint_error"
        assert payload["jobId"] == "job-1"
        assert "RuntimeError: upload failed" in payload["exception"]
