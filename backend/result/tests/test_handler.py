"""Tests for the result Lambda handler.

Covers SQS message parsing, presigned URL generation, connectionId lookup,
WebSocket delivery, and the orchestration flow including failure paths.
"""

import json
import os
from unittest.mock import MagicMock, patch

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from handler import (
    generate_result_urls,
    get_connection_id,
    lambda_handler,
    parse_message,
    send_to_connection,
)

PAINT_BUCKET = "test-paint-bucket"
TABLE_NAME = "test-jobs-table"
WS_BASE_URL = "wss://abc123.execute-api.us-east-1.amazonaws.com/prod/"
WS_ENDPOINT = "https://abc123.execute-api.us-east-1.amazonaws.com/prod"
JOB_ID = "123e4567-e89b-12d3-a456-426614174000"
CONNECTION_ID = "conn-1"


def _event(body) -> dict:
    """Build an SQS event; non-string bodies are JSON-encoded."""
    return {"Records": [{"body": body if isinstance(body, str) else json.dumps(body)}]}


def _client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "x"}}, "Op")


@pytest.fixture(autouse=True)
def env(monkeypatch):
    """Set required environment variables."""
    monkeypatch.setenv("PAINT_BUCKET_NAME", PAINT_BUCKET)
    monkeypatch.setenv("JOBS_TABLE_NAME", TABLE_NAME)
    monkeypatch.setenv("VITE_WS_BASE_URL", WS_BASE_URL)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")


@pytest.fixture
def jobs_table(env):
    """Create a mocked DynamoDB jobs table."""
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        client.create_table(
            TableName=TABLE_NAME,
            KeySchema=[{"AttributeName": "jobId", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "jobId", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        yield client


class TestParseMessage:
    def test_returns_job_id_and_status(self):
        assert parse_message(_event({"jobId": f" {JOB_ID} ", "status": "COMPLETED"})) == (
            JOB_ID,
            "COMPLETED",
        )

    @pytest.mark.parametrize(
        "event",
        [
            {},
            {"Records": []},
            _event("not json"),
            _event("[1]"),
            _event({"status": "COMPLETED"}),
            _event({"jobId": "  ", "status": "COMPLETED"}),
            _event({"jobId": JOB_ID}),
            _event({"jobId": JOB_ID, "status": ""}),
            _event({"jobId": 5, "status": "COMPLETED"}),
        ],
    )
    def test_returns_none_for_invalid_message(self, event):
        assert parse_message(event) is None


class TestGenerateResultUrls:
    @mock_aws
    def test_returns_presigned_urls_for_both_files(self):
        urls = generate_result_urls(JOB_ID)
        assert f"painted_images/{JOB_ID}/result.json" in urls["resultUrl"]
        assert f"painted_images/{JOB_ID}/image_0.png" in urls["imageUrl"]
        assert PAINT_BUCKET in urls["resultUrl"]

    def test_raises_key_error_when_bucket_not_configured(self, monkeypatch):
        monkeypatch.delenv("PAINT_BUCKET_NAME")
        with pytest.raises(KeyError):
            generate_result_urls(JOB_ID)


class TestGetConnectionId:
    def test_returns_connection_id(self, jobs_table):
        jobs_table.put_item(
            TableName=TABLE_NAME,
            Item={"jobId": {"S": JOB_ID}, "connectionId": {"S": CONNECTION_ID}},
        )
        assert get_connection_id(JOB_ID) == CONNECTION_ID

    def test_returns_none_when_no_connection_attached(self, jobs_table):
        jobs_table.put_item(TableName=TABLE_NAME, Item={"jobId": {"S": JOB_ID}})
        assert get_connection_id(JOB_ID) is None

    def test_raises_key_error_when_job_missing(self, jobs_table):
        with pytest.raises(KeyError):
            get_connection_id(JOB_ID)

    def test_raises_key_error_when_table_not_configured(self, monkeypatch):
        monkeypatch.delenv("JOBS_TABLE_NAME")
        with pytest.raises(KeyError):
            get_connection_id(JOB_ID)


class TestSendToConnection:
    def test_posts_json_payload(self):
        client = MagicMock()
        with patch("handler.boto3.client", return_value=client) as factory:
            assert send_to_connection(CONNECTION_ID, {"a": 1}) is True
        factory.assert_called_once_with("apigatewaymanagementapi", endpoint_url=WS_ENDPOINT)
        client.post_to_connection.assert_called_once_with(
            ConnectionId=CONNECTION_ID, Data=b'{"a": 1}'
        )

    def test_returns_false_when_connection_is_gone(self):
        client = MagicMock()
        client.post_to_connection.side_effect = _client_error("GoneException")
        with patch("handler.boto3.client", return_value=client):
            assert send_to_connection(CONNECTION_ID, {}) is False

    def test_reraises_other_client_errors(self):
        client = MagicMock()
        client.post_to_connection.side_effect = _client_error("ForbiddenException")
        with patch("handler.boto3.client", return_value=client):
            with pytest.raises(ClientError):
                send_to_connection(CONNECTION_ID, {})

    def test_raises_key_error_when_endpoint_not_configured(self, monkeypatch):
        monkeypatch.delenv("VITE_WS_BASE_URL")
        with pytest.raises(KeyError):
            send_to_connection(CONNECTION_ID, {})


class TestLambdaHandler:
    COMPLETED = {"jobId": JOB_ID, "status": "COMPLETED"}

    def test_sends_urls_for_completed_job(self):
        with patch("handler.get_connection_id", return_value=CONNECTION_ID), patch(
            "handler.generate_result_urls",
            return_value={"resultUrl": "r", "imageUrl": "i"},
        ), patch("handler.send_to_connection", return_value=True) as send:
            lambda_handler(_event(self.COMPLETED), None)
        send.assert_called_once_with(
            CONNECTION_ID,
            {"jobId": JOB_ID, "status": "COMPLETED", "resultUrl": "r", "imageUrl": "i"},
        )

    def test_sends_status_only_for_non_completed_job(self):
        with patch("handler.get_connection_id", return_value=CONNECTION_ID), patch(
            "handler.generate_result_urls"
        ) as presign, patch("handler.send_to_connection", return_value=True) as send:
            lambda_handler(_event({"jobId": JOB_ID, "status": "FAILED"}), None)
        presign.assert_not_called()
        send.assert_called_once_with(CONNECTION_ID, {"jobId": JOB_ID, "status": "FAILED"})

    def test_raises_value_error_for_invalid_message(self):
        with pytest.raises(ValueError):
            lambda_handler(_event("garbage"), None)

    def test_raises_value_error_when_job_not_found(self):
        with patch("handler.get_connection_id", side_effect=KeyError("missing")):
            with pytest.raises(ValueError):
                lambda_handler(_event(self.COMPLETED), None)

    def test_skips_sending_when_job_has_no_connection(self):
        with patch("handler.get_connection_id", return_value=None), patch(
            "handler.send_to_connection"
        ) as send:
            lambda_handler(_event(self.COMPLETED), None)
        send.assert_not_called()

    def test_acknowledges_message_when_connection_is_gone(self):
        with patch("handler.get_connection_id", return_value=CONNECTION_ID), patch(
            "handler.generate_result_urls", return_value={}
        ), patch("handler.send_to_connection", return_value=False):
            lambda_handler(_event(self.COMPLETED), None)

    def test_reraises_dynamodb_errors(self):
        with patch("handler.get_connection_id", side_effect=_client_error("Throttling")):
            with pytest.raises(ClientError):
                lambda_handler(_event(self.COMPLETED), None)

    def test_reraises_presign_errors(self):
        with patch("handler.get_connection_id", return_value=CONNECTION_ID), patch(
            "handler.generate_result_urls", side_effect=_client_error("AccessDenied")
        ):
            with pytest.raises(ClientError):
                lambda_handler(_event(self.COMPLETED), None)

    def test_reraises_send_errors(self):
        with patch("handler.get_connection_id", return_value=CONNECTION_ID), patch(
            "handler.generate_result_urls", return_value={}
        ), patch("handler.send_to_connection", side_effect=_client_error("Forbidden")):
            with pytest.raises(ClientError):
                lambda_handler(_event(self.COMPLETED), None)
