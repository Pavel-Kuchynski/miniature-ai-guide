"""Tests for the `put_job_item` DynamoDB write helper.

Tests the idempotent job item creation, ensuring new jobs are created with the
correct attributes (jobId, imageUrls, jobStatus, createdAt, updatedAt, ttl)
and duplicate calls return the existing item without overwriting.
"""

import datetime
from typing import Any, Dict, Iterator
from unittest.mock import patch

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from handler import put_job_item

BUCKET_NAME = "test-upload-bucket"
TABLE_NAME = "test-jobs-table"
JOB_ID = "123e4567-e89b-12d3-a456-426614174000"


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
                AttributeDefinitions=[
                    {"AttributeName": "jobId", "AttributeType": "S"}
                ],
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

    def test_put_job_item_creates_new_record_returns_created_true(self) -> None:
        """New job creation should return created=True with the written item."""
        created, item = put_job_item(JOB_ID, self.IMAGE_URLS)

        assert created is True
        assert item["jobId"]["S"] == JOB_ID
        assert item["jobStatus"]["S"] == "UPLOADED"

    def test_put_job_item_returns_existing_record_created_false(self) -> None:
        """Duplicate call for the same jobId should return created=False."""
        _, first_item = put_job_item(JOB_ID, self.IMAGE_URLS)

        created, second_item = put_job_item(
            JOB_ID, ["s3://different/url.png"]
        )

        assert created is False
        assert second_item == first_item

    def test_put_job_item_sets_correct_attributes(self) -> None:
        """Item should include jobId, imageUrls, jobStatus, createdAt, updatedAt, ttl."""
        created, item = put_job_item(JOB_ID, self.IMAGE_URLS)

        assert created is True
        assert "jobId" in item
        assert "imageUrls" in item
        assert "jobStatus" in item
        assert "createdAt" in item
        assert "updatedAt" in item
        assert "ttl" in item

        stored_item = self._get_raw_item(JOB_ID)
        assert stored_item == item

    def test_put_job_item_calculates_ttl_as_created_at_plus_7_days(
        self,
    ) -> None:
        """TTL should be createdAt + 7 days in Unix epoch seconds."""
        _, item = put_job_item(JOB_ID, self.IMAGE_URLS)

        created_at = datetime.datetime.fromisoformat(item["createdAt"]["S"])
        expected_ttl = int(created_at.timestamp()) + 7 * 24 * 60 * 60
        assert int(item["ttl"]["N"]) == pytest.approx(expected_ttl, abs=2)

    def test_put_job_item_handles_dynamodb_client_error(self) -> None:
        """Non-conditional ClientError should propagate unchanged."""
        throttling_error = ClientError(
            error_response={
                "Error": {
                    "Code": "ProvisionedThroughputExceededException",
                    "Message": "Throttled",
                }
            },
            operation_name="PutItem",
        )

        with patch("handler.boto3.client") as mock_client:
            mock_client.return_value.put_item.side_effect = throttling_error

            with pytest.raises(ClientError) as exc_info:
                put_job_item(JOB_ID, self.IMAGE_URLS)

        assert (
            exc_info.value.response["Error"]["Code"]
            == "ProvisionedThroughputExceededException"
        )

    def test_put_job_item_handles_conditional_write_conflict(self) -> None:
        """Conditional failure with no existing item should raise RuntimeError."""
        conditional_error = ClientError(
            error_response={
                "Error": {
                    "Code": "ConditionalCheckFailedException",
                    "Message": "collision",
                }
            },
            operation_name="PutItem",
        )

        with patch("handler.boto3.client") as mock_client:
            mock_dynamodb = mock_client.return_value
            mock_dynamodb.put_item.side_effect = conditional_error
            mock_dynamodb.get_item.return_value = {}

            with pytest.raises(RuntimeError):
                put_job_item(JOB_ID, self.IMAGE_URLS)

    def test_put_job_item_stores_image_urls_as_s3_keys(self) -> None:
        """Image URLs should be stored in s3://bucket/key format."""
        urls = [
            "s3://my-bucket/uploads/job-123/image1.png",
            "s3://my-bucket/uploads/job-123/image2.jpg",
        ]
        created, item = put_job_item(JOB_ID, urls)

        assert created is True
        assert item["imageUrls"]["L"] == [{"S": url} for url in urls]

        stored_item = self._get_raw_item(JOB_ID)
        assert stored_item["imageUrls"]["L"] == [{"S": url} for url in urls]
