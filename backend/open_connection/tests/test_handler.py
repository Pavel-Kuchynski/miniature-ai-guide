"""Unit tests for the open_connection handler."""

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from botocore.exceptions import ClientError
import jwt

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import handler


class TestOpenConnectionHandler(unittest.TestCase):
    """Tests for WebSocket connection handler."""

    def _create_jwt_token(
        self, sub: str = "cognito-user-123", email: str = "user@example.com"
    ) -> str:
        """Helper to create a valid JWT token.

        Args:
            sub: Subject (user ID) claim.
            email: Email claim.

        Returns:
            JWT token string.
        """
        payload = {"sub": sub, "email": email}
        return jwt.encode(payload, "test-secret", algorithm="HS256")

    def _create_event(
        self,
        job_id: str | None = "test-job-123",
        connection_id: str = "test-connection-456",
        sub: str = "cognito-user-123",
        email: str = "user@example.com",
        token: str | None = None,
    ) -> dict:
        """Helper to create a WebSocket event.

        Args:
            job_id: Job ID (None to omit from event).
            connection_id: Connection ID.
            sub: Cognito user ID.
            email: User email.
            token: JWT token (auto-generated if None).

        Returns:
            WebSocket event dict.
        """
        query_params = {}
        if job_id is not None:
            query_params["jobId"] = job_id

        if token is None:
            token = self._create_jwt_token(sub, email)

        query_params["token"] = token

        return {
            "requestContext": {
                "connectionId": connection_id,
            },
            "queryStringParameters": query_params or None,
            "headers": {},
        }

    def test_successful_connection_establishment(self) -> None:
        """Test successful connection with all valid inputs."""
        event = self._create_event()

        with patch.dict(
            "os.environ",
            {"JOBS_TABLE_NAME": "test-jobs"},
            clear=True,
        ), patch("handler.boto3.resource") as mock_dynamodb_factory, \
            patch("handler.jwt.decode") as mock_jwt_decode:
            mock_jwt_decode.return_value = {
                "sub": "cognito-user-123",
                "email": "user@example.com",
            }
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
        jobs_table.update_item.assert_called_once()

    @patch("handler.boto3.resource")
    @patch("handler.jwt.decode")
    def test_missing_job_id_returns_400(
        self, mock_jwt_decode, mock_dynamodb_factory
    ) -> None:
        """Test that missing jobId returns 400 Bad Request."""
        event = self._create_event(job_id=None)

        response = handler.lambda_handler(event, None)

        self.assertEqual(response["statusCode"], 400)
        payload = json.loads(response["body"])
        self.assertEqual(payload["error"], "InvalidRequest")
        self.assertIn("jobId is required", payload["message"])

    @patch("handler.boto3.resource")
    @patch("handler.jwt.decode")
    def test_missing_connection_id_returns_400(
        self, mock_jwt_decode, mock_dynamodb_factory
    ) -> None:
        """Test that missing connectionId returns 400 Bad Request."""
        mock_jwt_decode.return_value = {
            "sub": "cognito-user",
            "email": "user@example.com",
        }
        event = {
            "requestContext": {},
            "queryStringParameters": {"jobId": "test-job-123", "token": self._create_jwt_token()},
            "headers": {},
        }

        response = handler.lambda_handler(event, None)

        self.assertEqual(response["statusCode"], 400)
        payload = json.loads(response["body"])
        self.assertEqual(payload["error"], "InvalidRequest")
        self.assertIn("Connection ID is required", payload["message"])

    @patch("handler.boto3.resource")
    @patch("handler.jwt.decode")
    def test_missing_jobs_table_env_var_returns_500(
        self, mock_jwt_decode, mock_dynamodb_factory
    ) -> None:
        """Test missing JOBS_TABLE_NAME env var returns 500."""
        mock_jwt_decode.return_value = {
            "sub": "cognito-user-123",
            "email": "user@example.com",
        }
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
        ), patch("handler.boto3.resource") as mock_dynamodb_factory, \
            patch("handler.jwt.decode") as mock_jwt_decode:
            mock_jwt_decode.return_value = {
                "sub": "cognito-user-123",
                "email": "user@example.com",
            }
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
        ), patch("handler.boto3.resource") as mock_dynamodb_factory, \
            patch("handler.jwt.decode") as mock_jwt_decode:
            mock_jwt_decode.return_value = {
                "sub": "cognito-user-123",
                "email": "user@example.com",
            }
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
        ), patch("handler.boto3.resource") as mock_dynamodb_factory, \
            patch("handler.jwt.decode") as mock_jwt_decode:
            mock_jwt_decode.return_value = {
                "sub": "cognito-user-123",
                "email": "user@example.com",
            }
            mock_dynamodb = MagicMock()
            mock_dynamodb_factory.return_value = mock_dynamodb
            jobs_table = MagicMock()

            def get_table(name):
                if name == "test-jobs":
                    return jobs_table
                raise ValueError(f"Unexpected table: {name}")

            mock_dynamodb.Table.side_effect = get_table
            jobs_table.get_item.return_value = {"Item": {"jobId": "test-job-123"}}
            jobs_table.update_item.side_effect = ClientError(
                error_response={"Error": {"Code": "ProvisionedThroughputExceededException"}},
                operation_name="UpdateItem",
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
        ), patch("handler.boto3.resource") as mock_dynamodb_factory, \
            patch("handler.jwt.decode") as mock_jwt_decode:
            mock_jwt_decode.return_value = {
                "sub": "cognito-123",
                "email": "test@example.com",
            }
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

        call_args = jobs_table.update_item.call_args
        self.assertEqual(call_args.kwargs["Key"], {"jobId": "job-abc"})
        self.assertEqual(
            call_args.kwargs["UpdateExpression"],
            "SET connectionId = :connectionId, connectedAt = :connectedAt, userId = :userId, email = :email",
        )

        attrs = call_args.kwargs["ExpressionAttributeValues"]
        self.assertEqual(attrs[":connectionId"], "conn-xyz")
        self.assertEqual(attrs[":connectedAt"], 1000)
        self.assertEqual(attrs[":userId"], "cognito-123")
        self.assertEqual(attrs[":email"], "test@example.com")

    def test_missing_user_info_requires_valid_sub(self) -> None:
        """Test that empty 'sub' claim in JWT is rejected with 401."""
        event = {
            "requestContext": {
                "connectionId": "conn-123",
            },
            "queryStringParameters": {"jobId": "job-123"},
            "headers": {
                "Authorization": f"Bearer {self._create_jwt_token(sub='', email='')}",
            },
        }

        with patch.dict(
            "os.environ",
            {"JOBS_TABLE_NAME": "test-jobs"},
            clear=True,
        ), patch("handler.boto3.resource") as mock_dynamodb_factory, \
            patch("handler.jwt.decode") as mock_jwt_decode:
            mock_jwt_decode.return_value = {"sub": "", "email": ""}
            mock_dynamodb = MagicMock()
            mock_dynamodb_factory.return_value = mock_dynamodb

            response = handler.lambda_handler(event, None)

        # Empty 'sub' should result in 401 Unauthorized
        self.assertEqual(response["statusCode"], 401)
        payload = json.loads(response["body"])
        self.assertEqual(payload["error"], "Unauthorized")

    @patch("handler.boto3.resource")
    @patch("handler.jwt.decode")
    def test_empty_event_handles_gracefully(
        self, mock_jwt_decode, mock_dynamodb_factory
    ) -> None:
        """Test that empty/None event values are handled gracefully."""
        event: dict = {}

        response = handler.lambda_handler(event, None)

        self.assertEqual(response["statusCode"], 400)
        payload = json.loads(response["body"])
        self.assertIn("jobId is required", payload["message"])

    def test_missing_jwt_returns_401(self) -> None:
        """Test that missing JWT token returns 401 Unauthorized."""
        event = {
            "requestContext": {"connectionId": "conn-123"},
            "queryStringParameters": {"jobId": "job-123"},
            "headers": {},
        }

        with patch.dict(
            "os.environ",
            {"JOBS_TABLE_NAME": "test-jobs"},
            clear=True,
        ), patch("handler.boto3.resource"):
            response = handler.lambda_handler(event, None)

        self.assertEqual(response["statusCode"], 401)
        payload = json.loads(response["body"])
        self.assertEqual(payload["error"], "Unauthorized")
        self.assertIn("Invalid or missing JWT token", payload["message"])

    def test_empty_jwt_query_param_returns_401(self) -> None:
        """Test that empty token query parameter returns 401 Unauthorized."""
        event = {
            "requestContext": {"connectionId": "conn-123"},
            "queryStringParameters": {"jobId": "job-123", "token": ""},
            "headers": {},
        }

        with patch.dict(
            "os.environ",
            {"JOBS_TABLE_NAME": "test-jobs"},
            clear=True,
        ), patch("handler.boto3.resource"):
            response = handler.lambda_handler(event, None)

        self.assertEqual(response["statusCode"], 401)
        payload = json.loads(response["body"])
        self.assertEqual(payload["error"], "Unauthorized")

    @patch("handler.jwt.decode")
    def test_jwt_decode_error_returns_401(self, mock_jwt_decode) -> None:
        """Test that JWT decoding error returns 401 Unauthorized."""
        mock_jwt_decode.side_effect = handler.JWTError("Invalid signature")
        event = {
            "requestContext": {"connectionId": "conn-123"},
            "queryStringParameters": {"jobId": "job-123"},
            "headers": {"Authorization": "Bearer invalid-token"},
        }

        with patch.dict(
            "os.environ",
            {"JOBS_TABLE_NAME": "test-jobs"},
            clear=True,
        ), patch("handler.boto3.resource"):
            response = handler.lambda_handler(event, None)

        self.assertEqual(response["statusCode"], 401)
        payload = json.loads(response["body"])
        self.assertEqual(payload["error"], "Unauthorized")

    def test_response_has_correct_headers(self) -> None:
        """Test that response includes correct Content-Type header."""
        event = self._create_event()

        with patch.dict(
            "os.environ",
            {"JOBS_TABLE_NAME": "test-jobs"},
            clear=True,
        ), patch("handler.boto3.resource") as mock_dynamodb_factory, \
            patch("handler.jwt.decode") as mock_jwt_decode:
            mock_jwt_decode.return_value = {
                "sub": "cognito-user-123",
                "email": "user@example.com",
            }
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

    def test_extract_jwt_from_query_params_success(self) -> None:
        """Test extracting JWT from query parameters."""
        token = "test-token-abc123"
        event = {
            "queryStringParameters": {
                "token": token,
            }
        }
        extracted = handler._extract_jwt_from_query_params(event)
        self.assertEqual(extracted, token)

    def test_extract_jwt_from_query_params_missing_param(self) -> None:
        """Test extracting JWT when token query parameter is missing."""
        event = {"queryStringParameters": {}}
        with self.assertRaises(handler.JWTError) as ctx:
            handler._extract_jwt_from_query_params(event)
        self.assertIn("Missing token query parameter", str(ctx.exception))

    def test_extract_jwt_from_query_params_empty_token(self) -> None:
        """Test extracting JWT when token query parameter is empty."""
        event = {
            "queryStringParameters": {
                "token": "",
            }
        }
        with self.assertRaises(handler.JWTError) as ctx:
            handler._extract_jwt_from_query_params(event)
        self.assertIn("Missing token query parameter", str(ctx.exception))

    def test_extract_jwt_from_query_params_missing_query_params_key(self) -> None:
        """Test extracting JWT when queryStringParameters key is missing."""
        event = {}
        with self.assertRaises(handler.JWTError) as ctx:
            handler._extract_jwt_from_query_params(event)
        self.assertIn("Missing token query parameter", str(ctx.exception))

    def test_decode_jwt_token_success(self) -> None:
        """Test decoding a valid JWT token."""
        payload = {"sub": "user-123", "email": "user@example.com"}
        token = jwt.encode(payload, "test-secret", algorithm="HS256")
        decoded = handler._decode_jwt_token(token, "test-secret")
        self.assertEqual(decoded["sub"], "user-123")
        self.assertEqual(decoded["email"], "user@example.com")

    def test_decode_jwt_token_without_verification(self) -> None:
        """Test decoding JWT without signature verification."""
        payload = {"sub": "user-123", "email": "user@example.com"}
        token = jwt.encode(payload, "other-secret", algorithm="HS256")
        decoded = handler._decode_jwt_token(token, None)
        self.assertEqual(decoded["sub"], "user-123")
        self.assertEqual(decoded["email"], "user@example.com")

    def test_decode_jwt_token_invalid_signature(self) -> None:
        """Test decoding JWT with invalid signature."""
        payload = {"sub": "user-123"}
        token = jwt.encode(payload, "secret1", algorithm="HS256")
        with self.assertRaises(handler.JWTError) as ctx:
            handler._decode_jwt_token(token, "secret2")
        self.assertIn("Invalid JWT token", str(ctx.exception))

    def test_decode_jwt_token_malformed(self) -> None:
        """Test decoding malformed JWT token."""
        with self.assertRaises(handler.JWTError) as ctx:
            handler._decode_jwt_token("not-a-jwt-token", "secret")
        self.assertIn("Invalid JWT token", str(ctx.exception))

    def test_extract_user_info_from_jwt(self) -> None:
        """Test extracting user info from JWT in query parameters."""
        token = jwt.encode(
            {"sub": "user-123", "email": "user@example.com"},
            "test-secret",
            algorithm="HS256",
        )
        event = {
            "queryStringParameters": {"token": token},
        }

        with patch.dict("os.environ", {"JWT_SECRET_KEY": "test-secret"}, clear=True), \
            patch("handler.jwt.decode") as mock_decode:
            mock_decode.return_value = {
                "sub": "user-123",
                "email": "user@example.com",
            }
            user_info = handler._extract_user_info(event)

        self.assertEqual(user_info["userId"], "user-123")
        self.assertEqual(user_info["email"], "user@example.com")

    def test_extract_user_info_with_missing_email(self) -> None:
        """Test extracting user info when email claim is missing."""
        event = {
            "queryStringParameters": {"token": "test-token"},
        }

        with patch("handler.jwt.decode") as mock_decode, \
            patch("handler.jwt.get_unverified_header") as mock_header:
            mock_header.return_value = {"alg": "HS256"}
            mock_decode.return_value = {"sub": "user-123"}
            user_info = handler._extract_user_info(event)

        self.assertEqual(user_info["userId"], "user-123")
        self.assertEqual(user_info["email"], "")

    def test_extract_user_info_missing_jwt_raises_error(self) -> None:
        """Test extracting user info when JWT is missing."""
        event = {"queryStringParameters": {}}
        with self.assertRaises(handler.JWTError):
            handler._extract_user_info(event)

    def test_decode_jwt_token_requires_secret_in_production(self) -> None:
        """Test that JWT_SECRET_KEY is required in production mode."""
        payload = {"sub": "user-123", "email": "user@example.com"}
        token = jwt.encode(payload, "test-secret", algorithm="HS256")

        with patch.dict("os.environ", {"ENVIRONMENT": "production"}, clear=True):
            with self.assertRaises(handler.JWTError) as ctx:
                handler._decode_jwt_token(token, None)
            self.assertIn(
                "JWT_SECRET_KEY environment variable is required in production",
                str(ctx.exception),
            )

    def test_decode_jwt_token_logs_warning_in_development(self) -> None:
        """Test that missing JWT_SECRET_KEY logs warning in development."""
        payload = {"sub": "user-123", "email": "user@example.com"}
        token = jwt.encode(payload, "test-secret", algorithm="HS256")

        with patch.dict("os.environ", {"ENVIRONMENT": "development"}, clear=True), \
            patch("handler.logger") as mock_logger:
            decoded = handler._decode_jwt_token(token, None)
            self.assertEqual(decoded["sub"], "user-123")
            mock_logger.warning.assert_called_once()

    def test_decode_jwt_token_rs256(self) -> None:
        """Test decoding RS256 (asymmetric) JWT token."""
        # For testing RS256, we use HS256 but set the header alg to RS256
        # since we're testing algorithm extraction from header
        payload = {"sub": "user-123", "email": "user@example.com"}
        # Create HS256 token but test that algorithm is extracted from header
        token = jwt.encode(payload, "test-secret", algorithm="HS256")

        with patch("handler.jwt.get_unverified_header") as mock_header:
            mock_header.return_value = {"alg": "RS256"}
            with patch("handler.jwt.decode") as mock_decode:
                mock_decode.return_value = payload
                decoded = handler._decode_jwt_token(token, "test-secret")
                # Verify RS256 was used (not HS256)
                mock_decode.assert_called_once()
                call_args = mock_decode.call_args
                self.assertEqual(call_args[1]["algorithms"], ["RS256"])

    def test_extract_user_info_requires_sub_claim(self) -> None:
        """Test that 'sub' claim is required in JWT."""
        event = {
            "queryStringParameters": {"token": "test-token"},
        }

        with patch("handler.jwt.decode") as mock_decode, \
            patch("handler.jwt.get_unverified_header") as mock_header:
            mock_header.return_value = {"alg": "HS256"}
            mock_decode.return_value = {"email": "user@example.com"}
            with self.assertRaises(handler.JWTError) as ctx:
                handler._extract_user_info(event)
            self.assertIn(
                "'sub' claim (user ID) is required and cannot be empty",
                str(ctx.exception),
            )

    def test_extract_user_info_rejects_empty_sub_claim(self) -> None:
        """Test that empty 'sub' claim is rejected."""
        event = {
            "queryStringParameters": {"token": "test-token"},
        }

        with patch("handler.jwt.decode") as mock_decode, \
            patch("handler.jwt.get_unverified_header") as mock_header:
            mock_header.return_value = {"alg": "HS256"}
            mock_decode.return_value = {"sub": "", "email": "user@example.com"}
            with self.assertRaises(handler.JWTError) as ctx:
                handler._extract_user_info(event)
            self.assertIn(
                "'sub' claim (user ID) is required and cannot be empty",
                str(ctx.exception),
            )

    def test_extract_user_info_rejects_whitespace_only_sub(self) -> None:
        """Test that whitespace-only 'sub' claim is rejected."""
        event = {
            "queryStringParameters": {"token": "test-token"},
        }

        with patch("handler.jwt.decode") as mock_decode, \
            patch("handler.jwt.get_unverified_header") as mock_header:
            mock_header.return_value = {"alg": "HS256"}
            mock_decode.return_value = {"sub": "   ", "email": "user@example.com"}
            with self.assertRaises(handler.JWTError) as ctx:
                handler._extract_user_info(event)
            self.assertIn(
                "'sub' claim (user ID) is required and cannot be empty",
                str(ctx.exception),
            )

    def test_decode_jwt_token_without_verification_no_secret(self) -> None:
        """Test that forged tokens are accepted when verification is skipped."""
        payload = {"sub": "attacker", "email": "attacker@example.com"}
        token = jwt.encode(payload, "attacker-secret", algorithm="HS256")

        # In development mode with no JWT_SECRET_KEY, forged tokens are accepted
        with patch.dict("os.environ", {"ENVIRONMENT": "development"}, clear=True), \
            patch("handler.logger"):
            decoded = handler._decode_jwt_token(token, None)
            # Token is accepted without verification
            self.assertEqual(decoded["sub"], "attacker")
            self.assertEqual(decoded["email"], "attacker@example.com")


if __name__ == "__main__":
    unittest.main()
