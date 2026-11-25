"""Tests for the bedrock Lambda handler.

Covers SQS parsing, the cached boto3 client factories, S3 image/prompt download, Bedrock
invocation, result validation and upload, the `result.json` existence check used for
redelivery, DynamoDB status handling, SQS notifications (COMPLETED and FAILED), the
processing and status-marking helpers, and the orchestration flow: failure reporting to the
result queue, redelivery idempotency, notification retries and the structured logs emitted
for each path.
"""

import json
import logging
from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock, call, patch

import boto3
import pytest
from botocore.exceptions import BotoCoreError, ClientError, EndpointConnectionError
from moto import mock_aws

from handler import (
    BEDROCK_MAX_ATTEMPTS,
    DEFAULT_BEDROCK_MAX_TOKENS,
    DEFAULT_BEDROCK_MODEL_ID,
    DEFAULT_BEDROCK_READ_TIMEOUT_SECONDS,
    PROMPT_KEY,
    _get_bedrock_client,
    _get_client,
    _mark_failed,
    _process_painted_image,
    _report_failure,
    download_painted_image_from_s3,
    fetch_prompt_from_s3,
    get_job_status,
    invoke_bedrock,
    lambda_handler,
    notify_result,
    notify_result_failure,
    parse_job_id,
    parse_model_output,
    result_exists,
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

    @pytest.mark.parametrize(
        "reply",
        [{}, {"output": {}}, {"output": {"message": {}}}],
    )
    def test_reply_without_content_raises_value_error_not_key_error(
        self, env_vars: dict, reply: dict
    ) -> None:
        """A malformed reply is a processing failure (ValueError), never a config KeyError."""
        client = MagicMock()
        client.converse.return_value = reply
        with patch("handler._get_bedrock_client", return_value=client):
            with pytest.raises(ValueError, match="no output message content"):
                invoke_bedrock(FAKE_IMAGE, FAKE_PROMPT)

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


class TestResultExists:
    """Tests for `result_exists`, the `result.json` check that makes redelivery idempotent."""

    @mock_aws
    def test_returns_true_when_result_json_is_stored(self, env_vars: dict) -> None:
        """A stored result.json means an earlier invocation already paid for Bedrock."""
        s3 = boto3.client("s3")
        s3.create_bucket(Bucket=PAINT_BUCKET)
        s3.put_object(Bucket=PAINT_BUCKET, Key=f"painted_images/{JOB_ID}/result.json", Body=b"{}")

        assert result_exists(JOB_ID) is True

    @mock_aws
    def test_returns_false_when_result_json_is_missing(self, env_vars: dict) -> None:
        """Only the painted PNG exists, so the Bedrock step still has to run."""
        s3 = boto3.client("s3")
        s3.create_bucket(Bucket=PAINT_BUCKET)
        s3.put_object(Bucket=PAINT_BUCKET, Key=f"painted_images/{JOB_ID}/image_0.png", Body=FAKE_IMAGE)

        assert result_exists(JOB_ID) is False

    @mock_aws
    def test_other_jobs_result_does_not_count(self, env_vars: dict) -> None:
        """A result.json under another job's prefix must not satisfy this job's check."""
        s3 = boto3.client("s3")
        s3.create_bucket(Bucket=PAINT_BUCKET)
        s3.put_object(Bucket=PAINT_BUCKET, Key="painted_images/other/result.json", Body=b"{}")

        assert result_exists(JOB_ID) is False

    def test_access_denied_propagates(self, env_vars: dict) -> None:
        """Any S3 error other than a missing key is transient and must not read as 'not stored'."""
        with patch("handler._get_client") as get_client:
            get_client.return_value.head_object.side_effect = ClientError(
                {"Error": {"Code": "503", "Message": "Slow down"}}, "HeadObject"
            )
            with pytest.raises(ClientError):
                result_exists(JOB_ID)

    def test_missing_bucket_env_raises_key_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An unset PAINT_BUCKET_NAME should raise KeyError."""
        monkeypatch.delenv("PAINT_BUCKET_NAME", raising=False)
        with pytest.raises(KeyError):
            result_exists(JOB_ID)


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


class TestNotifyResultFailure:
    """Tests for `notify_result_failure`."""

    @mock_aws
    def test_sends_failed_message_with_reason(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The queue should receive jobId, status FAILED and the client-safe reason."""
        sqs = boto3.client("sqs")
        url = sqs.create_queue(QueueName="result")["QueueUrl"]
        monkeypatch.setenv("RESULT_QUEUE_URL", url)

        notify_result_failure(JOB_ID, "Guide generation failed: ClientError")

        messages = sqs.receive_message(QueueUrl=url)["Messages"]
        assert json.loads(messages[0]["Body"]) == {
            "jobId": JOB_ID,
            "status": "FAILED",
            "message": "Guide generation failed: ClientError",
        }

    def test_missing_queue_env_raises_key_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An unset RESULT_QUEUE_URL should raise KeyError."""
        monkeypatch.delenv("RESULT_QUEUE_URL", raising=False)
        with pytest.raises(KeyError):
            notify_result_failure(JOB_ID, "reason")


@pytest.fixture
def steps() -> Iterator[dict]:
    """Patch every handler helper used by the orchestration with happy-path defaults.

    `result_exists` defaults to False, so the Bedrock path runs unless a test says otherwise.

    Yields:
        Mapping of helper name to its `MagicMock`.
    """
    names = [
        "get_job_status",
        "result_exists",
        "download_painted_image_from_s3",
        "fetch_prompt_from_s3",
        "invoke_bedrock",
        "upload_result_to_s3",
        "notify_result",
        "notify_result_failure",
        "update_job_status",
    ]
    patchers = {name: patch(f"handler.{name}") for name in names}
    mocks = {name: p.start() for name, p in patchers.items()}
    mocks["get_job_status"].return_value = "PAINTED"
    mocks["result_exists"].return_value = False
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


class TestReportFailure:
    """Tests for `_report_failure`, which tells the client a job failed and never raises."""

    def test_sends_failed_message_with_exception_class_name_only(self) -> None:
        """The client message carries only the exception class name, never its text."""
        with patch("handler.notify_result_failure") as mock_notify:
            _report_failure(JOB_ID, ValueError("secret detail"))

        mock_notify.assert_called_once_with(JOB_ID, "Guide generation failed: ValueError")

    @pytest.mark.parametrize(
        "send_error",
        [
            _client_error("Throttling"),
            EndpointConnectionError(endpoint_url="https://sqs.example.invalid"),
            KeyError("RESULT_QUEUE_URL"),
        ],
    )
    def test_send_failure_is_logged_and_swallowed(
        self, send_error: Exception, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A failed report must not raise, or it would mask the original processing failure."""
        with patch("handler.notify_result_failure", side_effect=send_error):
            _report_failure(JOB_ID, ValueError("boom"))

        [record] = _records_for_stage(caplog, "report_failure_error")
        assert record.jobId == JOB_ID
        assert record.exc_info is not None


class TestMarkStatusHelpers:
    """Tests for `_mark_failed`, which logs instead of raising."""

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

    def test_mark_failed_logs_botocore_error(self, caplog: pytest.LogCaptureFixture) -> None:
        """A connection-level DynamoDB failure should be logged, not raised, from `_mark_failed`."""
        with patch("handler.update_job_status", side_effect=BotoCoreError()):
            _mark_failed(JOB_ID)

        assert len(_records_for_stage(caplog, "update_status_failed_error")) == 1


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

    @pytest.mark.parametrize("status", ["IN_PROGRESS", "FAILED"])
    def test_job_not_painted_or_completed_raises_value_error(self, steps: dict, status: str) -> None:
        """A job in any other status should raise ValueError and not call Bedrock or notify."""
        steps["get_job_status"].return_value = status
        with pytest.raises(ValueError, match="not in PAINTED status"):
            lambda_handler(_sqs_event(JOB_ID), None)
        steps["invoke_bedrock"].assert_not_called()
        steps["notify_result"].assert_not_called()
        steps["update_job_status"].assert_not_called()

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

    def test_notify_failure_is_reraised_for_sqs_retry(self, steps: dict) -> None:
        """A failed result notification must propagate so SQS redelivers; the job stays PAINTED."""
        steps["notify_result"].side_effect = _client_error("Throttling")

        with pytest.raises(ClientError):
            lambda_handler(_sqs_event(JOB_ID), None)

        steps["update_job_status"].assert_not_called()
        steps["notify_result_failure"].assert_not_called()

    def test_notify_failure_is_logged_with_traceback(
        self, steps: dict, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A failed notification should be logged at notify_error with exc_info before re-raising."""
        steps["notify_result"].side_effect = _client_error()

        with pytest.raises(ClientError):
            lambda_handler(_sqs_event(JOB_ID), None)

        [record] = _records_for_stage(caplog, "notify_error")
        assert record.getMessage().startswith("Failed to send result notification")
        assert record.jobId == JOB_ID
        assert record.exc_info is not None

    def test_notify_connection_error_is_reraised(self, steps: dict) -> None:
        """A connection-level SQS failure is transient and must also be redelivered."""
        steps["notify_result"].side_effect = EndpointConnectionError(
            endpoint_url="https://sqs.example.invalid"
        )

        with pytest.raises(BotoCoreError):
            lambda_handler(_sqs_event(JOB_ID), None)

        steps["update_job_status"].assert_not_called()

    def test_completed_status_update_failure_is_reraised(self, steps: dict) -> None:
        """A failing COMPLETED write must propagate so SQS redelivers and the job is not stuck."""
        steps["update_job_status"].side_effect = _client_error("Throttling")

        with pytest.raises(ClientError):
            lambda_handler(_sqs_event(JOB_ID), None)

        steps["update_job_status"].assert_called_once_with(JOB_ID, "COMPLETED")

    def test_completed_status_update_failure_is_logged_without_success_log(
        self, steps: dict, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A failing COMPLETED write should be logged and must not emit the success log."""
        steps["update_job_status"].side_effect = _client_error()

        with pytest.raises(ClientError):
            lambda_handler(_sqs_event(JOB_ID), None)

        [record] = _records_for_stage(caplog, "update_status_completed_error")
        assert record.exc_info is not None
        assert _records_for_stage(caplog, "complete") == []

    def test_processing_failure_reports_failed_to_result_queue(self, steps: dict) -> None:
        """A processing failure should send FAILED with the exception class name to the result queue."""
        steps["invoke_bedrock"].side_effect = _client_error("ThrottlingException")

        lambda_handler(_sqs_event(JOB_ID), None)

        steps["notify_result_failure"].assert_called_once_with(
            JOB_ID, "Guide generation failed: ClientError"
        )
        steps["notify_result"].assert_not_called()

    def test_processing_failure_does_not_reraise_when_report_fails(
        self, steps: dict, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A failing FAILED report is logged and the message is consumed, not retried."""
        steps["invoke_bedrock"].side_effect = _client_error("ThrottlingException")
        steps["notify_result_failure"].side_effect = _client_error("QueueDoesNotExist")

        lambda_handler(_sqs_event(JOB_ID), None)

        steps["update_job_status"].assert_called_once_with(JOB_ID, "FAILED")
        assert len(_records_for_stage(caplog, "report_failure_error")) == 1

    @pytest.mark.parametrize(
        "failing_step",
        ["download_painted_image_from_s3", "fetch_prompt_from_s3", "invoke_bedrock", "upload_result_to_s3"],
    )
    def test_missing_configuration_key_error_propagates_without_failing_job(
        self, steps: dict, failing_step: str
    ) -> None:
        """A KeyError for a missing env var is a deployment bug: no FAILED write, no report."""
        steps[failing_step].side_effect = KeyError("PAINT_BUCKET_NAME")

        with pytest.raises(KeyError):
            lambda_handler(_sqs_event(JOB_ID), None)

        steps["update_job_status"].assert_not_called()
        steps["notify_result_failure"].assert_not_called()

    def test_result_check_failure_propagates_without_processing(self, steps: dict) -> None:
        """A transient S3 error on the result.json check must retry, not fail the job."""
        steps["result_exists"].side_effect = _client_error("SlowDown")

        with pytest.raises(ClientError):
            lambda_handler(_sqs_event(JOB_ID), None)

        steps["invoke_bedrock"].assert_not_called()
        steps["update_job_status"].assert_not_called()
        steps["notify_result_failure"].assert_not_called()

    def test_existing_result_skips_bedrock_and_completes(self, steps: dict) -> None:
        """On redelivery with result.json stored, Bedrock is not called again; notify and COMPLETED run."""
        steps["result_exists"].return_value = True

        lambda_handler(_sqs_event(JOB_ID), None)

        steps["download_painted_image_from_s3"].assert_not_called()
        steps["fetch_prompt_from_s3"].assert_not_called()
        steps["invoke_bedrock"].assert_not_called()
        steps["upload_result_to_s3"].assert_not_called()
        steps["notify_result"].assert_called_once_with(JOB_ID)
        steps["update_job_status"].assert_called_once_with(JOB_ID, "COMPLETED")

    def test_existing_result_skip_is_logged(self, steps: dict, caplog: pytest.LogCaptureFixture) -> None:
        """Skipping Bedrock on redelivery should be visible in the logs at skip_bedrock."""
        steps["result_exists"].return_value = True

        lambda_handler(_sqs_event(JOB_ID), None)

        [record] = _records_for_stage(caplog, "skip_bedrock")
        assert record.jobId == JOB_ID

    def test_completed_job_redelivery_resends_notification_only(self, steps: dict) -> None:
        """A job already COMPLETED is a success on redelivery: notify again, write nothing."""
        steps["get_job_status"].return_value = "COMPLETED"

        lambda_handler(_sqs_event(JOB_ID), None)

        steps["notify_result"].assert_called_once_with(JOB_ID)
        steps["invoke_bedrock"].assert_not_called()
        steps["result_exists"].assert_not_called()
        steps["update_job_status"].assert_not_called()

    def test_completed_job_redelivery_notify_failure_is_reraised(self, steps: dict) -> None:
        """If the re-sent notification fails, the message must be redelivered again."""
        steps["get_job_status"].return_value = "COMPLETED"
        steps["notify_result"].side_effect = _client_error("Throttling")

        with pytest.raises(ClientError):
            lambda_handler(_sqs_event(JOB_ID), None)

    def test_missing_job_id_failure_is_not_sent_to_result_queue(self, steps: dict) -> None:
        """A malformed message is a DLQ case, so nothing is reported to the client."""
        with pytest.raises(ValueError):
            lambda_handler({"Records": []}, None)
        steps["notify_result_failure"].assert_not_called()

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


class TestEndToEndUnderMoto:
    """Runs the real S3, DynamoDB and SQS helpers under moto; only Bedrock is mocked.

    These tests prove the retry contract: what a redelivery sees in S3, DynamoDB and the
    result queue, not just which helper was called.
    """

    @staticmethod
    def _setup(env_vars: dict, monkeypatch: pytest.MonkeyPatch, status: str) -> str:
        """Create the buckets, jobs table with one job in `status` and a result queue."""
        s3 = boto3.client("s3")
        s3.create_bucket(Bucket=PAINT_BUCKET)
        s3.create_bucket(Bucket=STATIC_BUCKET)
        _create_table(boto3.client("dynamodb"), status=status)
        queue_url = boto3.client("sqs").create_queue(QueueName="result")["QueueUrl"]
        monkeypatch.setenv("RESULT_QUEUE_URL", queue_url)
        return queue_url

    @staticmethod
    def _read_queue(queue_url: str) -> list[dict]:
        """Return the decoded bodies of every message currently on the queue."""
        messages = boto3.client("sqs").receive_message(
            QueueUrl=queue_url, MaxNumberOfMessages=10
        ).get("Messages", [])
        return [json.loads(message["Body"]) for message in messages]

    @staticmethod
    def _job_status() -> str:
        """Return the stored jobStatus of the test job."""
        item = boto3.client("dynamodb").get_item(
            TableName=TABLE_NAME, Key={"jobId": {"S": JOB_ID}}
        )["Item"]
        return item["jobStatus"]["S"]

    @mock_aws
    def test_redelivery_after_notify_failure_does_not_call_bedrock_again(
        self, env_vars: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """First attempt stores result.json then fails to notify; the retry must not re-bill Bedrock."""
        queue_url = self._setup(env_vars, monkeypatch, "PAINTED")
        attempts: list[str] = []

        def flaky_notify(job_id: str) -> None:
            attempts.append(job_id)
            if len(attempts) == 1:
                raise _client_error("Throttling")
            notify_result(job_id)

        invoke = MagicMock(return_value=json.dumps(VALID_RESULT))
        with patch("handler.download_painted_image_from_s3", return_value=FAKE_IMAGE), patch(
            "handler.fetch_prompt_from_s3", return_value=FAKE_PROMPT
        ), patch("handler.invoke_bedrock", invoke), patch(
            "handler.notify_result", side_effect=flaky_notify
        ):
            with pytest.raises(ClientError):
                lambda_handler(_sqs_event(JOB_ID), None)
            assert self._job_status() == "PAINTED"
            assert self._read_queue(queue_url) == []

            lambda_handler(_sqs_event(JOB_ID), None)

        assert invoke.call_count == 1
        assert self._job_status() == "COMPLETED"
        assert self._read_queue(queue_url) == [{"jobId": JOB_ID, "status": "COMPLETED"}]

    @mock_aws
    def test_bedrock_failure_puts_failed_message_on_result_queue(
        self, env_vars: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A Bedrock failure must mark FAILED and deliver the FAILED message the client waits for."""
        queue_url = self._setup(env_vars, monkeypatch, "PAINTED")

        with patch("handler.download_painted_image_from_s3", return_value=FAKE_IMAGE), patch(
            "handler.fetch_prompt_from_s3", return_value=FAKE_PROMPT
        ), patch("handler.invoke_bedrock", side_effect=_client_error("ThrottlingException")):
            lambda_handler(_sqs_event(JOB_ID), None)

        assert self._job_status() == "FAILED"
        assert self._read_queue(queue_url) == [
            {
                "jobId": JOB_ID,
                "status": "FAILED",
                "message": "Guide generation failed: ClientError",
            }
        ]

    @mock_aws
    def test_duplicate_message_for_completed_job_renotifies_client(
        self, env_vars: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A duplicate delivery for a COMPLETED job re-sends COMPLETED and changes nothing else."""
        queue_url = self._setup(env_vars, monkeypatch, "COMPLETED")
        invoke = MagicMock()

        with patch("handler.invoke_bedrock", invoke):
            lambda_handler(_sqs_event(JOB_ID), None)

        invoke.assert_not_called()
        assert self._job_status() == "COMPLETED"
        assert self._read_queue(queue_url) == [{"jobId": JOB_ID, "status": "COMPLETED"}]
