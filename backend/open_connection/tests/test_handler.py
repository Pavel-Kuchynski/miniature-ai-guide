"""Unit tests for the open_connection handler."""

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


class TestOpenConnectionHandler(unittest.TestCase):
    """Tests for WebSocket connection handler."""

    def _create_event(
        self,
        job_id: str | None = "test-job-123",
        connection_id: str = "test-connection-456",
        sub: str = "cognito-user-123",
        email: str = "user@example.com",
    ) -> dict:
        """Helper to create a WebSocket event.

        Args:
            job_id: Job ID (None to omit from event).
            connection_id: Connection ID.
            sub: Cognito user ID.
            email: User email.

        Returns:
            WebSocket event dict.
        """
        query_params = {}
        if job_id is not None:
            query_params["jobId"] = job_id

        return {
            "requestContext": {
                "connectionId": connection_id,
                "authorizer": {
                    "claims": {
                        "sub": sub,
                        "email": email,
                    }
                },
            },
            "queryStringParameters": query_params or None,
        }

    def test_successful_connection_establishment(self) -> None:
        """Test successful connection with all valid inputs."""
        event = self._create_event()

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
            jobs_table.get_item.return_value = {"Item": {"jobId": "test-job-123"}}

            response = handler.lambda_handler(event, None)

        self.assertEqual(response["statusCode"], 200)
        payload = json.loads(response["body"])
        self.assertEqual(payload["message"], "Connection established successfully")
        jobs_table.get_item.assert_called_once_with(Key={"jobId": "test-job-123"})
        jobs_table.put_item.assert_called_once()

    @patch("handler.boto3.resource")
    def test_missing_job_id_returns_400(self, mock_dynamodb_factory) -> None:
        """Test that missing jobId returns 400 Bad Request."""
        event = self._create_event(job_id=None)

        response = handler.lambda_handler(event, None)

        self.assertEqual(response["statusCode"], 400)
        payload = json.loads(response["body"])
        self.assertEqual(payload["error"], "InvalidRequest")
        self.assertIn("jobId is required", payload["message"])

    @patch("handler.boto3.resource")
    def test_missing_connection_id_returns_400(
        self, mock_dynamodb_factory
    ) -> None:
        """Test that missing connectionId returns 400 Bad Request."""
        event = {
            "requestContext": {
                "authorizer": {
                    "claims": {
                        "sub": "cognito-user",
                        "email": "user@example.com",
                    }
                },
            },
            "queryStringParameters": {"jobId": "test-job-123"},
        }

        response = handler.lambda_handler(event, None)

        self.assertEqual(response["statusCode"], 400)
        payload = json.loads(response["body"])
        self.assertEqual(payload["error"], "InvalidRequest")
        self.assertIn("Connection ID is required", payload["message"])

    @patch("handler.boto3.resource")
    def test_missing_jobs_table_env_var_returns_500(
        self, mock_dynamodb_factory
    ) -> None:
        """Test missing JOBS_TABLE_NAME env var returns 500."""
        event = self._create_event()

        with patch.dict("os.environ", {}, clear=True):
            response = handler.lambda_handler(event, None)

        self.assertEqual(response["statusCode"], 500)
        payload = json.loads(response["body"])
        self.assertEqual(payload["error"], "ServerConfiguration")

    def test_job_does_not_exist_returns_404(self) -> None:
        """Test that non-existent jobId returns 404 Not Found."""
        event = self._create_event(job_id="non-existent-job")

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
            jobs_table.get_item.return_value = {}

            response = handler.lambda_handler(event, None)

        self.assertEqual(response["statusCode"], 404)
        payload = json.loads(response["body"])
        self.assertEqual(payload["error"], "NotFound")
        self.assertIn("jobId does not exist", payload["message"])

    def test_jobs_table_query_error_returns_500(self) -> None:
        """Test that DynamoDB error when querying jobs table returns 500."""
        event = self._create_event()

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
            jobs_table.get_item.side_effect = ClientError(
                error_response={"Error": {"Code": "AccessDenied"}},
                operation_name="GetItem",
            )

            response = handler.lambda_handler(event, None)

        self.assertEqual(response["statusCode"], 500)
        payload = json.loads(response["body"])
        self.assertEqual(payload["error"], "DatabaseError")

    def test_jobs_table_put_error_returns_500(self) -> None:
        """Test that DynamoDB error when storing connection to JOBS table returns 500."""
        event = self._create_event()

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
            jobs_table.get_item.return_value = {"Item": {"jobId": "test-job-123"}}
            jobs_table.put_item.side_effect = ClientError(
                error_response={"Error": {"Code": "ProvisionedThroughputExceededException"}},
                operation_name="PutItem",
            )

            response = handler.lambda_handler(event, None)

        self.assertEqual(response["statusCode"], 500)
        payload = json.loads(response["body"])
        self.assertEqual(payload["error"], "DatabaseError")

    def test_connection_data_stored_with_correct_fields(self) -> None:
        """Test that connection data is stored in JOBS table with correct fields."""
        event = self._create_event(
            job_id="job-abc",
            connection_id="conn-xyz",
            sub="cognito-123",
            email="test@example.com",
        )

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
            jobs_table.get_item.return_value = {"Item": {"jobId": "job-abc"}}

            with patch("handler.datetime") as mock_datetime:
                mock_datetime.now.return_value.timestamp.return_value = 1000.0
                response = handler.lambda_handler(event, None)

        self.assertEqual(response["statusCode"], 200)

        call_args = jobs_table.put_item.call_args
        item = call_args.kwargs["Item"]
        self.assertEqual(item["jobId"], "job-abc")
        self.assertEqual(item["connectionId"], "conn-xyz")
        self.assertEqual(item["connectedAt"], 1000)
        self.assertEqual(item["sub"], "cognito-123")
        self.assertEqual(item["email"], "test@example.com")

    def test_missing_user_info_uses_empty_strings(self) -> None:
        """Test that missing user info (sub, email) defaults to empty strings."""
        event = {
            "requestContext": {
                "connectionId": "conn-123",
                "authorizer": {"claims": {}},
            },
            "queryStringParameters": {"jobId": "job-123"},
        }

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
            jobs_table.get_item.return_value = {"Item": {"jobId": "job-123"}}

            response = handler.lambda_handler(event, None)

        self.assertEqual(response["statusCode"], 200)
        call_args = jobs_table.put_item.call_args
        item = call_args.kwargs["Item"]
        self.assertEqual(item["sub"], "")
        self.assertEqual(item["email"], "")

    @patch("handler.boto3.resource")
    def test_empty_event_handles_gracefully(self, mock_dynamodb_factory) -> None:
        """Test that empty/None event values are handled gracefully."""
        event: dict = {}

        response = handler.lambda_handler(event, None)

        self.assertEqual(response["statusCode"], 400)
        payload = json.loads(response["body"])
        self.assertIn("jobId is required", payload["message"])

    def test_response_has_correct_headers(self) -> None:
        """Test that response includes correct Content-Type header."""
        event = self._create_event()

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
            jobs_table.get_item.return_value = {"Item": {"jobId": "test-job-123"}}

            response = handler.lambda_handler(event, None)

        self.assertEqual(response["headers"]["Content-Type"], "application/json")


class TestEventParsing(unittest.TestCase):
    """Tests for event parsing helper functions."""

    def test_extract_job_id_from_query_params(self) -> None:
        """Test extracting jobId from queryStringParameters."""
        event = {"queryStringParameters": {"jobId": "job-123"}}
        job_id = handler._extract_job_id(event)
        self.assertEqual(job_id, "job-123")

    def test_extract_job_id_returns_none_when_missing(self) -> None:
        """Test that missing jobId returns None."""
        event = {"queryStringParameters": {}}
        job_id = handler._extract_job_id(event)
        self.assertIsNone(job_id)

    def test_extract_job_id_handles_none_query_params(self) -> None:
        """Test extracting jobId when queryStringParameters is None."""
        event = {"queryStringParameters": None}
        job_id = handler._extract_job_id(event)
        self.assertIsNone(job_id)

    def test_extract_connection_id_from_request_context(self) -> None:
        """Test extracting connectionId from requestContext."""
        event = {
            "requestContext": {"connectionId": "conn-456"}
        }
        connection_id = handler._extract_connection_id(event)
        self.assertEqual(connection_id, "conn-456")

    def test_extract_connection_id_returns_none_when_missing(self) -> None:
        """Test that missing connectionId returns None."""
        event = {"requestContext": {}}
        connection_id = handler._extract_connection_id(event)
        self.assertIsNone(connection_id)

    def test_extract_user_info_from_claims(self) -> None:
        """Test extracting user info from authorizer claims."""
        event = {
            "requestContext": {
                "authorizer": {
                    "claims": {
                        "sub": "user-123",
                        "email": "user@example.com",
                    }
                }
            }
        }
        user_info = handler._extract_user_info(event)
        self.assertEqual(user_info["sub"], "user-123")
        self.assertEqual(user_info["email"], "user@example.com")

    def test_extract_user_info_with_missing_fields(self) -> None:
        """Test extracting user info when some fields are missing."""
        event = {
            "requestContext": {
                "authorizer": {"claims": {"sub": "user-123"}}
            }
        }
        user_info = handler._extract_user_info(event)
        self.assertEqual(user_info["sub"], "user-123")
        self.assertEqual(user_info["email"], "")

    def test_extract_user_info_with_empty_claims(self) -> None:
        """Test extracting user info from empty claims."""
        event = {
            "requestContext": {"authorizer": {"claims": {}}}
        }
        user_info = handler._extract_user_info(event)
        self.assertEqual(user_info["sub"], "")
        self.assertEqual(user_info["email"], "")


if __name__ == "__main__":
    unittest.main()
