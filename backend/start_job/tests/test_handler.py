"""Tests for the start-job Lambda.

Covers parsing jobId validation from path parameters, S3 image listing, DynamoDB
job status checks, job status updates, SQS message sending, and the full
orchestration flow.
"""

import datetime
import json
import os
import unittest
from unittest.mock import MagicMock, patch

import boto3
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


class TestParseJobId(unittest.TestCase):
    """Tests for the `parse_job_id` request-validation helper."""

    def test_valid_job_id_from_path_parameters(self) -> None:
        """Valid `jobId` in pathParameters should be returned as-is."""
        event = {"pathParameters": {"jobId": JOB_ID}}
        job_id, error_response = parse_job_id(event)
        self.assertEqual(job_id, JOB_ID)
        self.assertIsNone(error_response)

    def test_job_id_with_whitespace_is_trimmed(self) -> None:
        """`jobId` with leading/trailing whitespace should be trimmed."""
        event = {"pathParameters": {"jobId": f"  {JOB_ID}  "}}
        job_id, error_response = parse_job_id(event)
        self.assertEqual(job_id, JOB_ID)
        self.assertIsNone(error_response)

    def test_missing_path_parameters_returns_400(self) -> None:
        """No `pathParameters` key should return a 400 InvalidRequest error."""
        job_id, error_response = parse_job_id({})
        self.assertIsNone(job_id)
        self.assertIsNotNone(error_response)
        self.assertEqual(error_response["statusCode"], 400)
        body = json.loads(error_response["body"])
        self.assertEqual(body["error"], "InvalidRequest")
        self.assertEqual(body["message"], "jobId is required")

    def test_missing_job_id_in_path_parameters_returns_400(self) -> None:
        """No `jobId` in pathParameters should return a 400 InvalidRequest error."""
        event = {"pathParameters": {}}
        job_id, error_response = parse_job_id(event)
        self.assertIsNone(job_id)
        self.assertIsNotNone(error_response)
        self.assertEqual(error_response["statusCode"], 400)

    def test_null_path_parameters_returns_400(self) -> None:
        """`pathParameters` set to None should return a 400 InvalidRequest error."""
        event = {"pathParameters": None}
        job_id, error_response = parse_job_id(event)
        self.assertIsNone(job_id)
        self.assertIsNotNone(error_response)
        self.assertEqual(error_response["statusCode"], 400)

    def test_empty_job_id_returns_400(self) -> None:
        """Empty `jobId` should return a 400 InvalidRequest error."""
        event = {"pathParameters": {"jobId": ""}}
        job_id, error_response = parse_job_id(event)
        self.assertIsNone(job_id)
        self.assertIsNotNone(error_response)
        self.assertEqual(error_response["statusCode"], 400)

    def test_whitespace_only_job_id_returns_400(self) -> None:
        """Whitespace-only `jobId` should return a 400 InvalidRequest error."""
        event = {"pathParameters": {"jobId": "   "}}
        job_id, error_response = parse_job_id(event)
        self.assertIsNone(job_id)
        self.assertIsNotNone(error_response)
        self.assertEqual(error_response["statusCode"], 400)

    def test_non_string_job_id_returns_400(self) -> None:
        """Non-string `jobId` should return a 400 InvalidRequest error."""
        event = {"pathParameters": {"jobId": 12345}}
        job_id, error_response = parse_job_id(event)
        self.assertIsNone(job_id)
        self.assertIsNotNone(error_response)
        self.assertEqual(error_response["statusCode"], 400)


class TestListUploadedImages(unittest.TestCase):
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

        with patch.dict(os.environ, {"UPLOAD_BUCKET_NAME": BUCKET_NAME}):
            images = list_uploaded_images(JOB_ID)

        self.assertEqual(len(images), 4)
        self.assertTrue(all(img.startswith(f"s3://{BUCKET_NAME}/") for img in images))

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

        with patch.dict(os.environ, {"UPLOAD_BUCKET_NAME": BUCKET_NAME}):
            images = list_uploaded_images(JOB_ID)

        self.assertEqual(len(images), 3)

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

        with patch.dict(os.environ, {"UPLOAD_BUCKET_NAME": BUCKET_NAME}):
            images = list_uploaded_images(JOB_ID)

        expected_keys = [
            f"s3://{BUCKET_NAME}/{prefix}image_0.jpg",
            f"s3://{BUCKET_NAME}/{prefix}image_1.jpg",
            f"s3://{BUCKET_NAME}/{prefix}image_2.jpg",
            f"s3://{BUCKET_NAME}/{prefix}image_3.jpg",
        ]
        self.assertEqual(images, expected_keys)

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

        with patch.dict(os.environ, {"UPLOAD_BUCKET_NAME": BUCKET_NAME}):
            images = list_uploaded_images(JOB_ID)

        self.assertEqual(len(images), 2)

    def test_missing_bucket_env_var_raises_key_error(self) -> None:
        """Missing UPLOAD_BUCKET_NAME env var should raise KeyError."""
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(KeyError):
                list_uploaded_images(JOB_ID)


class TestGetJobStatus(unittest.TestCase):
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
                "createdAt": {
                    "S": datetime.datetime.now(datetime.timezone.utc).isoformat()
                },
            },
        )

        with patch.dict(os.environ, {"JOBS_TABLE_NAME": TABLE_NAME}):
            status = get_job_status(JOB_ID)

        self.assertEqual(status, "UPLOADED")

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

        with patch.dict(os.environ, {"JOBS_TABLE_NAME": TABLE_NAME}):
            status = get_job_status(JOB_ID)

        self.assertIsNone(status)

    def test_missing_table_env_var_raises_key_error(self) -> None:
        """Missing JOBS_TABLE_NAME env var should raise KeyError."""
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(KeyError):
                get_job_status(JOB_ID)


class TestUpdateJobItem(unittest.TestCase):
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
                "createdAt": {
                    "S": datetime.datetime.now(datetime.timezone.utc).isoformat()
                },
                "updatedAt": {
                    "S": datetime.datetime.now(datetime.timezone.utc).isoformat()
                },
            },
        )

        with patch.dict(os.environ, {"JOBS_TABLE_NAME": TABLE_NAME}):
            update_job_item(JOB_ID)

        response = dynamodb.get_item(
            TableName=TABLE_NAME,
            Key={"jobId": {"S": JOB_ID}},
        )
        self.assertEqual(response["Item"]["jobStatus"]["S"], "IN_PROGRESS")

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

        with patch.dict(os.environ, {"JOBS_TABLE_NAME": TABLE_NAME}):
            with self.assertRaises(ClientError) as context:
                update_job_item(JOB_ID)
            self.assertEqual(
                context.exception.response["Error"]["Code"],
                "ConditionalCheckFailedException",
            )

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
                "createdAt": {
                    "S": datetime.datetime.now(datetime.timezone.utc).isoformat()
                },
                "updatedAt": {
                    "S": datetime.datetime.now(datetime.timezone.utc).isoformat()
                },
            },
        )

        with patch.dict(os.environ, {"JOBS_TABLE_NAME": TABLE_NAME}):
            with self.assertRaises(ClientError) as context:
                update_job_item(JOB_ID)
            self.assertEqual(
                context.exception.response["Error"]["Code"],
                "ConditionalCheckFailedException",
            )

    def test_missing_table_env_var_raises_key_error(self) -> None:
        """Missing JOBS_TABLE_NAME env var should raise KeyError."""
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(KeyError):
                update_job_item(JOB_ID)


class TestTriggerGuideCreation(unittest.TestCase):
    """Tests for the `trigger_guide_creation` SQS message sending helper."""

    @mock_aws
    def test_sends_sqs_message(self) -> None:
        """Should send jobId message to SQS queue."""
        sqs = boto3.client("sqs")
        response = sqs.create_queue(QueueName="test-queue")
        queue_url = response["QueueUrl"]

        with patch.dict(os.environ, {"GUIDE_CREATION_QUEUE_URL": queue_url}):
            trigger_guide_creation(JOB_ID)

        messages = sqs.receive_message(QueueUrl=queue_url)
        self.assertEqual(len(messages["Messages"]), 1)
        body = json.loads(messages["Messages"][0]["Body"])
        self.assertEqual(body["jobId"], JOB_ID)

    def test_missing_queue_url_env_var_raises_key_error(self) -> None:
        """Missing GUIDE_CREATION_QUEUE_URL env var should raise KeyError."""
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(KeyError):
                trigger_guide_creation(JOB_ID)


class TestLambdaHandlerExceptions(unittest.TestCase):
    """Tests for exception handling paths in lambda_handler."""

    def setUp(self) -> None:
        """Set up test environment variables."""
        self.env_patcher = patch.dict(
            os.environ,
            {
                "JOBS_TABLE_NAME": TABLE_NAME,
                "UPLOAD_BUCKET_NAME": BUCKET_NAME,
                "GUIDE_CREATION_QUEUE_URL": QUEUE_URL,
            },
        )
        self.env_patcher.start()

    def tearDown(self) -> None:
        """Clean up environment variable patches."""
        self.env_patcher.stop()

    def test_dynamodb_client_error_on_get_job_status_returns_500(self) -> None:
        """DynamoDB ClientError when getting job status should return 500."""
        event = {"pathParameters": {"jobId": JOB_ID}}

        with patch("handler.get_job_status") as mock_get:
            mock_get.side_effect = ClientError(
                {"Error": {"Code": "ThrottlingException", "Message": "Rate exceeded"}},
                "GetItem",
            )

            response = lambda_handler(event, None)

            self.assertEqual(response["statusCode"], 500)
            body = json.loads(response["body"])
            self.assertEqual(body["error"], "InternalError")
            self.assertIn("Failed to check job status", body["message"])

    def test_dynamodb_key_error_on_get_job_status_returns_500(self) -> None:
        """Missing JOBS_TABLE_NAME env var should return 500."""
        event = {"pathParameters": {"jobId": JOB_ID}}

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("JOBS_TABLE_NAME", None)

            response = lambda_handler(event, None)

            self.assertEqual(response["statusCode"], 500)
            body = json.loads(response["body"])
            self.assertIn("Server misconfiguration", body["message"])
            self.assertIn("DynamoDB table name", body["message"])

    @mock_aws
    def test_s3_client_error_on_list_images_returns_500(self) -> None:
        """S3 ClientError when listing images should return 500."""
        event = {"pathParameters": {"jobId": JOB_ID}}

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

            response = lambda_handler(event, None)

            self.assertEqual(response["statusCode"], 500)
            body = json.loads(response["body"])
            self.assertIn("Failed to list uploaded images", body["message"])

    @mock_aws
    def test_s3_key_error_on_list_images_returns_500(self) -> None:
        """Missing UPLOAD_BUCKET_NAME env var should return 500."""
        event = {"pathParameters": {"jobId": JOB_ID}}

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

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("UPLOAD_BUCKET_NAME", None)

            response = lambda_handler(event, None)

            self.assertEqual(response["statusCode"], 500)
            body = json.loads(response["body"])
            self.assertIn("Server misconfiguration", body["message"])
            self.assertIn("S3 bucket name", body["message"])

    @mock_aws
    def test_dynamodb_client_error_on_update_job_returns_500(self) -> None:
        """DynamoDB ClientError on update_job_item should return 500."""
        event = {"pathParameters": {"jobId": JOB_ID}}

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

            response = lambda_handler(event, None)

            self.assertEqual(response["statusCode"], 500)
            body = json.loads(response["body"])
            self.assertIn("Failed to record job", body["message"])

    @mock_aws
    def test_dynamodb_key_error_on_update_job_returns_500(self) -> None:
        """Missing JOBS_TABLE_NAME on update should return 500."""
        event = {"pathParameters": {"jobId": JOB_ID}}

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

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("JOBS_TABLE_NAME", None)

            response = lambda_handler(event, None)

            self.assertEqual(response["statusCode"], 500)
            body = json.loads(response["body"])
            self.assertIn("Server misconfiguration", body["message"])

    @mock_aws
    def test_sqs_client_error_on_trigger_returns_500(self) -> None:
        """SQS ClientError on trigger_guide_creation should return 500."""
        event = {"pathParameters": {"jobId": JOB_ID}}

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

            response = lambda_handler(event, None)

            self.assertEqual(response["statusCode"], 500)
            body = json.loads(response["body"])
            self.assertIn("Failed to trigger guide creation", body["message"])

    @mock_aws
    def test_sqs_key_error_on_trigger_returns_500(self) -> None:
        """Missing GUIDE_CREATION_QUEUE_URL on trigger should return 500."""
        event = {"pathParameters": {"jobId": JOB_ID}}

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

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GUIDE_CREATION_QUEUE_URL", None)

            response = lambda_handler(event, None)

            self.assertEqual(response["statusCode"], 500)
            body = json.loads(response["body"])
            self.assertIn("Server misconfiguration", body["message"])
            self.assertIn("SQS queue URL", body["message"])

    @mock_aws
    def test_race_condition_on_update_job_handled_gracefully(self) -> None:
        """ConditionalCheckFailedException on update should be handled gracefully."""
        event = {"pathParameters": {"jobId": JOB_ID}}

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

        with patch.dict(
            os.environ,
            {
                "JOBS_TABLE_NAME": TABLE_NAME,
                "UPLOAD_BUCKET_NAME": BUCKET_NAME,
                "GUIDE_CREATION_QUEUE_URL": queue_url,
            },
        ):
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

                response_result = lambda_handler(event, None)

                self.assertEqual(response_result["statusCode"], 200)
                body = json.loads(response_result["body"])
                self.assertEqual(body["jobId"], JOB_ID)
                self.assertEqual(body["jobStatus"], "IN_PROGRESS")

                messages = sqs.receive_message(QueueUrl=queue_url)
                self.assertIn("Messages", messages)
                message_body = json.loads(messages["Messages"][0]["Body"])
                self.assertEqual(message_body["jobId"], JOB_ID)


class TestLambdaHandler(unittest.TestCase):
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
                "createdAt": {
                    "S": datetime.datetime.now(datetime.timezone.utc).isoformat()
                },
                "updatedAt": {
                    "S": datetime.datetime.now(datetime.timezone.utc).isoformat()
                },
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

        event = {"pathParameters": {"jobId": JOB_ID}}
        with patch.dict(os.environ, env_vars):
            result = lambda_handler(event, None)

        self.assertEqual(result["statusCode"], 200)
        body = json.loads(result["body"])
        self.assertEqual(body["jobId"], JOB_ID)
        self.assertEqual(body["jobStatus"], "IN_PROGRESS")

    @mock_aws
    def test_invalid_job_id_returns_400(self) -> None:
        """Missing jobId should return 400."""
        env_vars = {
            "UPLOAD_BUCKET_NAME": BUCKET_NAME,
            "JOBS_TABLE_NAME": TABLE_NAME,
            "GUIDE_CREATION_QUEUE_URL": QUEUE_URL,
        }

        event = {"pathParameters": {}}
        with patch.dict(os.environ, env_vars):
            result = lambda_handler(event, None)

        self.assertEqual(result["statusCode"], 400)
        body = json.loads(result["body"])
        self.assertEqual(body["error"], "InvalidRequest")

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

        event = {"pathParameters": {"jobId": JOB_ID}}
        with patch.dict(os.environ, env_vars):
            result = lambda_handler(event, None)

        self.assertEqual(result["statusCode"], 404)
        body = json.loads(result["body"])
        self.assertEqual(body["error"], "NotFound")

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
                "createdAt": {
                    "S": datetime.datetime.now(datetime.timezone.utc).isoformat()
                },
                "updatedAt": {
                    "S": datetime.datetime.now(datetime.timezone.utc).isoformat()
                },
            },
        )

        env_vars = {
            "UPLOAD_BUCKET_NAME": BUCKET_NAME,
            "JOBS_TABLE_NAME": TABLE_NAME,
            "GUIDE_CREATION_QUEUE_URL": QUEUE_URL,
        }

        event = {"pathParameters": {"jobId": JOB_ID}}
        with patch.dict(os.environ, env_vars):
            result = lambda_handler(event, None)

        self.assertEqual(result["statusCode"], 409)
        body = json.loads(result["body"])
        self.assertEqual(body["error"], "Conflict")

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
                "createdAt": {
                    "S": datetime.datetime.now(datetime.timezone.utc).isoformat()
                },
                "updatedAt": {
                    "S": datetime.datetime.now(datetime.timezone.utc).isoformat()
                },
            },
        )

        env_vars = {
            "UPLOAD_BUCKET_NAME": BUCKET_NAME,
            "JOBS_TABLE_NAME": TABLE_NAME,
            "GUIDE_CREATION_QUEUE_URL": QUEUE_URL,
        }

        event = {"pathParameters": {"jobId": JOB_ID}}
        with patch.dict(os.environ, env_vars):
            result = lambda_handler(event, None)

        self.assertEqual(result["statusCode"], 422)
        body = json.loads(result["body"])
        self.assertEqual(body["error"], "InvalidImageCount")
        self.assertEqual(body["imageCount"], 2)

    @mock_aws
    def test_empty_job_id_returns_400(self) -> None:
        """Empty jobId should return 400."""
        env_vars = {
            "UPLOAD_BUCKET_NAME": BUCKET_NAME,
            "JOBS_TABLE_NAME": TABLE_NAME,
            "GUIDE_CREATION_QUEUE_URL": QUEUE_URL,
        }

        event = {"pathParameters": {"jobId": ""}}
        with patch.dict(os.environ, env_vars):
            result = lambda_handler(event, None)

        self.assertEqual(result["statusCode"], 400)
        body = json.loads(result["body"])
        self.assertEqual(body["error"], "InvalidRequest")


if __name__ == "__main__":
    unittest.main()
