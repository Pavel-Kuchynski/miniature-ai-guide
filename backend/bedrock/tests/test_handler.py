"""Tests for the bedrock Lambda handler.

Covers SQS parsing, S3 image/prompt download, Bedrock invocation, result upload,
DynamoDB status handling, SQS notification, and the orchestration flow including
failure paths.
"""

import json
from typing import Any
from unittest.mock import MagicMock, patch

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from handler import (
    DEFAULT_BEDROCK_MAX_TOKENS,
    DEFAULT_BEDROCK_MODEL_ID,
    DEFAULT_BEDROCK_READ_TIMEOUT_SECONDS,
    PROMPT_KEY,
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

PAINT_BUCKET = "test-paint-bucket"
STATIC_BUCKET = "test-static-bucket"
TABLE_NAME = "test-jobs-table"
MODEL_ID = "test-model-id"
JOB_ID = "123e4567-e89b-12d3-a456-426614174000"
FAKE_IMAGE = b"fake-png-bytes"
FAKE_PROMPT = "Describe the RGB colors."


@pytest.fixture
def env_vars(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Set all required environment variables."""
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
    return {"Records": [{"body": json.dumps({"jobId": job_id})}]}


def _client_error(code: str = "Boom") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "x"}}, "Op")


def _create_table(client: Any, status: str = "PAINTED") -> None:
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


class TestParseJobId:
    def test_valid_event_returns_job_id(self) -> None:
        assert parse_job_id(_sqs_event(JOB_ID)) == JOB_ID

    def test_whitespace_is_trimmed(self) -> None:
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
        ],
    )
    def test_invalid_event_returns_none(self, event: dict) -> None:
        assert parse_job_id(event) is None


class TestDownloadPaintedImage:
    def _put(self, s3: Any, key: str, body: bytes) -> None:
        s3.put_object(Bucket=PAINT_BUCKET, Key=key, Body=body)

    @mock_aws
    def test_downloads_single_png_ignoring_result_json(self, env_vars: dict) -> None:
        s3 = boto3.client("s3")
        s3.create_bucket(Bucket=PAINT_BUCKET)
        self._put(s3, f"painted_images/{JOB_ID}/image_0.png", FAKE_IMAGE)
        self._put(s3, f"painted_images/{JOB_ID}/result.json", b"{}")
        self._put(s3, f"painted_images/{JOB_ID}/", b"")

        assert download_painted_image_from_s3(JOB_ID) == FAKE_IMAGE

    @mock_aws
    def test_ignores_other_jobs(self, env_vars: dict) -> None:
        s3 = boto3.client("s3")
        s3.create_bucket(Bucket=PAINT_BUCKET)
        self._put(s3, f"painted_images/{JOB_ID}/image_0.png", FAKE_IMAGE)
        self._put(s3, "painted_images/other/image_0.png", b"other")

        assert download_painted_image_from_s3(JOB_ID) == FAKE_IMAGE

    @mock_aws
    def test_no_png_raises_value_error(self, env_vars: dict) -> None:
        boto3.client("s3").create_bucket(Bucket=PAINT_BUCKET)
        with pytest.raises(ValueError):
            download_painted_image_from_s3(JOB_ID)

    @mock_aws
    def test_multiple_pngs_raise_value_error(self, env_vars: dict) -> None:
        s3 = boto3.client("s3")
        s3.create_bucket(Bucket=PAINT_BUCKET)
        self._put(s3, f"painted_images/{JOB_ID}/image_0.png", FAKE_IMAGE)
        self._put(s3, f"painted_images/{JOB_ID}/image_1.png", FAKE_IMAGE)
        with pytest.raises(ValueError):
            download_painted_image_from_s3(JOB_ID)

    def test_missing_bucket_env_raises_key_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("PAINT_BUCKET_NAME", raising=False)
        with pytest.raises(KeyError):
            download_painted_image_from_s3(JOB_ID)


class TestFetchPrompt:
    @mock_aws
    def test_reads_prompt_text(self, env_vars: dict) -> None:
        s3 = boto3.client("s3")
        s3.create_bucket(Bucket=STATIC_BUCKET)
        s3.put_object(Bucket=STATIC_BUCKET, Key=PROMPT_KEY, Body=FAKE_PROMPT.encode())
        assert fetch_prompt_from_s3() == FAKE_PROMPT

    @mock_aws
    def test_missing_prompt_raises_client_error(self, env_vars: dict) -> None:
        boto3.client("s3").create_bucket(Bucket=STATIC_BUCKET)
        with pytest.raises(ClientError):
            fetch_prompt_from_s3()

    def test_missing_bucket_env_raises_key_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("STATIC_BUCKET_NAME", raising=False)
        with pytest.raises(KeyError):
            fetch_prompt_from_s3()


class TestInvokeBedrock:
    def _run(self, content: list) -> tuple:
        client = MagicMock()
        client.converse.return_value = {"output": {"message": {"content": content}}}
        with patch("handler.boto3.client", return_value=client):
            return client, invoke_bedrock(FAKE_IMAGE, FAKE_PROMPT)

    def test_returns_joined_text_blocks(self, env_vars: dict) -> None:
        _, text = self._run([{"text": '{"a":'}, {"text": " 1}"}])
        assert text == '{"a": 1}'

    def test_sends_prompt_and_png_image(self, env_vars: dict) -> None:
        client, _ = self._run([{"text": "ok"}])
        kwargs = client.converse.call_args.kwargs
        assert kwargs["modelId"] == MODEL_ID
        content = kwargs["messages"][0]["content"]
        assert {"text": FAKE_PROMPT} in content
        assert {"image": {"format": "png", "source": {"bytes": FAKE_IMAGE}}} in content

    def test_empty_response_raises_value_error(self, env_vars: dict) -> None:
        with pytest.raises(ValueError):
            self._run([{"text": "  "}])

    def test_client_error_propagates(self, env_vars: dict) -> None:
        client = MagicMock()
        client.converse.side_effect = _client_error("ThrottlingException")
        with patch("handler.boto3.client", return_value=client):
            with pytest.raises(ClientError):
                invoke_bedrock(FAKE_IMAGE, FAKE_PROMPT)

    def test_uses_defaults_when_optional_env_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for name in ("BEDROCK_MODEL_ID", "BEDROCK_MAX_TOKENS", "BEDROCK_READ_TIMEOUT_SECONDS"):
            monkeypatch.delenv(name, raising=False)
        client = MagicMock()
        client.converse.return_value = {"output": {"message": {"content": [{"text": "ok"}]}}}
        with patch("handler.boto3.client", return_value=client) as mock_factory:
            invoke_bedrock(FAKE_IMAGE, FAKE_PROMPT)

        kwargs = client.converse.call_args.kwargs
        assert kwargs["modelId"] == DEFAULT_BEDROCK_MODEL_ID
        assert kwargs["inferenceConfig"] == {"maxTokens": DEFAULT_BEDROCK_MAX_TOKENS}
        config = mock_factory.call_args.kwargs["config"]
        assert config.read_timeout == DEFAULT_BEDROCK_READ_TIMEOUT_SECONDS

    def test_env_overrides_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BEDROCK_MODEL_ID", "custom-model")
        monkeypatch.setenv("BEDROCK_MAX_TOKENS", "128")
        monkeypatch.setenv("BEDROCK_READ_TIMEOUT_SECONDS", "42")
        client = MagicMock()
        client.converse.return_value = {"output": {"message": {"content": [{"text": "ok"}]}}}
        with patch("handler.boto3.client", return_value=client) as mock_factory:
            invoke_bedrock(FAKE_IMAGE, FAKE_PROMPT)

        kwargs = client.converse.call_args.kwargs
        assert kwargs["modelId"] == "custom-model"
        assert kwargs["inferenceConfig"] == {"maxTokens": 128}
        assert mock_factory.call_args.kwargs["config"].read_timeout == 42

    @pytest.mark.parametrize("name", ["BEDROCK_MAX_TOKENS", "BEDROCK_READ_TIMEOUT_SECONDS"])
    @pytest.mark.parametrize("value", ["abc", "0", "-5"])
    def test_invalid_numeric_env_raises_value_error(
        self, monkeypatch: pytest.MonkeyPatch, name: str, value: str
    ) -> None:
        monkeypatch.setenv(name, value)
        with pytest.raises(ValueError):
            invoke_bedrock(FAKE_IMAGE, FAKE_PROMPT)

    def test_blank_numeric_env_uses_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BEDROCK_MAX_TOKENS", "  ")
        client = MagicMock()
        client.converse.return_value = {"output": {"message": {"content": [{"text": "ok"}]}}}
        with patch("handler.boto3.client", return_value=client):
            invoke_bedrock(FAKE_IMAGE, FAKE_PROMPT)
        assert client.converse.call_args.kwargs["inferenceConfig"] == {
            "maxTokens": DEFAULT_BEDROCK_MAX_TOKENS
        }


VALID_RESULT = {"colors": [{"detail": "armor", "paint": "A9A9A9"}]}


class TestParseModelOutput:
    def test_valid_json_is_returned(self) -> None:
        assert parse_model_output(json.dumps(VALID_RESULT)) == VALID_RESULT

    def test_markdown_code_fence_is_tolerated(self) -> None:
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
        with pytest.raises(ValueError):
            parse_model_output(text)


class TestUploadResult:
    @mock_aws
    def test_stores_result_json(self, env_vars: dict) -> None:
        s3 = boto3.client("s3")
        s3.create_bucket(Bucket=PAINT_BUCKET)

        key = upload_result_to_s3(JOB_ID, VALID_RESULT)

        assert key == f"painted_images/{JOB_ID}/result.json"
        obj = s3.get_object(Bucket=PAINT_BUCKET, Key=key)
        assert obj["ContentType"] == "application/json"
        assert json.loads(obj["Body"].read()) == VALID_RESULT

    def test_missing_bucket_env_raises_key_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("PAINT_BUCKET_NAME", raising=False)
        with pytest.raises(KeyError):
            upload_result_to_s3(JOB_ID, VALID_RESULT)


class TestJobStatus:
    @mock_aws
    def test_get_returns_status(self, env_vars: dict) -> None:
        _create_table(boto3.client("dynamodb"))
        assert get_job_status(JOB_ID) == "PAINTED"

    @mock_aws
    def test_get_returns_none_when_missing(self, env_vars: dict) -> None:
        _create_table(boto3.client("dynamodb"))
        assert get_job_status("other") is None

    @mock_aws
    def test_update_moves_painted_to_completed(self, env_vars: dict) -> None:
        client = boto3.client("dynamodb")
        _create_table(client)
        update_job_status(JOB_ID, "COMPLETED")
        item = client.get_item(TableName=TABLE_NAME, Key={"jobId": {"S": JOB_ID}})["Item"]
        assert item["jobStatus"]["S"] == "COMPLETED"
        assert "updatedAt" in item

    @mock_aws
    def test_update_fails_when_not_painted(self, env_vars: dict) -> None:
        _create_table(boto3.client("dynamodb"), status="COMPLETED")
        with pytest.raises(ClientError) as exc_info:
            update_job_status(JOB_ID, "FAILED")
        assert exc_info.value.response["Error"]["Code"] == "ConditionalCheckFailedException"

    @mock_aws
    def test_update_does_not_create_missing_job(self, env_vars: dict) -> None:
        client = boto3.client("dynamodb")
        _create_table(client)
        with pytest.raises(ClientError):
            update_job_status("other", "COMPLETED")

    def test_missing_table_env_raises_key_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("JOBS_TABLE_NAME", raising=False)
        with pytest.raises(KeyError):
            get_job_status(JOB_ID)
        with pytest.raises(KeyError):
            update_job_status(JOB_ID, "COMPLETED")


class TestNotifyResult:
    @mock_aws
    def test_sends_completed_message(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sqs = boto3.client("sqs")
        url = sqs.create_queue(QueueName="result")["QueueUrl"]
        monkeypatch.setenv("RESULT_QUEUE_URL", url)

        notify_result(JOB_ID)

        messages = sqs.receive_message(QueueUrl=url)["Messages"]
        assert json.loads(messages[0]["Body"]) == {"jobId": JOB_ID, "status": "COMPLETED"}

    def test_missing_queue_env_raises_key_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("RESULT_QUEUE_URL", raising=False)
        with pytest.raises(KeyError):
            notify_result(JOB_ID)


@pytest.fixture
def steps():
    """Patch every handler helper; yields a dict of mocks with happy-path defaults."""
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


class TestLambdaHandler:
    def test_happy_path(self, steps: dict) -> None:
        lambda_handler(_sqs_event(JOB_ID), None)

        steps["invoke_bedrock"].assert_called_once_with(FAKE_IMAGE, FAKE_PROMPT)
        steps["upload_result_to_s3"].assert_called_once_with(JOB_ID, VALID_RESULT)
        steps["notify_result"].assert_called_once_with(JOB_ID)
        steps["update_job_status"].assert_called_once_with(JOB_ID, "COMPLETED")

    def test_logs_bedrock_response(self, steps: dict) -> None:
        with patch("handler.logger.handle") as mock_handle:
            lambda_handler(_sqs_event(JOB_ID), None)
        logged = [c.args[0] for c in mock_handle.call_args_list]
        record = next(r for r in logged if r.__dict__.get("stage") == "bedrock_response")
        assert record.getMessage() == f"Bedrock response: {json.dumps(VALID_RESULT)}"
        assert record.__dict__["jobId"] == JOB_ID

    def test_logs_unparsable_bedrock_response_before_failing(self, steps: dict) -> None:
        steps["invoke_bedrock"].return_value = "garbage reply"
        with patch("handler.logger.handle") as mock_handle:
            lambda_handler(_sqs_event(JOB_ID), None)
        messages = [c.args[0].getMessage() for c in mock_handle.call_args_list]
        assert "Bedrock response: garbage reply" in messages
        steps["update_job_status"].assert_called_once_with(JOB_ID, "FAILED")

    def test_invalid_job_id_raises_value_error(self, steps: dict) -> None:
        with pytest.raises(ValueError):
            lambda_handler({"Records": []}, None)
        steps["get_job_status"].assert_not_called()

    def test_unknown_job_raises_value_error(self, steps: dict) -> None:
        steps["get_job_status"].return_value = None
        with pytest.raises(ValueError):
            lambda_handler(_sqs_event(JOB_ID), None)
        steps["download_painted_image_from_s3"].assert_not_called()

    def test_job_not_painted_raises_value_error(self, steps: dict) -> None:
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
        if isinstance(error, Exception):
            steps[failing_step].side_effect = error
        else:
            steps[failing_step].return_value = error

        lambda_handler(_sqs_event(JOB_ID), None)

        steps["update_job_status"].assert_called_once_with(JOB_ID, "FAILED")
        steps["notify_result"].assert_not_called()

    def test_failed_status_update_error_is_swallowed(self, steps: dict) -> None:
        steps["invoke_bedrock"].side_effect = ValueError("bad")
        steps["update_job_status"].side_effect = _client_error("ConditionalCheckFailedException")

        lambda_handler(_sqs_event(JOB_ID), None)

    def test_notify_failure_skips_status_update(self, steps: dict) -> None:
        steps["notify_result"].side_effect = _client_error()

        lambda_handler(_sqs_event(JOB_ID), None)

        steps["update_job_status"].assert_not_called()

    def test_completed_status_update_failure_does_not_raise(self, steps: dict) -> None:
        steps["update_job_status"].side_effect = _client_error()

        lambda_handler(_sqs_event(JOB_ID), None)

        steps["update_job_status"].assert_called_once_with(JOB_ID, "COMPLETED")
