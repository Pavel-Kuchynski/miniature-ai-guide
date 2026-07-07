"""Integration tests for the presigned URL Lambda handler.

Tests the complete request/response flow for presigned URL generation,
covering happy path (200), error cases (500), and edge cases.
"""

import json
import uuid
from typing import Any, Dict
from unittest.mock import patch

import pytest

import handler
from handler import lambda_handler


class TestPresignedUrlGeneration:
    """Tests for presigned URL generation flow."""

    def test_generates_four_presigned_urls_on_success(
        self, monkeypatch: Any, sample_event: Dict[str, Any]
    ) -> None:
        """Successful request generates 4 presigned upload URLs with 200 status."""
        monkeypatch.setenv("UPLOAD_BUCKET_NAME", "test-bucket")
        monkeypatch.setenv("UPLOAD_URL_EXPIRES_SECONDS", "600")

        with patch("handler.uuid.uuid4") as mock_uuid:
            mock_uuid.return_value = uuid.UUID("11111111-1111-1111-1111-111111111111")
            with patch.object(
                handler.s3_client,
                "generate_presigned_url",
                side_effect=["url1", "url2", "url3", "url4"],
            ) as mock_presign:
                response = lambda_handler(sample_event, None)

        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert body["bucket"] == "test-bucket"
        assert body["folder"] == "11111111-1111-1111-1111-111111111111"
        assert body["expiresIn"] == 600
        assert len(body["uploadItems"]) == 4
        assert mock_presign.call_count == 4

    def test_all_four_files_share_same_uuid_folder(
        self, monkeypatch: Any, sample_event: Dict[str, Any]
    ) -> None:
        """All 4 presigned URLs are under the same UUID folder."""
        monkeypatch.setenv("UPLOAD_BUCKET_NAME", "test-bucket")

        with patch("handler.uuid.uuid4") as mock_uuid:
            mock_uuid.return_value = uuid.UUID("22222222-2222-2222-2222-222222222222")
            with patch.object(
                handler.s3_client,
                "generate_presigned_url",
                side_effect=["url1", "url2", "url3", "url4"],
            ):
                response = lambda_handler(sample_event, None)

        body = json.loads(response["body"])
        prefix = body["prefix"]
        expected_prefix = "uploads/22222222-2222-2222-2222-222222222222"
        assert prefix == expected_prefix

        for item in body["uploadItems"]:
            assert item["key"].startswith(expected_prefix)

    def test_default_expires_in_applies_when_env_var_absent(
        self, monkeypatch: Any, sample_event: Dict[str, Any]
    ) -> None:
        """Default expires_in (900 seconds) is used when env var is absent."""
        monkeypatch.setenv("UPLOAD_BUCKET_NAME", "test-bucket")
        monkeypatch.delenv("UPLOAD_URL_EXPIRES_SECONDS", raising=False)

        with patch.object(
            handler.s3_client,
            "generate_presigned_url",
            side_effect=["url1", "url2", "url3", "url4"],
        ):
            response = lambda_handler(sample_event, None)

        body = json.loads(response["body"])
        assert body["expiresIn"] == 900

    def test_response_includes_cors_headers(
        self, monkeypatch: Any, sample_event: Dict[str, Any]
    ) -> None:
        """Response includes CORS headers for cross-origin requests."""
        monkeypatch.setenv("UPLOAD_BUCKET_NAME", "test-bucket")

        with patch.object(
            handler.s3_client,
            "generate_presigned_url",
            side_effect=["url1", "url2", "url3", "url4"],
        ):
            response = lambda_handler(sample_event, None)

        assert "headers" in response
        headers = response["headers"]
        assert headers["Content-Type"] == "application/json"
        assert headers["Access-Control-Allow-Origin"] == "*"
        assert "Access-Control-Allow-Methods" in headers
        assert "Access-Control-Allow-Headers" in headers

    def test_partial_file_names_fall_back_to_defaults(
        self, monkeypatch: Any
    ) -> None:
        """Missing file names fall back to file_N.bin for remaining slots."""
        monkeypatch.setenv("UPLOAD_BUCKET_NAME", "test-bucket")

        event = {
            "body": json.dumps({
                "fileNames": ["provided_1.png", "provided_2.png"],
                "contentTypes": ["image/png", "image/png"],
            }),
        }

        with patch.object(
            handler.s3_client,
            "generate_presigned_url",
            side_effect=["url1", "url2", "url3", "url4"],
        ):
            response = lambda_handler(event, None)

        body = json.loads(response["body"])
        file_names = [item["fileName"] for item in body["uploadItems"]]
        assert file_names == ["provided_1.png", "provided_2.png", "file_3.bin", "file_4.bin"]

    def test_more_than_four_file_names_uses_only_first_four(
        self, monkeypatch: Any
    ) -> None:
        """Extra file names beyond the first 4 are ignored."""
        monkeypatch.setenv("UPLOAD_BUCKET_NAME", "test-bucket")

        event = {
            "body": json.dumps({
                "fileNames": ["a.png", "b.png", "c.png", "d.png", "e.png", "f.png"],
                "contentTypes": ["image/png"] * 6,
            }),
        }

        with patch.object(
            handler.s3_client,
            "generate_presigned_url",
            side_effect=["url1", "url2", "url3", "url4"],
        ) as mock_presign:
            response = lambda_handler(event, None)

        body = json.loads(response["body"])
        file_names = [item["fileName"] for item in body["uploadItems"]]
        assert file_names == ["a.png", "b.png", "c.png", "d.png"]
        assert mock_presign.call_count == 4

    def test_missing_content_types_fall_back_to_first_provided_or_default(
        self, monkeypatch: Any
    ) -> None:
        """Missing content types fall back to first provided, or default."""
        monkeypatch.setenv("UPLOAD_BUCKET_NAME", "test-bucket")

        event = {
            "body": json.dumps({
                "fileNames": ["a.png", "b.png", "c.png", "d.png"],
                "contentTypes": ["image/custom"],
            }),
        }

        with patch.object(
            handler.s3_client,
            "generate_presigned_url",
            side_effect=["url1", "url2", "url3", "url4"],
        ):
            response = lambda_handler(event, None)

        body = json.loads(response["body"])
        content_types = [item["contentType"] for item in body["uploadItems"]]
        # All items use the provided content type
        assert all(ct == "image/custom" for ct in content_types)

    def test_no_content_types_provided_uses_octet_stream_default(
        self, monkeypatch: Any
    ) -> None:
        """No content types provided defaults to application/octet-stream."""
        monkeypatch.setenv("UPLOAD_BUCKET_NAME", "test-bucket")

        event = {
            "body": json.dumps({
                "fileNames": ["file_1.bin", "file_2.bin", "file_3.bin", "file_4.bin"],
            }),
        }

        with patch.object(
            handler.s3_client,
            "generate_presigned_url",
            side_effect=["url1", "url2", "url3", "url4"],
        ):
            response = lambda_handler(event, None)

        body = json.loads(response["body"])
        content_types = [item["contentType"] for item in body["uploadItems"]]
        assert all(ct == "application/octet-stream" for ct in content_types)


class TestErrorHandling:
    """Tests for error cases and error responses."""

    def test_missing_upload_bucket_env_returns_500(
        self, monkeypatch: Any, sample_event: Dict[str, Any]
    ) -> None:
        """Missing UPLOAD_BUCKET_NAME env var returns HTTP 500."""
        monkeypatch.delenv("UPLOAD_BUCKET_NAME", raising=False)

        response = lambda_handler(sample_event, None)

        assert response["statusCode"] == 500
        body = json.loads(response["body"])
        assert "error" in body
        assert "UPLOAD_BUCKET_NAME" in body["error"]

    def test_invalid_expires_in_env_returns_500(
        self, monkeypatch: Any, sample_event: Dict[str, Any]
    ) -> None:
        """Invalid UPLOAD_URL_EXPIRES_SECONDS env var returns HTTP 500."""
        monkeypatch.setenv("UPLOAD_BUCKET_NAME", "test-bucket")
        monkeypatch.setenv("UPLOAD_URL_EXPIRES_SECONDS", "not-a-number")

        response = lambda_handler(sample_event, None)

        assert response["statusCode"] == 500
        body = json.loads(response["body"])
        assert "error" in body
        assert "UPLOAD_URL_EXPIRES_SECONDS" in body["error"]

    def test_expires_in_out_of_range_returns_500(
        self, monkeypatch: Any, sample_event: Dict[str, Any]
    ) -> None:
        """UPLOAD_URL_EXPIRES_SECONDS outside valid range returns HTTP 500."""
        monkeypatch.setenv("UPLOAD_BUCKET_NAME", "test-bucket")
        monkeypatch.setenv("UPLOAD_URL_EXPIRES_SECONDS", "999999999")

        response = lambda_handler(sample_event, None)

        assert response["statusCode"] == 500
        body = json.loads(response["body"])
        assert "error" in body

    def test_s3_client_error_returns_500(
        self, monkeypatch: Any, sample_event: Dict[str, Any]
    ) -> None:
        """S3 client error during presigned URL generation returns 500."""
        monkeypatch.setenv("UPLOAD_BUCKET_NAME", "test-bucket")

        from botocore.exceptions import ClientError

        with patch.object(
            handler.s3_client,
            "generate_presigned_url",
            side_effect=ClientError(
                error_response={"Error": {"Code": "NoSuchBucket", "Message": "Bucket not found"}},
                operation_name="PutObject",
            ),
        ):
            response = lambda_handler(sample_event, None)

        assert response["statusCode"] == 500
        body = json.loads(response["body"])
        assert "error" in body
        assert "Failed" in body["error"] or "error" in body["error"].lower()


class TestParameterPrecedence:
    """Tests for query string vs JSON body parameter precedence."""

    def test_body_file_names_override_query_file_names(
        self, monkeypatch: Any
    ) -> None:
        """JSON body file names take precedence over query string."""
        monkeypatch.setenv("UPLOAD_BUCKET_NAME", "test-bucket")

        event = {
            "queryStringParameters": {"fileNames": "query.png"},
            "body": json.dumps({"fileNames": ["body.png", "body2.png", "body3.png", "body4.png"]}),
        }

        with patch.object(
            handler.s3_client,
            "generate_presigned_url",
            side_effect=["url1", "url2", "url3", "url4"],
        ):
            response = lambda_handler(event, None)

        body = json.loads(response["body"])
        first_file = body["uploadItems"][0]["fileName"]
        assert first_file == "body.png"

    def test_body_content_types_override_query_content_types(
        self, monkeypatch: Any
    ) -> None:
        """JSON body content types take precedence over query string."""
        monkeypatch.setenv("UPLOAD_BUCKET_NAME", "test-bucket")

        event = {
            "queryStringParameters": {"contentTypes": "image/png"},
            "body": json.dumps({
                "fileNames": ["a.bin", "b.bin", "c.bin", "d.bin"],
                "contentTypes": ["image/jpeg"],
            }),
        }

        with patch.object(
            handler.s3_client,
            "generate_presigned_url",
            side_effect=["url1", "url2", "url3", "url4"],
        ):
            response = lambda_handler(event, None)

        body = json.loads(response["body"])
        content_types = [item["contentType"] for item in body["uploadItems"]]
        assert all(ct == "image/jpeg" for ct in content_types)


class TestJobIdHandling:
    """Tests for jobId extraction and usage in logging."""

    def test_job_id_extracted_from_query_string(
        self, monkeypatch: Any
    ) -> None:
        """jobId is extracted from query string parameters."""
        monkeypatch.setenv("UPLOAD_BUCKET_NAME", "test-bucket")

        event = {
            "queryStringParameters": {"jobId": "test-job-123"},
            "body": json.dumps({"fileNames": ["a.bin", "b.bin", "c.bin", "d.bin"]}),
        }

        with patch.object(
            handler.s3_client,
            "generate_presigned_url",
            side_effect=["url1", "url2", "url3", "url4"],
        ):
            response = lambda_handler(event, None)

        assert response["statusCode"] == 200

    def test_job_id_extracted_from_body(
        self, monkeypatch: Any
    ) -> None:
        """jobId is extracted from JSON body."""
        monkeypatch.setenv("UPLOAD_BUCKET_NAME", "test-bucket")

        event = {
            "body": json.dumps({
                "jobId": "body-job-456",
                "fileNames": ["a.bin", "b.bin", "c.bin", "d.bin"],
            }),
        }

        with patch.object(
            handler.s3_client,
            "generate_presigned_url",
            side_effect=["url1", "url2", "url3", "url4"],
        ):
            response = lambda_handler(event, None)

        assert response["statusCode"] == 200

    def test_missing_job_id_defaults_to_unknown(
        self, monkeypatch: Any
    ) -> None:
        """Missing jobId defaults to 'unknown' and doesn't cause errors."""
        monkeypatch.setenv("UPLOAD_BUCKET_NAME", "test-bucket")

        event = {
            "body": json.dumps({
                "fileNames": ["a.bin", "b.bin", "c.bin", "d.bin"],
            }),
        }

        with patch.object(
            handler.s3_client,
            "generate_presigned_url",
            side_effect=["url1", "url2", "url3", "url4"],
        ):
            response = lambda_handler(event, None)

        assert response["statusCode"] == 200
