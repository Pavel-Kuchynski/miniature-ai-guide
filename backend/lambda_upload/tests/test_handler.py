"""Unit tests for the lambda_upload handler."""

import io
import json
import logging
import sys
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from botocore.exceptions import ClientError

# Ensure imports work when running tests from repository root.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import handler
from logging_config import JSONFormatter


class TestLambdaUploadHandler(unittest.TestCase):
    def test_missing_bucket_env_returns_500(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            response = handler.lambda_handler({}, None)

        self.assertEqual(response["statusCode"], 500)
        payload = json.loads(response["body"])
        self.assertIn("UPLOAD_BUCKET_NAME", payload["error"])

    def test_generates_four_urls_in_single_uuid_folder(self) -> None:
        fixed_uuid = uuid.UUID("11111111-1111-1111-1111-111111111111")

        event = {
            "body": json.dumps(
                {
                    "fileNames": ["a.png", "b.png", "c.png", "d.png"],
                    "contentTypes": ["image/png", "image/png", "image/png", "image/png"],
                }
            )
        }

        with patch.dict(
            "os.environ",
            {
                "UPLOAD_BUCKET_NAME": "test-bucket",
                "UPLOAD_URL_EXPIRES_SECONDS": "600",
            },
            clear=True,
        ), patch("handler.uuid.uuid4", return_value=fixed_uuid), patch.object(
            handler.s3_client,
            "generate_presigned_url",
            side_effect=["url1", "url2", "url3", "url4"],
        ) as mocked_presign:
            response = handler.lambda_handler(event, None)

        self.assertEqual(response["statusCode"], 200)
        payload = json.loads(response["body"])

        self.assertEqual(payload["bucket"], "test-bucket")
        self.assertEqual(payload["folder"], str(fixed_uuid))
        self.assertEqual(payload["prefix"], f"uploads/{fixed_uuid}")
        self.assertEqual(payload["expiresIn"], 600)
        self.assertEqual(len(payload["uploadItems"]), 4)
        self.assertEqual(mocked_presign.call_count, 4)

        expected_keys = [
            f"uploads/{fixed_uuid}/a.png",
            f"uploads/{fixed_uuid}/b.png",
            f"uploads/{fixed_uuid}/c.png",
            f"uploads/{fixed_uuid}/d.png",
        ]
        actual_keys = [item["key"] for item in payload["uploadItems"]]
        self.assertEqual(actual_keys, expected_keys)

    def test_defaults_and_client_error_returns_500(self) -> None:
        event = {
            "queryStringParameters": {
                "fileName": "fallback.bin",
                "contentType": "application/octet-stream",
            }
        }

        with patch.dict("os.environ", {"UPLOAD_BUCKET_NAME": "test-bucket"}, clear=True), patch.object(
            handler.s3_client,
            "generate_presigned_url",
            side_effect=ClientError(
                error_response={"Error": {"Code": "500", "Message": "boom"}},
                operation_name="PutObject",
            ),
        ):
            response = handler.lambda_handler(event, None)

        self.assertEqual(response["statusCode"], 500)
        payload = json.loads(response["body"])
        self.assertIn("Failed to create upload URL", payload["error"])

    def test_body_overrides_query_string_values(self) -> None:
        event = {
            "queryStringParameters": {"fileName": "query.png"},
            "body": json.dumps({"fileNames": ["body.png"]}),
        }

        with patch.dict("os.environ", {"UPLOAD_BUCKET_NAME": "test-bucket"}, clear=True), patch.object(
            handler.s3_client,
            "generate_presigned_url",
            side_effect=["url1", "url2", "url3", "url4"],
        ):
            response = handler.lambda_handler(event, None)

        payload = json.loads(response["body"])
        self.assertEqual(payload["uploadItems"][0]["fileName"], "body.png")

    def test_partial_file_names_fall_back_for_remaining_slots(self) -> None:
        event = {"body": json.dumps({"fileNames": ["a.png", "b.png"]})}

        with patch.dict("os.environ", {"UPLOAD_BUCKET_NAME": "test-bucket"}, clear=True), patch.object(
            handler.s3_client,
            "generate_presigned_url",
            side_effect=["url1", "url2", "url3", "url4"],
        ):
            response = handler.lambda_handler(event, None)

        payload = json.loads(response["body"])
        file_names = [item["fileName"] for item in payload["uploadItems"]]
        self.assertEqual(file_names, ["a.png", "b.png", "file_3.bin", "file_4.bin"])

    def test_more_than_four_file_names_uses_only_first_four(self) -> None:
        event = {
            "body": json.dumps(
                {"fileNames": ["a.png", "b.png", "c.png", "d.png", "e.png", "f.png"]}
            )
        }

        with patch.dict("os.environ", {"UPLOAD_BUCKET_NAME": "test-bucket"}, clear=True), patch.object(
            handler.s3_client,
            "generate_presigned_url",
            side_effect=["url1", "url2", "url3", "url4"],
        ) as mocked_presign:
            response = handler.lambda_handler(event, None)

        self.assertEqual(response["statusCode"], 200)
        payload = json.loads(response["body"])
        file_names = [item["fileName"] for item in payload["uploadItems"]]
        self.assertEqual(file_names, ["a.png", "b.png", "c.png", "d.png"])
        self.assertEqual(mocked_presign.call_count, 4)

    def test_default_expires_in_applies_when_env_var_absent(self) -> None:
        event = {"body": json.dumps({"fileNames": ["a.png"]})}

        with patch.dict("os.environ", {"UPLOAD_BUCKET_NAME": "test-bucket"}, clear=True), patch.object(
            handler.s3_client,
            "generate_presigned_url",
            side_effect=["url1", "url2", "url3", "url4"],
        ):
            response = handler.lambda_handler(event, None)

        payload = json.loads(response["body"])
        self.assertEqual(payload["expiresIn"], 900)

    def test_malformed_json_body_falls_back_to_query_params(self) -> None:
        event = {
            "queryStringParameters": {"fileName": "fallback.png"},
            "body": "{not valid json",
        }

        with patch.dict("os.environ", {"UPLOAD_BUCKET_NAME": "test-bucket"}, clear=True), patch.object(
            handler.s3_client,
            "generate_presigned_url",
            side_effect=["url1", "url2", "url3", "url4"],
        ):
            response = handler.lambda_handler(event, None)

        self.assertEqual(response["statusCode"], 200)
        payload = json.loads(response["body"])
        self.assertEqual(payload["uploadItems"][0]["fileName"], "fallback.png")

    def test_invalid_expires_in_returns_500(self) -> None:
        with patch.dict(
            "os.environ",
            {"UPLOAD_BUCKET_NAME": "test-bucket", "UPLOAD_URL_EXPIRES_SECONDS": "not-a-number"},
            clear=True,
        ):
            response = handler.lambda_handler({}, None)

        self.assertEqual(response["statusCode"], 500)
        payload = json.loads(response["body"])
        self.assertIn("UPLOAD_URL_EXPIRES_SECONDS", payload["error"])


class TestStructuredLogging(unittest.TestCase):
    """Tests for structured JSON logging functionality."""

    def _capture_logs(self, test_func) -> str:
        """Helper to capture log output during a test function.

        Args:
            test_func: A callable that triggers logging.

        Returns:
            The captured log output as a string.
        """
        log_stream = io.StringIO()
        test_handler = logging.StreamHandler(log_stream)
        test_handler.setFormatter(JSONFormatter())

        original_handlers = handler.logger.handlers[:]
        handler.logger.handlers = [test_handler]
        handler.logger.setLevel(logging.INFO)

        try:
            test_func()
        finally:
            handler.logger.handlers = original_handlers

        return log_stream.getvalue()

    def _parse_json_logs(self, log_output: str) -> list:
        """Parse JSON log lines from raw log output.

        Args:
            log_output: Raw log output with one JSON object per line.

        Returns:
            List of parsed log dicts.
        """
        logs = []
        for line in log_output.strip().split("\n"):
            if line:
                try:
                    logs.append(json.loads(line))
                except json.JSONDecodeError:
                    self.fail(f"Invalid JSON in log line: {line}")
        return logs

    def test_successful_request_logs_json_with_required_fields(self) -> None:
        """Verify successful request logs contain all required JSON fields."""
        fixed_uuid = uuid.UUID("11111111-1111-1111-1111-111111111111")
        job_id = "test-job-123"

        event = {
            "queryStringParameters": {"jobId": job_id},
            "body": json.dumps(
                {
                    "fileNames": ["a.png", "b.png", "c.png", "d.png"],
                    "contentTypes": ["image/png"] * 4,
                }
            ),
        }

        def test_func():
            with patch.dict(
                "os.environ",
                {"UPLOAD_BUCKET_NAME": "test-bucket"},
                clear=True,
            ), patch("handler.uuid.uuid4", return_value=fixed_uuid), patch.object(
                handler.s3_client,
                "generate_presigned_url",
                side_effect=["url1", "url2", "url3", "url4"],
            ):
                handler.lambda_handler(event, None)

        log_output = self._capture_logs(test_func)
        logs = self._parse_json_logs(log_output)

        self.assertGreaterEqual(len(logs), 2, "Expected at least 2 log lines")

        # Verify all logs have the correct jobId (extracted immediately at handler start).
        for log in logs:
            self.assertEqual(log["jobId"], job_id)
            self.assertIn("timestamp", log)
            self.assertIn("level", log)
            self.assertIn("stage", log)
            self.assertIn("message", log)
            self.assertIn(log["level"], ["INFO", "WARNING", "ERROR"])

        # Verify first log is from parse_input stage.
        self.assertEqual(logs[0]["stage"], "parse_input")

    def test_error_case_logs_error_stage_with_full_details(self) -> None:
        """Verify error case logs with error stage and exception details."""
        job_id = "error-job-456"
        event = {
            "queryStringParameters": {"jobId": job_id},
            "body": json.dumps({"fileNames": ["test.png"]}),
        }

        def test_func():
            with patch.dict(
                "os.environ",
                {"UPLOAD_BUCKET_NAME": "test-bucket"},
                clear=True,
            ), patch.object(
                handler.s3_client,
                "generate_presigned_url",
                side_effect=ClientError(
                    error_response={"Error": {"Code": "NoSuchBucket", "Message": "bucket not found"}},
                    operation_name="PutObject",
                ),
            ):
                handler.lambda_handler(event, None)

        log_output = self._capture_logs(test_func)
        logs = self._parse_json_logs(log_output)

        # Find error log.
        error_logs = [log for log in logs if log["level"] == "ERROR"]
        self.assertGreater(len(error_logs), 0, "Expected at least one ERROR log")

        error_log = error_logs[0]
        self.assertEqual(error_log["jobId"], job_id)
        self.assertEqual(error_log["stage"], "error")
        # Verify exception details are in the message
        self.assertIn("NoSuchBucket", error_log["message"])

    def test_job_id_extracted_from_query_params(self) -> None:
        """Verify jobId is correctly extracted from query parameters."""
        job_id = "query-job-789"
        event = {"queryStringParameters": {"jobId": job_id}}

        def test_func():
            with patch.dict(
                "os.environ",
                {"UPLOAD_BUCKET_NAME": "test-bucket"},
                clear=True,
            ), patch.object(
                handler.s3_client,
                "generate_presigned_url",
                side_effect=["url1", "url2", "url3", "url4"],
            ):
                handler.lambda_handler(event, None)

        log_output = self._capture_logs(test_func)
        logs = self._parse_json_logs(log_output)

        # All logs after the first should have the extracted jobId.
        for log in logs[1:]:
            self.assertEqual(log["jobId"], job_id)

    def test_job_id_extracted_from_body(self) -> None:
        """Verify jobId is correctly extracted from JSON body."""
        job_id = "body-job-xyz"
        event = {
            "body": json.dumps({"jobId": job_id, "fileNames": ["test.png"]})
        }

        def test_func():
            with patch.dict(
                "os.environ",
                {"UPLOAD_BUCKET_NAME": "test-bucket"},
                clear=True,
            ), patch.object(
                handler.s3_client,
                "generate_presigned_url",
                side_effect=["url1", "url2", "url3", "url4"],
            ):
                handler.lambda_handler(event, None)

        log_output = self._capture_logs(test_func)
        logs = self._parse_json_logs(log_output)

        # All logs after the first should have the extracted jobId.
        for log in logs[1:]:
            self.assertEqual(log["jobId"], job_id)

    def test_missing_job_id_defaults_to_unknown(self) -> None:
        """Verify missing jobId defaults to 'unknown' throughout."""
        event = {"body": json.dumps({"fileNames": ["test.png"]})}

        def test_func():
            with patch.dict(
                "os.environ",
                {"UPLOAD_BUCKET_NAME": "test-bucket"},
                clear=True,
            ), patch.object(
                handler.s3_client,
                "generate_presigned_url",
                side_effect=["url1", "url2", "url3", "url4"],
            ):
                handler.lambda_handler(event, None)

        log_output = self._capture_logs(test_func)
        logs = self._parse_json_logs(log_output)

        # First log should have "unknown" (pre-parse).
        self.assertEqual(logs[0]["jobId"], "unknown")

        # All subsequent logs should also have "unknown" since jobId wasn't provided.
        for log in logs[1:]:
            self.assertEqual(log["jobId"], "unknown")

    def test_log_contains_count_not_full_urls(self) -> None:
        """Verify success log contains count, not full URLs."""
        job_id = "count-job-123"
        event = {
            "queryStringParameters": {"jobId": job_id},
            "body": json.dumps({"fileNames": ["a.png", "b.png", "c.png", "d.png"]}),
        }

        def test_func():
            with patch.dict(
                "os.environ",
                {"UPLOAD_BUCKET_NAME": "test-bucket"},
                clear=True,
            ), patch.object(
                handler.s3_client,
                "generate_presigned_url",
                side_effect=["url1", "url2", "url3", "url4"],
            ):
                handler.lambda_handler(event, None)

        log_output = self._capture_logs(test_func)
        logs = self._parse_json_logs(log_output)

        # Find success log mentioning URL count.
        success_logs = [
            log for log in logs
            if "generated" in log["message"].lower() and "4" in log["message"]
        ]
        self.assertGreater(len(success_logs), 0)

        success_log = success_logs[0]
        self.assertNotIn("url1", success_log["message"])
        self.assertNotIn("url2", success_log["message"])
        self.assertNotIn("http", success_log["message"].lower())

    def test_env_var_error_logs_with_error_stage(self) -> None:
        """Verify environment variable errors log with error stage."""

        def test_func():
            with patch.dict("os.environ", {}, clear=True):
                handler.lambda_handler({}, None)

        log_output = self._capture_logs(test_func)
        logs = self._parse_json_logs(log_output)

        # Should have error logs.
        error_logs = [log for log in logs if log["level"] == "ERROR"]
        self.assertGreater(len(error_logs), 0)
        self.assertEqual(error_logs[0]["stage"], "error")


if __name__ == "__main__":
    unittest.main()
