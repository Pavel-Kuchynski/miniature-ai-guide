"""Unit tests for the close_connection handler."""

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from botocore.exceptions import ClientError

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import handler


class TestCloseConnectionHandler(unittest.TestCase):
    """Tests for WebSocket disconnection handler."""

    def _create_event(
        self,
        connection_id: str = "test-connection-456",
    ) -> dict:
        """Helper to create a WebSocket disconnect event.

        Args:
            connection_id: Connection ID.

        Returns:
            WebSocket disconnect event dict.
        """
        return {
            "requestContext": {
                "connectionId": connection_id,
                "routeKey": "$disconnect",
            },
        }

    def test_successful_disconnection(self) -> None:
        """Test successful disconnection with all valid inputs."""
        event = self._create_event(connection_id="test-connection-456")

        with patch.dict(
            "os.environ",
            {"JOBS_TABLE_NAME": "test-jobs"},
            clear=True,
        ), patch("handler.boto3.resource") as mock_dynamodb_factory:
            mock_dynamodb = MagicMock()
            mock_dynamodb_factory.return_value = mock_dynamodb
            jobs_table = MagicMock()

            def get_table(name):
                if name == "test-jobs":
                    return jobs_table
                raise ValueError(f"Unexpected table: {name}")

            mock_dynamodb.Table.side_effect = get_table

            jobs_table.query.return_value = {
                "Items": [
                    {
                        "jobId": "test-job-123",
                        "connectionId": "test-connection-456",
                    }
                ]
            }

            response = handler.lambda_handler(event, None)

        self.assertEqual(response["statusCode"], 200)
        payload = json.loads(response["body"])
        self.assertEqual(payload["message"], "Connection closed successfully")
        jobs_table.query.assert_called_once()
        jobs_table.update_item.assert_called_once()
        call_args = jobs_table.update_item.call_args
        self.assertEqual(call_args.kwargs["Key"], {"jobId": "test-job-123"})
        self.assertIn("REMOVE connectionId", call_args.kwargs["UpdateExpression"])

    @patch("handler.boto3.resource")
    def test_missing_connection_id_returns_400(self, mock_dynamodb_factory) -> None:
        """Test that missing connectionId returns 400 Bad Request."""
        event = {
            "requestContext": {},
        }

        response = handler.lambda_handler(event, None)

        self.assertEqual(response["statusCode"], 400)
        payload = json.loads(response["body"])
        self.assertEqual(payload["error"], "InvalidRequest")
        self.assertIn("connectionId is required", payload["message"])


    @patch("handler.boto3.resource")
    def test_none_request_context_returns_400(self, mock_dynamodb_factory) -> None:
        """Test that missing requestContext is handled gracefully."""
        event = {}

        response = handler.lambda_handler(event, None)

        self.assertEqual(response["statusCode"], 400)
        payload = json.loads(response["body"])
        self.assertEqual(payload["error"], "InvalidRequest")
        self.assertIn("connectionId is required", payload["message"])

    @patch("handler.boto3.resource")
    def test_missing_jobs_table_env_var_returns_500(self, mock_dynamodb_factory) -> None:
        """Test that missing JOBS_TABLE_NAME env var returns 500."""
        event = self._create_event()

        with patch.dict("os.environ", {}, clear=True):
            response = handler.lambda_handler(event, None)

        self.assertEqual(response["statusCode"], 500)
        payload = json.loads(response["body"])
        self.assertEqual(payload["error"], "ServerConfiguration")
        self.assertIn("Failed to initialize handler", payload["message"])

    def test_dynamodb_update_item_error_returns_500(self) -> None:
        """Test that DynamoDB update errors are caught and return 500."""
        event = self._create_event(connection_id="test-connection-456")

        with patch.dict(
            "os.environ",
            {"JOBS_TABLE_NAME": "test-jobs"},
            clear=True,
        ), patch("handler.boto3.resource") as mock_dynamodb_factory:
            mock_dynamodb = MagicMock()
            mock_dynamodb_factory.return_value = mock_dynamodb
            jobs_table = MagicMock()

            def get_table(name):
                if name == "test-jobs":
                    return jobs_table
                raise ValueError(f"Unexpected table: {name}")

            mock_dynamodb.Table.side_effect = get_table
            jobs_table.query.return_value = {
                "Items": [
                    {
                        "jobId": "test-job-123",
                        "connectionId": "test-connection-456",
                    }
                ]
            }
            jobs_table.update_item.side_effect = ClientError(
                {"Error": {"Code": "AccessDenied"}}, "UpdateItem"
            )

            response = handler.lambda_handler(event, None)

        self.assertEqual(response["statusCode"], 500)
        payload = json.loads(response["body"])
        self.assertEqual(payload["error"], "DatabaseError")
        self.assertIn("Failed to remove connection", payload["message"])

    def test_returns_404_when_connection_not_found(self) -> None:
        """Test that no matching connection returns 404 Not Found."""
        event = self._create_event(connection_id="nonexistent-connection")

        with patch.dict(
            "os.environ",
            {"JOBS_TABLE_NAME": "test-jobs"},
            clear=True,
        ), patch("handler.boto3.resource") as mock_dynamodb_factory:
            mock_dynamodb = MagicMock()
            mock_dynamodb_factory.return_value = mock_dynamodb
            jobs_table = MagicMock()

            def get_table(name):
                if name == "test-jobs":
                    return jobs_table
                raise ValueError(f"Unexpected table: {name}")

            mock_dynamodb.Table.side_effect = get_table
            jobs_table.query.return_value = {"Items": []}

            response = handler.lambda_handler(event, None)

        self.assertEqual(response["statusCode"], 404)
        payload = json.loads(response["body"])
        self.assertEqual(payload["error"], "NotFound")
        self.assertIn("No active job found for this connection",
                      payload["message"])

    def test_response_headers_are_correct(self) -> None:
        """Test that response headers include Content-Type."""
        event = self._create_event(connection_id="test-connection-456")

        with patch.dict(
            "os.environ",
            {"JOBS_TABLE_NAME": "test-jobs"},
            clear=True,
        ), patch("handler.boto3.resource") as mock_dynamodb_factory:
            mock_dynamodb = MagicMock()
            mock_dynamodb_factory.return_value = mock_dynamodb
            jobs_table = MagicMock()
            mock_dynamodb.Table.return_value = jobs_table
            jobs_table.query.return_value = {
                "Items": [
                    {
                        "jobId": "test-job-123",
                        "connectionId": "test-connection-456",
                    }
                ]
            }

            response = handler.lambda_handler(event, None)

        self.assertIn("headers", response)
        self.assertEqual(response["headers"]["Content-Type"], "application/json")

    @patch("handler.boto3.resource")
    def test_empty_event_handled_gracefully(self, mock_dynamodb_factory) -> None:
        """Test that empty event is handled gracefully."""
        response = handler.lambda_handler({}, None)

        self.assertEqual(response["statusCode"], 400)
        payload = json.loads(response["body"])
        self.assertEqual(payload["error"], "InvalidRequest")


    def test_update_expression_removes_correct_fields(self) -> None:
        """Test that update_item removes connectionId and connectedAt."""
        event = self._create_event(connection_id="test-connection-456")

        with patch.dict(
            "os.environ",
            {"JOBS_TABLE_NAME": "test-jobs"},
            clear=True,
        ), patch("handler.boto3.resource") as mock_dynamodb_factory:
            mock_dynamodb = MagicMock()
            mock_dynamodb_factory.return_value = mock_dynamodb
            jobs_table = MagicMock()
            mock_dynamodb.Table.return_value = jobs_table
            jobs_table.query.return_value = {
                "Items": [
                    {
                        "jobId": "test-job-123",
                        "connectionId": "test-connection-456",
                    }
                ]
            }

            handler.lambda_handler(event, None)

            call_kwargs = jobs_table.update_item.call_args.kwargs
            update_expr = call_kwargs["UpdateExpression"]
            self.assertIn("REMOVE", update_expr)
            self.assertIn("connectionId", update_expr)
            self.assertIn("connectedAt", update_expr)

    @patch("handler.boto3.resource")
    def test_context_parameter_unused(self, mock_dynamodb_factory) -> None:
        """Test that context parameter is accepted but not used."""
        event = self._create_event(connection_id="test-connection-456")

        with patch.dict(
            "os.environ",
            {"JOBS_TABLE_NAME": "test-jobs"},
            clear=True,
        ), patch("handler.boto3.resource") as mock_dynamodb_factory:
            mock_dynamodb = MagicMock()
            mock_dynamodb_factory.return_value = mock_dynamodb
            jobs_table = MagicMock()
            mock_dynamodb.Table.return_value = jobs_table
            jobs_table.query.return_value = {
                "Items": [
                    {
                        "jobId": "test-job-123",
                        "connectionId": "test-connection-456",
                    }
                ]
            }

            mock_context = MagicMock()
            response = handler.lambda_handler(event, mock_context)

            self.assertEqual(response["statusCode"], 200)


    def test_response_helper_builds_correct_structure(self) -> None:
        """Test _response helper builds correct response structure."""
        body = {"message": "test"}
        response = handler._response(200, body)

        self.assertEqual(response["statusCode"], 200)
        self.assertEqual(response["headers"]["Content-Type"], "application/json")
        self.assertEqual(json.loads(response["body"]), body)

    def test_error_response_does_not_expose_internal_details(self) -> None:
        """Test that error responses don't expose AWS/implementation details."""
        event = self._create_event(connection_id="test-connection-456")

        with patch.dict(
            "os.environ",
            {"JOBS_TABLE_NAME": "test-jobs"},
            clear=True,
        ), patch("handler.boto3.resource") as mock_dynamodb_factory:
            mock_dynamodb = MagicMock()
            mock_dynamodb_factory.return_value = mock_dynamodb
            jobs_table = MagicMock()
            mock_dynamodb.Table.return_value = jobs_table
            jobs_table.query.return_value = {
                "Items": [
                    {
                        "jobId": "test-job-123",
                        "connectionId": "test-connection-456",
                    }
                ]
            }
            jobs_table.update_item.side_effect = ClientError(
                {
                    "Error": {
                        "Code": "ValidationException",
                        "Message": "Sensitive AWS error message with table name",
                    }
                },
                "UpdateItem",
            )

            response = handler.lambda_handler(event, None)

        payload = json.loads(response["body"])
        response_body = json.dumps(payload)
        self.assertNotIn("ValidationException", response_body)
        self.assertNotIn("table", response_body.lower())

    def test_dynamodb_query_error_returns_500(self) -> None:
        """Test that DynamoDB query errors return 500."""
        event = self._create_event(connection_id="test-connection-456")

        with patch.dict(
            "os.environ",
            {"JOBS_TABLE_NAME": "test-jobs"},
            clear=True,
        ), patch("handler.boto3.resource") as mock_dynamodb_factory:
            mock_dynamodb = MagicMock()
            mock_dynamodb_factory.return_value = mock_dynamodb
            jobs_table = MagicMock()
            mock_dynamodb.Table.return_value = jobs_table
            jobs_table.query.side_effect = ClientError(
                {"Error": {"Code": "AccessDenied"}}, "Query"
            )

            response = handler.lambda_handler(event, None)

        self.assertEqual(response["statusCode"], 500)
        payload = json.loads(response["body"])
        self.assertEqual(payload["error"], "DatabaseError")
        self.assertIn("Failed to query jobs table", payload["message"])

    def test_job_deleted_between_query_and_update_returns_400(self) -> None:
        """Test that ConditionalCheckFailedException during update returns 400."""
        event = self._create_event(connection_id="test-connection-456")

        with patch.dict(
            "os.environ",
            {"JOBS_TABLE_NAME": "test-jobs"},
            clear=True,
        ), patch("handler.boto3.resource") as mock_dynamodb_factory:
            mock_dynamodb = MagicMock()
            mock_dynamodb_factory.return_value = mock_dynamodb
            jobs_table = MagicMock()
            mock_dynamodb.Table.return_value = jobs_table
            jobs_table.query.return_value = {
                "Items": [
                    {
                        "jobId": "test-job-123",
                        "connectionId": "test-connection-456",
                    }
                ]
            }
            jobs_table.update_item.side_effect = ClientError(
                {"Error": {"Code": "ConditionalCheckFailedException"}},
                "UpdateItem",
            )

            response = handler.lambda_handler(event, None)

        self.assertEqual(response["statusCode"], 400)
        payload = json.loads(response["body"])
        self.assertEqual(payload["error"], "NotFound")
        self.assertIn("Job not found", payload["message"])


class TestFindJobIdByConnectionId(unittest.TestCase):
    """Tests for _find_job_id_by_connection_id helper function."""

    def test_finds_job_id_when_connection_exists(self) -> None:
        """Test successful retrieval of jobId by connectionId."""
        mock_dynamodb = MagicMock()
        mock_table = MagicMock()
        mock_dynamodb.Table.return_value = mock_table

        mock_table.query.return_value = {
            "Items": [
                {
                    "jobId": "found-job-id",
                    "connectionId": "test-connection",
                }
            ]
        }

        result = handler._find_job_id_by_connection_id(
            mock_dynamodb,
            "test-connection",
            "jobs-table",
        )

        self.assertEqual(result, "found-job-id")
        mock_table.query.assert_called_once()

    def test_raises_error_when_connection_not_found(self) -> None:
        """Test that ValueError is raised when connection doesn't exist."""
        mock_dynamodb = MagicMock()
        mock_table = MagicMock()
        mock_dynamodb.Table.return_value = mock_table
        mock_table.query.return_value = {"Items": []}

        with self.assertRaises(ValueError) as context:
            handler._find_job_id_by_connection_id(
                mock_dynamodb,
                "nonexistent-connection",
                "jobs-table",
            )

        self.assertIn("No job found", str(context.exception))

    def test_raises_error_when_multiple_jobs_found(self) -> None:
        """Test that ValueError is raised when multiple jobs match."""
        mock_dynamodb = MagicMock()
        mock_table = MagicMock()
        mock_dynamodb.Table.return_value = mock_table

        mock_table.query.return_value = {
            "Items": [
                {"jobId": "job-1", "connectionId": "dup-conn"},
                {"jobId": "job-2", "connectionId": "dup-conn"},
            ]
        }

        with self.assertRaises(ValueError) as context:
            handler._find_job_id_by_connection_id(
                mock_dynamodb,
                "dup-conn",
                "jobs-table",
            )

        self.assertIn("Multiple jobs found", str(context.exception))

    def test_raises_error_when_job_missing_job_id(self) -> None:
        """Test that ValueError is raised when job record lacks jobId."""
        mock_dynamodb = MagicMock()
        mock_table = MagicMock()
        mock_dynamodb.Table.return_value = mock_table

        mock_table.query.return_value = {
            "Items": [{"connectionId": "test-connection"}]
        }

        with self.assertRaises(ValueError) as context:
            handler._find_job_id_by_connection_id(
                mock_dynamodb,
                "test-connection",
                "jobs-table",
            )

        self.assertIn("jobId is missing", str(context.exception))

    def test_query_uses_correct_key_condition_expression(self) -> None:
        """Test that query is called with correct KeyConditionExpression."""
        mock_dynamodb = MagicMock()
        mock_table = MagicMock()
        mock_dynamodb.Table.return_value = mock_table

        mock_table.query.return_value = {
            "Items": [
                {
                    "jobId": "test-job",
                    "connectionId": "test-conn",
                }
            ]
        }

        handler._find_job_id_by_connection_id(
            mock_dynamodb,
            "test-conn",
            "test-table",
        )

        call_kwargs = mock_table.query.call_args.kwargs
        self.assertEqual(call_kwargs["IndexName"], "connectionId-index")
        self.assertEqual(call_kwargs["KeyConditionExpression"],
                         "connectionId = :conn_id")
        self.assertEqual(
            call_kwargs["ExpressionAttributeValues"],
            {":conn_id": "test-conn"},
        )

    def test_propagates_dynamodb_client_error(self) -> None:
        """Test that ClientError from query is propagated."""
        mock_dynamodb = MagicMock()
        mock_table = MagicMock()
        mock_dynamodb.Table.return_value = mock_table
        mock_table.query.side_effect = ClientError(
            {"Error": {"Code": "AccessDenied"}}, "Query"
        )

        with self.assertRaises(ClientError):
            handler._find_job_id_by_connection_id(
                mock_dynamodb,
                "test-conn",
                "test-table",
            )


class TestRemoveConnectionFromDynamoDB(unittest.TestCase):
    """Tests for _remove_connection_from_dynamodb helper function."""

    def test_remove_connection_calls_update_item(self) -> None:
        """Test that remove_connection_from_dynamodb calls update_item."""
        mock_dynamodb = MagicMock()
        mock_table = MagicMock()
        mock_dynamodb.Table.return_value = mock_table

        handler._remove_connection_from_dynamodb(
            mock_dynamodb,
            "test-job-123",
            "test-jobs-table",
        )

        mock_table.update_item.assert_called_once()

    def test_remove_connection_uses_correct_key(self) -> None:
        """Test that update_item is called with correct jobId key."""
        mock_dynamodb = MagicMock()
        mock_table = MagicMock()
        mock_dynamodb.Table.return_value = mock_table

        handler._remove_connection_from_dynamodb(
            mock_dynamodb,
            "my-job-id",
            "jobs-table",
        )

        call_kwargs = mock_table.update_item.call_args.kwargs
        self.assertEqual(call_kwargs["Key"], {"jobId": "my-job-id"})

    def test_remove_connection_propagates_client_error(self) -> None:
        """Test that ClientError is propagated, not caught."""
        mock_dynamodb = MagicMock()
        mock_table = MagicMock()
        mock_dynamodb.Table.return_value = mock_table
        mock_table.update_item.side_effect = ClientError(
            {"Error": {"Code": "AccessDenied"}}, "UpdateItem"
        )

        with self.assertRaises(ClientError):
            handler._remove_connection_from_dynamodb(
                mock_dynamodb,
                "test-job-123",
                "test-jobs-table",
            )


if __name__ == "__main__":
    unittest.main()
