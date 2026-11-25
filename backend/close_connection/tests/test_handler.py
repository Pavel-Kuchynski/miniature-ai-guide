"""Unit tests for the close_connection handler.

Uses `unittest` with mocked boto3 resources, matching the CI gate
(`python -m unittest discover -s tests`), which installs only runtime requirements.
"""

import io
import json
import logging
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from botocore.exceptions import ClientError

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import handler
from logging_config import JSONFormatter, StructuredLoggerAdapter

JOBS_TABLE = "test-jobs"
CONNECTION_ID = "conn-456"
JOB_ID = "job-123"


def _client_error(code: str, operation: str) -> ClientError:
    """Build a botocore ClientError with the given error code.

    Args:
        code: DynamoDB error code, e.g. "ConditionalCheckFailedException".
        operation: Name of the failed DynamoDB operation.

    Returns:
        ClientError instance usable as a mock side effect.
    """
    return ClientError({"Error": {"Code": code, "Message": "sensitive detail"}}, operation)


class _HandlerTestCase(unittest.TestCase):
    """Patches environment, boto3 and the JOBS table for each handler test."""

    def setUp(self) -> None:
        """Configure a JOBS table name, a mocked DynamoDB resource and a GSI hit for CONNECTION_ID."""
        env_patcher = patch.dict("os.environ", {"JOBS_TABLE_NAME": JOBS_TABLE}, clear=True)
        env_patcher.start()
        self.addCleanup(env_patcher.stop)

        resource_patcher = patch("handler.boto3.resource")
        self.mock_resource_factory = resource_patcher.start()
        self.addCleanup(resource_patcher.stop)

        self.jobs_table = MagicMock()
        self.mock_resource_factory.return_value.Table.return_value = self.jobs_table
        self.jobs_table.query.return_value = {
            "Items": [{"jobId": JOB_ID, "connectionId": CONNECTION_ID}]
        }

    def _invoke(self, connection_id: str | None = CONNECTION_ID) -> dict:
        """Invoke the handler with a $disconnect event for the given connectionId.

        Args:
            connection_id: Connection ID placed in requestContext; None omits the key.

        Returns:
            API Gateway response dict returned by the handler.
        """
        request_context = {} if connection_id is None else {"connectionId": connection_id}
        return handler.lambda_handler(
            {"requestContext": {**request_context, "routeKey": "$disconnect"}},
            None,
        )


class TestCloseConnectionHandler(_HandlerTestCase):
    """Contract tests for lambda_handler."""

    def test_disconnect_removes_connection_attributes_for_owning_job(self) -> None:
        """Test that a matching disconnect removes connectionId and connectedAt with a condition."""
        response = self._invoke()

        self.assertEqual(response["statusCode"], 200)
        self.assertEqual(
            json.loads(response["body"])["message"], "Connection closed successfully"
        )
        self.jobs_table.update_item.assert_called_once_with(
            Key={"jobId": JOB_ID},
            UpdateExpression="REMOVE connectionId, connectedAt",
            ConditionExpression="connectionId = :connectionId",
            ExpressionAttributeValues={":connectionId": CONNECTION_ID},
        )

    def test_lookup_uses_connection_id_index(self) -> None:
        """Test that the job lookup queries the connectionId-index GSI on the JOBS table."""
        self._invoke()

        self.mock_resource_factory.return_value.Table.assert_called_with(JOBS_TABLE)
        self.jobs_table.query.assert_called_once_with(
            IndexName="connectionId-index",
            KeyConditionExpression="connectionId = :conn_id",
            ExpressionAttributeValues={":conn_id": CONNECTION_ID},
        )

    def test_stale_disconnect_is_noop_returning_200(self) -> None:
        """Test that a failed REMOVE condition (reconnect or repeat disconnect) returns 200."""
        self.jobs_table.update_item.side_effect = _client_error(
            "ConditionalCheckFailedException", "UpdateItem"
        )

        response = self._invoke()

        self.assertEqual(response["statusCode"], 200)
        self.assertEqual(json.loads(response["body"])["message"], "Connection already closed")
        self.jobs_table.update_item.assert_called_once()

    def test_disconnect_without_matching_job_returns_200_noop(self) -> None:
        """Test that a connection with no job is an idempotent no-op, not an error."""
        self.jobs_table.query.return_value = {"Items": []}

        response = self._invoke()

        self.assertEqual(response["statusCode"], 200)
        self.assertEqual(
            json.loads(response["body"])["message"],
            "No active job found for this connection",
        )
        self.jobs_table.update_item.assert_not_called()

    def test_multiple_matching_jobs_returns_500_without_writes(self) -> None:
        """Test that several jobs holding one connectionId is a server error and nothing is written."""
        self.jobs_table.query.return_value = {
            "Items": [
                {"jobId": "job-1", "connectionId": CONNECTION_ID},
                {"jobId": "job-2", "connectionId": CONNECTION_ID},
            ]
        }

        response = self._invoke()

        self.assertEqual(response["statusCode"], 500)
        payload = json.loads(response["body"])
        self.assertEqual(payload["error"], "DatabaseError")
        self.jobs_table.update_item.assert_not_called()

    def test_matching_record_without_job_id_returns_500(self) -> None:
        """Test that an index hit without a jobId is a server error and nothing is written."""
        self.jobs_table.query.return_value = {"Items": [{"connectionId": CONNECTION_ID}]}

        response = self._invoke()

        self.assertEqual(response["statusCode"], 500)
        self.jobs_table.update_item.assert_not_called()

    def test_query_error_returns_500(self) -> None:
        """Test that a DynamoDB query failure returns a generic 500."""
        self.jobs_table.query.side_effect = _client_error("AccessDenied", "Query")

        response = self._invoke()

        self.assertEqual(response["statusCode"], 500)
        payload = json.loads(response["body"])
        self.assertEqual(payload["error"], "DatabaseError")
        self.assertEqual(payload["message"], "Failed to query jobs table")

    def test_update_error_returns_500_without_leaking_details(self) -> None:
        """Test that an unexpected update failure returns 500 with no AWS error details."""
        self.jobs_table.update_item.side_effect = _client_error(
            "ValidationException", "UpdateItem"
        )

        response = self._invoke()

        self.assertEqual(response["statusCode"], 500)
        payload = json.loads(response["body"])
        self.assertEqual(payload["message"], "Failed to remove connection")
        serialized = json.dumps(payload)
        self.assertNotIn("ValidationException", serialized)
        self.assertNotIn("sensitive detail", serialized)
        self.assertNotIn(JOBS_TABLE, serialized)

    def test_missing_or_blank_connection_id_returns_400(self) -> None:
        """Test that a missing, empty or whitespace-only connectionId returns 400 without a query."""
        for connection_id in (None, "", "   "):
            with self.subTest(connection_id=connection_id):
                self.jobs_table.query.reset_mock()

                response = self._invoke(connection_id=connection_id)

                self.assertEqual(response["statusCode"], 400)
                payload = json.loads(response["body"])
                self.assertEqual(payload["error"], "InvalidRequest")
                self.jobs_table.query.assert_not_called()

    def test_event_without_request_context_returns_400(self) -> None:
        """Test that events without requestContext (including None) return 400."""
        for event in ({}, None):
            with self.subTest(event=event):
                response = handler.lambda_handler(event, None)

                self.assertEqual(response["statusCode"], 400)
                self.assertIn("connectionId is required", json.loads(response["body"])["message"])

    def test_missing_jobs_table_env_var_returns_500_without_dynamodb_call(self) -> None:
        """Test that a missing JOBS_TABLE_NAME returns 500 and never touches DynamoDB."""
        with patch.dict("os.environ", {}, clear=True):
            response = self._invoke()

        self.assertEqual(response["statusCode"], 500)
        payload = json.loads(response["body"])
        self.assertEqual(payload["error"], "ServerConfiguration")
        self.mock_resource_factory.assert_not_called()

    def test_response_headers_are_json(self) -> None:
        """Test that every response declares a JSON Content-Type."""
        response = self._invoke()

        self.assertEqual(response["headers"]["Content-Type"], "application/json")


class TestFindJobIdByConnectionId(unittest.TestCase):
    """Tests for the _find_job_id_by_connection_id helper."""

    def setUp(self) -> None:
        """Create a mocked JOBS table and a DynamoDB resource that returns it."""
        self.jobs_table = MagicMock()
        self.dynamodb = MagicMock()
        self.dynamodb.Table.return_value = self.jobs_table

    def test_returns_job_id_of_single_matching_record(self) -> None:
        """Test that the jobId of the one matching record is returned."""
        self.jobs_table.query.return_value = {"Items": [{"jobId": "found-job", "connectionId": "c"}]}

        result = handler._find_job_id_by_connection_id(self.dynamodb, "c", "jobs-table")

        self.assertEqual(result, "found-job")
        self.dynamodb.Table.assert_called_once_with("jobs-table")

    def test_raises_job_not_found_when_no_record_matches(self) -> None:
        """Test that an empty index result raises JobNotFoundError."""
        self.jobs_table.query.return_value = {"Items": []}

        with self.assertRaises(handler.JobNotFoundError):
            handler._find_job_id_by_connection_id(self.dynamodb, "missing", "jobs-table")

    def test_raises_integrity_error_when_several_records_match(self) -> None:
        """Test that multiple matches raise ConnectionIndexIntegrityError, not JobNotFoundError."""
        self.jobs_table.query.return_value = {
            "Items": [{"jobId": "job-1"}, {"jobId": "job-2"}]
        }

        with self.assertRaises(handler.ConnectionIndexIntegrityError):
            handler._find_job_id_by_connection_id(self.dynamodb, "dup", "jobs-table")

    def test_raises_integrity_error_when_record_has_no_job_id(self) -> None:
        """Test that a matching record without jobId raises ConnectionIndexIntegrityError."""
        self.jobs_table.query.return_value = {"Items": [{"connectionId": "c"}]}

        with self.assertRaises(handler.ConnectionIndexIntegrityError):
            handler._find_job_id_by_connection_id(self.dynamodb, "c", "jobs-table")

    def test_propagates_client_error(self) -> None:
        """Test that a DynamoDB query ClientError is propagated to the caller."""
        self.jobs_table.query.side_effect = _client_error("AccessDenied", "Query")

        with self.assertRaises(ClientError):
            handler._find_job_id_by_connection_id(self.dynamodb, "c", "jobs-table")

    def test_query_uses_connection_id_index_and_key_condition(self) -> None:
        """Test that the query targets connectionId-index with the connectionId key condition."""
        self.jobs_table.query.return_value = {"Items": [{"jobId": "j"}]}

        handler._find_job_id_by_connection_id(self.dynamodb, "test-conn", "test-table")

        self.assertEqual(
            self.jobs_table.query.call_args.kwargs,
            {
                "IndexName": "connectionId-index",
                "KeyConditionExpression": "connectionId = :conn_id",
                "ExpressionAttributeValues": {":conn_id": "test-conn"},
            },
        )


class TestRemoveConnectionFromDynamoDB(unittest.TestCase):
    """Tests for the _remove_connection_from_dynamodb helper."""

    def setUp(self) -> None:
        """Create a mocked JOBS table and a DynamoDB resource that returns it."""
        self.jobs_table = MagicMock()
        self.dynamodb = MagicMock()
        self.dynamodb.Table.return_value = self.jobs_table

    def test_update_is_keyed_by_job_id_and_conditional_on_connection_id(self) -> None:
        """Test that the REMOVE targets the job and is conditional on the disconnecting connectionId."""
        handler._remove_connection_from_dynamodb(self.dynamodb, "my-job-id", "conn-9", "jobs-table")

        self.assertEqual(
            self.jobs_table.update_item.call_args.kwargs,
            {
                "Key": {"jobId": "my-job-id"},
                "UpdateExpression": "REMOVE connectionId, connectedAt",
                "ConditionExpression": "connectionId = :connectionId",
                "ExpressionAttributeValues": {":connectionId": "conn-9"},
            },
        )

    def test_propagates_client_error(self) -> None:
        """Test that ClientError from update_item is propagated to the caller."""
        self.jobs_table.update_item.side_effect = _client_error("AccessDenied", "UpdateItem")

        with self.assertRaises(ClientError):
            handler._remove_connection_from_dynamodb(self.dynamodb, "j", "c", "jobs-table")


class TestConditionalCheckFailedDetection(unittest.TestCase):
    """Tests for the _is_conditional_check_failed helper."""

    def test_true_only_for_conditional_check_failed(self) -> None:
        """Test that only ConditionalCheckFailedException is classified as a stale-write condition."""
        self.assertTrue(
            handler._is_conditional_check_failed(
                _client_error("ConditionalCheckFailedException", "UpdateItem")
            )
        )
        self.assertFalse(
            handler._is_conditional_check_failed(_client_error("AccessDenied", "UpdateItem"))
        )


class TestStructuredLogging(_HandlerTestCase):
    """Tests that log records carry the correlation fields."""

    def test_disconnect_log_records_carry_connection_id(self) -> None:
        """Test that every log record of a disconnect includes the connectionId."""
        with self.assertLogs(handler.logger, level="INFO") as captured:
            self._invoke()

        self.assertGreaterEqual(len(captured.records), 2)
        for record in captured.records:
            with self.subTest(message=record.getMessage()):
                self.assertEqual(record.connectionId, CONNECTION_ID)
        completed = [r for r in captured.records if r.getMessage() == "Connection closed successfully"]
        self.assertEqual(completed[0].jobId, JOB_ID)

    def test_json_formatter_emits_connection_id_and_job_id(self) -> None:
        """Test that the JSON line contains connectionId, jobId and stage as top-level keys."""
        stream = io.StringIO()
        formatter_handler = logging.StreamHandler(stream)
        formatter_handler.setFormatter(JSONFormatter())
        test_logger = logging.getLogger("close_connection.test_formatter")
        test_logger.addHandler(formatter_handler)
        test_logger.setLevel(logging.INFO)
        test_logger.propagate = False
        self.addCleanup(test_logger.removeHandler, formatter_handler)

        StructuredLoggerAdapter(
            test_logger,
            {"connectionId": CONNECTION_ID, "stage": "remove_connection"},
        ).info("probe")

        line = json.loads(stream.getvalue().strip())
        self.assertEqual(line["connectionId"], CONNECTION_ID)
        self.assertEqual(line["jobId"], "unknown")
        self.assertEqual(line["stage"], "remove_connection")
        self.assertEqual(line["message"], "probe")


if __name__ == "__main__":
    unittest.main()
