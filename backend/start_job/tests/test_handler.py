"""Tests for the start-job Lambda.

Covers parsing jobId validation from path parameters, S3 image listing, DynamoDB
job status checks, job status updates, SQS message sending, and the full
orchestration flow.
"""

import datetime
import json
import logging
import os
from typing import Any, Iterator

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws
from unittest.mock import patch

from handler import (
    _get_client,
    get_job_status,
    lambda_handler,
    list_uploaded_images,
    parse_instruction,
    parse_job_id,
    revert_job_item,
    trigger_guide_creation,
    update_job_item,
)

BUCKET_NAME = "test-upload-bucket"
TABLE_NAME = "test-jobs-table"
QUEUE_URL = "https://sqs.eu-central-1.amazonaws.com/123456789/test-queue"
JOB_ID = "123e4567-e89b-12d3-a456-426614174000"


@pytest.fixture(autouse=True)
def _clear_client_cache() -> Iterator[None]:
    """Reset the boto3 client cache so a client from one moto mock cannot leak into the next."""
    _get_client.cache_clear()
    yield
    _get_client.cache_clear()


class TestParseJobId:
    """Tests for the `parse_job_id` request-validation helper."""

    def test_valid_job_id_from_path_parameters(self) -> None:
        """Valid `jobId` in pathParameters should be returned as-is."""
        event = {"pathParameters": {"jobId": JOB_ID}}
        job_id, error_response = parse_job_id(event)
        assert job_id == JOB_ID
        assert error_response is None

    def test_job_id_with_whitespace_is_trimmed(self) -> None:
        """`jobId` with leading/trailing whitespace should be trimmed."""
        event = {"pathParameters": {"jobId": f"  {JOB_ID}  "}}
        job_id, error_response = parse_job_id(event)
        assert job_id == JOB_ID
        assert error_response is None

    def test_missing_path_parameters_returns_400(self) -> None:
        """No `pathParameters` key should return a 400 InvalidRequest error."""
        job_id, error_response = parse_job_id({})
        assert job_id is None
        assert error_response is not None
        assert error_response["statusCode"] == 400
        body = json.loads(error_response["body"])
        assert body["error"] == "InvalidRequest"
        assert body["message"] == "jobId is required"

    def test_missing_job_id_in_path_parameters_returns_400(self) -> None:
        """No `jobId` in pathParameters should return a 400 InvalidRequest error."""
        event = {"pathParameters": {}}
        job_id, error_response = parse_job_id(event)
        assert job_id is None
        assert error_response is not None
        assert error_response["statusCode"] == 400

    def test_null_path_parameters_returns_400(self) -> None:
        """`pathParameters` set to None should return a 400 InvalidRequest error."""
        event = {"pathParameters": None}
        job_id, error_response = parse_job_id(event)
        assert job_id is None
        assert error_response is not None
        assert error_response["statusCode"] == 400

    def test_empty_job_id_returns_400(self) -> None:
        """Empty `jobId` should return a 400 InvalidRequest error."""
        event = {"pathParameters": {"jobId": ""}}
        job_id, error_response = parse_job_id(event)
        assert job_id is None
        assert error_response is not None
        assert error_response["statusCode"] == 400

    def test_whitespace_only_job_id_returns_400(self) -> None:
        """Whitespace-only `jobId` should return a 400 InvalidRequest error."""
        event = {"pathParameters": {"jobId": "   "}}
        job_id, error_response = parse_job_id(event)
        assert job_id is None
        assert error_response is not None
        assert error_response["statusCode"] == 400

    def test_non_string_job_id_returns_400(self) -> None:
        """Non-string `jobId` should return a 400 InvalidRequest error."""
        event = {"pathParameters": {"jobId": 12345}}
        job_id, error_response = parse_job_id(event)
        assert job_id is None
        assert error_response is not None
        assert error_response["statusCode"] == 400


class TestListUploadedImages:
    """Tests for the `list_uploaded_images` S3 listing helper."""

    @mock_aws
    def test_lists_single_image(self) -> None:
        """List exactly 1 image from S3."""
        s3 = boto3.client("s3")
        s3.create_bucket(
            Bucket=BUCKET_NAME,
            CreateBucketConfiguration={"LocationConstraint": "eu-central-1"},
        )

        prefix = f"uploads/{JOB_ID}/"
        for i in range(1):
            s3.put_object(
                Bucket=BUCKET_NAME,
                Key=f"{prefix}image_{i}.jpg",
                Body=b"fake image data",
            )

        with patch.dict(os.environ, {"UPLOAD_BUCKET_NAME": BUCKET_NAME}):
            images = list_uploaded_images(JOB_ID)

        assert len(images) == 1
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

        with patch.dict(os.environ, {"UPLOAD_BUCKET_NAME": BUCKET_NAME}):
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

        with patch.dict(os.environ, {"UPLOAD_BUCKET_NAME": BUCKET_NAME}):
            images = list_uploaded_images(JOB_ID)

        expected_keys = [
            f"s3://{BUCKET_NAME}/{prefix}image_0.jpg",
            f"s3://{BUCKET_NAME}/{prefix}image_1.jpg",
            f"s3://{BUCKET_NAME}/{prefix}image_2.jpg",
            f"s3://{BUCKET_NAME}/{prefix}image_3.jpg",
        ]
        assert images == expected_keys

    @mock_aws
    def test_lists_all_images_without_enforcing_count(self) -> None:
        """Listing returns every image found; the count rule is enforced by the handler."""
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

        assert len(images) == 2

    def test_missing_bucket_env_var_raises_key_error(self) -> None:
        """Missing UPLOAD_BUCKET_NAME env var should raise KeyError."""
        with patch.dict(os.environ, {}, clear=True):
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
                "createdAt": {
                    "S": datetime.datetime.now(datetime.timezone.utc).isoformat()
                },
            },
        )

        with patch.dict(os.environ, {"JOBS_TABLE_NAME": TABLE_NAME}):
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

        with patch.dict(os.environ, {"JOBS_TABLE_NAME": TABLE_NAME}):
            status = get_job_status(JOB_ID)

        assert status is None

    def test_missing_table_env_var_raises_key_error(self) -> None:
        """Missing JOBS_TABLE_NAME env var should raise KeyError."""
        with patch.dict(os.environ, {}, clear=True):
            with pytest.raises(KeyError):
                get_job_status(JOB_ID)


class TestParseInstruction:
    """Tests for the `parse_instruction` optional-body helper."""

    def test_returns_instruction_from_body(self) -> None:
        event = {"body": json.dumps({"instruction": "  Paint the cloak red  "})}
        assert parse_instruction(event) == ("Paint the cloak red", None)

    @pytest.mark.parametrize(
        "body",
        [None, "", "   ", "{}", json.dumps({"instruction": None}),
         json.dumps({"instruction": ""}), json.dumps({"instruction": "   "})],
    )
    def test_returns_none_when_instruction_absent_or_blank(self, body) -> None:
        assert parse_instruction({"body": body}) == (None, None)

    def test_returns_none_when_event_has_no_body(self) -> None:
        assert parse_instruction({}) == (None, None)

    @pytest.mark.parametrize("body", ["not json", "[1, 2]", json.dumps({"instruction": 5})])
    def test_returns_400_for_malformed_body_or_non_string_instruction(self, body) -> None:
        instruction, error_response = parse_instruction({"body": body})
        assert instruction is None
        assert error_response["statusCode"] == 400


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
        assert response["Item"]["jobStatus"]["S"] == "IN_PROGRESS"

    @mock_aws
    def test_stores_instruction_when_provided(self) -> None:
        """Should save the instruction on the job item alongside the status."""
        dynamodb = boto3.client("dynamodb")
        dynamodb.create_table(
            TableName=TABLE_NAME,
            KeySchema=[{"AttributeName": "jobId", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "jobId", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        dynamodb.put_item(
            TableName=TABLE_NAME,
            Item={"jobId": {"S": JOB_ID}, "jobStatus": {"S": "UPLOADED"}},
        )

        with patch.dict(os.environ, {"JOBS_TABLE_NAME": TABLE_NAME}):
            update_job_item(JOB_ID, "Paint the cloak red")

        item = dynamodb.get_item(TableName=TABLE_NAME, Key={"jobId": {"S": JOB_ID}})["Item"]
        assert item["jobStatus"]["S"] == "IN_PROGRESS"
        assert item["instruction"]["S"] == "Paint the cloak red"

    @mock_aws
    def test_does_not_store_instruction_when_not_provided(self) -> None:
        """Should leave the item without an `instruction` attribute."""
        dynamodb = boto3.client("dynamodb")
        dynamodb.create_table(
            TableName=TABLE_NAME,
            KeySchema=[{"AttributeName": "jobId", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "jobId", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        dynamodb.put_item(
            TableName=TABLE_NAME,
            Item={"jobId": {"S": JOB_ID}, "jobStatus": {"S": "UPLOADED"}},
        )

        with patch.dict(os.environ, {"JOBS_TABLE_NAME": TABLE_NAME}):
            update_job_item(JOB_ID, None)

        item = dynamodb.get_item(TableName=TABLE_NAME, Key={"jobId": {"S": JOB_ID}})["Item"]
        assert "instruction" not in item

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
            with pytest.raises(ClientError) as exc_info:
                update_job_item(JOB_ID)
            assert (
                exc_info.value.response["Error"]["Code"]
                == "ConditionalCheckFailedException"
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
            with pytest.raises(ClientError) as exc_info:
                update_job_item(JOB_ID)
            assert (
                exc_info.value.response["Error"]["Code"]
                == "ConditionalCheckFailedException"
            )

    def test_missing_table_env_var_raises_key_error(self) -> None:
        """Missing JOBS_TABLE_NAME env var should raise KeyError."""
        with patch.dict(os.environ, {}, clear=True):
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

        with patch.dict(os.environ, {"PAINT_QUEUE_URL": queue_url}):
            trigger_guide_creation(JOB_ID)

        messages = sqs.receive_message(QueueUrl=queue_url)
        assert len(messages["Messages"]) == 1
        body = json.loads(messages["Messages"][0]["Body"])
        assert body["jobId"] == JOB_ID

    def test_missing_queue_url_env_var_raises_key_error(self) -> None:
        """Missing PAINT_QUEUE_URL env var should raise KeyError."""
        with patch.dict(os.environ, {}, clear=True):
            with pytest.raises(KeyError):
                trigger_guide_creation(JOB_ID)


def _create_jobs_table(dynamodb) -> None:
    """Create the moto jobs table keyed by `jobId`."""
    dynamodb.create_table(
        TableName=TABLE_NAME,
        KeySchema=[{"AttributeName": "jobId", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "jobId", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )


def _get_job_item(dynamodb) -> dict:
    """Return the stored job item for `JOB_ID`."""
    return dynamodb.get_item(TableName=TABLE_NAME, Key={"jobId": {"S": JOB_ID}})["Item"]


class TestRevertJobItem:
    """Tests for `revert_job_item`, which undoes `update_job_item` after a failed trigger."""

    @mock_aws
    def test_reverts_in_progress_job_and_removes_its_instruction(self) -> None:
        dynamodb = boto3.client("dynamodb")
        _create_jobs_table(dynamodb)
        dynamodb.put_item(
            TableName=TABLE_NAME,
            Item={
                "jobId": {"S": JOB_ID},
                "jobStatus": {"S": "IN_PROGRESS"},
                "instruction": {"S": "Paint the cloak red"},
            },
        )

        with patch.dict(os.environ, {"JOBS_TABLE_NAME": TABLE_NAME}):
            revert_job_item(JOB_ID, "Paint the cloak red")

        item = _get_job_item(dynamodb)
        assert item["jobStatus"]["S"] == "UPLOADED"
        assert "instruction" not in item
        assert "updatedAt" in item

    @mock_aws
    def test_keeps_instruction_it_did_not_write(self) -> None:
        dynamodb = boto3.client("dynamodb")
        _create_jobs_table(dynamodb)
        dynamodb.put_item(
            TableName=TABLE_NAME,
            Item={
                "jobId": {"S": JOB_ID},
                "jobStatus": {"S": "IN_PROGRESS"},
                "instruction": {"S": "earlier instruction"},
            },
        )

        with patch.dict(os.environ, {"JOBS_TABLE_NAME": TABLE_NAME}):
            revert_job_item(JOB_ID, None)

        item = _get_job_item(dynamodb)
        assert item["jobStatus"]["S"] == "UPLOADED"
        assert item["instruction"]["S"] == "earlier instruction"

    @mock_aws
    def test_fails_when_status_is_no_longer_in_progress(self) -> None:
        """A status set by another writer (e.g. PAINTED) must not be overwritten."""
        dynamodb = boto3.client("dynamodb")
        _create_jobs_table(dynamodb)
        dynamodb.put_item(
            TableName=TABLE_NAME,
            Item={"jobId": {"S": JOB_ID}, "jobStatus": {"S": "PAINTED"}},
        )

        with patch.dict(os.environ, {"JOBS_TABLE_NAME": TABLE_NAME}):
            with pytest.raises(ClientError) as exc_info:
                revert_job_item(JOB_ID)

        assert exc_info.value.response["Error"]["Code"] == "ConditionalCheckFailedException"
        assert _get_job_item(dynamodb)["jobStatus"]["S"] == "PAINTED"

    def test_missing_table_env_var_raises_key_error(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with pytest.raises(KeyError):
                revert_job_item(JOB_ID)


class TestLambdaHandlerTriggerFailure:
    """A failed SQS send must not leave the job stuck in IN_PROGRESS."""

    SQS_ERROR = ClientError(
        {"Error": {"Code": "QueueDoesNotExist", "Message": "Queue not found"}},
        "SendMessage",
    )

    def _arrange_uploaded_job(self) -> Any:
        """Create one uploaded image and an UPLOADED job; return the DynamoDB client."""
        s3 = boto3.client("s3")
        s3.create_bucket(
            Bucket=BUCKET_NAME,
            CreateBucketConfiguration={"LocationConstraint": "eu-central-1"},
        )
        s3.put_object(Bucket=BUCKET_NAME, Key=f"uploads/{JOB_ID}/image_0.jpg", Body=b"data")
        dynamodb = boto3.client("dynamodb")
        _create_jobs_table(dynamodb)
        dynamodb.put_item(
            TableName=TABLE_NAME,
            Item={"jobId": {"S": JOB_ID}, "jobStatus": {"S": "UPLOADED"}},
        )
        return dynamodb

    @mock_aws
    def test_sqs_client_error_reverts_job_so_retry_succeeds(self) -> None:
        dynamodb = self._arrange_uploaded_job()
        queue_url = boto3.client("sqs").create_queue(QueueName="test-queue")["QueueUrl"]
        env = {
            "JOBS_TABLE_NAME": TABLE_NAME,
            "UPLOAD_BUCKET_NAME": BUCKET_NAME,
            "PAINT_QUEUE_URL": queue_url,
        }
        event = {
            "pathParameters": {"jobId": JOB_ID},
            "body": json.dumps({"instruction": "Paint the cloak red"}),
        }

        with patch.dict(os.environ, env):
            with patch("handler.trigger_guide_creation", side_effect=self.SQS_ERROR):
                failed = lambda_handler(event, None)
            reverted_item = _get_job_item(dynamodb)
            retried = lambda_handler({"pathParameters": {"jobId": JOB_ID}}, None)

        assert failed["statusCode"] == 500
        assert reverted_item["jobStatus"]["S"] == "UPLOADED"
        assert "instruction" not in reverted_item
        assert retried["statusCode"] == 200
        assert _get_job_item(dynamodb)["jobStatus"]["S"] == "IN_PROGRESS"

    @mock_aws
    def test_missing_queue_url_reverts_job(self) -> None:
        dynamodb = self._arrange_uploaded_job()
        env = {"JOBS_TABLE_NAME": TABLE_NAME, "UPLOAD_BUCKET_NAME": BUCKET_NAME}

        with patch.dict(os.environ, env):
            os.environ.pop("PAINT_QUEUE_URL", None)
            response = lambda_handler({"pathParameters": {"jobId": JOB_ID}}, None)

        assert response["statusCode"] == 500
        assert _get_job_item(dynamodb)["jobStatus"]["S"] == "UPLOADED"

    @mock_aws
    def test_revert_failure_is_logged_and_original_500_returned(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        self._arrange_uploaded_job()
        env = {
            "JOBS_TABLE_NAME": TABLE_NAME,
            "UPLOAD_BUCKET_NAME": BUCKET_NAME,
            "PAINT_QUEUE_URL": QUEUE_URL,
        }
        revert_error = ClientError(
            {"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "x"}},
            "UpdateItem",
        )

        with patch.dict(os.environ, env):
            with patch("handler.trigger_guide_creation", side_effect=self.SQS_ERROR):
                with patch("handler.revert_job_item", side_effect=revert_error):
                    with caplog.at_level(logging.ERROR, logger="handler"):
                        response = lambda_handler({"pathParameters": {"jobId": JOB_ID}}, None)

        assert response["statusCode"] == 500
        assert json.loads(response["body"])["message"] == "Failed to trigger guide creation."
        revert_records = [r for r in caplog.records if r.stage == "revert_status_error"]
        assert len(revert_records) == 1
        assert revert_records[0].jobId == JOB_ID
        assert revert_records[0].exc_info is not None

    @mock_aws
    def test_revert_skipped_when_job_already_moved_on(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """If another writer changed the status, the revert loses and logs a warning."""
        dynamodb = self._arrange_uploaded_job()
        env = {
            "JOBS_TABLE_NAME": TABLE_NAME,
            "UPLOAD_BUCKET_NAME": BUCKET_NAME,
            "PAINT_QUEUE_URL": QUEUE_URL,
        }

        def _fail_after_status_moves_on(job_id: str) -> None:
            """Simulate another writer moving the job on before the send fails."""
            dynamodb.update_item(
                TableName=TABLE_NAME,
                Key={"jobId": {"S": job_id}},
                UpdateExpression="SET jobStatus = :status",
                ExpressionAttributeValues={":status": {"S": "FAILED"}},
            )
            raise self.SQS_ERROR

        with patch.dict(os.environ, env):
            with patch("handler.trigger_guide_creation", side_effect=_fail_after_status_moves_on):
                with caplog.at_level(logging.WARNING, logger="handler"):
                    response = lambda_handler({"pathParameters": {"jobId": JOB_ID}}, None)

        assert response["statusCode"] == 500
        assert _get_job_item(dynamodb)["jobStatus"]["S"] == "FAILED"
        [warning] = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert warning.stage == "revert_status"


@pytest.fixture
def env_vars() -> dict:
    """Fixture providing standard environment variables."""
    return {
        "JOBS_TABLE_NAME": TABLE_NAME,
        "UPLOAD_BUCKET_NAME": BUCKET_NAME,
        "PAINT_QUEUE_URL": QUEUE_URL,
    }


class TestLambdaHandlerExceptions:
    """Tests for exception handling paths in lambda_handler."""

    def test_dynamodb_client_error_on_get_job_status_returns_500(
        self, env_vars
    ) -> None:
        """DynamoDB ClientError when getting job status should return 500."""
        event = {"pathParameters": {"jobId": JOB_ID}}

        with patch.dict(os.environ, env_vars):
            with patch("handler.get_job_status") as mock_get:
                mock_get.side_effect = ClientError(
                    {
                        "Error": {
                            "Code": "ThrottlingException",
                            "Message": "Rate exceeded",
                        }
                    },
                    "GetItem",
                )

                response = lambda_handler(event, None)

                assert response["statusCode"] == 500
                body = json.loads(response["body"])
                assert body["error"] == "InternalError"
                assert "Failed to check job status" in body["message"]

    def test_client_error_is_logged_with_traceback_and_not_leaked(
        self, env_vars, caplog: pytest.LogCaptureFixture
    ) -> None:
        """AWS failure details go to the log record only, never into the response body."""
        event = {"pathParameters": {"jobId": JOB_ID}}
        error = ClientError(
            {"Error": {"Code": "ThrottlingException", "Message": "Rate exceeded"}},
            "GetItem",
        )

        with patch.dict(os.environ, env_vars):
            with patch("handler.get_job_status", side_effect=error):
                with caplog.at_level(logging.ERROR, logger="handler"):
                    response = lambda_handler(event, None)

        [record] = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert record.stage == "get_status"
        assert record.jobId == JOB_ID
        assert record.exc_info is not None
        assert "Throttling" not in response["body"]
        assert "Rate exceeded" not in response["body"]

    def test_dynamodb_key_error_on_get_job_status_returns_500(self) -> None:
        """Missing JOBS_TABLE_NAME env var should return 500."""
        event = {"pathParameters": {"jobId": JOB_ID}}

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("JOBS_TABLE_NAME", None)

            response = lambda_handler(event, None)

            assert response["statusCode"] == 500
            body = json.loads(response["body"])
            assert "Server misconfiguration" in body["message"]
            assert "DynamoDB table name" in body["message"]

    @mock_aws
    def test_s3_client_error_on_list_images_returns_500(
        self, env_vars
    ) -> None:
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

        with patch.dict(os.environ, env_vars):
            with patch("handler.list_uploaded_images") as mock_list:
                mock_list.side_effect = ClientError(
                    {
                        "Error": {
                            "Code": "NoSuchBucket",
                            "Message": "Bucket not found",
                        }
                    },
                    "ListObjectsV2",
                )

                response = lambda_handler(event, None)

                assert response["statusCode"] == 500
                body = json.loads(response["body"])
                assert "Failed to list uploaded images" in body["message"]

    @mock_aws
    def test_s3_key_error_on_list_images_returns_500(
        self, env_vars
    ) -> None:
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

        env_vars_copy = env_vars.copy()
        env_vars_copy.pop("UPLOAD_BUCKET_NAME", None)

        with patch.dict(os.environ, env_vars_copy):
            response = lambda_handler(event, None)

            assert response["statusCode"] == 500
            body = json.loads(response["body"])
            assert "Server misconfiguration" in body["message"]
            assert "S3 bucket name" in body["message"]

    @mock_aws
    def test_dynamodb_client_error_on_update_job_returns_500(
        self, env_vars
    ) -> None:
        """DynamoDB ClientError on update_job_item should return 500."""
        event = {"pathParameters": {"jobId": JOB_ID}}

        s3 = boto3.client("s3")
        s3.create_bucket(
            Bucket=BUCKET_NAME,
            CreateBucketConfiguration={"LocationConstraint": "eu-central-1"},
        )
        for i in range(1):
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

        with patch.dict(os.environ, env_vars):
            with patch("handler.update_job_item") as mock_update:
                mock_update.side_effect = ClientError(
                    {
                        "Error": {
                            "Code": "AccessDeniedException",
                            "Message": "Access denied",
                        }
                    },
                    "UpdateItem",
                )

                response = lambda_handler(event, None)

                assert response["statusCode"] == 500
                body = json.loads(response["body"])
                assert "Failed to record job" in body["message"]

    @mock_aws
    def test_dynamodb_key_error_on_update_job_returns_500(self) -> None:
        """Missing JOBS_TABLE_NAME on update should return 500."""
        event = {"pathParameters": {"jobId": JOB_ID}}

        s3 = boto3.client("s3")
        s3.create_bucket(
            Bucket=BUCKET_NAME,
            CreateBucketConfiguration={"LocationConstraint": "eu-central-1"},
        )
        for i in range(1):
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

            assert response["statusCode"] == 500
            body = json.loads(response["body"])
            assert "Server misconfiguration" in body["message"]

    @mock_aws
    def test_sqs_client_error_on_trigger_returns_500(
        self, env_vars
    ) -> None:
        """SQS ClientError on trigger_guide_creation should return 500."""
        event = {"pathParameters": {"jobId": JOB_ID}}

        s3 = boto3.client("s3")
        s3.create_bucket(
            Bucket=BUCKET_NAME,
            CreateBucketConfiguration={"LocationConstraint": "eu-central-1"},
        )
        for i in range(1):
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

        with patch.dict(os.environ, env_vars):
            with patch("handler.trigger_guide_creation") as mock_trigger:
                mock_trigger.side_effect = ClientError(
                    {
                        "Error": {
                            "Code": "QueueDoesNotExist",
                            "Message": "Queue not found",
                        }
                    },
                    "SendMessage",
                )

                response = lambda_handler(event, None)

                assert response["statusCode"] == 500
                body = json.loads(response["body"])
                assert "Failed to trigger guide creation" in body["message"]

    @mock_aws
    def test_sqs_key_error_on_trigger_returns_500(
        self, env_vars
    ) -> None:
        """Missing PAINT_QUEUE_URL on trigger should return 500."""
        event = {"pathParameters": {"jobId": JOB_ID}}

        s3 = boto3.client("s3")
        s3.create_bucket(
            Bucket=BUCKET_NAME,
            CreateBucketConfiguration={"LocationConstraint": "eu-central-1"},
        )
        for i in range(1):
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

        env_vars_copy = env_vars.copy()
        env_vars_copy.pop("PAINT_QUEUE_URL", None)

        with patch.dict(os.environ, env_vars_copy):
            response = lambda_handler(event, None)

            assert response["statusCode"] == 500
            body = json.loads(response["body"])
            assert "Server misconfiguration" in body["message"]
            assert "SQS queue URL" in body["message"]

    @mock_aws
    def test_race_condition_on_update_job_returns_409_without_sqs_message(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A lost conditional write must not send a second paint message."""
        event = {"pathParameters": {"jobId": JOB_ID}}

        s3 = boto3.client("s3")
        s3.create_bucket(
            Bucket=BUCKET_NAME,
            CreateBucketConfiguration={"LocationConstraint": "eu-central-1"},
        )
        for i in range(1):
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
                "PAINT_QUEUE_URL": queue_url,
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

                with caplog.at_level(logging.WARNING, logger="handler"):
                    response_result = lambda_handler(event, None)

        assert response_result["statusCode"] == 409
        body = json.loads(response_result["body"])
        assert body == {
            "error": "Conflict",
            "message": "jobId is already in progress or completed",
        }
        messages = sqs.receive_message(QueueUrl=queue_url)
        assert "Messages" not in messages
        [record] = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert record.stage == "update_status"
        assert record.jobId == JOB_ID

    @mock_aws
    def test_status_changed_after_read_returns_409_and_keeps_winner_state(self) -> None:
        """A real conditional write losing to a concurrent start leaves the winner's item."""
        event = {
            "pathParameters": {"jobId": JOB_ID},
            "body": json.dumps({"instruction": "loser instruction"}),
        }
        s3 = boto3.client("s3")
        s3.create_bucket(
            Bucket=BUCKET_NAME,
            CreateBucketConfiguration={"LocationConstraint": "eu-central-1"},
        )
        s3.put_object(Bucket=BUCKET_NAME, Key=f"uploads/{JOB_ID}/image_0.jpg", Body=b"data")
        dynamodb = boto3.client("dynamodb")
        dynamodb.create_table(
            TableName=TABLE_NAME,
            KeySchema=[{"AttributeName": "jobId", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "jobId", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        dynamodb.put_item(
            TableName=TABLE_NAME,
            Item={"jobId": {"S": JOB_ID}, "jobStatus": {"S": "UPLOADED"}},
        )
        queue_url = boto3.client("sqs").create_queue(QueueName="test-queue")["QueueUrl"]

        def _concurrent_start(job_id: str) -> list:
            """Simulate another request winning the start right after our status read."""
            dynamodb.update_item(
                TableName=TABLE_NAME,
                Key={"jobId": {"S": job_id}},
                UpdateExpression="SET jobStatus = :status",
                ExpressionAttributeValues={":status": {"S": "IN_PROGRESS"}},
            )
            return [f"s3://{BUCKET_NAME}/uploads/{job_id}/image_0.jpg"]

        with patch.dict(
            os.environ,
            {
                "JOBS_TABLE_NAME": TABLE_NAME,
                "UPLOAD_BUCKET_NAME": BUCKET_NAME,
                "PAINT_QUEUE_URL": queue_url,
            },
        ):
            with patch("handler.list_uploaded_images", side_effect=_concurrent_start):
                response = lambda_handler(event, None)

        assert response["statusCode"] == 409
        item = dynamodb.get_item(TableName=TABLE_NAME, Key={"jobId": {"S": JOB_ID}})["Item"]
        assert item["jobStatus"]["S"] == "IN_PROGRESS"
        assert "instruction" not in item
        assert "Messages" not in boto3.client("sqs").receive_message(QueueUrl=queue_url)




class TestLambdaHandler:
    """Tests for the full lambda_handler orchestration."""

    @mock_aws
    def test_happy_path_starts_job(self, env_vars) -> None:
        """Full flow: valid jobId, job exists, 1 image, update status."""
        s3 = boto3.client("s3")
        s3.create_bucket(
            Bucket=BUCKET_NAME,
            CreateBucketConfiguration={"LocationConstraint": "eu-central-1"},
        )
        prefix = f"uploads/{JOB_ID}/"
        for i in range(1):
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

        env_vars["PAINT_QUEUE_URL"] = queue_url

        event = {"pathParameters": {"jobId": JOB_ID}}
        with patch.dict(os.environ, env_vars):
            result = lambda_handler(event, None)

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["jobId"] == JOB_ID
        assert body["jobStatus"] == "IN_PROGRESS"

    @mock_aws
    def test_invalid_job_id_returns_400(self, env_vars) -> None:
        """Missing jobId should return 400."""
        event = {"pathParameters": {}}
        with patch.dict(os.environ, env_vars):
            result = lambda_handler(event, None)

        assert result["statusCode"] == 400
        body = json.loads(result["body"])
        assert body["error"] == "InvalidRequest"

    @mock_aws
    def test_job_not_found_returns_404(self, env_vars) -> None:
        """Job not found in DynamoDB should return 404."""
        dynamodb = boto3.client("dynamodb")
        dynamodb.create_table(
            TableName=TABLE_NAME,
            KeySchema=[{"AttributeName": "jobId", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "jobId", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )

        event = {"pathParameters": {"jobId": JOB_ID}}
        with patch.dict(os.environ, env_vars):
            result = lambda_handler(event, None)

        assert result["statusCode"] == 404
        body = json.loads(result["body"])
        assert body["error"] == "NotFound"

    @mock_aws
    def test_job_status_not_uploaded_returns_409(self, env_vars) -> None:
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

        event = {"pathParameters": {"jobId": JOB_ID}}
        with patch.dict(os.environ, env_vars):
            result = lambda_handler(event, None)

        assert result["statusCode"] == 409
        body = json.loads(result["body"])
        assert body["error"] == "Conflict"

    @mock_aws
    def test_image_count_not_one_returns_422(self, env_vars) -> None:
        """Image count != 1 should return 422."""
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

        event = {"pathParameters": {"jobId": JOB_ID}}
        with patch.dict(os.environ, env_vars):
            result = lambda_handler(event, None)

        assert result["statusCode"] == 422
        body = json.loads(result["body"])
        assert body["error"] == "InvalidImageCount"
        assert body["imageCount"] == 2

    @mock_aws
    def test_empty_job_id_returns_400(self, env_vars) -> None:
        """Empty jobId should return 400."""
        event = {"pathParameters": {"jobId": ""}}
        with patch.dict(os.environ, env_vars):
            result = lambda_handler(event, None)

        assert result["statusCode"] == 400
        body = json.loads(result["body"])
        assert body["error"] == "InvalidRequest"
