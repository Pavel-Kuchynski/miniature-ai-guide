"""Tests for the bedrock Lambda handler.

Covers SQS parsing, the cached boto3 client factories, S3 image/prompt download, Bedrock
invocation, result validation and upload, DynamoDB status handling, SQS notification, the
processing and status-marking helpers, and the orchestration flow including failure paths
and the structured logs emitted for each failure.
"""

import json
import logging
from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock, call, patch

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from handler import (
    BEDROCK_MAX_ATTEMPTS,
    DEFAULT_BEDROCK_MAX_TOKENS,
    DEFAULT_BEDROCK_MODEL_ID,
    DEFAULT_BEDROCK_READ_TIMEOUT_SECONDS,
    PROMPT_KEY,
    _get_bedrock_client,
    _get_client,
    _mark_completed,
    _mark_failed,
    _process_painted_image,
    download_painted_image_from_s3,
    fetch_prompt_from_s3,
    get_job_status,
    invoke_bedrock,
    lambda_handler,
    notify_result,
    parse_job_id,
    parse_model_output,
    update_job_status,
    upload_result_to_s3,
)
from job_status import JobStatus
from logging_config import JSONFormatter

PAINT_BUCKET = "test-paint-bucket"
STATIC_BUCKET = "test-static-bucket"
TABLE_NAME = "test-jobs-table"
MODEL_ID = "test-model-id"
JOB_ID = "123e4567-e89b-12d3-a456-426614174000"
FAKE_IMAGE = b"fake-png-bytes"
FAKE_PROMPT = "Describe the RGB colors."
VALID_RESULT = {"colors": [{"detail": "armor", "paint": "A9A9A9"}]}


@pytest.fixture(autouse=True)
def _clear_client_caches() -> Iterator[None]:
    """Reset the cached boto3 clients before and after every test.

    Without this, a client built under one test's `mock_aws` or `patch` would be reused by
    the next test.
    """
    _get_client.cache_clear()
    _get_bedrock_client.cache_clear()
    yield
    _get_client.cache_clear()
    _get_bedrock_client.cache_clear()


@pytest.fixture
def env_vars(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Set all required environment variables and return them.

    Args:
        monkeypatch: Pytest fixture used to set the variables for the test only.

    Returns:
        Mapping of the variable names to the values that were set.
    """
    values = {
        "PAINT_BUCKET_NAME": PAINT_BUCKET,
        "STATIC_BUCKET_NAME": STATIC_BUCKET,
        "JOBS_TABLE_NAME": TABLE_NAME,
        "BEDROCK_MODEL_ID": MODEL_ID,
        "RESULT_QUEUE_URL": "https://sqs.us-east-1.amazonaws.com/123456789/result",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    return values


def _sqs_event(job_id: str) -> dict:
    """Build a minimal SQS Lambda event carrying the given job id."""
    return {"Records": [{"body": json.dumps({"jobId": job_id})}]}


def _client_error(code: str = "Boom") -> ClientError:
    """Build a botocore ClientError with the given error code."""
    return ClientError({"Error": {"Code": code, "Message": "x"}}, "Op")


def _create_table(client: Any, status: str = "PAINTED") -> None:
    """Create the jobs table and insert one job item with the given status."""
    client.create_table(
        TableName=TABLE_NAME,
        KeySchema=[{"AttributeName": "jobId", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "jobId", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    client.put_item(
        TableName=TABLE_NAME,
        Item={"jobId": {"S": JOB_ID}, "jobStatus": {"S": status}},
    )


def _records_for_stage(caplog: pytest.LogCaptureFixture, stage: str) -> list[logging.LogRecord]:
    """Return the captured log records whose `stage` extra equals the given stage."""
    return [record for record in caplog.records if getattr(record, "stage", None) == stage]


class TestClientFactories:
    """Tests for the cached boto3 client factories `_get_client` and `_get_bedrock_client`."""

    def test_get_client_reuses_client_for_same_service(self) -> None:
        """Repeated calls for one service should build the boto3 client only once."""
        with patch("handler.boto3.client") as mock_boto_client:
            first = _get_client("s3")
            second = _get_client("s3")

        mock_boto_client.assert_called_once_with("s3")
        assert first is second

    def test_get_client_builds_one_client_per_service(self) -> None:
        """Different service names should each get their own boto3 client."""
        with patch("handler.boto3.client") as mock_boto_client:
            _get_client("s3")
            _get_client("sqs")

        assert mock_boto_client.call_args_list == [call("s3"), call("sqs")]

    def test_cache_clear_forces_a_new_client(self) -> None:
        """After `cache_clear()` the next call should build a fresh client."""
        with patch("handler.boto3.client") as mock_boto_client:
            _get_client("dynamodb")
            _get_client.cache_clear()
            _get_client("dynamodb")

        assert mock_boto_client.call_count == 2

    def test_bedrock_client_reused_for_same_read_timeout(self) -> None:
        """Repeated calls with the same read timeout should reuse one Bedrock client."""
        with patch("handler.boto3.client") as mock_boto_client:
            first = _get_bedrock_client(DEFAULT_BEDROCK_READ_TIMEOUT_SECONDS)
            second = _get_bedrock_client(DEFAULT_BEDROCK_READ_TIMEOUT_SECONDS)

        mock_boto_client.assert_called_once()
        assert mock_boto_client.call_args.args == ("bedrock-runtime",)
        assert first is second

    def test_bedrock_client_is_built_with_read_timeout_and_pinned_retries(self) -> None:
        """The Bedrock client config should carry the read timeout and standard-mode retries."""
        with patch("handler.boto3.client") as mock_boto_client:
            _get_bedrock_client(42)

        config = mock_boto_client.call_args.kwargs["config"]
        assert config.read_timeout == 42
        assert config.retries == {
            "mode": "standard",
            "total_max_attempts": BEDROCK_MAX_ATTEMPTS,
        }

    def test_bedrock_client_is_rebuilt_when_read_timeout_changes(self) -> None:
        """A different read timeout should produce a client with that timeout, not a stale one."""
        with patch("handler.boto3.client") as mock_boto_client:
            _get_bedrock_client(300)
            _get_bedrock_client(42)

        assert mock_boto_client.call_count == 2
        assert mock_boto_client.call_args_list[1].kwargs["config"].read_timeout == 42


class TestParseJobId:
    """Tests for `parse_job_id`."""

    def test_valid_event_returns_job_id(self) -> None:
        """A well-formed SQS event should yield its jobId."""
        assert parse_job_id(_sqs_event(JOB_ID)) == JOB_ID

    def test_whitespace_is_trimmed(self) -> None:
        """Surrounding whitespace in the jobId should be removed."""
        assert parse_job_id(_sqs_event(f"  {JOB_ID} ")) == JOB_ID

    @pytest.mark.parametrize(
        "event",
        [
            {},
            {"Records": None},
            {"Records": []},
            {"Records": [{"body": "not json"}]},
            {"Records": [{"body": None}]},
            {"Records": [{"body": "[1]"}]},
            {"Records": [{"body": "{}"}]},
            {"Records": [{"body": json.dumps({"jobId": ""})}]},
            {"Records": [{"body": json.dumps({"jobId": "   "})}]},
            {"Records": [{"body": json.dumps({"jobId": 5})}]},
            {"Records": [1]},
            {"Records": "x"},
            {"Records": [None]},
            {"Records": 5},
        ],
    )
    def test_invalid_event_returns_none(self, event: dict) -> None:
        """Malformed events and missing or blank jobIds should return None."""
        assert parse_job_id(event) is None


class TestDownloadPaintedImage:
    """Tests for `download_painted_image_from_s3`."""

    def _put(self, s3: Any, key: str, body: bytes) -> None:
        """Store one object in the paint bucket."""
        s3.put_object(Bucket=PAINT_BUCKET, Key=key, Body=body)

    @mock_aws
    def test_downloads_single_png_ignoring_result_json(self, env_vars: dict) -> None:
        """Only the PNG should be downloaded; `result.json` and folder placeholders are ignored."""
        s3 = boto3.client("s3")
        s3.create_bucket(Bucket=PAINT_BUCKET)
        self._put(s3, f"painted_images/{JOB_ID}/image_0.png", FAKE_IMAGE)
        self._put(s3, f"painted_images/{JOB_ID}/result.json", b"{}")
        self._put(s3, f"painted_images/{JOB_ID}/", b"")

        assert download_painted_image_from_s3(JOB_ID) == FAKE_IMAGE

    @mock_aws
    def test_ignores_other_jobs(self, env_vars: dict) -> None:
        """PNGs stored under another job's prefix should not be picked up."""
        s3 = boto3.client("s3")
        s3.create_bucket(Bucket=PAINT_BUCKET)
        self._put(s3, f"painted_images/{JOB_ID}/image_0.png", FAKE_IMAGE)
        self._put(s3, "painted_images/other/image_0.png", b"other")

        assert download_painted_image_from_s3(JOB_ID) == FAKE_IMAGE

    @mock_aws
    def test_no_png_raises_value_error(self, env_vars: dict) -> None:
        """A job with no painted PNG should raise ValueError."""
        boto3.client("s3").create_bucket(Bucket=PAINT_BUCKET)
        with pytest.raises(ValueError):
            download_painted_image_from_s3(JOB_ID)

    @mock_aws
    def test_multiple_pngs_raise_value_error(self, env_vars: dict) -> None:
        """More than one painted PNG for a job is ambiguous and should raise ValueError."""
        s3 = boto3.client("s3")
        s3.create_bucket(Bucket=PAINT_BUCKET)
        self._put(s3, f"painted_images/{JOB_ID}/image_0.png", FAKE_IMAGE)
        self._put(s3, f"painted_images/{JOB_ID}/image_1.png", FAKE_IMAGE)
        with pytest.raises(ValueError):
            download_painted_image_from_s3(JOB_ID)

    def test_missing_bucket_env_raises_key_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An unset PAINT_BUCKET_NAME should raise KeyError."""
        monkeypatch.delenv("PAINT_BUCKET_NAME", raising=False)
        with pytest.raises(KeyError):
            download_painted_image_from_s3(JOB_ID)


class TestFetchPrompt:
    """Tests for `fetch_prompt_from_s3`."""

    @mock_aws
    def test_reads_prompt_text(self, env_vars: dict) -> None:
        """The prompt object should be decoded from UTF-8 and returned as text."""
        s3 = boto3.client("s3")
        s3.create_bucket(Bucket=STATIC_BUCKET)
        s3.put_object(Bucket=STATIC_BUCKET, Key=PROMPT_KEY, Body=FAKE_PROMPT.encode())
        assert fetch_prompt_from_s3() == FAKE_PROMPT

    @mock_aws
    def test_missing_prompt_raises_client_error(self, env_vars: dict) -> None:
        """A missing prompt object should propagate the S3 ClientError."""
        boto3.client("s3").create_bucket(Bucket=STATIC_BUCKET)
        with pytest.raises(ClientError):
            fetch_prompt_from_s3()

    def test_missing_bucket_env_raises_key_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An unset STATIC_BUCKET_NAME should raise KeyError."""
        monkeypatch.delenv("STATIC_BUCKET_NAME", raising=False)
        with pytest.raises(KeyError):
            fetch_prompt_from_s3()


class TestInvokeBedrock:
    """Tests for `invoke_bedrock`."""

    def _run(self, content: list) -> tuple:
        """Invoke Bedrock with a mocked client returning the given content blocks."""
        client = MagicMock()
        client.converse.return_value = {"output": {"message": {"content": content}}}
        with patch("handler._get_bedrock_client", return_value=client):
            return client, invoke_bedrock(FAKE_IMAGE, FAKE_PROMPT)

    def test_returns_joined_text_blocks(self, env_vars: dict) -> None:
        """Text blocks of the reply should be concatenated in order."""
        _, text = self._run([{"text": '{"a":'}, {"text": " 1}"}])
        assert text == '{"a": 1}'

    def test_sends_prompt_and_png_image(self, env_vars: dict) -> None:
        """The request should carry the model id, the prompt text and the PNG bytes."""
        client, _ = self._run([{"text": "ok"}])
        kwargs = client.converse.call_args.kwargs
        assert kwargs["modelId"] == MODEL_ID
        content = kwargs["messages"][0]["content"]
        assert {"text": FAKE_PROMPT} in content
        assert {"image": {"format": "png", "source": {"bytes": FAKE_IMAGE}}} in content

    def test_empty_response_raises_value_error(self, env_vars: dict) -> None:
        """A reply with only whitespace text should raise ValueError."""
        with pytest.raises(ValueError):
            self._run([{"text": "  "}])

    def test_client_error_propagates(self, env_vars: dict) -> None:
        """A Bedrock ClientError should propagate unchanged."""
        client = MagicMock()
        client.converse.side_effect = _client_error("ThrottlingException")
        with patch("handler._get_bedrock_client", return_value=client):
            with pytest.raises(ClientError):
                invoke_bedrock(FAKE_IMAGE, FAKE_PROMPT)

    def test_uses_defaults_when_optional_env_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Unset optional settings should fall back to the documented defaults."""
        for name in ("BEDROCK_MODEL_ID", "BEDROCK_MAX_TOKENS", "BEDROCK_READ_TIMEOUT_SECONDS"):
            monkeypatch.delenv(name, raising=False)
        client = MagicMock()
        client.converse.return_value = {"output": {"message": {"content": [{"text": "ok"}]}}}
        with patch("handler._get_bedrock_client", return_value=client) as mock_builder:
            invoke_bedrock(FAKE_IMAGE, FAKE_PROMPT)

        kwargs = client.converse.call_args.kwargs
        assert kwargs["modelId"] == DEFAULT_BEDROCK_MODEL_ID
        assert kwargs["inferenceConfig"] == {"maxTokens": DEFAULT_BEDROCK_MAX_TOKENS}
        mock_builder.assert_called_once_with(DEFAULT_BEDROCK_READ_TIMEOUT_SECONDS)

    def test_env_overrides_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Set optional environment variables should override every default."""
        monkeypatch.setenv("BEDROCK_MODEL_ID", "custom-model")
        monkeypatch.setenv("BEDROCK_MAX_TOKENS", "128")
        monkeypatch.setenv("BEDROCK_READ_TIMEOUT_SECONDS", "42")
        client = MagicMock()
        client.converse.return_value = {"output": {"message": {"content": [{"text": "ok"}]}}}
        with patch("handler._get_bedrock_client", return_value=client) as mock_builder:
            invoke_bedrock(FAKE_IMAGE, FAKE_PROMPT)

        kwargs = client.converse.call_args.kwargs
        assert kwargs["modelId"] == "custom-model"
        assert kwargs["inferenceConfig"] == {"maxTokens": 128}
        mock_builder.assert_called_once_with(42)

    @pytest.mark.parametrize("name", ["BEDROCK_MAX_TOKENS", "BEDROCK_READ_TIMEOUT_SECONDS"])
    @pytest.mark.parametrize("value", ["abc", "0", "-5"])
    def test_invalid_numeric_env_raises_value_error(
        self, monkeypatch: pytest.MonkeyPatch, name: str, value: str
    ) -> None:
        """Non-integer or non-positive numeric settings should raise ValueError."""
        monkeypatch.setenv(name, value)
        with pytest.raises(ValueError):
            invoke_bedrock(FAKE_IMAGE, FAKE_PROMPT)

    def test_blank_numeric_env_uses_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A blank numeric setting should be treated as unset and use the default."""
        monkeypatch.setenv("BEDROCK_MAX_TOKENS", "  ")
        client = MagicMock()
        client.converse.return_value = {"output": {"message": {"content": [{"text": "ok"}]}}}
        with patch("handler._get_bedrock_client", return_value=client):
            invoke_bedrock(FAKE_IMAGE, FAKE_PROMPT)
        assert client.converse.call_args.kwargs["inferenceConfig"] == {
            "maxTokens": DEFAULT_BEDROCK_MAX_TOKENS
        }


class TestParseModelOutput:
    """Tests for `parse_model_output`."""

    def test_valid_json_is_returned(self) -> None:
        """A valid document should be returned unchanged."""
        assert parse_model_output(json.dumps(VALID_RESULT)) == VALID_RESULT

    def test_markdown_code_fence_is_tolerated(self) -> None:
        """A surrounding markdown json code fence should be stripped before parsing."""
        text = "```json\n" + json.dumps(VALID_RESULT) + "\n```"
        assert parse_model_output(text) == VALID_RESULT

    @pytest.mark.parametrize(
        "text",
        [
            "not json",
            "[]",
            "{}",
            '{"colors": []}',
            '{"colors": "red"}',
            '{"colors": ["armor"]}',
            '{"colors": [{"paint": "A9A9A9"}]}',
            '{"colors": [{"detail": " ", "paint": "A9A9A9"}]}',
            '{"colors": [{"detail": "armor"}]}',
            '{"colors": [{"detail": "armor", "paint": "#A9A9A9"}]}',
            '{"colors": [{"detail": "armor", "paint": "a9a9a9"}]}',
            '{"colors": [{"detail": "armor", "paint": "A9A9"}]}',
            '{"colors": [{"detail": "armor", "paint": 123456}]}',
        ],
    )
    def test_invalid_output_raises_value_error(self, text: str) -> None:
        """Text that is not the expected colors document should raise ValueError."""
        with pytest.raises(ValueError):
            parse_model_output(text)


class TestUploadResult:
    """Tests for `upload_result_to_s3`."""

    @mock_aws
    def test_stores_result_json(self, env_vars: dict) -> None:
        """The validated document should be stored as JSON under the job's result key."""
        s3 = boto3.client("s3")
        s3.create_bucket(Bucket=PAINT_BUCKET)

        key = upload_result_to_s3(JOB_ID, VALID_RESULT)

        assert key == f"painted_images/{JOB_ID}/result.json"
        obj = s3.get_object(Bucket=PAINT_BUCKET, Key=key)
        assert obj["ContentType"] == "application/json"
        assert json.loads(obj["Body"].read()) == VALID_RESULT

    def test_missing_bucket_env_raises_key_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An unset PAINT_BUCKET_NAME should raise KeyError."""
        monkeypatch.delenv("PAINT_BUCKET_NAME", raising=False)
        with pytest.raises(KeyError):
            upload_result_to_s3(JOB_ID, VALID_RESULT)


class TestDynamoDbJobStatus:
    """Tests for `get_job_status` and `update_job_status` against the jobs table."""

    @mock_aws
    def test_get_returns_status(self, env_vars: dict) -> None:
        """An existing job should return its raw status string."""
        _create_table(boto3.client("dynamodb"))
        assert get_job_status(JOB_ID) == "PAINTED"

    @mock_aws
    def test_get_returns_none_when_missing(self, env_vars: dict) -> None:
        """An unknown job id should return None rather than raising."""
        _create_table(boto3.client("dynamodb"))
        assert get_job_status("other") is None

    @mock_aws
    def test_update_moves_painted_to_completed(self, env_vars: dict) -> None:
        """A PAINTED job should move to COMPLETED and record an updatedAt timestamp."""
        client = boto3.client("dynamodb")
        _create_table(client)
        update_job_status(JOB_ID, JobStatus.COMPLETED)
        item = client.get_item(TableName=TABLE_NAME, Key={"jobId": {"S": JOB_ID}})["Item"]
        assert item["jobStatus"]["S"] == "COMPLETED"
        assert "updatedAt" in item

    @mock_aws
    def test_update_fails_when_not_painted(self, env_vars: dict) -> None:
        """The conditional write should be rejected when the job is not PAINTED."""
        _create_table(boto3.client("dynamodb"), status="COMPLETED")
        with pytest.raises(ClientError) as exc_info:
            update_job_status(JOB_ID, JobStatus.FAILED)
        assert exc_info.value.response["Error"]["Code"] == "ConditionalCheckFailedException"

    @mock_aws
    def test_update_does_not_create_missing_job(self, env_vars: dict) -> None:
        """Updating an unknown job should fail instead of creating an item."""
        client = boto3.client("dynamodb")
        _create_table(client)
        with pytest.raises(ClientError):
            update_job_status("other", JobStatus.COMPLETED)

    def test_missing_table_env_raises_key_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An unset JOBS_TABLE_NAME should raise KeyError for both reads and writes."""
        monkeypatch.delenv("JOBS_TABLE_NAME", raising=False)
        with pytest.raises(KeyError):
            get_job_status(JOB_ID)
        with pytest.raises(KeyError):
            update_job_status(JOB_ID, JobStatus.COMPLETED)


class TestNotifyResult:
    """Tests for `notify_result`."""

    @mock_aws
    def test_sends_completed_message(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The queue should receive a jobId and COMPLETED status message."""
        sqs = boto3.client("sqs")
        url = sqs.create_queue(QueueName="result")["QueueUrl"]
        monkeypatch.setenv("RESULT_QUEUE_URL", url)

        notify_result(JOB_ID)

        messages = sqs.receive_message(QueueUrl=url)["Messages"]
        assert json.loads(messages[0]["Body"]) == {"jobId": JOB_ID, "status": "COMPLETED"}

    def test_missing_queue_env_raises_key_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An unset RESULT_QUEUE_URL should raise KeyError."""
        monkeypatch.delenv("RESULT_QUEUE_URL", raising=False)
        with pytest.raises(KeyError):
            notify_result(JOB_ID)


@pytest.fixture
def steps() -> Iterator[dict]:
    """Patch every handler helper used by the orchestration with happy-path defaults.

    Yields:
        Mapping of helper name to its `MagicMock`.
    """
    names = [
        "get_job_status",
        "download_painted_image_from_s3",
        "fetch_prompt_from_s3",
        "invoke_bedrock",
        "upload_result_to_s3",
        "notify_result",
        "update_job_status",
    ]
    patchers = {name: patch(f"handler.{name}") for name in names}
    mocks = {name: p.start() for name, p in patchers.items()}
    mocks["get_job_status"].return_value = "PAINTED"
    mocks["download_painted_image_from_s3"].return_value = FAKE_IMAGE
    mocks["fetch_prompt_from_s3"].return_value = FAKE_PROMPT
    mocks["invoke_bedrock"].return_value = json.dumps(VALID_RESULT)
    yield mocks
    for p in patchers.values():
        p.stop()


class TestProcessPaintedImage:
    """Tests for `_process_painted_image`, the Bedrock step run by the orchestration."""

    def test_uploads_parsed_result_for_job(self, steps: dict) -> None:
        """The validated model document should be uploaded under the job id."""
        _process_painted_image(JOB_ID)

        steps["invoke_bedrock"].assert_called_once_with(FAKE_IMAGE, FAKE_PROMPT)
        steps["upload_result_to_s3"].assert_called_once_with(JOB_ID, VALID_RESULT)

    def test_invalid_reply_raises_before_upload(self, steps: dict) -> None:
        """An unparsable reply should raise ValueError and nothing should be uploaded."""
        steps["invoke_bedrock"].return_value = "garbage reply"

        with pytest.raises(ValueError):
            _process_painted_image(JOB_ID)
        steps["upload_result_to_s3"].assert_not_called()


class TestMarkStatusHelpers:
    """Tests for `_mark_failed` and `_mark_completed`, which log instead of raising."""

    def test_mark_failed_writes_failed_status(self) -> None:
        """`_mark_failed` should write the FAILED status for the job."""
        with patch("handler.update_job_status") as mock_update:
            _mark_failed(JOB_ID)
        mock_update.assert_called_once_with(JOB_ID, JobStatus.FAILED)

    def test_mark_failed_logs_client_error_with_traceback(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A DynamoDB failure should be logged at update_status_failed_error with exc_info."""
        with patch("handler.update_job_status", side_effect=_client_error("Throttling")):
            _mark_failed(JOB_ID)

        [record] = _records_for_stage(caplog, "update_status_failed_error")
        assert record.getMessage().startswith("Failed to update job status to FAILED")
        assert record.jobId == JOB_ID
        assert record.exc_info is not None

    def test_mark_failed_logs_missing_table_env(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A missing JOBS_TABLE_NAME should be logged rather than raised from `_mark_failed`."""
        monkeypatch.delenv("JOBS_TABLE_NAME", raising=False)

        _mark_failed(JOB_ID)

        assert len(_records_for_stage(caplog, "update_status_failed_error")) == 1

    def test_mark_completed_returns_true_on_success(self) -> None:
        """A successful COMPLETED write should return True."""
        with patch("handler.update_job_status") as mock_update:
            assert _mark_completed(JOB_ID) is True
        mock_update.assert_called_once_with(JOB_ID, JobStatus.COMPLETED)

    def test_mark_completed_returns_false_and_logs_on_client_error(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A failed COMPLETED write should return False and log at update_status_completed_error."""
        with patch("handler.update_job_status", side_effect=_client_error()):
            assert _mark_completed(JOB_ID) is False

        [record] = _records_for_stage(caplog, "update_status_completed_error")
        assert record.getMessage().startswith("Failed to update job status to COMPLETED")
        assert record.jobId == JOB_ID
        assert record.exc_info is not None


class TestLambdaHandler:
    """Tests for `lambda_handler` orchestration, failure paths and their logs."""

    def test_happy_path(self, steps: dict) -> None:
        """A PAINTED job should be processed, notified and marked COMPLETED."""
        lambda_handler(_sqs_event(JOB_ID), None)

        steps["invoke_bedrock"].assert_called_once_with(FAKE_IMAGE, FAKE_PROMPT)
        steps["upload_result_to_s3"].assert_called_once_with(JOB_ID, VALID_RESULT)
        steps["notify_result"].assert_called_once_with(JOB_ID)
        steps["update_job_status"].assert_called_once_with(JOB_ID, "COMPLETED")

    def test_notifies_result_before_marking_completed(self, steps: dict) -> None:
        """The result notification must be sent before the COMPLETED status write."""
        calls: list[str] = []
        steps["notify_result"].side_effect = lambda job_id: calls.append("notify")
        steps["update_job_status"].side_effect = lambda job_id, status: calls.append(str(status))

        lambda_handler(_sqs_event(JOB_ID), None)

        assert calls == ["notify", "COMPLETED"]

    def test_success_logs_complete_stage(
        self, steps: dict, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A successful run should end with a `complete` stage log carrying the job id."""
        lambda_handler(_sqs_event(JOB_ID), None)

        [record] = _records_for_stage(caplog, "complete")
        assert record.getMessage() == "Successfully completed bedrock job"
        assert record.jobId == JOB_ID

    def test_logs_bedrock_response(self, steps: dict) -> None:
        """The raw Bedrock reply should be logged at stage bedrock_response with the job id."""
        with patch("handler.logger.handle") as mock_handle:
            lambda_handler(_sqs_event(JOB_ID), None)
        logged = [c.args[0] for c in mock_handle.call_args_list]
        record = next(r for r in logged if r.__dict__.get("stage") == "bedrock_response")
        assert record.getMessage() == f"Bedrock response: {json.dumps(VALID_RESULT)}"
        assert record.__dict__["jobId"] == JOB_ID

    def test_logs_unparsable_bedrock_response_before_failing(self, steps: dict) -> None:
        """A malformed reply should be logged before the job is marked FAILED."""
        steps["invoke_bedrock"].return_value = "garbage reply"
        with patch("handler.logger.handle") as mock_handle:
            lambda_handler(_sqs_event(JOB_ID), None)
        messages = [c.args[0].getMessage() for c in mock_handle.call_args_list]
        assert "Bedrock response: garbage reply" in messages
        steps["update_job_status"].assert_called_once_with(JOB_ID, "FAILED")

    def test_invalid_job_id_raises_value_error(self, steps: dict) -> None:
        """A message without a usable jobId should raise ValueError before any lookup."""
        with pytest.raises(ValueError):
            lambda_handler({"Records": []}, None)
        steps["get_job_status"].assert_not_called()

    def test_invalid_job_id_logs_parse_input_stage_with_unknown_job(
        self, steps: dict, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A parse failure should be logged at parse_input with jobId `unknown`."""
        with pytest.raises(ValueError):
            lambda_handler({"Records": []}, None)

        [record] = _records_for_stage(caplog, "parse_input")
        assert record.jobId == "unknown"

    def test_unknown_job_raises_value_error(self, steps: dict) -> None:
        """A job missing from DynamoDB should raise ValueError and not start processing."""
        steps["get_job_status"].return_value = None
        with pytest.raises(ValueError):
            lambda_handler(_sqs_event(JOB_ID), None)
        steps["download_painted_image_from_s3"].assert_not_called()

    def test_unknown_job_logs_validate_job_stage_with_job_id(
        self, steps: dict, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Per-call stage names should reach the record, with the job id from the adapter."""
        steps["get_job_status"].return_value = None
        with pytest.raises(ValueError):
            lambda_handler(_sqs_event(JOB_ID), None)

        [record] = _records_for_stage(caplog, "validate_job")
        assert record.getMessage() == "Job not found"
        assert record.jobId == JOB_ID

    def test_job_not_painted_raises_value_error(self, steps: dict) -> None:
        """A job that is not PAINTED should raise ValueError and not call Bedrock."""
        steps["get_job_status"].return_value = "COMPLETED"
        with pytest.raises(ValueError):
            lambda_handler(_sqs_event(JOB_ID), None)
        steps["invoke_bedrock"].assert_not_called()

    @pytest.mark.parametrize(
        "failing_step,error",
        [
            ("download_painted_image_from_s3", ValueError("no image")),
            ("fetch_prompt_from_s3", _client_error("NoSuchKey")),
            ("invoke_bedrock", _client_error("ThrottlingException")),
            ("invoke_bedrock", "not json"),
            ("upload_result_to_s3", _client_error("AccessDenied")),
        ],
    )
    def test_processing_failure_marks_job_failed(
        self, steps: dict, failing_step: str, error: Exception
    ) -> None:
        """A failure in any processing step should mark the job FAILED without notifying."""
        if isinstance(error, Exception):
            steps[failing_step].side_effect = error
        else:
            steps[failing_step].return_value = error

        lambda_handler(_sqs_event(JOB_ID), None)

        steps["update_job_status"].assert_called_once_with(JOB_ID, "FAILED")
        steps["notify_result"].assert_not_called()

    def test_processing_failure_logs_traceback_at_bedrock_error(
        self, steps: dict, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A processing failure should be logged at bedrock_error with exc_info and the job id."""
        steps["invoke_bedrock"].side_effect = _client_error("ThrottlingException")

        lambda_handler(_sqs_event(JOB_ID), None)

        [record] = _records_for_stage(caplog, "bedrock_error")
        assert record.getMessage().startswith("Failed to process painted image")
        assert record.jobId == JOB_ID
        assert record.exc_info is not None

    def test_processing_failure_json_line_contains_exception_field(
        self, steps: dict, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The emitted JSON line for a processing failure should include the traceback."""
        steps["invoke_bedrock"].side_effect = _client_error("ThrottlingException")

        lambda_handler(_sqs_event(JOB_ID), None)

        [record] = _records_for_stage(caplog, "bedrock_error")
        payload = json.loads(JSONFormatter().format(record))
        assert payload["stage"] == "bedrock_error"
        assert "ThrottlingException" in payload["exception"]

    def test_failed_status_update_error_is_swallowed(self, steps: dict) -> None:
        """A failing FAILED write should be swallowed so the handler returns normally."""
        steps["invoke_bedrock"].side_effect = ValueError("bad")
        steps["update_job_status"].side_effect = _client_error("ConditionalCheckFailedException")

        lambda_handler(_sqs_event(JOB_ID), None)

    def test_failed_status_update_error_is_logged_with_traceback(
        self, steps: dict, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A failing FAILED write should be logged at update_status_failed_error with exc_info."""
        steps["invoke_bedrock"].side_effect = ValueError("bad")
        steps["update_job_status"].side_effect = _client_error("ConditionalCheckFailedException")

        lambda_handler(_sqs_event(JOB_ID), None)

        [record] = _records_for_stage(caplog, "update_status_failed_error")
        assert record.jobId == JOB_ID
        assert record.exc_info is not None

    def test_notify_failure_skips_status_update(self, steps: dict) -> None:
        """A failed result notification should leave the job unchanged and skip COMPLETED."""
        steps["notify_result"].side_effect = _client_error()

        lambda_handler(_sqs_event(JOB_ID), None)

        steps["update_job_status"].assert_not_called()

    def test_notify_failure_is_logged_with_traceback(
        self, steps: dict, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A failed notification should be logged at notify_error with exc_info."""
        steps["notify_result"].side_effect = _client_error()

        lambda_handler(_sqs_event(JOB_ID), None)

        [record] = _records_for_stage(caplog, "notify_error")
        assert record.getMessage().startswith("Failed to send result notification")
        assert record.jobId == JOB_ID
        assert record.exc_info is not None

    def test_completed_status_update_failure_does_not_raise(self, steps: dict) -> None:
        """A failing COMPLETED write should be swallowed after the notification was sent."""
        steps["update_job_status"].side_effect = _client_error()

        lambda_handler(_sqs_event(JOB_ID), None)

        steps["update_job_status"].assert_called_once_with(JOB_ID, "COMPLETED")

    def test_completed_status_update_failure_is_logged_without_success_log(
        self, steps: dict, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A failing COMPLETED write should be logged and must not emit the success log."""
        steps["update_job_status"].side_effect = _client_error()

        lambda_handler(_sqs_event(JOB_ID), None)

        [record] = _records_for_stage(caplog, "update_status_completed_error")
        assert record.exc_info is not None
        assert _records_for_stage(caplog, "complete") == []

    def test_missing_jobs_table_env_raises_key_error_without_writing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A missing JOBS_TABLE_NAME should propagate KeyError and never write a status."""
        monkeypatch.delenv("JOBS_TABLE_NAME", raising=False)
        with patch("handler.update_job_status") as mock_update:
            with pytest.raises(KeyError):
                lambda_handler(_sqs_event(JOB_ID), None)
        mock_update.assert_not_called()

    def test_status_read_client_error_propagates(self) -> None:
        """A DynamoDB ClientError from the initial status read should propagate to SQS."""
        with patch("handler.get_job_status", side_effect=_client_error("Throttling")):
            with pytest.raises(ClientError):
                lambda_handler(_sqs_event(JOB_ID), None)
