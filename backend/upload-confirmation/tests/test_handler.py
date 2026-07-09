"""Tests for the upload-confirmation Lambda.

Covers the `parse_job_id` input-validation helper (TASK-02), the `list_uploaded_images`
S3 presence check (TASK-03), and the `put_job_item` DynamoDB write (TASK-04). Handler
orchestration tests are in manual_test.py (TASK-05).
"""

import datetime
import json
from typing import Any, Dict, Iterator
from unittest.mock import patch

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from handler import lambda_handler, list_uploaded_images, parse_job_id, put_job_item

BUCKET_NAME = "test-upload-bucket"
TABLE_NAME = "test-jobs-table"
JOB_ID = "123e4567-e89b-12d3-a456-426614174000"


class TestParseJobId:
    """Tests for the `parse_job_id` request-validation helper."""

    def test_missing_job_id_returns_400(self) -> None:
        """No `jobId` key at all should be rejected as an invalid request."""
        job_id, error_response = parse_job_id({})

        assert job_id is None
        assert error_response["statusCode"] == 400
        body = json.loads(error_response["body"])
        assert body == {"error": "InvalidRequest", "message": "jobId is required"}

    def test_empty_string_job_id_returns_400(self) -> None:
        """An empty string `jobId` should be rejected as an invalid request."""
        job_id, error_response = parse_job_id({"jobId": ""})

        assert job_id is None
        assert error_response["statusCode"] == 400

    def test_non_string_job_id_returns_400(self) -> None:
        """A non-string `jobId` (e.g. a number) should be rejected."""
        job_id, error_response = parse_job_id({"jobId": 123})

        assert job_id is None
        assert error_response["statusCode"] == 400
        body = json.loads(error_response["body"])
        assert body["error"] == "InvalidRequest"

    def test_whitespace_only_job_id_returns_400(self) -> None:
        """A whitespace-only `jobId` should be rejected."""
        job_id, error_response = parse_job_id({"jobId": "   "})

        assert job_id is None
        assert error_response["statusCode"] == 400

    def test_valid_job_id_returns_job_id_and_no_error(self) -> None:
        """A present, non-empty `jobId` should be returned trimmed with no error."""
        job_id, error_response = parse_job_id(
            {"jobId": "123e4567-e89b-12d3-a456-426614174000"}
        )

        assert job_id == "123e4567-e89b-12d3-a456-426614174000"
        assert error_response is None

    def test_valid_job_id_is_trimmed(self) -> None:
        """Leading/trailing whitespace around a valid `jobId` should be trimmed."""
        job_id, error_response = parse_job_id({"jobId": "  abc123  "})

        assert job_id == "abc123"
        assert error_response is None

    def test_query_string_parameter_job_id(self) -> None:
        """A valid `jobId` in queryStringParameters should be returned when top-level is missing."""
        job_id, error_response = parse_job_id(
            {"queryStringParameters": {"jobId": "query-job-id-123"}}
        )

        assert job_id == "query-job-id-123"
        assert error_response is None

    def test_query_string_parameter_job_id_with_whitespace(self) -> None:
        """A `jobId` in queryStringParameters should be trimmed just like top-level."""
        job_id, error_response = parse_job_id(
            {"queryStringParameters": {"jobId": "  query-job-id  "}}
        )

        assert job_id == "query-job-id"
        assert error_response is None

    def test_query_string_parameter_empty_string_returns_400(self) -> None:
        """An empty string `jobId` in queryStringParameters should be rejected."""
        job_id, error_response = parse_job_id(
            {"queryStringParameters": {"jobId": ""}}
        )

        assert job_id is None
        assert error_response["statusCode"] == 400

    def test_base64_encoded_body_with_valid_job_id(self) -> None:
        """A valid `jobId` in a base64-encoded JSON body should be decoded and returned."""
        import base64

        body_json = json.dumps({"jobId": "base64-job-id"})
        encoded_body = base64.b64encode(body_json.encode("utf-8")).decode("utf-8")

        job_id, error_response = parse_job_id(
            {
                "body": encoded_body,
                "isBase64Encoded": True,
            }
        )

        assert job_id == "base64-job-id"
        assert error_response is None

    def test_base64_decoding_error_falls_back_gracefully(self) -> None:
        """Invalid base64 in body should fall back to query params and return 400 if no fallback."""
        job_id, error_response = parse_job_id(
            {
                "body": "not-valid-base64!!!",
                "isBase64Encoded": True,
            }
        )

        assert job_id is None
        assert error_response["statusCode"] == 400

    def test_unicode_decode_error_in_base64_falls_back_gracefully(self) -> None:
        """Invalid UTF-8 in decoded base64 should fall back and return 400 if no fallback."""
        import base64

        # Create invalid UTF-8 bytes by encoding an invalid sequence
        invalid_utf8 = b"\xff\xfe"
        encoded_body = base64.b64encode(invalid_utf8).decode("utf-8")

        job_id, error_response = parse_job_id(
            {
                "body": encoded_body,
                "isBase64Encoded": True,
            }
        )

        assert job_id is None
        assert error_response["statusCode"] == 400

    def test_json_parsing_error_in_base64_body_falls_back_gracefully(self) -> None:
        """Invalid JSON in decoded base64 body should fall back and return 400 if no fallback."""
        import base64

        invalid_json = '{"jobId": unclosed-string'
        encoded_body = base64.b64encode(invalid_json.encode("utf-8")).decode("utf-8")

        job_id, error_response = parse_job_id(
            {
                "body": encoded_body,
                "isBase64Encoded": True,
            }
        )

        assert job_id is None
        assert error_response["statusCode"] == 400

    def test_base64_body_with_missing_job_id_returns_400(self) -> None:
        """A valid JSON body without `jobId` should fall back and return 400 if no other source."""
        import base64

        body_json = json.dumps({"otherField": "value"})
        encoded_body = base64.b64encode(body_json.encode("utf-8")).decode("utf-8")

        job_id, error_response = parse_job_id(
            {
                "body": encoded_body,
                "isBase64Encoded": True,
            }
        )

        assert job_id is None
        assert error_response["statusCode"] == 400

    def test_base64_body_with_non_string_job_id_returns_400(self) -> None:
        """A JSON body with non-string `jobId` should fall back and return 400."""
        import base64

        body_json = json.dumps({"jobId": 12345})
        encoded_body = base64.b64encode(body_json.encode("utf-8")).decode("utf-8")

        job_id, error_response = parse_job_id(
            {
                "body": encoded_body,
                "isBase64Encoded": True,
            }
        )

        assert job_id is None
        assert error_response["statusCode"] == 400

    def test_base64_body_with_non_dict_json_returns_400(self) -> None:
        """A base64-encoded JSON body that's not a dict (e.g. array) should fall back."""
        import base64

        body_json = json.dumps(["item1", "item2"])
        encoded_body = base64.b64encode(body_json.encode("utf-8")).decode("utf-8")

        job_id, error_response = parse_job_id(
            {
                "body": encoded_body,
                "isBase64Encoded": True,
            }
        )

        assert job_id is None
        assert error_response["statusCode"] == 400


class TestListUploadedImages:
    """Tests for the `list_uploaded_images` S3 presence-check helper."""

    @pytest.fixture(autouse=True)
    def _mocked_bucket(self, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
        """Create a mocked S3 bucket and point `UPLOAD_BUCKET_NAME` at it."""
        monkeypatch.setenv("UPLOAD_BUCKET_NAME", BUCKET_NAME)
        with mock_aws():
            s3_client = boto3.client("s3", region_name="us-east-1")
            s3_client.create_bucket(Bucket=BUCKET_NAME)
            self.s3_client = s3_client
            yield

    def _put_object(self, key: str, body: bytes = b"fake-image-bytes") -> None:
        """Upload a test object under the bucket used by these tests."""
        self.s3_client.put_object(Bucket=BUCKET_NAME, Key=key, Body=body)

    def test_returns_all_urls_for_exactly_four_objects(self) -> None:
        """Exactly 4 uploaded objects should yield 4 sorted `s3://` URLs."""
        for name in ["b.png", "a.png", "d.png", "c.png"]:
            self._put_object(f"uploads/{JOB_ID}/{name}")

        urls = list_uploaded_images(JOB_ID)

        assert urls == [
            f"s3://{BUCKET_NAME}/uploads/{JOB_ID}/a.png",
            f"s3://{BUCKET_NAME}/uploads/{JOB_ID}/b.png",
            f"s3://{BUCKET_NAME}/uploads/{JOB_ID}/c.png",
            f"s3://{BUCKET_NAME}/uploads/{JOB_ID}/d.png",
        ]

    def test_ignores_zero_byte_marker_objects(self) -> None:
        """Zero-byte "folder marker" objects should be excluded from the result."""
        self._put_object(f"uploads/{JOB_ID}/", body=b"")
        for name in ["a.png", "b.png", "c.png", "d.png"]:
            self._put_object(f"uploads/{JOB_ID}/{name}")

        urls = list_uploaded_images(JOB_ID)

        assert len(urls) == 4
        assert f"s3://{BUCKET_NAME}/uploads/{JOB_ID}/" not in urls

    def test_returns_fewer_than_four_without_padding(self) -> None:
        """Fewer than 4 uploaded objects should be returned as-is, with no padding."""
        for name in ["a.png", "b.png"]:
            self._put_object(f"uploads/{JOB_ID}/{name}")

        urls = list_uploaded_images(JOB_ID)

        assert len(urls) == 2

    def test_returns_more_than_four_without_truncation(self) -> None:
        """More than 4 uploaded objects should be returned in full, with no truncation."""
        for name in ["a.png", "b.png", "c.png", "d.png", "e.png"]:
            self._put_object(f"uploads/{JOB_ID}/{name}")

        urls = list_uploaded_images(JOB_ID)

        assert len(urls) == 5

    def test_returns_empty_list_for_empty_prefix(self) -> None:
        """A job id with no uploaded objects should yield an empty list."""
        urls = list_uploaded_images(JOB_ID)

        assert urls == []

    def test_only_lists_objects_under_the_given_job_prefix(self) -> None:
        """Objects under a different job id's prefix should not be included."""
        self._put_object(f"uploads/{JOB_ID}/a.png")
        self._put_object("uploads/some-other-job/b.png")

        urls = list_uploaded_images(JOB_ID)

        assert urls == [f"s3://{BUCKET_NAME}/uploads/{JOB_ID}/a.png"]

    def test_handles_pagination_beyond_default_page_size(self) -> None:
        """More than 1000 objects under a prefix should all be returned (paginated)."""
        object_count = 1001
        for index in range(object_count):
            self._put_object(f"uploads/{JOB_ID}/img_{index:04d}.png")

        urls = list_uploaded_images(JOB_ID)

        assert len(urls) == object_count

    def test_propagates_client_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A `ClientError` from S3 (e.g. missing bucket) should propagate unchanged."""
        monkeypatch.setenv("UPLOAD_BUCKET_NAME", "bucket-that-does-not-exist")

        with pytest.raises(ClientError):
            list_uploaded_images(JOB_ID)


class TestPutJobItem:
    """Tests for the `put_job_item` DynamoDB write helper."""

    IMAGE_URLS = [f"s3://{BUCKET_NAME}/uploads/{JOB_ID}/a.png"]

    @pytest.fixture(autouse=True)
    def _mocked_table(self, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
        """Create a mocked DynamoDB table and point `JOBS_TABLE_NAME` at it."""
        monkeypatch.setenv("JOBS_TABLE_NAME", TABLE_NAME)
        monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
        with mock_aws():
            dynamodb_client = boto3.client("dynamodb", region_name="us-east-1")
            dynamodb_client.create_table(
                TableName=TABLE_NAME,
                KeySchema=[{"AttributeName": "jobId", "KeyType": "HASH"}],
                AttributeDefinitions=[{"AttributeName": "jobId", "AttributeType": "S"}],
                BillingMode="PAY_PER_REQUEST",
            )
            self.dynamodb_client = dynamodb_client
            yield

    def _get_raw_item(self, job_id: str) -> Dict[str, Any]:
        """Fetch the raw (low-level, typed-attribute) item for a job id."""
        response = self.dynamodb_client.get_item(
            TableName=TABLE_NAME, Key={"jobId": {"S": job_id}}
        )
        return response["Item"]

    def test_first_call_creates_item_with_required_fields(self) -> None:
        """The first call for a new `jobId` should create the item with all required fields."""
        created, item = put_job_item(JOB_ID, self.IMAGE_URLS)

        assert created is True
        assert item["jobId"]["S"] == JOB_ID
        assert item["imageUrls"]["L"] == [{"S": url} for url in self.IMAGE_URLS]
        assert item["jobStatus"]["S"] == "UPLOADED"
        assert item["createdAt"]["S"] == item["updatedAt"]["S"]

        stored_item = self._get_raw_item(JOB_ID)
        assert stored_item == item

    def test_ttl_is_seven_days_from_created_at(self) -> None:
        """`ttl` should be `createdAt` plus 7 days, expressed as epoch seconds."""
        _, item = put_job_item(JOB_ID, self.IMAGE_URLS)

        created_at = datetime.datetime.fromisoformat(item["createdAt"]["S"])
        expected_ttl = int(created_at.timestamp()) + 7 * 24 * 60 * 60
        assert int(item["ttl"]["N"]) == pytest.approx(expected_ttl, abs=2)

    def test_optional_fields_are_absent_not_null(self) -> None:
        """`connectionId`, `pdfUrl`, and `errorMessage` must be absent from the written item."""
        _, item = put_job_item(JOB_ID, self.IMAGE_URLS)

        assert "connectionId" not in item
        assert "pdfUrl" not in item
        assert "errorMessage" not in item

        stored_item = self._get_raw_item(JOB_ID)
        assert "connectionId" not in stored_item
        assert "pdfUrl" not in stored_item
        assert "errorMessage" not in stored_item

    def test_second_call_does_not_overwrite_and_returns_created_false(self) -> None:
        """A duplicate call for the same `jobId` should return the original item unchanged."""
        _, first_item = put_job_item(JOB_ID, self.IMAGE_URLS)

        created, second_item = put_job_item(JOB_ID, ["s3://different/url.png"])

        assert created is False
        assert second_item == first_item

        stored_item = self._get_raw_item(JOB_ID)
        assert stored_item == first_item

    def test_client_error_other_than_conditional_check_propagates(self) -> None:
        """A non-conditional `ClientError` (e.g. throttling) must propagate, not be misread as a duplicate."""
        throttling_error = ClientError(
            error_response={
                "Error": {"Code": "ProvisionedThroughputExceededException", "Message": "Throttled"}
            },
            operation_name="PutItem",
        )

        with patch("handler.boto3.client") as mock_client:
            mock_client.return_value.put_item.side_effect = throttling_error

            with pytest.raises(ClientError) as exc_info:
                put_job_item(JOB_ID, self.IMAGE_URLS)

        assert exc_info.value.response["Error"]["Code"] == "ProvisionedThroughputExceededException"

    def test_conditional_check_failure_with_no_existing_item_raises(self) -> None:
        """If `GetItem` unexpectedly finds nothing after a collision, raise rather than fabricate."""
        conditional_error = ClientError(
            error_response={
                "Error": {"Code": "ConditionalCheckFailedException", "Message": "collision"}
            },
            operation_name="PutItem",
        )

        with patch("handler.boto3.client") as mock_client:
            mock_dynamodb = mock_client.return_value
            mock_dynamodb.put_item.side_effect = conditional_error
            mock_dynamodb.get_item.return_value = {}

            with pytest.raises(RuntimeError):
                put_job_item(JOB_ID, self.IMAGE_URLS)


class TestLambdaHandler:
    """Tests for the full lambda_handler orchestration."""

    @pytest.fixture(autouse=True)
    def _setup_mocks(self, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
        """Create mocked S3 bucket and DynamoDB table, set up env vars."""
        monkeypatch.setenv("UPLOAD_BUCKET_NAME", BUCKET_NAME)
        monkeypatch.setenv("JOBS_TABLE_NAME", TABLE_NAME)
        monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
        with mock_aws():
            # Set up S3 bucket
            s3_client = boto3.client("s3", region_name="us-east-1")
            s3_client.create_bucket(Bucket=BUCKET_NAME)

            # Set up DynamoDB table
            dynamodb_client = boto3.client("dynamodb", region_name="us-east-1")
            dynamodb_client.create_table(
                TableName=TABLE_NAME,
                KeySchema=[{"AttributeName": "jobId", "KeyType": "HASH"}],
                AttributeDefinitions=[{"AttributeName": "jobId", "AttributeType": "S"}],
                BillingMode="PAY_PER_REQUEST",
            )

            # Upload 4 test images
            for name in ["a.png", "b.png", "c.png", "d.png"]:
                s3_client.put_object(
                    Bucket=BUCKET_NAME,
                    Key=f"uploads/{JOB_ID}/{name}",
                    Body=b"fake-image-bytes",
                )

            yield

    def test_handler_returns_201_for_new_job(self) -> None:
        """A new job with exactly 4 images should return 201 Created."""
        response = lambda_handler({"jobId": JOB_ID}, context=None)

        assert response["statusCode"] == 201
        body = json.loads(response["body"])
        assert body["jobId"] == JOB_ID
        assert body["jobStatus"] == "UPLOADED"

    def test_handler_returns_200_for_duplicate_job(self) -> None:
        """A second confirmation of the same jobId should return 200 OK."""
        # First call
        response1 = lambda_handler({"jobId": JOB_ID}, context=None)
        assert response1["statusCode"] == 201

        # Second call with same jobId
        response2 = lambda_handler({"jobId": JOB_ID}, context=None)

        assert response2["statusCode"] == 200
        body = json.loads(response2["body"])
        assert body["jobId"] == JOB_ID

    def test_handler_returns_400_for_missing_job_id(self) -> None:
        """Missing jobId should return 400 Invalid Request."""
        response = lambda_handler({}, context=None)

        assert response["statusCode"] == 400
        body = json.loads(response["body"])
        assert body["error"] == "InvalidRequest"

    def test_handler_returns_422_for_wrong_image_count(self) -> None:
        """A job with fewer than 4 images should return 422 Unprocessable Entity."""
        # Existing bucket has 4 images for JOB_ID, but create a new job with 2 images
        new_job_id = "different-job-id"
        s3_client = boto3.client("s3", region_name="us-east-1")
        for name in ["a.png", "b.png"]:
            s3_client.put_object(
                Bucket=BUCKET_NAME,
                Key=f"uploads/{new_job_id}/{name}",
                Body=b"fake-image-bytes",
            )

        response = lambda_handler({"jobId": new_job_id}, context=None)

        assert response["statusCode"] == 422
        body = json.loads(response["body"])
        assert body["error"] == "MissingImages"
        assert body["imageCount"] == 2

    def test_handler_missing_upload_bucket_env_var_returns_500(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Missing UPLOAD_BUCKET_NAME environment variable should return 500 InternalError."""
        monkeypatch.delenv("UPLOAD_BUCKET_NAME")

        response = lambda_handler({"jobId": JOB_ID}, context=None)

        assert response["statusCode"] == 500
        body = json.loads(response["body"])
        assert body["error"] == "InternalError"
        assert "S3 bucket name" in body["message"]

    def test_handler_missing_jobs_table_env_var_returns_500(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Missing JOBS_TABLE_NAME environment variable should return 500 InternalError."""
        monkeypatch.delenv("JOBS_TABLE_NAME")

        response = lambda_handler({"jobId": JOB_ID}, context=None)

        assert response["statusCode"] == 500
        body = json.loads(response["body"])
        assert body["error"] == "InternalError"
        assert "DynamoDB table name" in body["message"]

    def test_handler_s3_client_error_returns_500(self) -> None:
        """An S3 ClientError (e.g. throttling) should return 500 InternalError."""
        with patch("handler.list_uploaded_images") as mock_list:
            mock_list.side_effect = ClientError(
                error_response={
                    "Error": {"Code": "ThrottlingException", "Message": "Rate exceeded"}
                },
                operation_name="ListObjectsV2",
            )

            response = lambda_handler({"jobId": JOB_ID}, context=None)

            assert response["statusCode"] == 500
            body = json.loads(response["body"])
            assert body["error"] == "InternalError"
            assert "Failed to list uploaded images" in body["message"]

    def test_handler_dynamodb_client_error_returns_500(self) -> None:
        """A DynamoDB ClientError (e.g. throttling) should return 500 InternalError."""
        with patch("handler.put_job_item") as mock_put:
            mock_put.side_effect = ClientError(
                error_response={
                    "Error": {"Code": "ProvisionedThroughputExceededException"}
                },
                operation_name="PutItem",
            )

            response = lambda_handler({"jobId": JOB_ID}, context=None)

            assert response["statusCode"] == 500
            body = json.loads(response["body"])
            assert body["error"] == "InternalError"
            assert "Failed to record job" in body["message"]

    def test_handler_dynamodb_race_condition_returns_500(self) -> None:
        """A RuntimeError from the race condition should return 500 InternalError."""
        with patch("handler.put_job_item") as mock_put:
            mock_put.side_effect = RuntimeError("Race condition detected")

            response = lambda_handler({"jobId": JOB_ID}, context=None)

            assert response["statusCode"] == 500
            body = json.loads(response["body"])
            assert body["error"] == "InternalError"
            assert "race condition" in body["message"]
