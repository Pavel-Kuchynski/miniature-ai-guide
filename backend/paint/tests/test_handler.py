"""Tests for the paint Lambda handler.

Covers SQS message parsing, S3 image download, prompt fetching, OpenAI image
generation, S3 upload via presigned URLs, DynamoDB status updates, SQS notification,
and the full orchestration flow including failure paths.
"""

import base64
import datetime
import json
import os
from typing import Any
from unittest.mock import MagicMock, patch

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from handler import (
    EXPECTED_IMAGE_COUNT,
    _request_presigned_url,
    _upload_image_via_presigned_url,
    download_images_from_s3,
    fetch_prompt_from_s3,
    generate_painted_images,
    lambda_handler,
    notify_guide_creation,
    parse_job_id,
    update_job_status,
    upload_painted_images,
)

UPLOAD_BUCKET = "test-upload-bucket"
STATIC_BUCKET = "test-static-bucket"
PAINT_BUCKET = "test-paint-bucket"
TABLE_NAME = "test-jobs-table"
QUEUE_URL = "https://sqs.us-east-1.amazonaws.com/123456789/test-queue"
JOB_ID = "123e4567-e89b-12d3-a456-426614174000"
FAKE_IMAGE = b"fake-image-data"
FAKE_PROMPT = "Paint this miniature in classic style."


def _make_sqs_event(job_id: str) -> dict:
    """Build a minimal SQS Lambda event containing the given jobId."""
    return {"Records": [{"body": json.dumps({"jobId": job_id})}]}


def _make_openai_response(count: int = EXPECTED_IMAGE_COUNT) -> MagicMock:
    """Build a mock OpenAI images.edit response with `count` base64-encoded images."""
    mock_response = MagicMock()
    mock_response.data = [
        MagicMock(b64_json=base64.b64encode(FAKE_IMAGE).decode())
        for _ in range(count)
    ]
    return mock_response


def _create_dynamo_table(dynamodb_client: Any) -> None:
    dynamodb_client.create_table(
        TableName=TABLE_NAME,
        KeySchema=[{"AttributeName": "jobId", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "jobId", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )


def _put_job(dynamodb_client: Any, status: str = "IN_PROGRESS") -> None:
    dynamodb_client.put_item(
        TableName=TABLE_NAME,
        Item={
            "jobId": {"S": JOB_ID},
            "jobStatus": {"S": status},
            "createdAt": {"S": datetime.datetime.now(datetime.timezone.utc).isoformat()},
            "updatedAt": {"S": datetime.datetime.now(datetime.timezone.utc).isoformat()},
        },
    )


def _upload_reference_images(s3_client: Any) -> None:
    for i in range(EXPECTED_IMAGE_COUNT):
        s3_client.put_object(
            Bucket=UPLOAD_BUCKET,
            Key=f"uploads/{JOB_ID}/image_{i}.jpg",
            Body=FAKE_IMAGE,
        )


class TestParseJobId:
    """Tests for `parse_job_id` SQS message parsing."""

    def test_valid_event_returns_job_id(self) -> None:
        """Valid SQS event with jobId should return the job id."""
        event = _make_sqs_event(JOB_ID)
        result = parse_job_id(event)
        assert result == JOB_ID

    def test_whitespace_job_id_is_trimmed(self) -> None:
        """`jobId` with surrounding whitespace should be trimmed and returned."""
        event = {"Records": [{"body": json.dumps({"jobId": f"  {JOB_ID}  "})}]}
        result = parse_job_id(event)
        assert result == JOB_ID

    def test_missing_records_returns_none(self) -> None:
        """Event with no Records should return None."""
        result = parse_job_id({})
        assert result is None

    def test_empty_records_list_returns_none(self) -> None:
        """Event with empty Records list should return None."""
        result = parse_job_id({"Records": []})
        assert result is None

    def test_null_records_returns_none(self) -> None:
        """Event with Records=None should return None."""
        result = parse_job_id({"Records": None})
        assert result is None

    def test_invalid_json_body_returns_none(self) -> None:
        """SQS record with non-JSON body should return None."""
        event = {"Records": [{"body": "not-json"}]}
        result = parse_job_id(event)
        assert result is None

    def test_missing_job_id_in_body_returns_none(self) -> None:
        """Body JSON without `jobId` field should return None."""
        event = {"Records": [{"body": json.dumps({"otherId": "abc"})}]}
        result = parse_job_id(event)
        assert result is None

    def test_empty_job_id_returns_none(self) -> None:
        """Empty string `jobId` should return None."""
        event = {"Records": [{"body": json.dumps({"jobId": ""})}]}
        result = parse_job_id(event)
        assert result is None

    def test_whitespace_only_job_id_returns_none(self) -> None:
        """Whitespace-only `jobId` should return None."""
        event = {"Records": [{"body": json.dumps({"jobId": "   "})}]}
        result = parse_job_id(event)
        assert result is None

    def test_non_string_job_id_returns_none(self) -> None:
        """Non-string `jobId` (integer) should return None."""
        event = {"Records": [{"body": json.dumps({"jobId": 12345})}]}
        result = parse_job_id(event)
        assert result is None


class TestDownloadImagesFromS3:
    """Tests for `download_images_from_s3`."""

    @mock_aws
    def test_downloads_four_images(self) -> None:
        """Should download exactly 4 reference images from S3."""
        s3 = boto3.client("s3")
        s3.create_bucket(Bucket=UPLOAD_BUCKET)
        _upload_reference_images(s3)

        with patch.dict(os.environ, {"UPLOAD_BUCKET_NAME": UPLOAD_BUCKET}):
            images = download_images_from_s3(JOB_ID)

        assert len(images) == EXPECTED_IMAGE_COUNT
        assert all(img == FAKE_IMAGE for img in images)

    @mock_aws
    def test_excludes_zero_byte_objects(self) -> None:
        """Zero-byte folder marker objects should be excluded from downloads."""
        s3 = boto3.client("s3")
        s3.create_bucket(Bucket=UPLOAD_BUCKET)
        for i in range(3):
            s3.put_object(Bucket=UPLOAD_BUCKET, Key=f"uploads/{JOB_ID}/image_{i}.jpg", Body=FAKE_IMAGE)
        s3.put_object(Bucket=UPLOAD_BUCKET, Key=f"uploads/{JOB_ID}/.marker", Body=b"")

        with patch.dict(os.environ, {"UPLOAD_BUCKET_NAME": UPLOAD_BUCKET}):
            images = download_images_from_s3(JOB_ID)

        assert len(images) == 3

    @mock_aws
    def test_returns_images_in_key_order(self) -> None:
        """Images should be returned in sorted key order."""
        s3 = boto3.client("s3")
        s3.create_bucket(Bucket=UPLOAD_BUCKET)
        bodies = {f"uploads/{JOB_ID}/image_{i}.jpg": bytes([i]) for i in range(4)}
        for key, body in bodies.items():
            s3.put_object(Bucket=UPLOAD_BUCKET, Key=key, Body=body)

        with patch.dict(os.environ, {"UPLOAD_BUCKET_NAME": UPLOAD_BUCKET}):
            images = download_images_from_s3(JOB_ID)

        assert images == [bytes([i]) for i in range(4)]

    def test_missing_bucket_env_var_raises_key_error(self) -> None:
        """Missing UPLOAD_BUCKET_NAME should raise KeyError."""
        with patch.dict(os.environ, {}, clear=True):
            with pytest.raises(KeyError):
                download_images_from_s3(JOB_ID)


class TestFetchPromptFromS3:
    """Tests for `fetch_prompt_from_s3`."""

    @mock_aws
    def test_reads_prompt_text(self) -> None:
        """Should return the prompt file contents as a string."""
        s3 = boto3.client("s3")
        s3.create_bucket(Bucket=STATIC_BUCKET)
        s3.put_object(
            Bucket=STATIC_BUCKET,
            Key="prompts/paint_images_promt.txt",
            Body=FAKE_PROMPT.encode("utf-8"),
        )

        with patch.dict(os.environ, {"STATIC_BUCKET_NAME": STATIC_BUCKET}):
            result = fetch_prompt_from_s3()

        assert result == FAKE_PROMPT

    def test_missing_bucket_env_var_raises_key_error(self) -> None:
        """Missing STATIC_BUCKET_NAME should raise KeyError."""
        with patch.dict(os.environ, {}, clear=True):
            with pytest.raises(KeyError):
                fetch_prompt_from_s3()

    @mock_aws
    def test_missing_prompt_file_raises_client_error(self) -> None:
        """Missing prompt file in S3 should raise ClientError."""
        s3 = boto3.client("s3")
        s3.create_bucket(Bucket=STATIC_BUCKET)

        with patch.dict(os.environ, {"STATIC_BUCKET_NAME": STATIC_BUCKET}):
            with pytest.raises(ClientError):
                fetch_prompt_from_s3()


class TestGeneratePaintedImages:
    """Tests for `generate_painted_images`."""

    def test_returns_decoded_images_from_openai(self) -> None:
        """Should decode base64 images from the OpenAI response."""
        mock_response = _make_openai_response(EXPECTED_IMAGE_COUNT)

        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}):
            with patch("handler.OpenAI") as mock_openai_class:
                mock_client = MagicMock()
                mock_openai_class.return_value = mock_client
                mock_client.images.edit.return_value = mock_response

                result = generate_painted_images([FAKE_IMAGE] * 4, FAKE_PROMPT)

        assert len(result) == EXPECTED_IMAGE_COUNT
        assert all(img == FAKE_IMAGE for img in result)

    def test_passes_correct_arguments_to_openai(self) -> None:
        """Should call OpenAI images.edit with correct model, n, and prompt."""
        mock_response = _make_openai_response(EXPECTED_IMAGE_COUNT)

        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}):
            with patch("handler.OpenAI") as mock_openai_class:
                mock_client = MagicMock()
                mock_openai_class.return_value = mock_client
                mock_client.images.edit.return_value = mock_response

                generate_painted_images([FAKE_IMAGE] * 4, FAKE_PROMPT)

        call_kwargs = mock_client.images.edit.call_args.kwargs
        assert call_kwargs["model"] == "gpt-image-1"
        assert call_kwargs["n"] == EXPECTED_IMAGE_COUNT
        assert call_kwargs["prompt"] == FAKE_PROMPT

    def test_openai_error_propagates(self) -> None:
        """OpenAIError from the API should propagate to the caller."""
        from openai import OpenAIError

        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}):
            with patch("handler.OpenAI") as mock_openai_class:
                mock_client = MagicMock()
                mock_openai_class.return_value = mock_client
                mock_client.images.edit.side_effect = OpenAIError("API error")

                with pytest.raises(OpenAIError):
                    generate_painted_images([FAKE_IMAGE] * 4, FAKE_PROMPT)

    def test_missing_api_key_env_var_raises_key_error(self) -> None:
        """Missing OPENAI_API_KEY should raise KeyError."""
        with patch.dict(os.environ, {}, clear=True):
            with pytest.raises(KeyError):
                generate_painted_images([FAKE_IMAGE] * 4, FAKE_PROMPT)


class TestUploadPaintedImages:
    """Tests for `upload_painted_images`."""

    @mock_aws
    def test_uploads_all_images_via_presigned_urls(self) -> None:
        """Should generate a presigned URL per image and upload each one."""
        s3 = boto3.client("s3")
        s3.create_bucket(Bucket=PAINT_BUCKET)
        images = [FAKE_IMAGE] * EXPECTED_IMAGE_COUNT

        with patch.dict(os.environ, {"PAINT_BUCKET_NAME": PAINT_BUCKET}):
            with patch("handler._upload_image_via_presigned_url") as mock_upload:
                upload_painted_images(JOB_ID, images)

        assert mock_upload.call_count == EXPECTED_IMAGE_COUNT

    @mock_aws
    def test_uploads_correct_image_data(self) -> None:
        """Each upload call should receive the correct image bytes."""
        s3 = boto3.client("s3")
        s3.create_bucket(Bucket=PAINT_BUCKET)
        images = [bytes([i]) for i in range(EXPECTED_IMAGE_COUNT)]

        uploaded_data = []

        def capture_upload(url: str, data: bytes) -> None:
            uploaded_data.append(data)

        with patch.dict(os.environ, {"PAINT_BUCKET_NAME": PAINT_BUCKET}):
            with patch("handler._upload_image_via_presigned_url", side_effect=capture_upload):
                upload_painted_images(JOB_ID, images)

        assert uploaded_data == images

    def test_missing_bucket_env_var_raises_key_error(self) -> None:
        """Missing PAINT_BUCKET_NAME should raise KeyError."""
        with patch.dict(os.environ, {}, clear=True):
            with pytest.raises(KeyError):
                upload_painted_images(JOB_ID, [FAKE_IMAGE])


class TestUpdateJobStatus:
    """Tests for `update_job_status`."""

    @mock_aws
    def test_updates_status_to_painted(self) -> None:
        """Should update job status from IN_PROGRESS to PAINTED."""
        dynamodb = boto3.client("dynamodb")
        _create_dynamo_table(dynamodb)
        _put_job(dynamodb, "IN_PROGRESS")

        with patch.dict(os.environ, {"JOBS_TABLE_NAME": TABLE_NAME}):
            update_job_status(JOB_ID, "PAINTED")

        item = dynamodb.get_item(TableName=TABLE_NAME, Key={"jobId": {"S": JOB_ID}})["Item"]
        assert item["jobStatus"]["S"] == "PAINTED"

    @mock_aws
    def test_updates_status_to_failed(self) -> None:
        """Should update job status from IN_PROGRESS to FAILED."""
        dynamodb = boto3.client("dynamodb")
        _create_dynamo_table(dynamodb)
        _put_job(dynamodb, "IN_PROGRESS")

        with patch.dict(os.environ, {"JOBS_TABLE_NAME": TABLE_NAME}):
            update_job_status(JOB_ID, "FAILED")

        item = dynamodb.get_item(TableName=TABLE_NAME, Key={"jobId": {"S": JOB_ID}})["Item"]
        assert item["jobStatus"]["S"] == "FAILED"

    @mock_aws
    def test_raises_when_job_not_in_progress(self) -> None:
        """Should raise ConditionalCheckFailedException if job is not IN_PROGRESS."""
        dynamodb = boto3.client("dynamodb")
        _create_dynamo_table(dynamodb)
        _put_job(dynamodb, "PAINTED")

        with patch.dict(os.environ, {"JOBS_TABLE_NAME": TABLE_NAME}):
            with pytest.raises(ClientError) as exc_info:
                update_job_status(JOB_ID, "PAINTED")

        assert exc_info.value.response["Error"]["Code"] == "ConditionalCheckFailedException"

    def test_missing_table_env_var_raises_key_error(self) -> None:
        """Missing JOBS_TABLE_NAME should raise KeyError."""
        with patch.dict(os.environ, {}, clear=True):
            with pytest.raises(KeyError):
                update_job_status(JOB_ID, "PAINTED")


class TestNotifyGuideCreation:
    """Tests for `notify_guide_creation`."""

    @mock_aws
    def test_sends_job_id_to_sqs(self) -> None:
        """Should send a message containing the jobId to the SQS queue."""
        sqs = boto3.client("sqs")
        queue_url = sqs.create_queue(QueueName="test-notify-queue")["QueueUrl"]

        with patch.dict(os.environ, {"GUIDE_CREATION_QUEUE_URL": queue_url}):
            notify_guide_creation(JOB_ID)

        messages = sqs.receive_message(QueueUrl=queue_url)["Messages"]
        assert len(messages) == 1
        body = json.loads(messages[0]["Body"])
        assert body["jobId"] == JOB_ID

    def test_missing_queue_url_env_var_raises_key_error(self) -> None:
        """Missing GUIDE_CREATION_QUEUE_URL should raise KeyError."""
        with patch.dict(os.environ, {}, clear=True):
            with pytest.raises(KeyError):
                notify_guide_creation(JOB_ID)


@pytest.fixture
def env_vars() -> dict:
    """Fixture providing all required environment variables for the handler."""
    return {
        "UPLOAD_BUCKET_NAME": UPLOAD_BUCKET,
        "STATIC_BUCKET_NAME": STATIC_BUCKET,
        "PAINT_BUCKET_NAME": PAINT_BUCKET,
        "JOBS_TABLE_NAME": TABLE_NAME,
        "GUIDE_CREATION_QUEUE_URL": QUEUE_URL,
        "OPENAI_API_KEY": "test-openai-key",
    }


class TestLambdaHandler:
    """Integration tests for the full `lambda_handler` orchestration."""

    @mock_aws
    def test_happy_path_paints_job(self, env_vars: dict) -> None:
        """Full success path: images downloaded, painted, uploaded, status PAINTED, SQS notified."""
        s3 = boto3.client("s3")
        s3.create_bucket(Bucket=UPLOAD_BUCKET)
        s3.create_bucket(Bucket=PAINT_BUCKET)
        s3.create_bucket(Bucket=STATIC_BUCKET)
        _upload_reference_images(s3)
        s3.put_object(
            Bucket=STATIC_BUCKET,
            Key="prompts/paint_images_promt.txt",
            Body=FAKE_PROMPT.encode(),
        )

        dynamodb = boto3.client("dynamodb")
        _create_dynamo_table(dynamodb)
        _put_job(dynamodb, "IN_PROGRESS")

        sqs = boto3.client("sqs")
        queue_url = sqs.create_queue(QueueName="guide-queue")["QueueUrl"]
        env_vars["GUIDE_CREATION_QUEUE_URL"] = queue_url

        mock_response = _make_openai_response(EXPECTED_IMAGE_COUNT)

        with patch.dict(os.environ, env_vars):
            with patch("handler.OpenAI") as mock_openai_class:
                mock_client = MagicMock()
                mock_openai_class.return_value = mock_client
                mock_client.images.edit.return_value = mock_response
                with patch("handler._upload_image_via_presigned_url"):
                    lambda_handler(_make_sqs_event(JOB_ID), None)

        item = dynamodb.get_item(TableName=TABLE_NAME, Key={"jobId": {"S": JOB_ID}})["Item"]
        assert item["jobStatus"]["S"] == "PAINTED"

        messages = sqs.receive_message(QueueUrl=queue_url)["Messages"]
        assert len(messages) == 1
        assert json.loads(messages[0]["Body"])["jobId"] == JOB_ID

    def test_missing_job_id_raises_value_error(self, env_vars: dict) -> None:
        """Unparseable SQS message should raise ValueError (routes to DLQ)."""
        with patch.dict(os.environ, env_vars):
            with pytest.raises(ValueError, match="Missing or invalid jobId"):
                lambda_handler({"Records": []}, None)

    @mock_aws
    def test_download_failure_updates_status_to_failed(self, env_vars: dict) -> None:
        """S3 download failure should set job status to FAILED."""
        dynamodb = boto3.client("dynamodb")
        _create_dynamo_table(dynamodb)
        _put_job(dynamodb, "IN_PROGRESS")

        with patch.dict(os.environ, env_vars):
            with patch("handler.download_images_from_s3") as mock_dl:
                mock_dl.side_effect = ClientError(
                    {"Error": {"Code": "NoSuchBucket", "Message": "Not found"}},
                    "ListObjectsV2",
                )
                lambda_handler(_make_sqs_event(JOB_ID), None)

        item = dynamodb.get_item(TableName=TABLE_NAME, Key={"jobId": {"S": JOB_ID}})["Item"]
        assert item["jobStatus"]["S"] == "FAILED"

    @mock_aws
    def test_prompt_fetch_failure_updates_status_to_failed(self, env_vars: dict) -> None:
        """S3 prompt fetch failure should set job status to FAILED."""
        dynamodb = boto3.client("dynamodb")
        _create_dynamo_table(dynamodb)
        _put_job(dynamodb, "IN_PROGRESS")

        with patch.dict(os.environ, env_vars):
            with patch("handler.download_images_from_s3", return_value=[FAKE_IMAGE] * 4):
                with patch("handler.fetch_prompt_from_s3") as mock_fetch:
                    mock_fetch.side_effect = ClientError(
                        {"Error": {"Code": "NoSuchKey", "Message": "Not found"}},
                        "GetObject",
                    )
                    lambda_handler(_make_sqs_event(JOB_ID), None)

        item = dynamodb.get_item(TableName=TABLE_NAME, Key={"jobId": {"S": JOB_ID}})["Item"]
        assert item["jobStatus"]["S"] == "FAILED"

    @mock_aws
    def test_openai_failure_updates_status_to_failed(self, env_vars: dict) -> None:
        """OpenAI API failure should set job status to FAILED."""
        from openai import OpenAIError

        dynamodb = boto3.client("dynamodb")
        _create_dynamo_table(dynamodb)
        _put_job(dynamodb, "IN_PROGRESS")

        with patch.dict(os.environ, env_vars):
            with patch("handler.download_images_from_s3", return_value=[FAKE_IMAGE] * 4):
                with patch("handler.fetch_prompt_from_s3", return_value=FAKE_PROMPT):
                    with patch("handler.generate_painted_images") as mock_gen:
                        mock_gen.side_effect = OpenAIError("Rate limit")
                        lambda_handler(_make_sqs_event(JOB_ID), None)

        item = dynamodb.get_item(TableName=TABLE_NAME, Key={"jobId": {"S": JOB_ID}})["Item"]
        assert item["jobStatus"]["S"] == "FAILED"

    @mock_aws
    def test_fewer_than_four_images_updates_status_to_failed(self, env_vars: dict) -> None:
        """OpenAI returning fewer than 4 images should set job status to FAILED."""
        dynamodb = boto3.client("dynamodb")
        _create_dynamo_table(dynamodb)
        _put_job(dynamodb, "IN_PROGRESS")

        with patch.dict(os.environ, env_vars):
            with patch("handler.download_images_from_s3", return_value=[FAKE_IMAGE] * 4):
                with patch("handler.fetch_prompt_from_s3", return_value=FAKE_PROMPT):
                    with patch("handler.generate_painted_images", return_value=[FAKE_IMAGE] * 2):
                        lambda_handler(_make_sqs_event(JOB_ID), None)

        item = dynamodb.get_item(TableName=TABLE_NAME, Key={"jobId": {"S": JOB_ID}})["Item"]
        assert item["jobStatus"]["S"] == "FAILED"

    @mock_aws
    def test_upload_failure_updates_status_to_failed(self, env_vars: dict) -> None:
        """S3 upload failure should set job status to FAILED."""
        import urllib.error

        dynamodb = boto3.client("dynamodb")
        _create_dynamo_table(dynamodb)
        _put_job(dynamodb, "IN_PROGRESS")

        with patch.dict(os.environ, env_vars):
            with patch("handler.download_images_from_s3", return_value=[FAKE_IMAGE] * 4):
                with patch("handler.fetch_prompt_from_s3", return_value=FAKE_PROMPT):
                    with patch("handler.generate_painted_images", return_value=[FAKE_IMAGE] * 4):
                        with patch("handler.upload_painted_images") as mock_upload:
                            mock_upload.side_effect = urllib.error.HTTPError(
                                None, 403, "Forbidden", {}, None
                            )
                            lambda_handler(_make_sqs_event(JOB_ID), None)

        item = dynamodb.get_item(TableName=TABLE_NAME, Key={"jobId": {"S": JOB_ID}})["Item"]
        assert item["jobStatus"]["S"] == "FAILED"

    @mock_aws
    def test_dynamodb_update_painted_failure_is_logged(self, env_vars: dict) -> None:
        """DynamoDB failure when updating to PAINTED should be handled without raising."""
        dynamodb = boto3.client("dynamodb")
        _create_dynamo_table(dynamodb)
        _put_job(dynamodb, "IN_PROGRESS")

        with patch.dict(os.environ, env_vars):
            with patch("handler.download_images_from_s3", return_value=[FAKE_IMAGE] * 4):
                with patch("handler.fetch_prompt_from_s3", return_value=FAKE_PROMPT):
                    with patch("handler.generate_painted_images", return_value=[FAKE_IMAGE] * 4):
                        with patch("handler.upload_painted_images"):
                            with patch("handler.update_job_status") as mock_update:
                                mock_update.side_effect = ClientError(
                                    {"Error": {"Code": "ThrottlingException", "Message": "Throttled"}},
                                    "UpdateItem",
                                )
                                lambda_handler(_make_sqs_event(JOB_ID), None)

    @mock_aws
    def test_notify_failure_is_logged_without_raising(self, env_vars: dict) -> None:
        """SQS notification failure after PAINTED should be handled without raising."""
        dynamodb = boto3.client("dynamodb")
        _create_dynamo_table(dynamodb)
        _put_job(dynamodb, "IN_PROGRESS")

        with patch.dict(os.environ, env_vars):
            with patch("handler.download_images_from_s3", return_value=[FAKE_IMAGE] * 4):
                with patch("handler.fetch_prompt_from_s3", return_value=FAKE_PROMPT):
                    with patch("handler.generate_painted_images", return_value=[FAKE_IMAGE] * 4):
                        with patch("handler.upload_painted_images"):
                            with patch("handler.update_job_status"):
                                with patch("handler.notify_guide_creation") as mock_notify:
                                    mock_notify.side_effect = ClientError(
                                        {"Error": {"Code": "QueueDoesNotExist", "Message": "No queue"}},
                                        "SendMessage",
                                    )
                                    lambda_handler(_make_sqs_event(JOB_ID), None)

    @mock_aws
    def test_failed_status_update_failure_is_logged(self, env_vars: dict) -> None:
        """If the FAILED status update itself fails, the error is logged without re-raising."""
        dynamodb = boto3.client("dynamodb")
        _create_dynamo_table(dynamodb)
        _put_job(dynamodb, "IN_PROGRESS")

        with patch.dict(os.environ, env_vars):
            with patch("handler.download_images_from_s3") as mock_dl:
                mock_dl.side_effect = ClientError(
                    {"Error": {"Code": "NoSuchBucket", "Message": "Not found"}},
                    "ListObjectsV2",
                )
                with patch("handler.update_job_status") as mock_update:
                    mock_update.side_effect = ClientError(
                        {"Error": {"Code": "ThrottlingException", "Message": "Throttled"}},
                        "UpdateItem",
                    )
                    lambda_handler(_make_sqs_event(JOB_ID), None)
