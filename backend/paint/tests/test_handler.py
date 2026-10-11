"""Tests for the paint Lambda handler.

Covers SQS message parsing, S3 image download, prompt fetching, OpenAI image
generation (including client timeouts and retries), S3 upload via presigned URLs
(including the request timeout), DynamoDB status updates, SQS notification, job
lookup errors, and the full orchestration flow including failure paths and the
structured error logs emitted on each failure.
"""

import base64
import datetime
import io
import json
import logging
import os
import urllib.error
from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock, patch

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws
from PIL import Image

from handler import (
    DEFAULT_OPENAI_API_KEY_SECRET_NAME,
    DEFAULT_OPENAI_MODEL,
    MAX_IMAGE_SIDE_PX,
    OPENAI_MAX_RETRIES,
    OPENAI_TIMEOUT_SECONDS,
    REFERENCE_IMAGE_COUNT,
    REQUESTED_OUTPUT_COUNT,
    UPLOAD_TIMEOUT_SECONDS,
    JobNotFoundError,
    JobRecord,
    JobStatus,
    _fetch_openai_api_key,
    _get_client,
    _resize_reference_image,
    _request_presigned_url,
    _upload_image_via_presigned_url,
    build_prompt,
    download_reference_image,
    fetch_prompt_from_s3,
    generate_painted_images,
    get_job,
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


def _make_image_bytes(width: int, height: int, fmt: str = "JPEG") -> bytes:
    """Build real encoded image bytes of the given size and format."""
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), "red").save(buffer, format=fmt)
    return buffer.getvalue()


def _make_sqs_event(job_id: str) -> dict:
    """Build a minimal SQS Lambda event containing the given jobId."""
    return {"Records": [{"body": json.dumps({"jobId": job_id})}]}


@pytest.fixture(autouse=True)
def _clear_handler_caches() -> Iterator[None]:
    """Reset the module-level boto3 client and API-key caches around every test.

    Without this, a client or key cached by one test (possibly built from a `patch`ed
    `boto3.client`) would leak into the next test.
    """
    _get_client.cache_clear()
    _fetch_openai_api_key.cache_clear()
    yield
    _get_client.cache_clear()
    _fetch_openai_api_key.cache_clear()


def _make_openai_response(count: int = REQUESTED_OUTPUT_COUNT) -> MagicMock:
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


def _put_job(
    dynamodb_client: Any,
    status: JobStatus = JobStatus.IN_PROGRESS,
    instruction: str | None = None,
) -> None:
    item = {
        "jobId": {"S": JOB_ID},
        "jobStatus": {"S": status.value},
        "createdAt": {"S": datetime.datetime.now(datetime.timezone.utc).isoformat()},
        "updatedAt": {"S": datetime.datetime.now(datetime.timezone.utc).isoformat()},
    }
    if instruction is not None:
        item["instruction"] = {"S": instruction}
    dynamodb_client.put_item(TableName=TABLE_NAME, Item=item)


def _upload_reference_images(s3_client: Any, body: bytes = FAKE_IMAGE) -> None:
    for i in range(REFERENCE_IMAGE_COUNT):
        s3_client.put_object(
            Bucket=UPLOAD_BUCKET,
            Key=f"uploads/{JOB_ID}/image_{i}.jpg",
            Body=body,
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

    @pytest.mark.parametrize("body", ["42", "[]", '"x"', "null"])
    def test_non_object_json_body_returns_none(self, body: str) -> None:
        """A JSON body that is valid but not an object should return None, not raise."""
        event = {"Records": [{"body": body}]}
        assert parse_job_id(event) is None

    @pytest.mark.parametrize("record", ["x", None, 42, ["body"]])
    def test_non_object_record_returns_none(self, record: Any) -> None:
        """A first record that is not a mapping should return None, not raise."""
        assert parse_job_id({"Records": [record]}) is None

    def test_non_list_records_returns_none(self) -> None:
        """`Records` that is a mapping instead of a list should return None, not raise."""
        assert parse_job_id({"Records": {"body": json.dumps({"jobId": JOB_ID})}}) is None


class TestDownloadReferenceImage:
    """Tests for `download_reference_image`."""

    @mock_aws
    def test_downloads_reference_image_bytes(self) -> None:
        """Should return the raw bytes of the single reference image from S3."""
        s3 = boto3.client("s3")
        s3.create_bucket(Bucket=UPLOAD_BUCKET)
        _upload_reference_images(s3)

        with patch.dict(os.environ, {"UPLOAD_BUCKET_NAME": UPLOAD_BUCKET}):
            image = download_reference_image(JOB_ID)

        assert image == FAKE_IMAGE

    @mock_aws
    def test_excludes_zero_byte_objects(self) -> None:
        """Zero-byte folder marker objects should be excluded from downloads."""
        s3 = boto3.client("s3")
        s3.create_bucket(Bucket=UPLOAD_BUCKET)
        s3.put_object(Bucket=UPLOAD_BUCKET, Key=f"uploads/{JOB_ID}/image_0.jpg", Body=FAKE_IMAGE)
        s3.put_object(Bucket=UPLOAD_BUCKET, Key=f"uploads/{JOB_ID}/.marker", Body=b"")

        with patch.dict(os.environ, {"UPLOAD_BUCKET_NAME": UPLOAD_BUCKET}):
            image = download_reference_image(JOB_ID)

        assert image == FAKE_IMAGE

    @mock_aws
    def test_downloads_only_first_image_when_several_exist(self) -> None:
        """Only the first key (sorted) should be downloaded to avoid extra model cost."""
        s3 = boto3.client("s3")
        s3.create_bucket(Bucket=UPLOAD_BUCKET)
        bodies = {f"uploads/{JOB_ID}/image_{i}.jpg": bytes([i]) for i in range(4)}
        for key, body in bodies.items():
            s3.put_object(Bucket=UPLOAD_BUCKET, Key=key, Body=body)

        with patch.dict(os.environ, {"UPLOAD_BUCKET_NAME": UPLOAD_BUCKET}):
            image = download_reference_image(JOB_ID)

        assert image == bytes([0])

    @mock_aws
    def test_no_reference_image_raises_value_error(self) -> None:
        """An empty upload prefix should raise instead of calling the model with no image."""
        s3 = boto3.client("s3")
        s3.create_bucket(Bucket=UPLOAD_BUCKET)

        with patch.dict(os.environ, {"UPLOAD_BUCKET_NAME": UPLOAD_BUCKET}):
            with pytest.raises(ValueError):
                download_reference_image(JOB_ID)

    def test_missing_bucket_env_var_raises_key_error(self) -> None:
        """Missing UPLOAD_BUCKET_NAME should raise KeyError."""
        with patch.dict(os.environ, {}, clear=True):
            with pytest.raises(KeyError):
                download_reference_image(JOB_ID)

    def test_reuses_one_s3_client_across_calls(self) -> None:
        """Repeated downloads in one execution context should not build a new S3 client."""
        with patch("handler.boto3.client") as mock_boto_client:
            mock_boto_client.return_value.get_paginator.return_value.paginate.return_value = [
                {"Contents": [{"Key": f"uploads/{JOB_ID}/a.jpg", "Size": 3}]}
            ]
            mock_boto_client.return_value.get_object.return_value = {
                "Body": MagicMock(read=MagicMock(return_value=FAKE_IMAGE))
            }
            with patch.dict(os.environ, {"UPLOAD_BUCKET_NAME": UPLOAD_BUCKET}):
                download_reference_image(JOB_ID)
                download_reference_image(JOB_ID)

        mock_boto_client.assert_called_once_with("s3")


class TestFetchPromptFromS3:
    """Tests for `fetch_prompt_from_s3`."""

    @mock_aws
    def test_reads_prompt_text(self) -> None:
        """Should return the prompt file contents as a string."""
        s3 = boto3.client("s3")
        s3.create_bucket(Bucket=STATIC_BUCKET)
        s3.put_object(
            Bucket=STATIC_BUCKET,
            Key="prompts/paint_images_prompt.txt",
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


class TestFetchOpenaiApiKey:
    """Tests for `_fetch_openai_api_key`."""

    def test_retrieves_api_key_from_secrets_manager(self) -> None:
        """Should retrieve and decode the OpenAI API key from Secrets Manager."""
        with patch("handler.boto3.client") as mock_boto_client:
            mock_secrets = MagicMock()
            mock_boto_client.return_value = mock_secrets
            mock_secrets.get_secret_value.return_value = {
                "SecretString": json.dumps({"OPENAI_API_KEY": "test-api-key-12345"})
            }

            result = _fetch_openai_api_key(DEFAULT_OPENAI_API_KEY_SECRET_NAME)

        assert result == "test-api-key-12345"
        mock_secrets.get_secret_value.assert_called_once_with(
            SecretId=DEFAULT_OPENAI_API_KEY_SECRET_NAME
        )

    def test_refetches_when_secret_name_changes(self) -> None:
        """The cache is keyed by secret name, so a different secret must be fetched again."""
        with patch("handler.boto3.client") as mock_boto_client:
            mock_secrets = MagicMock()
            mock_boto_client.return_value = mock_secrets
            mock_secrets.get_secret_value.return_value = {
                "SecretString": json.dumps({"OPENAI_API_KEY": "key"})
            }

            _fetch_openai_api_key("secret-a")
            _fetch_openai_api_key("secret-b")

        assert mock_secrets.get_secret_value.call_count == 2

    def test_uses_cached_api_key_on_second_call(self) -> None:
        """Should return cached key on second call without calling Secrets Manager."""
        with patch("handler.boto3.client") as mock_boto_client:
            mock_secrets = MagicMock()
            mock_boto_client.return_value = mock_secrets
            mock_secrets.get_secret_value.return_value = {
                "SecretString": json.dumps({"OPENAI_API_KEY": "cached-key-12345"})
            }

            result1 = _fetch_openai_api_key(DEFAULT_OPENAI_API_KEY_SECRET_NAME)
            result2 = _fetch_openai_api_key(DEFAULT_OPENAI_API_KEY_SECRET_NAME)

        assert result1 == "cached-key-12345"
        assert result2 == "cached-key-12345"
        mock_secrets.get_secret_value.assert_called_once()

    def test_raises_client_error_on_secret_not_found(self) -> None:
        """Should propagate ClientError if secret does not exist."""
        with patch("handler.boto3.client") as mock_boto_client:
            mock_secrets = MagicMock()
            mock_boto_client.return_value = mock_secrets
            mock_secrets.get_secret_value.side_effect = ClientError(
                {"Error": {"Code": "ResourceNotFoundException", "Message": "Not found"}},
                "GetSecretValue",
            )

            with pytest.raises(ClientError):
                _fetch_openai_api_key(DEFAULT_OPENAI_API_KEY_SECRET_NAME)

    def test_raises_json_decode_error_on_invalid_secret_format(self) -> None:
        """Should propagate JSONDecodeError if secret is not valid JSON."""
        with patch("handler.boto3.client") as mock_boto_client:
            mock_secrets = MagicMock()
            mock_boto_client.return_value = mock_secrets
            mock_secrets.get_secret_value.return_value = {"SecretString": "not-valid-json"}

            with pytest.raises(json.JSONDecodeError):
                _fetch_openai_api_key(DEFAULT_OPENAI_API_KEY_SECRET_NAME)

    def test_raises_key_error_if_api_key_field_missing(self) -> None:
        """Should raise KeyError if 'OPENAI_API_KEY' field is not in secret JSON."""
        with patch("handler.boto3.client") as mock_boto_client:
            mock_secrets = MagicMock()
            mock_boto_client.return_value = mock_secrets
            mock_secrets.get_secret_value.return_value = {
                "SecretString": json.dumps({"wrong_field": "value"})
            }

            with pytest.raises(KeyError):
                _fetch_openai_api_key(DEFAULT_OPENAI_API_KEY_SECRET_NAME)


class TestResizeReferenceImage:
    """Tests for `_resize_reference_image`."""

    @staticmethod
    def _open(data: bytes) -> Image.Image:
        return Image.open(io.BytesIO(data))

    def test_landscape_longer_side_is_capped(self) -> None:
        with self._open(_resize_reference_image(_make_image_bytes(4000, 1000))) as img:
            assert img.size == (1024, 256)

    def test_portrait_longer_side_is_capped(self) -> None:
        with self._open(_resize_reference_image(_make_image_bytes(1000, 3000))) as img:
            assert img.size == (341, 1024)

    def test_small_image_is_not_upscaled(self) -> None:
        with self._open(_resize_reference_image(_make_image_bytes(200, 100))) as img:
            assert img.size == (200, 100)

    def test_output_is_png(self) -> None:
        with self._open(_resize_reference_image(_make_image_bytes(50, 50, "JPEG"))) as img:
            assert img.format == "PNG"


class TestGeneratePaintedImages:
    """Tests for `generate_painted_images`."""

    @pytest.fixture(autouse=True)
    def _default_openai_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Unset the OpenAI overrides so defaults are asserted regardless of developer env."""
        monkeypatch.delenv("OPENAI_MODEL", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY_SECRET_NAME", raising=False)

    def test_returns_decoded_images_from_openai(self) -> None:
        """Should decode base64 images from the OpenAI response."""
        mock_response = _make_openai_response(REQUESTED_OUTPUT_COUNT)

        with patch("handler._fetch_openai_api_key", return_value="test-key"):
            with patch("handler.OpenAI") as mock_openai_class:
                mock_client = MagicMock()
                mock_openai_class.return_value = mock_client
                mock_client.images.edit.return_value = mock_response

                result = generate_painted_images([_make_image_bytes(8, 8)], FAKE_PROMPT)

        assert len(result) == REQUESTED_OUTPUT_COUNT
        assert all(img == FAKE_IMAGE for img in result)

    def test_passes_correct_arguments_to_openai(self) -> None:
        """Should call OpenAI images.edit with correct model, n, and prompt."""
        mock_response = _make_openai_response(REQUESTED_OUTPUT_COUNT)

        with patch("handler._fetch_openai_api_key", return_value="test-key"):
            with patch("handler.OpenAI") as mock_openai_class:
                mock_client = MagicMock()
                mock_openai_class.return_value = mock_client
                mock_client.images.edit.return_value = mock_response

                generate_painted_images([_make_image_bytes(8, 8)], FAKE_PROMPT)

        call_kwargs = mock_client.images.edit.call_args.kwargs
        assert call_kwargs["model"] == DEFAULT_OPENAI_MODEL
        assert call_kwargs["n"] == REQUESTED_OUTPUT_COUNT
        assert call_kwargs["prompt"] == FAKE_PROMPT
        assert call_kwargs["output_format"] == "png"
        assert call_kwargs["quality"] == "medium"

    def test_sends_resized_png_reference_to_openai(self) -> None:
        """Reference images should be downscaled and sent as PNG."""
        mock_response = _make_openai_response(REQUESTED_OUTPUT_COUNT)

        with patch("handler._fetch_openai_api_key", return_value="test-key"):
            with patch("handler.OpenAI") as mock_openai_class:
                mock_client = MagicMock()
                mock_openai_class.return_value = mock_client
                mock_client.images.edit.return_value = mock_response

                generate_painted_images([_make_image_bytes(3000, 2000)], FAKE_PROMPT)

        name, file_obj, mime = mock_client.images.edit.call_args.kwargs["image"][0]
        assert name.endswith(".png")
        assert mime == "image/png"
        with Image.open(io.BytesIO(file_obj.getvalue())) as sent:
            assert sent.format == "PNG"
            assert sent.size == (MAX_IMAGE_SIDE_PX, 683)

    def test_model_and_secret_name_are_read_from_env_at_call_time(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """OPENAI_MODEL and OPENAI_API_KEY_SECRET_NAME set after import should take effect."""
        monkeypatch.setenv("OPENAI_MODEL", "custom-image-model")
        monkeypatch.setenv("OPENAI_API_KEY_SECRET_NAME", "custom/secret")

        with patch("handler._fetch_openai_api_key", return_value="test-key") as mock_fetch:
            with patch("handler.OpenAI") as mock_openai_class:
                mock_openai_class.return_value.images.edit.return_value = (
                    _make_openai_response()
                )
                generate_painted_images([_make_image_bytes(8, 8)], FAKE_PROMPT)

        mock_fetch.assert_called_once_with("custom/secret")
        assert mock_openai_class.return_value.images.edit.call_args.kwargs["model"] == (
            "custom-image-model"
        )

    def test_openai_client_is_built_with_explicit_timeout_and_retries(self) -> None:
        """The OpenAI client should bound each request and retry count, not use SDK defaults."""
        with patch("handler._fetch_openai_api_key", return_value="test-key"):
            with patch("handler.OpenAI") as mock_openai_class:
                mock_openai_class.return_value.images.edit.return_value = (
                    _make_openai_response()
                )

                generate_painted_images([_make_image_bytes(8, 8)], FAKE_PROMPT)

        assert mock_openai_class.call_args.kwargs == {
            "api_key": "test-key",
            "timeout": OPENAI_TIMEOUT_SECONDS,
            "max_retries": OPENAI_MAX_RETRIES,
        }

    def test_undecodable_reference_image_raises(self) -> None:
        """Bytes that are not an image should raise before calling OpenAI."""
        from PIL import UnidentifiedImageError

        with patch("handler._fetch_openai_api_key", return_value="test-key"):
            with patch("handler.OpenAI") as mock_openai_class:
                with pytest.raises(UnidentifiedImageError):
                    generate_painted_images([FAKE_IMAGE], FAKE_PROMPT)

        mock_openai_class.return_value.images.edit.assert_not_called()

    def test_openai_error_propagates(self) -> None:
        """OpenAIError from the API should propagate to the caller."""
        from openai import OpenAIError

        with patch("handler._fetch_openai_api_key", return_value="test-key"):
            with patch("handler.OpenAI") as mock_openai_class:
                mock_client = MagicMock()
                mock_openai_class.return_value = mock_client
                mock_client.images.edit.side_effect = OpenAIError("API error")

                with pytest.raises(OpenAIError):
                    generate_painted_images([_make_image_bytes(8, 8)], FAKE_PROMPT)

    def test_secrets_manager_error_propagates(self) -> None:
        """ClientError from Secrets Manager should propagate to the caller."""
        with patch("handler._fetch_openai_api_key") as mock_fetch:
            mock_fetch.side_effect = ClientError(
                {"Error": {"Code": "AccessDenied", "Message": "Access denied"}},
                "GetSecretValue",
            )

            with pytest.raises(ClientError):
                generate_painted_images([_make_image_bytes(8, 8)], FAKE_PROMPT)


class TestUploadImageViaPresignedUrl:
    """Tests for `_upload_image_via_presigned_url`."""

    def test_put_request_is_bounded_by_upload_timeout(self) -> None:
        """The presigned PUT should pass UPLOAD_TIMEOUT_SECONDS to urlopen."""
        with patch("handler.urllib.request.urlopen") as mock_urlopen:
            _upload_image_via_presigned_url("https://example.com/put", FAKE_IMAGE)

        assert mock_urlopen.call_args.kwargs["timeout"] == UPLOAD_TIMEOUT_SECONDS

    def test_timeout_during_put_propagates(self) -> None:
        """A timeout from urlopen should propagate so the caller marks the job FAILED."""
        with patch("handler.urllib.request.urlopen", side_effect=TimeoutError("timed out")):
            with pytest.raises(TimeoutError):
                _upload_image_via_presigned_url("https://example.com/put", FAKE_IMAGE)


class TestUploadPaintedImages:
    """Tests for `upload_painted_images`."""

    @mock_aws
    def test_uploads_all_images_via_presigned_urls(self) -> None:
        """Should generate a presigned URL per image and upload each one."""
        s3 = boto3.client("s3")
        s3.create_bucket(Bucket=PAINT_BUCKET)
        images = [FAKE_IMAGE] * REQUESTED_OUTPUT_COUNT

        with patch.dict(os.environ, {"PAINT_BUCKET_NAME": PAINT_BUCKET}):
            with patch("handler._upload_image_via_presigned_url") as mock_upload:
                upload_painted_images(JOB_ID, images)

        assert mock_upload.call_count == REQUESTED_OUTPUT_COUNT

    @mock_aws
    def test_uploads_correct_image_data(self) -> None:
        """Each upload call should receive the correct image bytes."""
        s3 = boto3.client("s3")
        s3.create_bucket(Bucket=PAINT_BUCKET)
        images = [bytes([i]) for i in range(REQUESTED_OUTPUT_COUNT)]

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


class TestGetJob:
    """Tests for `get_job`."""

    @mock_aws
    def test_returns_status_and_trimmed_instruction(self) -> None:
        """Should return the job's status and its trimmed instruction from one read."""
        dynamodb = boto3.client("dynamodb")
        _create_dynamo_table(dynamodb)
        _put_job(dynamodb, JobStatus.IN_PROGRESS, instruction="  Paint the cloak red ")

        with patch.dict(os.environ, {"JOBS_TABLE_NAME": TABLE_NAME}):
            job = get_job(JOB_ID)

        assert job.status is JobStatus.IN_PROGRESS
        assert job.instruction == "Paint the cloak red"

    @mock_aws
    def test_returns_none_instruction_when_missing_or_blank(self) -> None:
        """A job without a non-blank instruction should yield instruction None."""
        dynamodb = boto3.client("dynamodb")
        _create_dynamo_table(dynamodb)
        _put_job(dynamodb, JobStatus.IN_PROGRESS)

        with patch.dict(os.environ, {"JOBS_TABLE_NAME": TABLE_NAME}):
            job = get_job(JOB_ID)

        assert job.instruction is None

    @mock_aws
    def test_returns_painted_status(self) -> None:
        """Should return PAINTED status if job is already painted."""
        dynamodb = boto3.client("dynamodb")
        _create_dynamo_table(dynamodb)
        _put_job(dynamodb, JobStatus.PAINTED)

        with patch.dict(os.environ, {"JOBS_TABLE_NAME": TABLE_NAME}):
            job = get_job(JOB_ID)

        assert job.status is JobStatus.PAINTED

    @mock_aws
    def test_raises_job_not_found_error_when_job_missing(self) -> None:
        """Should raise JobNotFoundError if job does not exist in DynamoDB."""
        dynamodb = boto3.client("dynamodb")
        _create_dynamo_table(dynamodb)

        with patch.dict(os.environ, {"JOBS_TABLE_NAME": TABLE_NAME}):
            with pytest.raises(JobNotFoundError, match="Job .* not found"):
                get_job(JOB_ID)

    @mock_aws
    def test_unknown_status_value_raises_value_error(self) -> None:
        """A stored jobStatus outside JobStatus should not be silently accepted."""
        dynamodb = boto3.client("dynamodb")
        _create_dynamo_table(dynamodb)
        dynamodb.put_item(
            TableName=TABLE_NAME,
            Item={"jobId": {"S": JOB_ID}, "jobStatus": {"S": "BOGUS"}},
        )

        with patch.dict(os.environ, {"JOBS_TABLE_NAME": TABLE_NAME}):
            with pytest.raises(ValueError):
                get_job(JOB_ID)

    def test_performs_single_strongly_consistent_read(self) -> None:
        """Status and instruction must come from one ConsistentRead GetItem call."""
        with patch("handler.boto3.client") as mock_boto_client:
            mock_dynamodb = mock_boto_client.return_value
            mock_dynamodb.get_item.return_value = {
                "Item": {
                    "jobStatus": {"S": "IN_PROGRESS"},
                    "instruction": {"S": "Paint it blue"},
                }
            }
            with patch.dict(os.environ, {"JOBS_TABLE_NAME": TABLE_NAME}):
                job = get_job(JOB_ID)

        mock_dynamodb.get_item.assert_called_once_with(
            TableName=TABLE_NAME,
            Key={"jobId": {"S": JOB_ID}},
            ConsistentRead=True,
        )
        assert job == JobRecord(status=JobStatus.IN_PROGRESS, instruction="Paint it blue")

    def test_missing_table_env_var_raises_key_error(self) -> None:
        """Missing JOBS_TABLE_NAME should raise KeyError."""
        with patch.dict(os.environ, {}, clear=True):
            with pytest.raises(KeyError):
                get_job(JOB_ID)


class TestBuildPrompt:
    """Tests for `build_prompt`."""

    def test_build_prompt_prepends_instruction(self) -> None:
        assert build_prompt("base", "do X") == "Instruction: do X + Prompt: base"

    def test_build_prompt_returns_prompt_unchanged_without_instruction(self) -> None:
        assert build_prompt("base", None) == "base"


class TestUpdateJobStatus:
    """Tests for `update_job_status`."""

    @mock_aws
    def test_updates_status_to_painted(self) -> None:
        """Should update job status from IN_PROGRESS to PAINTED."""
        dynamodb = boto3.client("dynamodb")
        _create_dynamo_table(dynamodb)
        _put_job(dynamodb, JobStatus.IN_PROGRESS)

        with patch.dict(os.environ, {"JOBS_TABLE_NAME": TABLE_NAME}):
            update_job_status(JOB_ID, JobStatus.PAINTED)

        item = dynamodb.get_item(TableName=TABLE_NAME, Key={"jobId": {"S": JOB_ID}})["Item"]
        assert item["jobStatus"]["S"] == JobStatus.PAINTED

    @mock_aws
    def test_updates_status_to_failed(self) -> None:
        """Should update job status from IN_PROGRESS to FAILED."""
        dynamodb = boto3.client("dynamodb")
        _create_dynamo_table(dynamodb)
        _put_job(dynamodb, JobStatus.IN_PROGRESS)

        with patch.dict(os.environ, {"JOBS_TABLE_NAME": TABLE_NAME}):
            update_job_status(JOB_ID, JobStatus.FAILED)

        item = dynamodb.get_item(TableName=TABLE_NAME, Key={"jobId": {"S": JOB_ID}})["Item"]
        assert item["jobStatus"]["S"] == JobStatus.FAILED

    @mock_aws
    def test_raises_when_job_not_in_progress(self) -> None:
        """Should raise ConditionalCheckFailedException if job is not IN_PROGRESS."""
        dynamodb = boto3.client("dynamodb")
        _create_dynamo_table(dynamodb)
        _put_job(dynamodb, JobStatus.PAINTED)

        with patch.dict(os.environ, {"JOBS_TABLE_NAME": TABLE_NAME}):
            with pytest.raises(ClientError) as exc_info:
                update_job_status(JOB_ID, JobStatus.PAINTED)

        assert exc_info.value.response["Error"]["Code"] == "ConditionalCheckFailedException"

    def test_missing_table_env_var_raises_key_error(self) -> None:
        """Missing JOBS_TABLE_NAME should raise KeyError."""
        with patch.dict(os.environ, {}, clear=True):
            with pytest.raises(KeyError):
                update_job_status(JOB_ID, JobStatus.PAINTED)


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
        "OPENAI_API_KEY_SECRET_NAME": "miniature-guide/openai/api-key",
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
        _upload_reference_images(s3, _make_image_bytes(8, 8))
        s3.put_object(
            Bucket=STATIC_BUCKET,
            Key="prompts/paint_images_prompt.txt",
            Body=FAKE_PROMPT.encode(),
        )

        dynamodb = boto3.client("dynamodb")
        _create_dynamo_table(dynamodb)
        _put_job(dynamodb, JobStatus.IN_PROGRESS)

        sqs = boto3.client("sqs")
        queue_url = sqs.create_queue(QueueName="guide-queue")["QueueUrl"]
        env_vars["GUIDE_CREATION_QUEUE_URL"] = queue_url

        mock_response = _make_openai_response(REQUESTED_OUTPUT_COUNT)

        with patch.dict(os.environ, env_vars):
            with patch("handler._fetch_openai_api_key", return_value="test-api-key"):
                with patch("handler.OpenAI") as mock_openai_class:
                    mock_client = MagicMock()
                    mock_openai_class.return_value = mock_client
                    mock_client.images.edit.return_value = mock_response
                    with patch("handler._upload_image_via_presigned_url"):
                        lambda_handler(_make_sqs_event(JOB_ID), None)

        item = dynamodb.get_item(TableName=TABLE_NAME, Key={"jobId": {"S": JOB_ID}})["Item"]
        assert item["jobStatus"]["S"] == JobStatus.PAINTED

        messages = sqs.receive_message(QueueUrl=queue_url)["Messages"]
        assert len(messages) == 1
        assert json.loads(messages[0]["Body"])["jobId"] == JOB_ID

    @mock_aws
    def test_instruction_is_prepended_to_prompt_sent_to_openai(self, env_vars: dict) -> None:
        """The OpenAI prompt should be 'Instruction: ... + Prompt: ...' when a job has one."""
        s3 = boto3.client("s3")
        for bucket in (UPLOAD_BUCKET, PAINT_BUCKET, STATIC_BUCKET):
            s3.create_bucket(Bucket=bucket)
        _upload_reference_images(s3, _make_image_bytes(8, 8))
        s3.put_object(
            Bucket=STATIC_BUCKET,
            Key="prompts/paint_images_prompt.txt",
            Body=FAKE_PROMPT.encode(),
        )
        dynamodb = boto3.client("dynamodb")
        _create_dynamo_table(dynamodb)
        _put_job(dynamodb, JobStatus.IN_PROGRESS, instruction="Paint the cloak red")
        sqs = boto3.client("sqs")
        env_vars["GUIDE_CREATION_QUEUE_URL"] = sqs.create_queue(QueueName="guide-queue")["QueueUrl"]

        with patch.dict(os.environ, env_vars):
            with patch("handler._fetch_openai_api_key", return_value="test-api-key"):
                with patch("handler.OpenAI") as mock_openai_class:
                    mock_client = MagicMock()
                    mock_openai_class.return_value = mock_client
                    mock_client.images.edit.return_value = _make_openai_response()
                    with patch("handler._upload_image_via_presigned_url"):
                        lambda_handler(_make_sqs_event(JOB_ID), None)

        prompt = mock_client.images.edit.call_args.kwargs["prompt"]
        assert prompt == f"Instruction: Paint the cloak red + Prompt: {FAKE_PROMPT}"

    def test_missing_job_id_raises_value_error(self, env_vars: dict) -> None:
        """Unparseable SQS message should raise ValueError (routes to DLQ)."""
        with patch.dict(os.environ, env_vars):
            with pytest.raises(ValueError, match="Missing or invalid jobId"):
                lambda_handler({"Records": []}, None)

    @mock_aws
    def test_job_not_in_progress_raises_value_error(self, env_vars: dict) -> None:
        """Job with non-IN_PROGRESS status should raise ValueError."""
        dynamodb = boto3.client("dynamodb")
        _create_dynamo_table(dynamodb)
        _put_job(dynamodb, JobStatus.PAINTED)

        with patch.dict(os.environ, env_vars):
            with pytest.raises(ValueError, match="not in IN_PROGRESS status"):
                lambda_handler(_make_sqs_event(JOB_ID), None)

    @mock_aws
    def test_job_not_found_raises_value_error(self, env_vars: dict) -> None:
        """Non-existent job should raise ValueError."""
        dynamodb = boto3.client("dynamodb")
        _create_dynamo_table(dynamodb)

        with patch.dict(os.environ, env_vars):
            with pytest.raises(ValueError, match="not found") as exc_info:
                lambda_handler(_make_sqs_event(JOB_ID), None)

        assert isinstance(exc_info.value.__cause__, JobNotFoundError)

    def test_missing_jobs_table_env_var_propagates_key_error(self, env_vars: dict) -> None:
        """A missing JOBS_TABLE_NAME is a configuration error and must not be reported as job-not-found."""
        env_without_table = {k: v for k, v in env_vars.items() if k != "JOBS_TABLE_NAME"}

        with patch.dict(os.environ, env_without_table, clear=True):
            with pytest.raises(KeyError, match="JOBS_TABLE_NAME"):
                lambda_handler(_make_sqs_event(JOB_ID), None)

    @mock_aws
    def test_download_failure_updates_status_to_failed(self, env_vars: dict) -> None:
        """S3 download failure should set job status to FAILED."""
        dynamodb = boto3.client("dynamodb")
        _create_dynamo_table(dynamodb)
        _put_job(dynamodb, JobStatus.IN_PROGRESS)

        with patch.dict(os.environ, env_vars):
            with patch("handler.download_reference_image") as mock_dl:
                mock_dl.side_effect = ClientError(
                    {"Error": {"Code": "NoSuchBucket", "Message": "Not found"}},
                    "ListObjectsV2",
                )
                lambda_handler(_make_sqs_event(JOB_ID), None)

        item = dynamodb.get_item(TableName=TABLE_NAME, Key={"jobId": {"S": JOB_ID}})["Item"]
        assert item["jobStatus"]["S"] == JobStatus.FAILED

    @mock_aws
    def test_prompt_fetch_failure_updates_status_to_failed(self, env_vars: dict) -> None:
        """S3 prompt fetch failure should set job status to FAILED."""
        dynamodb = boto3.client("dynamodb")
        _create_dynamo_table(dynamodb)
        _put_job(dynamodb, JobStatus.IN_PROGRESS)

        with patch.dict(os.environ, env_vars):
            with patch("handler.download_reference_image", return_value=FAKE_IMAGE):
                with patch("handler.fetch_prompt_from_s3") as mock_fetch:
                    mock_fetch.side_effect = ClientError(
                        {"Error": {"Code": "NoSuchKey", "Message": "Not found"}},
                        "GetObject",
                    )
                    lambda_handler(_make_sqs_event(JOB_ID), None)

        item = dynamodb.get_item(TableName=TABLE_NAME, Key={"jobId": {"S": JOB_ID}})["Item"]
        assert item["jobStatus"]["S"] == JobStatus.FAILED

    @mock_aws
    def test_openai_failure_updates_status_to_failed(self, env_vars: dict) -> None:
        """OpenAI API failure should set job status to FAILED."""
        from openai import OpenAIError

        dynamodb = boto3.client("dynamodb")
        _create_dynamo_table(dynamodb)
        _put_job(dynamodb, JobStatus.IN_PROGRESS)

        with patch.dict(os.environ, env_vars):
            with patch("handler.download_reference_image", return_value=FAKE_IMAGE):
                with patch("handler.fetch_prompt_from_s3", return_value=FAKE_PROMPT):
                    with patch("handler.generate_painted_images") as mock_gen:
                        mock_gen.side_effect = OpenAIError("Rate limit")
                        lambda_handler(_make_sqs_event(JOB_ID), None)

        item = dynamodb.get_item(TableName=TABLE_NAME, Key={"jobId": {"S": JOB_ID}})["Item"]
        assert item["jobStatus"]["S"] == JobStatus.FAILED

    @mock_aws
    def test_no_generated_images_updates_status_to_failed(self, env_vars: dict) -> None:
        """OpenAI returning no images should set job status to FAILED."""
        dynamodb = boto3.client("dynamodb")
        _create_dynamo_table(dynamodb)
        _put_job(dynamodb, JobStatus.IN_PROGRESS)

        with patch.dict(os.environ, env_vars):
            with patch("handler.download_reference_image", return_value=FAKE_IMAGE):
                with patch("handler.fetch_prompt_from_s3", return_value=FAKE_PROMPT):
                    with patch("handler.generate_painted_images", return_value=[]):
                        lambda_handler(_make_sqs_event(JOB_ID), None)

        item = dynamodb.get_item(TableName=TABLE_NAME, Key={"jobId": {"S": JOB_ID}})["Item"]
        assert item["jobStatus"]["S"] == JobStatus.FAILED

    @mock_aws
    def test_extra_generated_images_updates_status_to_failed_without_upload(
        self, env_vars: dict
    ) -> None:
        """OpenAI returning more images than requested is a contract violation and fails the job."""
        dynamodb = boto3.client("dynamodb")
        _create_dynamo_table(dynamodb)
        _put_job(dynamodb, JobStatus.IN_PROGRESS)

        with patch.dict(os.environ, env_vars):
            with patch("handler.download_reference_image", return_value=FAKE_IMAGE):
                with patch("handler.fetch_prompt_from_s3", return_value=FAKE_PROMPT):
                    with patch(
                        "handler.generate_painted_images",
                        return_value=[FAKE_IMAGE] * (REQUESTED_OUTPUT_COUNT + 1),
                    ):
                        with patch("handler.upload_painted_images") as mock_upload:
                            lambda_handler(_make_sqs_event(JOB_ID), None)

        mock_upload.assert_not_called()
        item = dynamodb.get_item(TableName=TABLE_NAME, Key={"jobId": {"S": JOB_ID}})["Item"]
        assert item["jobStatus"]["S"] == JobStatus.FAILED

    @mock_aws
    def test_upload_failure_updates_status_to_failed(self, env_vars: dict) -> None:
        """S3 upload failure should set job status to FAILED."""
        import urllib.error

        dynamodb = boto3.client("dynamodb")
        _create_dynamo_table(dynamodb)
        _put_job(dynamodb, JobStatus.IN_PROGRESS)

        with patch.dict(os.environ, env_vars):
            with patch("handler.download_reference_image", return_value=FAKE_IMAGE):
                with patch("handler.fetch_prompt_from_s3", return_value=FAKE_PROMPT):
                    with patch("handler.generate_painted_images", return_value=[FAKE_IMAGE]):
                        with patch("handler.upload_painted_images") as mock_upload:
                            mock_upload.side_effect = urllib.error.HTTPError(
                                None, 403, "Forbidden", {}, None
                            )
                            lambda_handler(_make_sqs_event(JOB_ID), None)

        item = dynamodb.get_item(TableName=TABLE_NAME, Key={"jobId": {"S": JOB_ID}})["Item"]
        assert item["jobStatus"]["S"] == JobStatus.FAILED

    @staticmethod
    def _error_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
        """Return the ERROR-level log records captured during the test, in emission order."""
        return [record for record in caplog.records if record.levelno == logging.ERROR]

    @mock_aws
    def test_dynamodb_update_painted_failure_is_logged(
        self, env_vars: dict, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A failed PAINTED write is logged at update_status_painted_error, skips notify, and does not raise."""
        dynamodb = boto3.client("dynamodb")
        _create_dynamo_table(dynamodb)
        _put_job(dynamodb, JobStatus.IN_PROGRESS)

        def concurrent_writer_marks_failed(*_args: Any, **_kwargs: Any) -> None:
            # Another writer changes the status after upload, so the real conditional
            # update to PAINTED in update_job_status is rejected by DynamoDB.
            dynamodb.update_item(
                TableName=TABLE_NAME,
                Key={"jobId": {"S": JOB_ID}},
                UpdateExpression="SET jobStatus = :status",
                ExpressionAttributeValues={":status": {"S": "FAILED"}},
            )

        with patch.dict(os.environ, env_vars):
            with patch("handler.download_reference_image", return_value=FAKE_IMAGE):
                with patch("handler.fetch_prompt_from_s3", return_value=FAKE_PROMPT):
                    with patch("handler.generate_painted_images", return_value=[FAKE_IMAGE]):
                        with patch(
                            "handler.upload_painted_images",
                            side_effect=concurrent_writer_marks_failed,
                        ):
                            with patch("handler.notify_guide_creation") as mock_notify:
                                lambda_handler(_make_sqs_event(JOB_ID), None)

        item = dynamodb.get_item(TableName=TABLE_NAME, Key={"jobId": {"S": JOB_ID}})["Item"]
        assert item["jobStatus"]["S"] == JobStatus.FAILED
        mock_notify.assert_not_called()

        [record] = self._error_records(caplog)
        assert record.getMessage().startswith("Failed to update job status to PAINTED")
        assert record.stage == "update_status_painted_error"
        assert record.jobId == JOB_ID
        assert record.exc_info is not None

    @mock_aws
    def test_notify_failure_is_logged_without_raising(
        self, env_vars: dict, caplog: pytest.LogCaptureFixture
    ) -> None:
        """An SQS failure after PAINTED is logged at notify_error, leaves the job PAINTED, and does not raise."""
        dynamodb = boto3.client("dynamodb")
        _create_dynamo_table(dynamodb)
        _put_job(dynamodb, JobStatus.IN_PROGRESS)

        with patch.dict(os.environ, env_vars):
            with patch("handler.download_reference_image", return_value=FAKE_IMAGE):
                with patch("handler.fetch_prompt_from_s3", return_value=FAKE_PROMPT):
                    with patch("handler.generate_painted_images", return_value=[FAKE_IMAGE]):
                        with patch("handler.upload_painted_images"):
                            with patch("handler.notify_guide_creation") as mock_notify:
                                mock_notify.side_effect = ClientError(
                                    {"Error": {"Code": "QueueDoesNotExist", "Message": "No queue"}},
                                    "SendMessage",
                                )
                                lambda_handler(_make_sqs_event(JOB_ID), None)

        item = dynamodb.get_item(TableName=TABLE_NAME, Key={"jobId": {"S": JOB_ID}})["Item"]
        assert item["jobStatus"]["S"] == JobStatus.PAINTED

        [record] = self._error_records(caplog)
        assert record.getMessage().startswith("Failed to send guide creation notification")
        assert record.stage == "notify_error"
        assert record.jobId == JOB_ID
        assert record.exc_info is not None

    @mock_aws
    def test_failed_status_update_failure_is_logged(
        self, env_vars: dict, caplog: pytest.LogCaptureFixture
    ) -> None:
        """If the FAILED write itself fails, it is logged at update_status_failed_error and the job stays IN_PROGRESS."""
        dynamodb = boto3.client("dynamodb")
        _create_dynamo_table(dynamodb)
        _put_job(dynamodb, JobStatus.IN_PROGRESS)

        with patch.dict(os.environ, env_vars):
            with patch("handler.download_reference_image") as mock_dl:
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

        mock_update.assert_called_once_with(JOB_ID, JobStatus.FAILED)
        item = dynamodb.get_item(TableName=TABLE_NAME, Key={"jobId": {"S": JOB_ID}})["Item"]
        assert item["jobStatus"]["S"] == JobStatus.IN_PROGRESS

        paint_error, update_error = self._error_records(caplog)
        assert paint_error.getMessage().startswith("Failed to paint images")
        assert paint_error.stage == "paint_error"
        assert update_error.getMessage().startswith("Failed to update job status to FAILED")
        assert update_error.stage == "update_status_failed_error"
        assert update_error.jobId == JOB_ID
        assert update_error.exc_info is not None
