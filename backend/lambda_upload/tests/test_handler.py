"""Unit tests for the lambda_upload handler."""

import json
import sys
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from botocore.exceptions import ClientError

# Ensure imports work when running tests from repository root.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import handler


class TestLambdaUploadHandler(unittest.TestCase):
    def test_missing_bucket_env_returns_500(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            response = handler.lambda_handler({}, None)

        self.assertEqual(response["statusCode"], 500)
        payload = json.loads(response["body"])
        self.assertIn("UPLOAD_BUCKET_NAME", payload["error"])

    def test_generates_four_urls_in_single_uuid_folder(self) -> None:
        fixed_uuid = uuid.UUID("11111111-1111-1111-1111-111111111111")

        event = {
            "body": json.dumps(
                {
                    "fileNames": ["a.png", "b.png", "c.png", "d.png"],
                    "contentTypes": ["image/png", "image/png", "image/png", "image/png"],
                }
            )
        }

        with patch.dict(
            "os.environ",
            {
                "UPLOAD_BUCKET_NAME": "test-bucket",
                "UPLOAD_URL_EXPIRES_SECONDS": "600",
            },
            clear=True,
        ), patch("handler.uuid.uuid4", return_value=fixed_uuid), patch.object(
            handler.s3_client,
            "generate_presigned_url",
            side_effect=["url1", "url2", "url3", "url4"],
        ) as mocked_presign:
            response = handler.lambda_handler(event, None)

        self.assertEqual(response["statusCode"], 200)
        payload = json.loads(response["body"])

        self.assertEqual(payload["bucket"], "test-bucket")
        self.assertEqual(payload["folder"], str(fixed_uuid))
        self.assertEqual(payload["prefix"], f"uploads/{fixed_uuid}")
        self.assertEqual(payload["expiresIn"], 600)
        self.assertEqual(len(payload["uploadItems"]), 4)
        self.assertEqual(mocked_presign.call_count, 4)

        expected_keys = [
            f"uploads/{fixed_uuid}/a.png",
            f"uploads/{fixed_uuid}/b.png",
            f"uploads/{fixed_uuid}/c.png",
            f"uploads/{fixed_uuid}/d.png",
        ]
        actual_keys = [item["key"] for item in payload["uploadItems"]]
        self.assertEqual(actual_keys, expected_keys)

    def test_defaults_and_client_error_returns_500(self) -> None:
        event = {
            "queryStringParameters": {
                "fileName": "fallback.bin",
                "contentType": "application/octet-stream",
            }
        }

        with patch.dict("os.environ", {"UPLOAD_BUCKET_NAME": "test-bucket"}, clear=True), patch.object(
            handler.s3_client,
            "generate_presigned_url",
            side_effect=ClientError(
                error_response={"Error": {"Code": "500", "Message": "boom"}},
                operation_name="PutObject",
            ),
        ):
            response = handler.lambda_handler(event, None)

        self.assertEqual(response["statusCode"], 500)
        payload = json.loads(response["body"])
        self.assertIn("Failed to create upload URL", payload["error"])

    def test_body_overrides_query_string_values(self) -> None:
        event = {
            "queryStringParameters": {"fileName": "query.png"},
            "body": json.dumps({"fileNames": ["body.png"]}),
        }

        with patch.dict("os.environ", {"UPLOAD_BUCKET_NAME": "test-bucket"}, clear=True), patch.object(
            handler.s3_client,
            "generate_presigned_url",
            side_effect=["url1", "url2", "url3", "url4"],
        ):
            response = handler.lambda_handler(event, None)

        payload = json.loads(response["body"])
        self.assertEqual(payload["uploadItems"][0]["fileName"], "body.png")

    def test_partial_file_names_fall_back_for_remaining_slots(self) -> None:
        event = {"body": json.dumps({"fileNames": ["a.png", "b.png"]})}

        with patch.dict("os.environ", {"UPLOAD_BUCKET_NAME": "test-bucket"}, clear=True), patch.object(
            handler.s3_client,
            "generate_presigned_url",
            side_effect=["url1", "url2", "url3", "url4"],
        ):
            response = handler.lambda_handler(event, None)

        payload = json.loads(response["body"])
        file_names = [item["fileName"] for item in payload["uploadItems"]]
        self.assertEqual(file_names, ["a.png", "b.png", "file_3.bin", "file_4.bin"])

    def test_more_than_four_file_names_uses_only_first_four(self) -> None:
        event = {
            "body": json.dumps(
                {"fileNames": ["a.png", "b.png", "c.png", "d.png", "e.png", "f.png"]}
            )
        }

        with patch.dict("os.environ", {"UPLOAD_BUCKET_NAME": "test-bucket"}, clear=True), patch.object(
            handler.s3_client,
            "generate_presigned_url",
            side_effect=["url1", "url2", "url3", "url4"],
        ) as mocked_presign:
            response = handler.lambda_handler(event, None)

        self.assertEqual(response["statusCode"], 200)
        payload = json.loads(response["body"])
        file_names = [item["fileName"] for item in payload["uploadItems"]]
        self.assertEqual(file_names, ["a.png", "b.png", "c.png", "d.png"])
        self.assertEqual(mocked_presign.call_count, 4)

    def test_default_expires_in_applies_when_env_var_absent(self) -> None:
        event = {"body": json.dumps({"fileNames": ["a.png"]})}

        with patch.dict("os.environ", {"UPLOAD_BUCKET_NAME": "test-bucket"}, clear=True), patch.object(
            handler.s3_client,
            "generate_presigned_url",
            side_effect=["url1", "url2", "url3", "url4"],
        ):
            response = handler.lambda_handler(event, None)

        payload = json.loads(response["body"])
        self.assertEqual(payload["expiresIn"], 900)

    def test_malformed_json_body_falls_back_to_query_params(self) -> None:
        event = {
            "queryStringParameters": {"fileName": "fallback.png"},
            "body": "{not valid json",
        }

        with patch.dict("os.environ", {"UPLOAD_BUCKET_NAME": "test-bucket"}, clear=True), patch.object(
            handler.s3_client,
            "generate_presigned_url",
            side_effect=["url1", "url2", "url3", "url4"],
        ):
            response = handler.lambda_handler(event, None)

        self.assertEqual(response["statusCode"], 200)
        payload = json.loads(response["body"])
        self.assertEqual(payload["uploadItems"][0]["fileName"], "fallback.png")

    def test_invalid_expires_in_returns_500(self) -> None:
        with patch.dict(
            "os.environ",
            {"UPLOAD_BUCKET_NAME": "test-bucket", "UPLOAD_URL_EXPIRES_SECONDS": "not-a-number"},
            clear=True,
        ):
            response = handler.lambda_handler({}, None)

        self.assertEqual(response["statusCode"], 500)
        payload = json.loads(response["body"])
        self.assertIn("UPLOAD_URL_EXPIRES_SECONDS", payload["error"])


if __name__ == "__main__":
    unittest.main()
