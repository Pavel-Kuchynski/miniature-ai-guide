"""Unit tests for the open_connection handler.

Tokens are real RS256 JWTs signed with throwaway keys generated when this module is imported,
and verified by python-jose against a JWKS served by a patched `requests.get`. No network or AWS
access is needed. Uses `unittest` to match the CI gate (`python -m unittest discover -s tests`),
which installs only runtime requirements.
"""

import base64
import hashlib
import io
import json
import logging
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests
from botocore.exceptions import ClientError
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jose import jwt
from jose.utils import calculate_at_hash

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import handler
from logging_config import JSONFormatter, StructuredLoggerAdapter

REGION = "eu-central-1"
USER_POOL_ID = "eu-central-1_testPool"
CLIENT_ID = "test-client-id"
ISSUER = f"https://cognito-idp.{REGION}.amazonaws.com/{USER_POOL_ID}"
JWKS_URL = f"https://cognito-idp.{REGION}.amazonaws.com/{USER_POOL_ID}/.well-known/jwks.json"
KID = "test-kid-1"
JOBS_TABLE = "test-jobs"
JOB_ID = "job-123"
CONNECTION_ID = "conn-456"
SUB = "cognito-user-123"
ACCESS_TOKEN = "test-access-token-value"
OTHER_ACCESS_TOKEN = "someone-elses-access-token"

ENV = {
    "COGNITO_REGION": REGION,
    "COGNITO_USER_POOL_ID": USER_POOL_ID,
    "COGNITO_CLIENT_ID": CLIENT_ID,
    "JOBS_TABLE_NAME": JOBS_TABLE,
}


def _b64url_uint(value: int) -> str:
    """Encode a non-negative integer as an unpadded base64url string, as JWK requires.

    Args:
        value: Non-negative integer (RSA modulus or exponent).

    Returns:
        Base64url string of the big-endian bytes without padding.
    """
    raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _generate_rsa_key_pair() -> tuple[str, dict]:
    """Generate an RSA key pair for signing test tokens.

    Returns:
        Tuple of (private key in PKCS#8 PEM, public JWK carrying KID).
    """
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")
    numbers = private_key.public_key().public_numbers()
    public_jwk = {
        "alg": "RS256",
        "e": _b64url_uint(numbers.e),
        "kid": KID,
        "kty": "RSA",
        "n": _b64url_uint(numbers.n),
        "use": "sig",
    }
    return private_pem, public_jwk


PRIVATE_PEM, PUBLIC_JWK = _generate_rsa_key_pair()
OTHER_PRIVATE_PEM, _ = _generate_rsa_key_pair()


def _make_id_token(
    sub: str = SUB,
    *,
    aud: str = CLIENT_ID,
    iss: str = ISSUER,
    token_use: str = "id",
    exp_offset: int = 3600,
    include_aud: bool = True,
    include_exp: bool = True,
    private_pem: str = PRIVATE_PEM,
    kid: str = KID,
    at_hash_for: str | None = None,
) -> str:
    """Sign a Cognito-shaped ID token.

    Args:
        sub: Subject claim (user ID).
        aud: Audience claim (app client ID).
        iss: Issuer claim.
        token_use: `token_use` claim, 'id' or 'access'.
        exp_offset: Seconds from now until expiry; negative values produce an expired token.
        include_aud: If False, omit the `aud` claim.
        include_exp: If False, omit the `exp` claim.
        private_pem: PEM private key used for signing.
        kid: Key ID written to the token header.
        at_hash_for: If set, add an `at_hash` claim computed from this access token, as Cognito
            does for the Hosted UI code flow; None omits the claim.

    Returns:
        Signed JWT string.
    """
    now = int(time.time())
    claims: dict = {
        "sub": sub,
        "iss": iss,
        "token_use": token_use,
        "iat": now,
        "email": "user@example.com",
    }
    if include_aud:
        claims["aud"] = aud
    if include_exp:
        claims["exp"] = now + exp_offset
    if at_hash_for is not None:
        claims["at_hash"] = calculate_at_hash(at_hash_for, hashlib.sha256)
    return jwt.encode(claims, private_pem, algorithm="RS256", headers={"kid": kid})


def _jwks_response(payload: dict) -> MagicMock:
    """Build a fake `requests` response carrying a JWKS payload.

    Args:
        payload: JSON-serialisable body returned by `.json()`.

    Returns:
        MagicMock behaving like a successful requests.Response.
    """
    response = MagicMock()
    response.json.return_value = payload
    response.raise_for_status.return_value = None
    return response


def _make_event(
    job_id: str | None = JOB_ID,
    connection_id: str | None = CONNECTION_ID,
    token: str | None = None,
    access_token: str | None = None,
) -> dict:
    """Build a $connect WebSocket event.

    Args:
        job_id: jobId query parameter; None omits it.
        connection_id: connectionId in requestContext; None omits it.
        token: Token query parameter; None signs a valid ID token for SUB.
        access_token: accessToken query parameter; None omits it.

    Returns:
        API Gateway WebSocket event dict.
    """
    query: dict = {"token": token if token is not None else _make_id_token()}
    if job_id is not None:
        query["jobId"] = job_id
    if access_token is not None:
        query["accessToken"] = access_token
    request_context: dict = {"routeKey": "$connect"}
    if connection_id is not None:
        request_context["connectionId"] = connection_id
    return {
        "requestContext": request_context,
        "queryStringParameters": query,
        "headers": {},
    }


class _OpenConnectionTestCase(unittest.TestCase):
    """Patches environment, boto3, JWKS fetching and the JOBS table; resets JWKS state per test."""

    def setUp(self) -> None:
        """Set a complete environment, a mocked JOBS table and a JWKS containing PUBLIC_JWK."""
        handler._reset_jwks_cache()
        self.addCleanup(handler._reset_jwks_cache)

        env_patcher = patch.dict("os.environ", ENV, clear=True)
        env_patcher.start()
        self.addCleanup(env_patcher.stop)

        resource_patcher = patch("handler.boto3.resource")
        self.mock_resource_factory = resource_patcher.start()
        self.addCleanup(resource_patcher.stop)
        self.jobs_table = MagicMock()
        self.mock_resource_factory.return_value.Table.return_value = self.jobs_table

        get_patcher = patch("handler.requests.get")
        self.mock_get = get_patcher.start()
        self.addCleanup(get_patcher.stop)
        self.mock_get.return_value = _jwks_response({"keys": [PUBLIC_JWK]})

    def _invoke(self, event: dict | None = None) -> dict:
        """Invoke lambda_handler with a valid event unless one is given.

        Args:
            event: Optional event to send instead of the default valid one.

        Returns:
            API Gateway response dict.
        """
        return handler.lambda_handler(event if event is not None else _make_event(), None)

    @staticmethod
    def _body(response: dict) -> dict:
        """Decode the JSON body of a response.

        Args:
            response: API Gateway response dict.

        Returns:
            Decoded body dictionary.
        """
        return json.loads(response["body"])


class TestConnectOwnership(_OpenConnectionTestCase):
    """Tests for attaching a connection to a job the caller owns."""

    def test_owned_job_gets_connection_attached(self) -> None:
        """Test that a valid owner attaches connectionId and connectedAt via a conditional update."""
        response = self._invoke()

        self.assertEqual(response["statusCode"], 200)
        self.assertEqual(
            self._body(response)["message"], "Connection established successfully"
        )
        self.jobs_table.update_item.assert_called_once()
        kwargs = self.jobs_table.update_item.call_args.kwargs
        self.assertEqual(kwargs["Key"], {"jobId": JOB_ID})
        self.assertEqual(
            kwargs["UpdateExpression"],
            "SET connectionId = :connectionId, connectedAt = :connectedAt",
        )
        self.assertEqual(
            kwargs["ConditionExpression"],
            "attribute_exists(jobId) AND userId = :userId",
        )
        self.assertEqual(kwargs["ExpressionAttributeValues"][":connectionId"], CONNECTION_ID)
        self.assertEqual(kwargs["ExpressionAttributeValues"][":userId"], SUB)
        self.assertIsInstance(kwargs["ExpressionAttributeValues"][":connectedAt"], int)

    def test_lookup_is_a_single_conditional_write_without_pre_read(self) -> None:
        """Test that the handler does not read the job before writing, which avoids a check-then-act race."""
        self._invoke()

        self.jobs_table.get_item.assert_not_called()
        self.jobs_table.update_item.assert_called_once()

    def test_write_does_not_persist_email_or_overwrite_owner(self) -> None:
        """Test that only connection attributes are written; the owner and email are never set."""
        self._invoke()

        kwargs = self.jobs_table.update_item.call_args.kwargs
        self.assertNotIn("email", kwargs["UpdateExpression"])
        self.assertNotIn("userId =", kwargs["UpdateExpression"])
        self.assertNotIn(":email", kwargs["ExpressionAttributeValues"])
        self.assertEqual(
            set(kwargs["ExpressionAttributeValues"]),
            {":connectionId", ":connectedAt", ":userId"},
        )

    def test_missing_job_returns_404(self) -> None:
        """Test that a conditional failure (job missing) returns 404 NotFound."""
        self.jobs_table.update_item.side_effect = _conditional_check_failed()

        response = self._invoke()

        self.assertEqual(response["statusCode"], 404)
        self.assertEqual(
            self._body(response),
            {"error": "NotFound", "message": "jobId does not exist"},
        )

    def test_job_owned_by_another_user_returns_identical_404(self) -> None:
        """Test that a job owned by someone else is rejected with the same 404 as a missing job.

        The write carries the caller's own sub as `:userId`; DynamoDB rejects it because the
        stored owner differs. Both cases return the same body, so jobIds cannot be probed.
        """
        self.jobs_table.update_item.side_effect = _conditional_check_failed()
        other_user_event = _make_event(token=_make_id_token(sub="intruder-999"))
        missing_job_event = _make_event(job_id="no-such-job")

        other_user_response = self._invoke(other_user_event)
        intruder_write = self.jobs_table.update_item.call_args.kwargs
        missing_job_response = self._invoke(missing_job_event)

        self.assertEqual(other_user_response["statusCode"], 404)
        self.assertEqual(other_user_response, missing_job_response)
        self.assertEqual(intruder_write["ExpressionAttributeValues"][":userId"], "intruder-999")

    def test_dynamodb_error_returns_500(self) -> None:
        """Test that a non-condition DynamoDB failure returns a generic 500."""
        self.jobs_table.update_item.side_effect = ClientError(
            {"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "secret"}},
            "UpdateItem",
        )

        response = self._invoke()

        self.assertEqual(response["statusCode"], 500)
        self.assertEqual(
            self._body(response),
            {"error": "DatabaseError", "message": "Failed to store connection"},
        )
        self.assertNotIn("secret", response["body"])


class TestRequestValidation(_OpenConnectionTestCase):
    """Tests for request-shape validation (400) and server configuration (500)."""

    def test_missing_or_blank_job_id_returns_400(self) -> None:
        """Test that a missing, empty or whitespace-only jobId returns 400 before any other work."""
        for job_id in (None, "", "   "):
            with self.subTest(job_id=job_id):
                response = self._invoke(_make_event(job_id=job_id))

                self.assertEqual(response["statusCode"], 400)
                self.assertEqual(self._body(response)["error"], "InvalidRequest")
                self.assertIn("jobId is required", self._body(response)["message"])
        self.jobs_table.update_item.assert_not_called()

    def test_missing_or_blank_connection_id_returns_400(self) -> None:
        """Test that a missing, empty or whitespace-only connectionId returns 400."""
        for connection_id in (None, "", "   "):
            with self.subTest(connection_id=connection_id):
                response = self._invoke(_make_event(connection_id=connection_id))

                self.assertEqual(response["statusCode"], 400)
                self.assertIn("Connection ID is required", self._body(response)["message"])
        self.jobs_table.update_item.assert_not_called()

    def test_empty_event_returns_400(self) -> None:
        """Test that an empty event is rejected as a missing jobId."""
        response = handler.lambda_handler({}, None)

        self.assertEqual(response["statusCode"], 400)
        self.assertIn("jobId is required", self._body(response)["message"])

    def test_missing_required_env_var_returns_500_not_401(self) -> None:
        """Test that a missing or blank configuration variable is a 500, even with a valid token."""
        for name in ENV:
            for value in (None, "   "):
                with self.subTest(variable=name, value=value):
                    env = dict(ENV)
                    if value is None:
                        del env[name]
                    else:
                        env[name] = value
                    with patch.dict("os.environ", env, clear=True):
                        response = self._invoke()

                    self.assertEqual(response["statusCode"], 500)
                    self.assertEqual(self._body(response)["error"], "ServerConfiguration")
        self.mock_resource_factory.assert_not_called()
        self.mock_get.assert_not_called()

    def test_response_has_json_content_type(self) -> None:
        """Test that every response declares a JSON Content-Type."""
        response = self._invoke()

        self.assertEqual(response["headers"]["Content-Type"], "application/json")


class TestTokenAuthentication(_OpenConnectionTestCase):
    """Tests for Cognito ID token validation (401 for client-side token problems)."""

    def test_missing_token_returns_401(self) -> None:
        """Test that an absent token query parameter returns 401."""
        event = {
            "requestContext": {"connectionId": CONNECTION_ID},
            "queryStringParameters": {"jobId": JOB_ID},
        }

        response = self._invoke(event)

        self.assertEqual(response["statusCode"], 401)
        self.assertEqual(
            self._body(response),
            {"error": "Unauthorized", "message": "Invalid or missing JWT token"},
        )
        self.jobs_table.update_item.assert_not_called()

    def test_empty_token_returns_401(self) -> None:
        """Test that an empty token query parameter returns 401."""
        response = self._invoke(_make_event(token=""))

        self.assertEqual(response["statusCode"], 401)

    def test_invalid_tokens_return_401(self) -> None:
        """Test that each kind of invalid token is rejected with 401 and never written to the table."""
        cases = {
            "malformed": {"token": "not-a-jwt"},
            "bad signature": {"private_pem": OTHER_PRIVATE_PEM},
            "wrong audience": {"aud": "some-other-client"},
            "missing audience": {"include_aud": False},
            "wrong issuer": {"iss": "https://cognito-idp.us-east-1.amazonaws.com/other-pool"},
            "expired": {"exp_offset": -3600},
            "missing exp": {"include_exp": False},
            "access token": {"token_use": "access"},
            "empty sub": {"sub": ""},
            "blank sub": {"sub": "   "},
            "unknown kid": {"kid": "rotated-away"},
        }
        for label, options in cases.items():
            with self.subTest(case=label):
                self.jobs_table.update_item.reset_mock()
                token = options.pop("token", None) or _make_id_token(**options)

                response = self._invoke(_make_event(token=token))

                self.assertEqual(response["statusCode"], 401)
                self.assertEqual(self._body(response)["error"], "Unauthorized")
                self.jobs_table.update_item.assert_not_called()


class TestAccessTokenHash(_OpenConnectionTestCase):
    """Tests for the `at_hash` check of the ID token against the `accessToken` query parameter."""

    def test_at_hash_with_matching_access_token_connects(self) -> None:
        """Test that an ID token with `at_hash` and its matching access token is accepted and stored."""
        token = _make_id_token(at_hash_for=ACCESS_TOKEN)

        response = self._invoke(_make_event(token=token, access_token=ACCESS_TOKEN))

        self.assertEqual(response["statusCode"], 200)
        self.jobs_table.update_item.assert_called_once()
        kwargs = self.jobs_table.update_item.call_args.kwargs
        self.assertEqual(kwargs["ExpressionAttributeValues"][":userId"], SUB)
        self.assertEqual(kwargs["ExpressionAttributeValues"][":connectionId"], CONNECTION_ID)

    def test_at_hash_without_access_token_returns_401(self) -> None:
        """Test that an ID token with `at_hash` is rejected when accessToken is absent or empty."""
        token = _make_id_token(at_hash_for=ACCESS_TOKEN)
        for access_token in (None, ""):
            with self.subTest(access_token=access_token):
                self.jobs_table.update_item.reset_mock()

                response = self._invoke(_make_event(token=token, access_token=access_token))

                self.assertEqual(response["statusCode"], 401)
                self.assertEqual(self._body(response)["error"], "Unauthorized")
                self.jobs_table.update_item.assert_not_called()

    def test_at_hash_with_other_access_token_returns_401(self) -> None:
        """Test that an ID token with `at_hash` is rejected when accessToken belongs to another token."""
        token = _make_id_token(at_hash_for=ACCESS_TOKEN)

        response = self._invoke(_make_event(token=token, access_token=OTHER_ACCESS_TOKEN))

        self.assertEqual(response["statusCode"], 401)
        self.assertEqual(self._body(response)["error"], "Unauthorized")
        self.jobs_table.update_item.assert_not_called()

    def test_no_at_hash_and_no_access_token_connects(self) -> None:
        """Test that an ID token without `at_hash` does not need an access token."""
        response = self._invoke(_make_event(token=_make_id_token()))

        self.assertEqual(response["statusCode"], 200)
        self.jobs_table.update_item.assert_called_once()


class TestJwksAvailability(_OpenConnectionTestCase):
    """Tests for Cognito JWKS fetch failures (503) and kid-miss refresh behaviour."""

    def test_network_failure_returns_503(self) -> None:
        """Test that a failed JWKS request is reported as 503 rather than 401."""
        self.mock_get.side_effect = requests.ConnectionError("unreachable")

        response = self._invoke()

        self.assertEqual(response["statusCode"], 503)
        self.assertEqual(self._body(response)["error"], "ServiceUnavailable")
        self.jobs_table.update_item.assert_not_called()

    def test_http_error_from_jwks_endpoint_returns_503(self) -> None:
        """Test that an HTTP error status from the JWKS endpoint returns 503."""
        failing = _jwks_response({})
        failing.raise_for_status.side_effect = requests.HTTPError("500 Server Error")
        self.mock_get.return_value = failing

        response = self._invoke()

        self.assertEqual(response["statusCode"], 503)

    def test_non_json_jwks_body_returns_503(self) -> None:
        """Test that a JWKS body that is not JSON returns 503."""
        broken = _jwks_response({})
        broken.json.side_effect = ValueError("Expecting value")
        self.mock_get.return_value = broken

        response = self._invoke()

        self.assertEqual(response["statusCode"], 503)

    def test_jwks_payload_without_keys_returns_503(self) -> None:
        """Test that a JWKS payload without a 'keys' list returns 503."""
        self.mock_get.return_value = _jwks_response({"unexpected": True})

        response = self._invoke()

        self.assertEqual(response["statusCode"], 503)

    def test_unknown_kid_refetches_jwks_once_and_accepts_rotated_key(self) -> None:
        """Test that a kid missing from the cache triggers one forced refresh that finds the new key."""
        self.mock_get.side_effect = [
            _jwks_response({"keys": []}),
            _jwks_response({"keys": [PUBLIC_JWK]}),
        ]

        response = self._invoke()

        self.assertEqual(response["statusCode"], 200)
        self.assertEqual(self.mock_get.call_count, 2)

    def test_unknown_kid_refresh_is_rate_limited(self) -> None:
        """Test that a second unknown-kid request inside the minimum interval does not refetch."""
        self.mock_get.return_value = _jwks_response({"keys": []})

        first = self._invoke()
        calls_after_first = self.mock_get.call_count
        second = self._invoke()

        self.assertEqual(first["statusCode"], 401)
        self.assertEqual(second["statusCode"], 401)
        self.assertEqual(calls_after_first, 2)
        self.assertEqual(self.mock_get.call_count, 2)

    def test_forced_refresh_allowed_again_after_minimum_interval(self) -> None:
        """Test the rate-limit boundary: refresh is blocked just inside the interval and allowed at it."""
        last_refresh = 1000.0
        handler._JWKS_LAST_FORCED_REFRESH_TIME = last_refresh
        interval = handler._JWKS_FORCED_REFRESH_MIN_INTERVAL

        self.assertFalse(handler._is_forced_refresh_allowed(last_refresh + interval - 1))
        self.assertTrue(handler._is_forced_refresh_allowed(last_refresh + interval))

    def test_jwks_is_cached_within_ttl(self) -> None:
        """Test that consecutive valid requests reuse the cached JWKS and use the bounded timeout."""
        self._invoke()
        self._invoke()

        self.mock_get.assert_called_once_with(
            JWKS_URL,
            timeout=handler.JWKS_REQUEST_TIMEOUT_SECONDS,
        )

    def test_reset_helper_clears_cached_keys(self) -> None:
        """Test that _reset_jwks_cache empties the cache so the next request refetches."""
        self._invoke()
        handler._reset_jwks_cache()
        self._invoke()

        self.assertEqual(self.mock_get.call_count, 2)


class TestStructuredLogging(_OpenConnectionTestCase):
    """Tests that log records carry correlation fields and never contain tokens."""

    def test_connection_id_reaches_every_log_record(self) -> None:
        """Test that log records of a successful connect carry both connectionId and jobId."""
        with self.assertLogs(handler.logger, level="INFO") as captured:
            self._invoke()

        self.assertGreaterEqual(len(captured.records), 2)
        for record in captured.records:
            with self.subTest(message=record.getMessage()):
                self.assertEqual(record.connectionId, CONNECTION_ID)
        stored = [r for r in captured.records if r.getMessage() == "Connection established successfully"]
        self.assertEqual(stored[0].jobId, JOB_ID)

    def test_logs_never_contain_the_token(self) -> None:
        """Test that a rejected token does not appear in any log line."""
        token = _make_id_token(private_pem=OTHER_PRIVATE_PEM)

        with self.assertLogs(handler.logger, level="INFO") as captured:
            self._invoke(_make_event(token=token))

        rendered = " ".join(str(record.getMessage()) for record in captured.records)
        self.assertNotIn(token, rendered)

    def test_logs_never_contain_the_access_token(self) -> None:
        """Test that neither token appears in any log line, on success or on an at_hash mismatch."""
        token = _make_id_token(at_hash_for=ACCESS_TOKEN)
        cases = {
            "accepted": ACCESS_TOKEN,
            "mismatch": OTHER_ACCESS_TOKEN,
        }
        for label, access_token in cases.items():
            with self.subTest(case=label):
                with self.assertLogs(handler.logger, level="INFO") as captured:
                    self._invoke(_make_event(token=token, access_token=access_token))

                rendered = " ".join(str(record.getMessage()) for record in captured.records)
                self.assertNotIn(access_token, rendered)
                self.assertNotIn(token, rendered)

    def test_json_formatter_emits_connection_id_and_job_id(self) -> None:
        """Test that the JSON line contains connectionId, jobId and stage as top-level keys."""
        stream = io.StringIO()
        formatter_handler = logging.StreamHandler(stream)
        formatter_handler.setFormatter(JSONFormatter())
        test_logger = logging.getLogger("open_connection.test_formatter")
        test_logger.addHandler(formatter_handler)
        test_logger.setLevel(logging.INFO)
        test_logger.propagate = False
        self.addCleanup(test_logger.removeHandler, formatter_handler)

        StructuredLoggerAdapter(
            test_logger,
            {"jobId": JOB_ID, "connectionId": CONNECTION_ID, "stage": "store_connection"},
        ).info("probe")

        line = json.loads(stream.getvalue().strip())
        self.assertEqual(line["jobId"], JOB_ID)
        self.assertEqual(line["connectionId"], CONNECTION_ID)
        self.assertEqual(line["stage"], "store_connection")
        self.assertEqual(line["message"], "probe")


class TestHelpers(unittest.TestCase):
    """Tests for small event-parsing helpers."""

    def test_extract_job_id_handles_missing_query_params(self) -> None:
        """Test that a None queryStringParameters yields no jobId."""
        self.assertIsNone(handler._extract_job_id({"queryStringParameters": None}))

    def test_extract_connection_id_handles_missing_request_context(self) -> None:
        """Test that a missing requestContext yields no connectionId."""
        self.assertIsNone(handler._extract_connection_id({}))

    def test_extract_jwt_rejects_missing_token(self) -> None:
        """Test that a missing token raises CognitoJWTError."""
        with self.assertRaises(handler.CognitoJWTError):
            handler._extract_jwt_from_query_params({"queryStringParameters": {}})

    def test_extract_access_token_returns_none_when_missing_or_empty(self) -> None:
        """Test that an absent, empty or None accessToken yields None, and a present one is returned."""
        for query in (None, {}, {"accessToken": ""}):
            with self.subTest(query=query):
                self.assertIsNone(
                    handler._extract_access_token_from_query_params({"queryStringParameters": query})
                )
        self.assertEqual(
            handler._extract_access_token_from_query_params(
                {"queryStringParameters": {"accessToken": ACCESS_TOKEN}}
            ),
            ACCESS_TOKEN,
        )

    def test_store_helper_passes_owner_condition(self) -> None:
        """Test that the store helper binds the owner condition to the supplied user ID."""
        dynamodb = MagicMock()
        table = dynamodb.Table.return_value

        handler._store_connection_in_dynamodb(dynamodb, JOB_ID, CONNECTION_ID, "owner-1", JOBS_TABLE)

        dynamodb.Table.assert_called_once_with(JOBS_TABLE)
        self.assertEqual(
            table.update_item.call_args.kwargs["ExpressionAttributeValues"][":userId"],
            "owner-1",
        )


def _conditional_check_failed() -> ClientError:
    """Build the ClientError DynamoDB raises when a ConditionExpression is false.

    Returns:
        ClientError with code ConditionalCheckFailedException.
    """
    return ClientError(
        {"Error": {"Code": "ConditionalCheckFailedException", "Message": "The conditional request failed"}},
        "UpdateItem",
    )


if __name__ == "__main__":
    unittest.main()
