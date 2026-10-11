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

import handler
from handler import (
    find_vallejo_colors,
    generate_result_urls,
    get_connection_id,
    lambda_handler,
    parse_message,
    send_to_connection,
    store_vallejo_result,
)

PAINT_BUCKET = "test-paint-bucket"
STATIC_BUCKET = "test-static-bucket"
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
    monkeypatch.setenv("STATIC_BUCKET_NAME", STATIC_BUCKET)
    monkeypatch.setenv("JOBS_TABLE_NAME", TABLE_NAME)
    monkeypatch.setattr(handler, "_vallejo_palette_cache", None)
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
        assert f"painted_images/{JOB_ID}/result_vallejo.json" in urls["resultUrl"]
        assert f"painted_images/{JOB_ID}/image_0.png" in urls["imageUrl"]
        assert PAINT_BUCKET in urls["resultUrl"]

    def test_raises_key_error_when_bucket_not_configured(self, monkeypatch):
        monkeypatch.delenv("PAINT_BUCKET_NAME")
        with pytest.raises(KeyError):
            generate_result_urls(JOB_ID)


PALETTE_CSV = (
    "reference,Color,name,r,g,b,hex\n"
    "70.740,70.740,Cam. Middle Brown,72,59,33,#483B21\n"
    "70.741,70.741,Cam. Black Green,42,45,40,#2A2D28\n"
    "70.745,70.745,Dark Rose,120,83,84,#785354\n"
)
BLACK_GREEN = {
    "reference": "70.741",
    "Color": "70.741",
    "name": "Cam. Black Green",
    "r": 42,
    "g": 45,
    "b": 40,
    "hex": "#2A2D28",
}


@pytest.fixture
def buckets(env):
    """Create mocked paint/static buckets with the Vallejo CSV uploaded."""
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=PAINT_BUCKET)
        client.create_bucket(Bucket=STATIC_BUCKET)
        client.put_object(Bucket=STATIC_BUCKET, Key="vallejo/vallejo_colors.csv", Body=PALETTE_CSV)
        yield client


class TestFindVallejoColors:
    def test_adds_nearest_vallejo_color(self, buckets):
        result = find_vallejo_colors({"colors": [{"detail": "armor", "paint": "#2E4A6B"}]})
        assert result["colors"][0]["detail"] == "armor"
        assert result["colors"][0]["vallejo_color"] == BLACK_GREEN

    def test_accepts_hex_without_hash_and_exact_match(self, buckets):
        result = find_vallejo_colors({"colors": [{"detail": "d", "paint": "785354"}]})
        assert result["colors"][0]["vallejo_color"]["name"] == "Dark Rose"

    def test_does_not_modify_input(self, buckets):
        source = {"colors": [{"detail": "d", "paint": "2A2D28"}]}
        find_vallejo_colors(source)
        assert "vallejo_color" not in source["colors"][0]

    def test_reads_csv_only_once(self, buckets):
        find_vallejo_colors({"colors": []})
        buckets.delete_object(Bucket=STATIC_BUCKET, Key="vallejo/vallejo_colors.csv")
        find_vallejo_colors({"colors": []})

    @pytest.mark.parametrize(
        "payload",
        [{}, {"colors": "x"}, {"colors": ["x"]}, {"colors": [{"paint": "xyz"}]}, {"colors": [{}]}],
    )
    def test_raises_value_error_for_invalid_result(self, buckets, payload):
        with pytest.raises(ValueError):
            find_vallejo_colors(payload)

    def test_raises_value_error_for_malformed_csv(self, buckets):
        buckets.put_object(
            Bucket=STATIC_BUCKET,
            Key="vallejo/vallejo_colors.csv",
            Body="reference,Color,name,r,g,b,hex\n1,1,x,a,b,c,#000000\n",
        )
        with pytest.raises(ValueError):
            find_vallejo_colors({"colors": []})

    def test_raises_client_error_when_csv_missing(self, buckets):
        buckets.delete_object(Bucket=STATIC_BUCKET, Key="vallejo/vallejo_colors.csv")
        with pytest.raises(ClientError):
            find_vallejo_colors({"colors": []})

    def test_raises_key_error_when_static_bucket_not_configured(self, monkeypatch):
        monkeypatch.delenv("STATIC_BUCKET_NAME")
        with pytest.raises(KeyError):
            find_vallejo_colors({"colors": []})


class TestStoreVallejoResult:
    KEY = f"painted_images/{JOB_ID}/result.json"

    def test_writes_enriched_json_and_keeps_original(self, buckets):
        original = json.dumps({"colors": [{"detail": "armor", "paint": "2E4A6B"}]})
        buckets.put_object(Bucket=PAINT_BUCKET, Key=self.KEY, Body=original)

        store_vallejo_result(JOB_ID)

        stored = buckets.get_object(
            Bucket=PAINT_BUCKET, Key=f"painted_images/{JOB_ID}/result_vallejo.json"
        )["Body"].read()
        assert json.loads(stored)["colors"][0]["vallejo_color"] == BLACK_GREEN
        assert buckets.get_object(Bucket=PAINT_BUCKET, Key=self.KEY)["Body"].read() == (
            original.encode()
        )

    @pytest.mark.parametrize("body", ["not json", "[1]"])
    def test_raises_value_error_for_invalid_result_file(self, buckets, body):
        buckets.put_object(Bucket=PAINT_BUCKET, Key=self.KEY, Body=body)
        with pytest.raises(ValueError):
            store_vallejo_result(JOB_ID)

    def test_raises_client_error_when_result_missing(self, buckets):
        with pytest.raises(ClientError):
            store_vallejo_result(JOB_ID)


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
            "handler.store_vallejo_result"
        ) as store, patch(
            "handler.generate_result_urls",
            return_value={"resultUrl": "r", "imageUrl": "i"},
        ), patch("handler.send_to_connection", return_value=True) as send:
            lambda_handler(_event(self.COMPLETED), None)
        store.assert_called_once_with(JOB_ID)
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
            "handler.store_vallejo_result"
        ), patch("handler.generate_result_urls", return_value={}), patch(
            "handler.send_to_connection", return_value=False
        ):
            lambda_handler(_event(self.COMPLETED), None)

    def test_reraises_dynamodb_errors(self):
        with patch("handler.get_connection_id", side_effect=_client_error("Throttling")):
            with pytest.raises(ClientError):
                lambda_handler(_event(self.COMPLETED), None)

    def test_reraises_presign_errors(self):
        with patch("handler.get_connection_id", return_value=CONNECTION_ID), patch(
            "handler.store_vallejo_result"
        ), patch("handler.generate_result_urls", side_effect=_client_error("AccessDenied")):
            with pytest.raises(ClientError):
                lambda_handler(_event(self.COMPLETED), None)

    def test_reraises_send_errors(self):
        with patch("handler.get_connection_id", return_value=CONNECTION_ID), patch(
            "handler.store_vallejo_result"
        ), patch("handler.generate_result_urls", return_value={}), patch(
            "handler.send_to_connection", side_effect=_client_error("Forbidden")
        ):
            with pytest.raises(ClientError):
                lambda_handler(_event(self.COMPLETED), None)

    @pytest.mark.parametrize("error", [_client_error("AccessDenied"), ValueError("bad")])
    def test_reraises_vallejo_errors_without_sending(self, error):
        with patch("handler.get_connection_id", return_value=CONNECTION_ID), patch(
            "handler.store_vallejo_result", side_effect=error
        ), patch("handler.send_to_connection") as send:
            with pytest.raises(type(error)):
                lambda_handler(_event(self.COMPLETED), None)
        send.assert_not_called()
