"""Tests for the start-job Lambda.

Covers parsing jobId validation, S3 image listing, DynamoDB job status checks,
job status updates, SQS message sending, and the full orchestration flow.
"""

import datetime
import json
from unittest.mock import patch

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from handler import (
    get_job_status,
    lambda_handler,
    list_uploaded_images,
    parse_job_id,
    trigger_guide_creation,
    update_job_item,
)

BUCKET_NAME = "test-upload-bucket"
TABLE_NAME = "test-jobs-table"
QUEUE_URL = "https://sqs.eu-central-1.amazonaws.com/123456789/test-queue"
JOB_ID = "123e4567-e89b-12d3-a456-426614174000"


class TestParseJobId:
    """Tests for the `parse_job_id` request-validation helper."""

    def test_valid_job_id_from_event(self) -> None:
        """Valid `jobId` in the event should be returned as-is."""
        job_id, error_response = parse_job_id({"jobId": JOB_ID})
        assert job_id == JOB_ID
        assert error_response is None

    def test_job_id_with_whitespace_is_trimmed(self) -> None:
        """`jobId` with leading/trailing whitespace should be trimmed."""
        job_id, error_response = parse_job_id({"jobId": f"  {JOB_ID}  "})
        assert job_id == JOB_ID
        assert error_response is None

    def test_missing_job_id_returns_400(self) -> None:
        """No `jobId` key should return a 400 InvalidRequest error."""
        job_id, error_response = parse_job_id({})
        assert job_id is None
        assert error_response is not None
        assert error_response["statusCode"] == 400
        body = json.loads(error_response["body"])
        assert body["error"] == "InvalidRequest"
        assert body["message"] == "jobId is required"

    def test_empty_job_id_returns_400(self) -> None:
        """Empty `jobId` should return a 400 InvalidRequest error."""
        job_id, error_response = parse_job_id({"jobId": ""})
        assert job_id is None
        assert error_response is not None
        assert error_response["statusCode"] == 400

    def test_whitespace_only_job_id_returns_400(self) -> None:
        """Whitespace-only `jobId` should return a 400 InvalidRequest error."""
        job_id, error_response = parse_job_id({"jobId": "   "})
        assert job_id is None
        assert error_response is not None
        assert error_response["statusCode"] == 400

    def test_non_string_job_id_returns_400(self) -> None:
        """Non-string `jobId` should return a 400 InvalidRequest error."""
        job_id, error_response = parse_job_id({"jobId": 12345})
        assert job_id is None
        assert error_response is not None
        assert error_response["statusCode"] == 400


class TestListUploadedImages:
    """Tests for the `list_uploaded_images` S3 listing helper."""

    @mock_aws
    def test_lists_four_images(self) -> None:
        """List exactly 4 images from S3."""
        s3 = boto3.client("s3")
        s3.create_bucket(
            Bucket=BUCKET_NAME,
            CreateBucketConfiguration={"LocationConstraint": "eu-central-1"},
        )

        prefix = f"uploads/{JOB_ID}/"
        for i in range(4):
            s3.put_object(
                Bucket=BUCKET_NAME,
                Key=f"{prefix}image_{i}.jpg",
                Body=b"fake image data",
            )

        with patch.dict("os.environ", {"UPLOAD_BUCKET_NAME": BUCKET_NAME}):
            images = list_uploaded_images(JOB_ID)

        assert len(images) == 4
        assert all(img.startswith(f"s3://{BUCKET_NAME}/") for img in images)

    @mock_aws
    def test_excludes_zero_byte_objects(self) -> None:
        """Zero-byte S3 objects (folder markers) should be excluded."""
        s3 = boto3.client("s3")
        s3.create_bucket(
            Bucket=BUCKET_NAME,
            CreateBucketConfiguration={"LocationConstraint": "eu-central-1"},
        )

        prefix = f"uploads/{JOB_ID}/"

        for i in range(3):
            s3.put_object(
                Bucket=BUCKET_NAME,
                Key=f"{prefix}image_{i}.jpg",
                Body=b"fake image data",
            )
        s3.put_object(
            Bucket=BUCKET_NAME,
            Key=f"{prefix}.folder_marker",
            Body=b"",
        )

        with patch.dict("os.environ", {"UPLOAD_BUCKET_NAME": BUCKET_NAME}):
            images = list_uploaded_images(JOB_ID)

        assert len(images) == 3

    @mock_aws
    def test_returns_sorted_urls(self) -> None:
        """Images should be returned as sorted S3 URLs."""
        s3 = boto3.client("s3")
        s3.create_bucket(
            Bucket=BUCKET_NAME,
            CreateBucketConfiguration={"LocationConstraint": "eu-central-1"},
        )

        prefix = f"uploads/{JOB_ID}/"
        for i in [2, 0, 3, 1]:
            s3.put_object(
                Bucket=BUCKET_NAME,
                Key=f"{prefix}image_{i}.jpg",
                Body=b"data",
            )

        with patch.dict("os.environ", {"UPLOAD_BUCKET_NAME": BUCKET_NAME}):
            images = list_uploaded_images(JOB_ID)

        expected_keys = [
            f"s3://{BUCKET_NAME}/{prefix}image_0.jpg",
            f"s3://{BUCKET_NAME}/{prefix}image_1.jpg",
            f"s3://{BUCKET_NAME}/{prefix}image_2.jpg",
            f"s3://{BUCKET_NAME}/{prefix}image_3.jpg",
        ]
        assert images == expected_keys

    @mock_aws
    def test_lists_fewer_than_four_images(self) -> None:
        """Should return correct count when fewer than 4 images exist."""
        s3 = boto3.client("s3")
        s3.create_bucket(
            Bucket=BUCKET_NAME,
            CreateBucketConfiguration={"LocationConstraint": "eu-central-1"},
        )

        prefix = f"uploads/{JOB_ID}/"
        s3.put_object(Bucket=BUCKET_NAME, Key=f"{prefix}image_0.jpg", Body=b"data")
        s3.put_object(Bucket=BUCKET_NAME, Key=f"{prefix}image_1.jpg", Body=b"data")

        with patch.dict("os.environ", {"UPLOAD_BUCKET_NAME": BUCKET_NAME}):
            images = list_uploaded_images(JOB_ID)

        assert len(images) == 2

    def test_missing_bucket_env_var_raises_key_error(self) -> None:
        """Missing UPLOAD_BUCKET_NAME env var should raise KeyError."""
        with patch.dict("os.environ", {}, clear=True):
            with pytest.raises(KeyError):
                list_uploaded_images(JOB_ID)


class TestGetJobStatus:
    """Tests for the `get_job_status` DynamoDB lookup helper."""

    @mock_aws
    def test_returns_status_when_job_exists(self) -> None:
        """Should return job status when job exists in DynamoDB."""
        dynamodb = boto3.client("dynamodb")
        dynamodb.create_table(
            TableName=TABLE_NAME,
            KeySchema=[{"AttributeName": "jobId", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "jobId", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )

        dynamodb.put_item(
            TableName=TABLE_NAME,
            Item={
                "jobId": {"S": JOB_ID},
                "jobStatus": {"S": "UPLOADED"},
                "createdAt": {"S": datetime.datetime.now(datetime.timezone.utc).isoformat()},
            },
        )

        with patch.dict("os.environ", {"JOBS_TABLE_NAME": TABLE_NAME}):
            status = get_job_status(JOB_ID)

        assert status == "UPLOADED"

    @mock_aws
    def test_returns_none_when_job_not_found(self) -> None:
        """Should return None when job does not exist in DynamoDB."""
        dynamodb = boto3.client("dynamodb")
        dynamodb.create_table(
            TableName=TABLE_NAME,
            KeySchema=[{"AttributeName": "jobId", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "jobId", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )

        with patch.dict("os.environ", {"JOBS_TABLE_NAME": TABLE_NAME}):
            status = get_job_status(JOB_ID)

        assert status is None

    def test_missing_table_env_var_raises_key_error(self) -> None:
        """Missing JOBS_TABLE_NAME env var should raise KeyError."""
        with patch.dict("os.environ", {}, clear=True):
            with pytest.raises(KeyError):
                get_job_status(JOB_ID)


class TestUpdateJobItem:
    """Tests for the `update_job_item` DynamoDB update helper."""

    @mock_aws
    def test_updates_status_to_in_progress(self) -> None:
        """Should update job status to IN_PROGRESS when status is UPLOADED."""
        dynamodb = boto3.client("dynamodb")
        dynamodb.create_table(
            TableName=TABLE_NAME,
            KeySchema=[{"AttributeName": "jobId", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "jobId", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )

        dynamodb.put_item(
            TableName=TABLE_NAME,
            Item={
                "jobId": {"S": JOB_ID},
                "jobStatus": {"S": "UPLOADED"},
                "createdAt": {"S": datetime.datetime.now(datetime.timezone.utc).isoformat()},
                "updatedAt": {"S": datetime.datetime.now(datetime.timezone.utc).isoformat()},
            },
        )

        with patch.dict("os.environ", {"JOBS_TABLE_NAME": TABLE_NAME}):
            update_job_item(JOB_ID)

        response = dynamodb.get_item(
            TableName=TABLE_NAME,
            Key={"jobId": {"S": JOB_ID}},
        )
        assert response["Item"]["jobStatus"]["S"] == "IN_PROGRESS"

    @mock_aws
    def test_fails_when_job_not_found(self) -> None:
        """Should fail when job not found."""
        dynamodb = boto3.client("dynamodb")
        dynamodb.create_table(
            TableName=TABLE_NAME,
            KeySchema=[{"AttributeName": "jobId", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "jobId", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )

        with patch.dict("os.environ", {"JOBS_TABLE_NAME": TABLE_NAME}):
            with pytest.raises(ClientError) as exc:
                update_job_item(JOB_ID)
            assert exc.value.response["Error"]["Code"] == "ConditionalCheckFailedException"

    @mock_aws
    def test_fails_when_status_not_uploaded(self) -> None:
        """Should fail when job status is not UPLOADED."""
        dynamodb = boto3.client("dynamodb")
        dynamodb.create_table(
            TableName=TABLE_NAME,
            KeySchema=[{"AttributeName": "jobId", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "jobId", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )

        dynamodb.put_item(
            TableName=TABLE_NAME,
            Item={
                "jobId": {"S": JOB_ID},
                "jobStatus": {"S": "IN_PROGRESS"},
                "createdAt": {"S": datetime.datetime.now(datetime.timezone.utc).isoformat()},
                "updatedAt": {"S": datetime.datetime.now(datetime.timezone.utc).isoformat()},
            },
        )

        with patch.dict("os.environ", {"JOBS_TABLE_NAME": TABLE_NAME}):
            with pytest.raises(ClientError) as exc:
                update_job_item(JOB_ID)
            assert exc.value.response["Error"]["Code"] == "ConditionalCheckFailedException"

    def test_missing_table_env_var_raises_key_error(self) -> None:
        """Missing JOBS_TABLE_NAME env var should raise KeyError."""
        with patch.dict("os.environ", {}, clear=True):
            with pytest.raises(KeyError):
                update_job_item(JOB_ID)


class TestTriggerGuideCreation:
    """Tests for the `trigger_guide_creation` SQS message sending helper."""

    @mock_aws
    def test_sends_sqs_message(self) -> None:
        """Should send jobId message to SQS queue."""
        sqs = boto3.client("sqs")
        response = sqs.create_queue(QueueName="test-queue")
        queue_url = response["QueueUrl"]

        with patch.dict("os.environ", {"GUIDE_CREATION_QUEUE_URL": queue_url}):
            trigger_guide_creation(JOB_ID)

        messages = sqs.receive_message(QueueUrl=queue_url)
        assert len(messages["Messages"]) == 1
        body = json.loads(messages["Messages"][0]["Body"])
        assert body["jobId"] == JOB_ID

    def test_missing_queue_url_env_var_raises_key_error(self) -> None:
        """Missing GUIDE_CREATION_QUEUE_URL env var should raise KeyError."""
        with patch.dict("os.environ", {}, clear=True):
            with pytest.raises(KeyError):
                trigger_guide_creation(JOB_ID)


class TestLambdaHandlerExceptions:
    """Tests for exception handling paths in lambda_handler."""

    def test_dynamodb_client_error_on_get_job_status_returns_500(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """DynamoDB ClientError when getting job status should return 500."""
        monkeypatch.setenv("JOBS_TABLE_NAME", TABLE_NAME)
        monkeypatch.setenv("UPLOAD_BUCKET_NAME", BUCKET_NAME)
        monkeypatch.setenv("GUIDE_CREATION_QUEUE_URL", QUEUE_URL)

        with patch("handler.get_job_status") as mock_get:
            mock_get.side_effect = ClientError(
                {"Error": {"Code": "ThrottlingException", "Message": "Rate exceeded"}},
                "GetItem",
            )

            response = lambda_handler({"jobId": JOB_ID}, None)

            assert response["statusCode"] == 500
            body = json.loads(response["body"])
            assert body["error"] == "InternalError"
            assert "Failed to check job status" in body["message"]

    def test_dynamodb_key_error_on_get_job_status_returns_500(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Missing JOBS_TABLE_NAME env var should return 500."""
        monkeypatch.delenv("JOBS_TABLE_NAME", raising=False)
        monkeypatch.setenv("UPLOAD_BUCKET_NAME", BUCKET_NAME)
        monkeypatch.setenv("GUIDE_CREATION_QUEUE_URL", QUEUE_URL)

        response = lambda_handler({"jobId": JOB_ID}, None)

        assert response["statusCode"] == 500
        body = json.loads(response["body"])
        assert "Server misconfiguration" in body["message"]
        assert "DynamoDB table name" in body["message"]

    def test_s3_client_error_on_list_images_returns_500(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """S3 ClientError when listing images should return 500."""
        monkeypatch.setenv("JOBS_TABLE_NAME", TABLE_NAME)
        monkeypatch.setenv("UPLOAD_BUCKET_NAME", BUCKET_NAME)
        monkeypatch.setenv("GUIDE_CREATION_QUEUE_URL", QUEUE_URL)

        with mock_aws():
            dynamodb = boto3.client("dynamodb")
            dynamodb.create_table(
                TableName=TABLE_NAME,
                KeySchema=[{"AttributeName": "jobId", "KeyType": "HASH"}],
                AttributeDefinitions=[{"AttributeName": "jobId", "AttributeType": "S"}],
                BillingMode="PAY_PER_REQUEST",
            )
            dynamodb.put_item(
                TableName=TABLE_NAME,
                Item={
                    "jobId": {"S": JOB_ID},
                    "jobStatus": {"S": "UPLOADED"},
                    "imageUrls": {"L": [{"S": "s3://bucket/1"}]},
                },
            )

            with patch("handler.list_uploaded_images") as mock_list:
                mock_list.side_effect = ClientError(
                    {"Error": {"Code": "NoSuchBucket", "Message": "Bucket not found"}},
                    "ListObjectsV2",
                )

                response = lambda_handler({"jobId": JOB_ID}, None)

                assert response["statusCode"] == 500
                body = json.loads(response["body"])
                assert "Failed to list uploaded images" in body["message"]

    def test_s3_key_error_on_list_images_returns_500(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Missing UPLOAD_BUCKET_NAME env var should return 500."""
        monkeypatch.setenv("JOBS_TABLE_NAME", TABLE_NAME)
        monkeypatch.delenv("UPLOAD_BUCKET_NAME", raising=False)
        monkeypatch.setenv("GUIDE_CREATION_QUEUE_URL", QUEUE_URL)

        with mock_aws():
            dynamodb = boto3.client("dynamodb")
            dynamodb.create_table(
                TableName=TABLE_NAME,
                KeySchema=[{"AttributeName": "jobId", "KeyType": "HASH"}],
                AttributeDefinitions=[{"AttributeName": "jobId", "AttributeType": "S"}],
                BillingMode="PAY_PER_REQUEST",
            )
            dynamodb.put_item(
                TableName=TABLE_NAME,
                Item={
                    "jobId": {"S": JOB_ID},
                    "jobStatus": {"S": "UPLOADED"},
                    "imageUrls": {"L": [{"S": "s3://bucket/1"}]},
                },
            )

            response = lambda_handler({"jobId": JOB_ID}, None)

            assert response["statusCode"] == 500
            body = json.loads(response["body"])
            assert "Server misconfiguration" in body["message"]
            assert "S3 bucket name" in body["message"]

    def test_dynamodb_client_error_on_update_job_returns_500(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """DynamoDB ClientError on update_job_item should return 500."""
        monkeypatch.setenv("JOBS_TABLE_NAME", TABLE_NAME)
        monkeypatch.setenv("UPLOAD_BUCKET_NAME", BUCKET_NAME)
        monkeypatch.setenv("GUIDE_CREATION_QUEUE_URL", QUEUE_URL)

        with mock_aws():
            s3 = boto3.client("s3")
            s3.create_bucket(
                Bucket=BUCKET_NAME,
                CreateBucketConfiguration={"LocationConstraint": "eu-central-1"},
            )
            for i in range(4):
                s3.put_object(
                    Bucket=BUCKET_NAME,
                    Key=f"uploads/{JOB_ID}/image_{i}.jpg",
                    Body=b"data",
                )

            dynamodb = boto3.client("dynamodb")
            dynamodb.create_table(
                TableName=TABLE_NAME,
                KeySchema=[{"AttributeName": "jobId", "KeyType": "HASH"}],
                AttributeDefinitions=[{"AttributeName": "jobId", "AttributeType": "S"}],
                BillingMode="PAY_PER_REQUEST",
            )
            dynamodb.put_item(
                TableName=TABLE_NAME,
                Item={
                    "jobId": {"S": JOB_ID},
                    "jobStatus": {"S": "UPLOADED"},
                    "imageUrls": {"L": [{"S": "s3://bucket/1"}]},
                },
            )

            with patch("handler.update_job_item") as mock_update:
                mock_update.side_effect = ClientError(
                    {"Error": {"Code": "AccessDeniedException", "Message": "Access denied"}},
                    "UpdateItem",
                )

                response = lambda_handler({"jobId": JOB_ID}, None)

                assert response["statusCode"] == 500
                body = json.loads(response["body"])
                assert "Failed to record job" in body["message"]

    def test_dynamodb_key_error_on_update_job_returns_500(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Missing JOBS_TABLE_NAME on update should return 500."""
        monkeypatch.setenv("JOBS_TABLE_NAME", TABLE_NAME)
        monkeypatch.setenv("UPLOAD_BUCKET_NAME", BUCKET_NAME)
        monkeypatch.setenv("GUIDE_CREATION_QUEUE_URL", QUEUE_URL)

        with mock_aws():
            s3 = boto3.client("s3")
            s3.create_bucket(
                Bucket=BUCKET_NAME,
                CreateBucketConfiguration={"LocationConstraint": "eu-central-1"},
            )
            for i in range(4):
                s3.put_object(
                    Bucket=BUCKET_NAME,
                    Key=f"uploads/{JOB_ID}/image_{i}.jpg",
                    Body=b"data",
                )

            dynamodb = boto3.client("dynamodb")
            dynamodb.create_table(
                TableName=TABLE_NAME,
                KeySchema=[{"AttributeName": "jobId", "KeyType": "HASH"}],
                AttributeDefinitions=[{"AttributeName": "jobId", "AttributeType": "S"}],
                BillingMode="PAY_PER_REQUEST",
            )
            dynamodb.put_item(
                TableName=TABLE_NAME,
                Item={
                    "jobId": {"S": JOB_ID},
                    "jobStatus": {"S": "UPLOADED"},
                    "imageUrls": {"L": [{"S": "s3://bucket/1"}]},
                },
            )

            monkeypatch.delenv("JOBS_TABLE_NAME")

            response = lambda_handler({"jobId": JOB_ID}, None)

            assert response["statusCode"] == 500
            body = json.loads(response["body"])
            assert "Server misconfiguration" in body["message"]

    def test_sqs_client_error_on_trigger_returns_500(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """SQS ClientError on trigger_guide_creation should return 500."""
        monkeypatch.setenv("JOBS_TABLE_NAME", TABLE_NAME)
        monkeypatch.setenv("UPLOAD_BUCKET_NAME", BUCKET_NAME)
        monkeypatch.setenv("GUIDE_CREATION_QUEUE_URL", QUEUE_URL)

        with mock_aws():
            s3 = boto3.client("s3")
            s3.create_bucket(
                Bucket=BUCKET_NAME,
                CreateBucketConfiguration={"LocationConstraint": "eu-central-1"},
            )
            for i in range(4):
                s3.put_object(
                    Bucket=BUCKET_NAME,
                    Key=f"uploads/{JOB_ID}/image_{i}.jpg",
                    Body=b"data",
                )

            dynamodb = boto3.client("dynamodb")
            dynamodb.create_table(
                TableName=TABLE_NAME,
                KeySchema=[{"AttributeName": "jobId", "KeyType": "HASH"}],
                AttributeDefinitions=[{"AttributeName": "jobId", "AttributeType": "S"}],
                BillingMode="PAY_PER_REQUEST",
            )
            dynamodb.put_item(
                TableName=TABLE_NAME,
                Item={
                    "jobId": {"S": JOB_ID},
                    "jobStatus": {"S": "UPLOADED"},
                    "imageUrls": {"L": [{"S": "s3://bucket/1"}]},
                },
            )

            with patch("handler.trigger_guide_creation") as mock_trigger:
                mock_trigger.side_effect = ClientError(
                    {"Error": {"Code": "QueueDoesNotExist", "Message": "Queue not found"}},
                    "SendMessage",
                )

                response = lambda_handler({"jobId": JOB_ID}, None)

                assert response["statusCode"] == 500
                body = json.loads(response["body"])
                assert "Failed to trigger guide creation" in body["message"]

    def test_sqs_key_error_on_trigger_returns_500(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Missing GUIDE_CREATION_QUEUE_URL on trigger should return 500."""
        monkeypatch.setenv("JOBS_TABLE_NAME", TABLE_NAME)
        monkeypatch.setenv("UPLOAD_BUCKET_NAME", BUCKET_NAME)
        monkeypatch.setenv("GUIDE_CREATION_QUEUE_URL", QUEUE_URL)

        with mock_aws():
            s3 = boto3.client("s3")
            s3.create_bucket(
                Bucket=BUCKET_NAME,
                CreateBucketConfiguration={"LocationConstraint": "eu-central-1"},
            )
            for i in range(4):
                s3.put_object(
                    Bucket=BUCKET_NAME,
                    Key=f"uploads/{JOB_ID}/image_{i}.jpg",
                    Body=b"data",
                )

            dynamodb = boto3.client("dynamodb")
            dynamodb.create_table(
                TableName=TABLE_NAME,
                KeySchema=[{"AttributeName": "jobId", "KeyType": "HASH"}],
                AttributeDefinitions=[{"AttributeName": "jobId", "AttributeType": "S"}],
                BillingMode="PAY_PER_REQUEST",
            )
            dynamodb.put_item(
                TableName=TABLE_NAME,
                Item={
                    "jobId": {"S": JOB_ID},
                    "jobStatus": {"S": "UPLOADED"},
                    "imageUrls": {"L": [{"S": "s3://bucket/1"}]},
                },
            )

            monkeypatch.delenv("GUIDE_CREATION_QUEUE_URL")

            response = lambda_handler({"jobId": JOB_ID}, None)

            assert response["statusCode"] == 500
            body = json.loads(response["body"])
            assert "Server misconfiguration" in body["message"]
            assert "SQS queue URL" in body["message"]

    def test_race_condition_on_update_job_handled_gracefully(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ConditionalCheckFailedException on update should be handled gracefully."""
        monkeypatch.setenv("JOBS_TABLE_NAME", TABLE_NAME)
        monkeypatch.setenv("UPLOAD_BUCKET_NAME", BUCKET_NAME)
        monkeypatch.setenv("GUIDE_CREATION_QUEUE_URL", QUEUE_URL)

        with mock_aws():
            s3 = boto3.client("s3")
            s3.create_bucket(
                Bucket=BUCKET_NAME,
                CreateBucketConfiguration={"LocationConstraint": "eu-central-1"},
            )
            for i in range(4):
                s3.put_object(
                    Bucket=BUCKET_NAME,
                    Key=f"uploads/{JOB_ID}/image_{i}.jpg",
                    Body=b"data",
                )

            dynamodb = boto3.client("dynamodb")
            dynamodb.create_table(
                TableName=TABLE_NAME,
                KeySchema=[{"AttributeName": "jobId", "KeyType": "HASH"}],
                AttributeDefinitions=[{"AttributeName": "jobId", "AttributeType": "S"}],
                BillingMode="PAY_PER_REQUEST",
            )
            dynamodb.put_item(
                TableName=TABLE_NAME,
                Item={
                    "jobId": {"S": JOB_ID},
                    "jobStatus": {"S": "UPLOADED"},
                    "imageUrls": {"L": [{"S": "s3://bucket/1"}]},
                },
            )
            sqs = boto3.client("sqs")
            response = sqs.create_queue(QueueName="test-queue")
            queue_url = response["QueueUrl"]
            monkeypatch.setenv("GUIDE_CREATION_QUEUE_URL", queue_url)

            with patch("handler.update_job_item") as mock_update:
                mock_update.side_effect = ClientError(
                    {
                        "Error": {
                            "Code": "ConditionalCheckFailedException",
                            "Message": "Condition check failed",
                        }
                    },
                    "UpdateItem",
                )

                response = lambda_handler({"jobId": JOB_ID}, None)

                # Race condition is handled: job already IN_PROGRESS
                # Still sends SQS message and returns 200
                assert response["statusCode"] == 200
                body = json.loads(response["body"])
                assert body["jobId"] == JOB_ID
                assert body["jobStatus"] == "IN_PROGRESS"

                # Verify SQS message was still sent
                messages = sqs.receive_message(QueueUrl=queue_url)
                assert "Messages" in messages
                message_body = json.loads(messages["Messages"][0]["Body"])
                assert message_body["jobId"] == JOB_ID


class TestLambdaHandler:
    """Tests for the full lambda_handler orchestration."""

    @mock_aws
    def test_happy_path_starts_job(self) -> None:
        """Full flow: valid jobId, job exists, 4 images, update status."""
        s3 = boto3.client("s3")
        s3.create_bucket(
            Bucket=BUCKET_NAME,
            CreateBucketConfiguration={"LocationConstraint": "eu-central-1"},
        )
        prefix = f"uploads/{JOB_ID}/"
        for i in range(4):
            s3.put_object(
                Bucket=BUCKET_NAME,
                Key=f"{prefix}image_{i}.jpg",
                Body=b"data",
            )

        dynamodb = boto3.client("dynamodb")
        dynamodb.create_table(
            TableName=TABLE_NAME,
            KeySchema=[{"AttributeName": "jobId", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "jobId", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        dynamodb.put_item(
            TableName=TABLE_NAME,
            Item={
                "jobId": {"S": JOB_ID},
                "jobStatus": {"S": "UPLOADED"},
                "createdAt": {"S": datetime.datetime.now(datetime.timezone.utc).isoformat()},
                "updatedAt": {"S": datetime.datetime.now(datetime.timezone.utc).isoformat()},
            },
        )

        sqs = boto3.client("sqs")
        response = sqs.create_queue(QueueName="test-queue")
        queue_url = response["QueueUrl"]

        env_vars = {
            "UPLOAD_BUCKET_NAME": BUCKET_NAME,
            "JOBS_TABLE_NAME": TABLE_NAME,
            "GUIDE_CREATION_QUEUE_URL": queue_url,
        }

        with patch.dict("os.environ", env_vars):
            result = lambda_handler({"jobId": JOB_ID}, None)

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["jobId"] == JOB_ID
        assert body["jobStatus"] == "IN_PROGRESS"

    @mock_aws
    def test_invalid_job_id_returns_400(self) -> None:
        """Missing jobId should return 400."""
        env_vars = {
            "UPLOAD_BUCKET_NAME": BUCKET_NAME,
            "JOBS_TABLE_NAME": TABLE_NAME,
            "GUIDE_CREATION_QUEUE_URL": QUEUE_URL,
        }

        with patch.dict("os.environ", env_vars):
            result = lambda_handler({}, None)

        assert result["statusCode"] == 400
        body = json.loads(result["body"])
        assert body["error"] == "InvalidRequest"

    @mock_aws
    def test_job_not_found_returns_404(self) -> None:
        """Job not found in DynamoDB should return 404."""
        dynamodb = boto3.client("dynamodb")
        dynamodb.create_table(
            TableName=TABLE_NAME,
            KeySchema=[{"AttributeName": "jobId", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "jobId", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )

        env_vars = {
            "UPLOAD_BUCKET_NAME": BUCKET_NAME,
            "JOBS_TABLE_NAME": TABLE_NAME,
            "GUIDE_CREATION_QUEUE_URL": QUEUE_URL,
        }

        with patch.dict("os.environ", env_vars):
            result = lambda_handler({"jobId": JOB_ID}, None)

        assert result["statusCode"] == 404
        body = json.loads(result["body"])
        assert body["error"] == "NotFound"

    @mock_aws
    def test_job_status_not_uploaded_returns_409(self) -> None:
        """Job with non-UPLOADED status should return 409."""
        dynamodb = boto3.client("dynamodb")
        dynamodb.create_table(
            TableName=TABLE_NAME,
            KeySchema=[{"AttributeName": "jobId", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "jobId", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        dynamodb.put_item(
            TableName=TABLE_NAME,
            Item={
                "jobId": {"S": JOB_ID},
                "jobStatus": {"S": "IN_PROGRESS"},
                "createdAt": {"S": datetime.datetime.now(datetime.timezone.utc).isoformat()},
                "updatedAt": {"S": datetime.datetime.now(datetime.timezone.utc).isoformat()},
            },
        )

        env_vars = {
            "UPLOAD_BUCKET_NAME": BUCKET_NAME,
            "JOBS_TABLE_NAME": TABLE_NAME,
            "GUIDE_CREATION_QUEUE_URL": QUEUE_URL,
        }

        with patch.dict("os.environ", env_vars):
            result = lambda_handler({"jobId": JOB_ID}, None)

        assert result["statusCode"] == 409
        body = json.loads(result["body"])
        assert body["error"] == "Conflict"

    @mock_aws
    def test_image_count_not_four_returns_422(self) -> None:
        """Image count != 4 should return 422."""
        s3 = boto3.client("s3")
        s3.create_bucket(
            Bucket=BUCKET_NAME,
            CreateBucketConfiguration={"LocationConstraint": "eu-central-1"},
        )
        prefix = f"uploads/{JOB_ID}/"
        for i in range(2):
            s3.put_object(
                Bucket=BUCKET_NAME,
                Key=f"{prefix}image_{i}.jpg",
                Body=b"data",
            )

        dynamodb = boto3.client("dynamodb")
        dynamodb.create_table(
            TableName=TABLE_NAME,
            KeySchema=[{"AttributeName": "jobId", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "jobId", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        dynamodb.put_item(
            TableName=TABLE_NAME,
            Item={
                "jobId": {"S": JOB_ID},
                "jobStatus": {"S": "UPLOADED"},
                "createdAt": {"S": datetime.datetime.now(datetime.timezone.utc).isoformat()},
                "updatedAt": {"S": datetime.datetime.now(datetime.timezone.utc).isoformat()},
            },
        )

        env_vars = {
            "UPLOAD_BUCKET_NAME": BUCKET_NAME,
            "JOBS_TABLE_NAME": TABLE_NAME,
            "GUIDE_CREATION_QUEUE_URL": QUEUE_URL,
        }

        with patch.dict("os.environ", env_vars):
            result = lambda_handler({"jobId": JOB_ID}, None)

        assert result["statusCode"] == 422
        body = json.loads(result["body"])
        assert body["error"] == "InvalidImageCount"
        assert body["imageCount"] == 2

    @mock_aws
    def test_invalid_job_id_returns_400_empty(self) -> None:
        """Empty jobId should return 400."""
        env_vars = {
            "UPLOAD_BUCKET_NAME": BUCKET_NAME,
            "JOBS_TABLE_NAME": TABLE_NAME,
            "GUIDE_CREATION_QUEUE_URL": QUEUE_URL,
        }

        with patch.dict("os.environ", env_vars):
            result = lambda_handler({"jobId": ""}, None)

        assert result["statusCode"] == 400
        body = json.loads(result["body"])
        assert body["error"] == "InvalidRequest"
