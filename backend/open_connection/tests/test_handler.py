"""Unit tests for the open_connection handler."""

import json
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from datetime import datetime, timezone

from botocore.exceptions import ClientError

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import handler


class TestOpenConnectionHandler(unittest.TestCase):
    """Tests for WebSocket connection handler."""

    def _create_mock_jwks(self) -> dict:
        """Create a mock JWKS response.

        Returns:
            Mock JWKS dict with RSA public key.
        """
        return {
            "keys": [
                {
                    "alg": "RS256",
                    "e": "AQAB",
                    "kid": "test-key-id-123",
                    "kty": "RSA",
                    "n": "test-modulus",
                    "use": "sig",
                }
            ]
        }

    def _create_cognito_jwt_token(
        self,
        sub: str = "cognito-user-123",
        email: str = "user@example.com",
        token_use: str = "id",
        expired: bool = False,
    ) -> str:
        """Create a mock Cognito JWT token.

        This creates a properly structured JWT with all required Cognito claims.

        Args:
            sub: Subject (user ID) claim.
            email: Email claim.
            token_use: Token use type (should be 'id' for id tokens).
            expired: If True, set exp to past timestamp.

        Returns:
            JWT token string (properly formatted with Cognito claims).
        """
        current_time = int(time.time())
        exp_time = current_time - 3600 if expired else current_time + 3600

        payload = {
            "sub": sub,
            "email": email,
            "iss": "https://cognito-idp.eu-central-1.amazonaws.com/test-pool-id",
            "token_use": token_use,
            "exp": exp_time,
            "iat": current_time,
            "kid": "test-key-id-123",
        }

        # Return a properly formatted token string
        # In tests we'll mock the actual JWT validation
        return json.dumps(payload)

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
            token = self._create_cognito_jwt_token(sub, email)

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
            {
                "JOBS_TABLE_NAME": "test-jobs",
                "COGNITO_REGION": "eu-central-1",
                "COGNITO_USER_POOL_ID": "test-pool-id",
            },
            clear=True,
        ), patch("handler.boto3.resource") as mock_dynamodb_factory, \
            patch("handler._validate_cognito_jwt") as mock_validate_jwt:
            mock_validate_jwt.return_value = {
                "sub": "cognito-user-123",
                "email": "user@example.com",
                "token_use": "id",
                "exp": int(time.time()) + 3600,
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
    @patch("handler._validate_cognito_jwt")
    def test_missing_job_id_returns_400(
        self, mock_validate_jwt, mock_dynamodb_factory
    ) -> None:
        """Test that missing jobId returns 400 Bad Request."""
        event = self._create_event(job_id=None)

        response = handler.lambda_handler(event, None)

        self.assertEqual(response["statusCode"], 400)
        payload = json.loads(response["body"])
        self.assertEqual(payload["error"], "InvalidRequest")
        self.assertIn("jobId is required", payload["message"])

    @patch("handler.boto3.resource")
    @patch("handler._validate_cognito_jwt")
    def test_missing_connection_id_returns_400(
        self, mock_validate_jwt, mock_dynamodb_factory
    ) -> None:
        """Test that missing connectionId returns 400 Bad Request."""
        mock_validate_jwt.return_value = {
            "sub": "cognito-user",
            "email": "user@example.com",
            "token_use": "id",
            "exp": int(time.time()) + 3600,
        }
        event = {
            "requestContext": {},
            "queryStringParameters": {
                "jobId": "test-job-123",
                "token": self._create_cognito_jwt_token(),
            },
            "headers": {},
        }

        response = handler.lambda_handler(event, None)

        self.assertEqual(response["statusCode"], 400)
        payload = json.loads(response["body"])
        self.assertEqual(payload["error"], "InvalidRequest")
        self.assertIn("Connection ID is required", payload["message"])

    @patch("handler.boto3.resource")
    @patch("handler._validate_cognito_jwt")
    def test_missing_jobs_table_env_var_returns_500(
        self, mock_validate_jwt, mock_dynamodb_factory
    ) -> None:
        """Test missing JOBS_TABLE_NAME env var returns 500."""
        mock_validate_jwt.return_value = {
            "sub": "cognito-user-123",
            "email": "user@example.com",
            "token_use": "id",
            "exp": int(time.time()) + 3600,
        }
        event = self._create_event()

        with patch.dict("os.environ", {
            "COGNITO_REGION": "eu-central-1",
            "COGNITO_USER_POOL_ID": "test-pool-id",
        }, clear=True):
            response = handler.lambda_handler(event, None)

        self.assertEqual(response["statusCode"], 500)
        payload = json.loads(response["body"])
        self.assertEqual(payload["error"], "ServerConfiguration")

    def test_job_does_not_exist_returns_404(self) -> None:
        """Test that non-existent jobId returns 404 Not Found."""
        event = self._create_event(job_id="non-existent-job")

        with patch.dict(
            "os.environ",
            {
                "JOBS_TABLE_NAME": "test-jobs",
                "COGNITO_REGION": "eu-central-1",
                "COGNITO_USER_POOL_ID": "test-pool-id",
            },
            clear=True,
        ), patch("handler.boto3.resource") as mock_dynamodb_factory, \
            patch("handler._validate_cognito_jwt") as mock_validate_jwt:
            mock_validate_jwt.return_value = {
                "sub": "cognito-user-123",
                "email": "user@example.com",
                "token_use": "id",
                "exp": int(time.time()) + 3600,
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
            {
                "JOBS_TABLE_NAME": "test-jobs",
                "COGNITO_REGION": "eu-central-1",
                "COGNITO_USER_POOL_ID": "test-pool-id",
            },
            clear=True,
        ), patch("handler.boto3.resource") as mock_dynamodb_factory, \
            patch("handler._validate_cognito_jwt") as mock_validate_jwt:
            mock_validate_jwt.return_value = {
                "sub": "cognito-user-123",
                "email": "user@example.com",
                "token_use": "id",
                "exp": int(time.time()) + 3600,
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
            {
                "JOBS_TABLE_NAME": "test-jobs",
                "COGNITO_REGION": "eu-central-1",
                "COGNITO_USER_POOL_ID": "test-pool-id",
            },
            clear=True,
        ), patch("handler.boto3.resource") as mock_dynamodb_factory, \
            patch("handler._validate_cognito_jwt") as mock_validate_jwt:
            mock_validate_jwt.return_value = {
                "sub": "cognito-user-123",
                "email": "user@example.com",
                "token_use": "id",
                "exp": int(time.time()) + 3600,
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
            {
                "JOBS_TABLE_NAME": "test-jobs",
                "COGNITO_REGION": "eu-central-1",
                "COGNITO_USER_POOL_ID": "test-pool-id",
            },
            clear=True,
        ), patch("handler.boto3.resource") as mock_dynamodb_factory, \
            patch("handler._validate_cognito_jwt") as mock_validate_jwt:
            mock_validate_jwt.return_value = {
                "sub": "cognito-123",
                "email": "test@example.com",
                "token_use": "id",
                "exp": int(time.time()) + 3600,
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
            "queryStringParameters": {"jobId": "job-123", "token": "test-token"},
            "headers": {},
        }

        with patch.dict(
            "os.environ",
            {
                "JOBS_TABLE_NAME": "test-jobs",
                "COGNITO_REGION": "eu-central-1",
                "COGNITO_USER_POOL_ID": "test-pool-id",
            },
            clear=True,
        ), patch("handler.boto3.resource"), \
            patch("handler._validate_cognito_jwt") as mock_validate_jwt:
            mock_validate_jwt.return_value = {"sub": "", "email": ""}

            response = handler.lambda_handler(event, None)

        # Empty 'sub' should result in 401 Unauthorized
        self.assertEqual(response["statusCode"], 401)
        payload = json.loads(response["body"])
        self.assertEqual(payload["error"], "Unauthorized")

    @patch("handler.boto3.resource")
    @patch("handler._validate_cognito_jwt")
    def test_empty_event_handles_gracefully(
        self, mock_validate_jwt, mock_dynamodb_factory
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
            {
                "JOBS_TABLE_NAME": "test-jobs",
                "COGNITO_REGION": "eu-central-1",
                "COGNITO_USER_POOL_ID": "test-pool-id",
            },
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
            {
                "JOBS_TABLE_NAME": "test-jobs",
                "COGNITO_REGION": "eu-central-1",
                "COGNITO_USER_POOL_ID": "test-pool-id",
            },
            clear=True,
        ), patch("handler.boto3.resource"):
            response = handler.lambda_handler(event, None)

        self.assertEqual(response["statusCode"], 401)
        payload = json.loads(response["body"])
        self.assertEqual(payload["error"], "Unauthorized")

    @patch("handler._validate_cognito_jwt")
    def test_jwt_validation_error_returns_401(self, mock_validate_jwt) -> None:
        """Test that JWT validation error returns 401 Unauthorized."""
        mock_validate_jwt.side_effect = handler.CognitoJWTError("Invalid signature")
        event = {
            "requestContext": {"connectionId": "conn-123"},
            "queryStringParameters": {"jobId": "job-123", "token": "invalid-token"},
            "headers": {},
        }

        with patch.dict(
            "os.environ",
            {
                "JOBS_TABLE_NAME": "test-jobs",
                "COGNITO_REGION": "eu-central-1",
                "COGNITO_USER_POOL_ID": "test-pool-id",
            },
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
            {
                "JOBS_TABLE_NAME": "test-jobs",
                "COGNITO_REGION": "eu-central-1",
                "COGNITO_USER_POOL_ID": "test-pool-id",
            },
            clear=True,
        ), patch("handler.boto3.resource") as mock_dynamodb_factory, \
            patch("handler._validate_cognito_jwt") as mock_validate_jwt:
            mock_validate_jwt.return_value = {
                "sub": "cognito-user-123",
                "email": "user@example.com",
                "token_use": "id",
                "exp": int(time.time()) + 3600,
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
        with self.assertRaises(handler.CognitoJWTError) as ctx:
            handler._extract_jwt_from_query_params(event)
        self.assertIn("Missing token query parameter", str(ctx.exception))

    def test_extract_jwt_from_query_params_empty_token(self) -> None:
        """Test extracting JWT when token query parameter is empty."""
        event = {
            "queryStringParameters": {
                "token": "",
            }
        }
        with self.assertRaises(handler.CognitoJWTError) as ctx:
            handler._extract_jwt_from_query_params(event)
        self.assertIn("Missing token query parameter", str(ctx.exception))

    def test_extract_jwt_from_query_params_missing_query_params_key(self) -> None:
        """Test extracting JWT when queryStringParameters key is missing."""
        event = {}
        with self.assertRaises(handler.CognitoJWTError) as ctx:
            handler._extract_jwt_from_query_params(event)
        self.assertIn("Missing token query parameter", str(ctx.exception))


class TestCognitoJWTValidation(unittest.TestCase):
    """Tests for Cognito JWT validation functions."""

    def test_extract_user_info_with_valid_cognito_token(self) -> None:
        """Test extracting user info from valid Cognito token."""
        event = {
            "queryStringParameters": {"token": "test-token"},
        }

        with patch.dict(
            "os.environ",
            {
                "COGNITO_REGION": "eu-central-1",
                "COGNITO_USER_POOL_ID": "test-pool-id",
            },
            clear=True,
        ), patch("handler._validate_cognito_jwt") as mock_validate:
            mock_validate.return_value = {
                "sub": "user-123",
                "email": "user@example.com",
                "token_use": "id",
                "exp": int(time.time()) + 3600,
            }
            user_info = handler._extract_user_info(event)

        self.assertEqual(user_info["userId"], "user-123")
        self.assertEqual(user_info["email"], "user@example.com")

    def test_extract_user_info_with_missing_email(self) -> None:
        """Test extracting user info when email claim is missing."""
        event = {
            "queryStringParameters": {"token": "test-token"},
        }

        with patch.dict(
            "os.environ",
            {
                "COGNITO_REGION": "eu-central-1",
                "COGNITO_USER_POOL_ID": "test-pool-id",
            },
            clear=True,
        ), patch("handler._validate_cognito_jwt") as mock_validate:
            mock_validate.return_value = {"sub": "user-123"}
            user_info = handler._extract_user_info(event)

        self.assertEqual(user_info["userId"], "user-123")
        self.assertEqual(user_info["email"], "")

    def test_extract_user_info_missing_jwt_raises_error(self) -> None:
        """Test extracting user info when JWT is missing."""
        event = {"queryStringParameters": {}}
        with patch.dict(
            "os.environ",
            {
                "COGNITO_REGION": "eu-central-1",
                "COGNITO_USER_POOL_ID": "test-pool-id",
            },
            clear=True,
        ):
            with self.assertRaises(handler.CognitoJWTError):
                handler._extract_user_info(event)

    def test_extract_user_info_requires_sub_claim(self) -> None:
        """Test that 'sub' claim is required in JWT."""
        event = {
            "queryStringParameters": {"token": "test-token"},
        }

        with patch.dict(
            "os.environ",
            {
                "COGNITO_REGION": "eu-central-1",
                "COGNITO_USER_POOL_ID": "test-pool-id",
            },
            clear=True,
        ), patch("handler._validate_cognito_jwt") as mock_validate:
            mock_validate.return_value = {"email": "user@example.com"}
            with self.assertRaises(handler.CognitoJWTError) as ctx:
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

        with patch.dict(
            "os.environ",
            {
                "COGNITO_REGION": "eu-central-1",
                "COGNITO_USER_POOL_ID": "test-pool-id",
            },
            clear=True,
        ), patch("handler._validate_cognito_jwt") as mock_validate:
            mock_validate.return_value = {"sub": "", "email": "user@example.com"}
            with self.assertRaises(handler.CognitoJWTError) as ctx:
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

        with patch.dict(
            "os.environ",
            {
                "COGNITO_REGION": "eu-central-1",
                "COGNITO_USER_POOL_ID": "test-pool-id",
            },
            clear=True,
        ), patch("handler._validate_cognito_jwt") as mock_validate:
            mock_validate.return_value = {"sub": "   ", "email": "user@example.com"}
            with self.assertRaises(handler.CognitoJWTError) as ctx:
                handler._extract_user_info(event)
            self.assertIn(
                "'sub' claim (user ID) is required and cannot be empty",
                str(ctx.exception),
            )

    def test_extract_user_info_missing_cognito_region_env_var(self) -> None:
        """Test that missing COGNITO_REGION env var raises error."""
        event = {
            "queryStringParameters": {"token": "test-token"},
        }

        with patch.dict(
            "os.environ",
            {"COGNITO_USER_POOL_ID": "test-pool-id"},
            clear=True,
        ):
            with self.assertRaises(handler.CognitoJWTError) as ctx:
                handler._extract_user_info(event)
            self.assertIn("Missing COGNITO_REGION", str(ctx.exception))

    def test_extract_user_info_missing_cognito_user_pool_id_env_var(self) -> None:
        """Test that missing COGNITO_USER_POOL_ID env var raises error."""
        event = {
            "queryStringParameters": {"token": "test-token"},
        }

        with patch.dict(
            "os.environ",
            {"COGNITO_REGION": "eu-central-1"},
            clear=True,
        ):
            with self.assertRaises(handler.CognitoJWTError) as ctx:
                handler._extract_user_info(event)
            self.assertIn("Missing COGNITO_USER_POOL_ID", str(ctx.exception))

    @patch("handler.requests.get")
    def test_fetch_cognito_jwks_success(self, mock_get) -> None:
        """Test successfully fetching JWKS from Cognito endpoint."""
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "keys": [
                {
                    "kid": "test-key-id",
                    "kty": "RSA",
                    "n": "test-modulus",
                    "e": "AQAB",
                }
            ]
        }
        mock_get.return_value = mock_response

        # Clear cache before test
        handler._JWKS_CACHE.clear()

        jwks = handler._fetch_cognito_jwks("eu-central-1", "test-pool-id")

        self.assertIn("keys", jwks)
        self.assertEqual(len(jwks["keys"]), 1)
        mock_get.assert_called_once_with(
            "https://cognito-idp.eu-central-1.amazonaws.com/test-pool-id/.well-known/jwks.json",
            timeout=5,
        )

    @patch("handler.requests.get")
    def test_fetch_cognito_jwks_caching(self, mock_get) -> None:
        """Test that JWKS is cached and not fetched again within TTL."""
        mock_response = MagicMock()
        mock_response.json.return_value = {"keys": [{"kid": "test-key-id"}]}
        mock_get.return_value = mock_response

        # Clear cache before test
        handler._JWKS_CACHE.clear()
        handler._JWKS_CACHE_TIME = 0

        # First call should fetch
        handler._fetch_cognito_jwks("eu-central-1", "test-pool-id")
        self.assertEqual(mock_get.call_count, 1)

        # Second call should use cache
        handler._fetch_cognito_jwks("eu-central-1", "test-pool-id")
        self.assertEqual(mock_get.call_count, 1)

    @patch("handler.requests.get")
    def test_fetch_cognito_jwks_network_error(self, mock_get) -> None:
        """Test that network error when fetching JWKS raises CognitoJWTError."""
        import requests
        mock_get.side_effect = requests.RequestException("Connection failed")

        handler._JWKS_CACHE.clear()

        with self.assertRaises(handler.CognitoJWTError) as ctx:
            handler._fetch_cognito_jwks("eu-central-1", "test-pool-id")
        self.assertIn("Failed to fetch Cognito JWKS", str(ctx.exception))

    @patch("handler._fetch_cognito_jwks")
    @patch("handler.jwt.get_unverified_header")
    def test_get_cognito_public_key_success(self, mock_header, mock_fetch_jwks) -> None:
        """Test successfully getting public key from JWKS."""
        mock_header.return_value = {"kid": "test-key-id"}
        mock_fetch_jwks.return_value = {
            "keys": [
                {
                    "kid": "test-key-id",
                    "kty": "RSA",
                    "n": "test-modulus",
                    "e": "AQAB",
                }
            ]
        }

        key = handler._get_cognito_public_key("test-token", "eu-central-1", "test-pool-id")

        self.assertEqual(key["kid"], "test-key-id")

    @patch("handler._fetch_cognito_jwks")
    @patch("handler.jwt.get_unverified_header")
    def test_get_cognito_public_key_not_found(self, mock_header, mock_fetch_jwks) -> None:
        """Test error when key with matching kid is not found in JWKS."""
        mock_header.return_value = {"kid": "unknown-key-id"}
        mock_fetch_jwks.return_value = {
            "keys": [
                {
                    "kid": "test-key-id",
                    "kty": "RSA",
                    "n": "test-modulus",
                    "e": "AQAB",
                }
            ]
        }

        with self.assertRaises(handler.CognitoJWTError) as ctx:
            handler._get_cognito_public_key("test-token", "eu-central-1", "test-pool-id")
        self.assertIn("No matching key found", str(ctx.exception))

    @patch("handler._fetch_cognito_jwks")
    @patch("handler.jwt.decode")
    @patch("handler.jwt.get_unverified_header")
    def test_validate_cognito_jwt_success(self, mock_header, mock_decode, mock_fetch_jwks) -> None:
        """Test successfully validating a Cognito JWT token."""
        mock_header.return_value = {"kid": "test-key-id"}
        mock_fetch_jwks.return_value = {
            "keys": [
                {
                    "kid": "test-key-id",
                    "kty": "RSA",
                    "n": "test-modulus",
                    "e": "AQAB",
                }
            ]
        }
        current_time = int(time.time())
        mock_decode.return_value = {
            "sub": "user-123",
            "email": "user@example.com",
            "iss": "https://cognito-idp.eu-central-1.amazonaws.com/test-pool-id",
            "token_use": "id",
            "exp": current_time + 3600,
        }

        claims = handler._validate_cognito_jwt(
            "test-token",
            "eu-central-1",
            "test-pool-id",
        )

        self.assertEqual(claims["sub"], "user-123")
        self.assertEqual(claims["token_use"], "id")

    @patch("handler._fetch_cognito_jwks")
    @patch("handler.jwt.decode")
    @patch("handler.jwt.get_unverified_header")
    def test_validate_cognito_jwt_expired_token(self, mock_header, mock_decode, mock_fetch_jwks) -> None:
        """Test that expired token is rejected."""
        mock_header.return_value = {"kid": "test-key-id"}
        mock_fetch_jwks.return_value = {
            "keys": [
                {
                    "kid": "test-key-id",
                    "kty": "RSA",
                    "n": "test-modulus",
                    "e": "AQAB",
                }
            ]
        }
        current_time = int(time.time())
        mock_decode.return_value = {
            "sub": "user-123",
            "email": "user@example.com",
            "iss": "https://cognito-idp.eu-central-1.amazonaws.com/test-pool-id",
            "token_use": "id",
            "exp": current_time - 3600,  # Expired
        }

        with self.assertRaises(handler.CognitoJWTError) as ctx:
            handler._validate_cognito_jwt(
                "test-token",
                "eu-central-1",
                "test-pool-id",
            )
        self.assertIn("Token expired", str(ctx.exception))

    @patch("handler._fetch_cognito_jwks")
    @patch("handler.jwt.decode")
    @patch("handler.jwt.get_unverified_header")
    def test_validate_cognito_jwt_invalid_issuer(self, mock_header, mock_decode, mock_fetch_jwks) -> None:
        """Test that token with invalid issuer is rejected."""
        mock_header.return_value = {"kid": "test-key-id"}
        mock_fetch_jwks.return_value = {
            "keys": [
                {
                    "kid": "test-key-id",
                    "kty": "RSA",
                    "n": "test-modulus",
                    "e": "AQAB",
                }
            ]
        }
        current_time = int(time.time())
        mock_decode.return_value = {
            "sub": "user-123",
            "email": "user@example.com",
            "iss": "https://cognito-idp.us-east-1.amazonaws.com/other-pool-id",
            "token_use": "id",
            "exp": current_time + 3600,
        }

        with self.assertRaises(handler.CognitoJWTError) as ctx:
            handler._validate_cognito_jwt(
                "test-token",
                "eu-central-1",
                "test-pool-id",
            )
        self.assertIn("Invalid issuer", str(ctx.exception))

    @patch("handler._fetch_cognito_jwks")
    @patch("handler.jwt.decode")
    @patch("handler.jwt.get_unverified_header")
    def test_validate_cognito_jwt_invalid_token_type(self, mock_header, mock_decode, mock_fetch_jwks) -> None:
        """Test that token with invalid token_use is rejected."""
        mock_header.return_value = {"kid": "test-key-id"}
        mock_fetch_jwks.return_value = {
            "keys": [
                {
                    "kid": "test-key-id",
                    "kty": "RSA",
                    "n": "test-modulus",
                    "e": "AQAB",
                }
            ]
        }
        current_time = int(time.time())
        mock_decode.return_value = {
            "sub": "user-123",
            "email": "user@example.com",
            "iss": "https://cognito-idp.eu-central-1.amazonaws.com/test-pool-id",
            "token_use": "access",  # Invalid, should be 'id'
            "exp": current_time + 3600,
        }

        with self.assertRaises(handler.CognitoJWTError) as ctx:
            handler._validate_cognito_jwt(
                "test-token",
                "eu-central-1",
                "test-pool-id",
            )
        self.assertIn("Invalid token type", str(ctx.exception))

    @patch("handler._fetch_cognito_jwks")
    @patch("handler.jwt.decode")
    @patch("handler.jwt.get_unverified_header")
    def test_validate_cognito_jwt_missing_exp(self, mock_header, mock_decode, mock_fetch_jwks) -> None:
        """Test that token without exp claim is rejected."""
        mock_header.return_value = {"kid": "test-key-id"}
        mock_fetch_jwks.return_value = {
            "keys": [
                {
                    "kid": "test-key-id",
                    "kty": "RSA",
                    "n": "test-modulus",
                    "e": "AQAB",
                }
            ]
        }
        mock_decode.return_value = {
            "sub": "user-123",
            "email": "user@example.com",
            "iss": "https://cognito-idp.eu-central-1.amazonaws.com/test-pool-id",
            "token_use": "id",
            # Missing exp claim
        }

        with self.assertRaises(handler.CognitoJWTError) as ctx:
            handler._validate_cognito_jwt(
                "test-token",
                "eu-central-1",
                "test-pool-id",
            )
        self.assertIn("Missing 'exp'", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
