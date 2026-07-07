#!/usr/bin/env python3
"""Manual test for lambda_handler orchestration logic (TASK-05).

This script tests the orchestration without relying on the broken .venv.
It validates the handler logic against the test requirements.
"""

import json
import sys
from unittest.mock import MagicMock, patch
from typing import Any, Dict

# Add current directory to path
sys.path.insert(0, '.')

import handler

def test_happy_path_new_job() -> None:
    """Test: new job created successfully (201)."""
    event = {"body": json.dumps({"jobId": "test-job-123"})}

    with patch("handler.parse_job_id") as mock_parse, \
         patch("handler.list_uploaded_images") as mock_list, \
         patch("handler.put_job_item") as mock_put:

        mock_parse.return_value = ("test-job-123", None)
        mock_list.return_value = ["s3://bucket/uploads/test-job-123/a.png",
                                  "s3://bucket/uploads/test-job-123/b.png",
                                  "s3://bucket/uploads/test-job-123/c.png",
                                  "s3://bucket/uploads/test-job-123/d.png"]
        mock_put.return_value = (True, {})  # created=True

        response = handler.lambda_handler(event, None)

    assert response["statusCode"] == 201, f"Expected 201, got {response['statusCode']}"
    body = json.loads(response["body"])
    assert body["jobId"] == "test-job-123"
    assert body["jobStatus"] == "UPLOADED"
    print("✓ Happy path (new job): 201 response")

def test_duplicate_confirmation() -> None:
    """Test: duplicate confirmation (200)."""
    event = {"body": json.dumps({"jobId": "test-job-123"})}

    with patch("handler.parse_job_id") as mock_parse, \
         patch("handler.list_uploaded_images") as mock_list, \
         patch("handler.put_job_item") as mock_put:

        mock_parse.return_value = ("test-job-123", None)
        mock_list.return_value = ["s3://bucket/uploads/test-job-123/a.png",
                                  "s3://bucket/uploads/test-job-123/b.png",
                                  "s3://bucket/uploads/test-job-123/c.png",
                                  "s3://bucket/uploads/test-job-123/d.png"]
        mock_put.return_value = (False, {})  # created=False (duplicate)

        response = handler.lambda_handler(event, None)

    assert response["statusCode"] == 200, f"Expected 200, got {response['statusCode']}"
    body = json.loads(response["body"])
    assert body["jobId"] == "test-job-123"
    assert body["jobStatus"] == "UPLOADED"
    print("✓ Duplicate confirmation: 200 response")

def test_invalid_job_id() -> None:
    """Test: invalid/missing jobId (400)."""
    event = {"body": json.dumps({})}  # missing jobId

    with patch("handler.parse_job_id") as mock_parse:
        error_resp = {
            "statusCode": 400,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"error": "InvalidRequest", "message": "jobId is required"})
        }
        mock_parse.return_value = (None, error_resp)

        response = handler.lambda_handler(event, None)

    assert response["statusCode"] == 400, f"Expected 400, got {response['statusCode']}"
    body = json.loads(response["body"])
    assert body["error"] == "InvalidRequest"
    print("✓ Invalid jobId: 400 response")

def test_missing_images() -> None:
    """Test: fewer than 4 images (422)."""
    event = {"body": json.dumps({"jobId": "test-job-123"})}

    with patch("handler.parse_job_id") as mock_parse, \
         patch("handler.list_uploaded_images") as mock_list, \
         patch("handler.put_job_item") as mock_put:

        mock_parse.return_value = ("test-job-123", None)
        mock_list.return_value = ["s3://bucket/uploads/test-job-123/a.png",
                                  "s3://bucket/uploads/test-job-123/b.png"]  # Only 2 images

        response = handler.lambda_handler(event, None)

    assert response["statusCode"] == 422, f"Expected 422, got {response['statusCode']}"
    body = json.loads(response["body"])
    assert body["error"] == "MissingImages"
    assert body["imageCount"] == 2
    assert "Expected 4 images" in body["message"]
    print("✓ Missing images (2 instead of 4): 422 response")

def test_too_many_images() -> None:
    """Test: more than 4 images (422)."""
    event = {"body": json.dumps({"jobId": "test-job-123"})}

    with patch("handler.parse_job_id") as mock_parse, \
         patch("handler.list_uploaded_images") as mock_list:

        mock_parse.return_value = ("test-job-123", None)
        mock_list.return_value = ["s3://bucket/uploads/test-job-123/a.png",
                                  "s3://bucket/uploads/test-job-123/b.png",
                                  "s3://bucket/uploads/test-job-123/c.png",
                                  "s3://bucket/uploads/test-job-123/d.png",
                                  "s3://bucket/uploads/test-job-123/e.png"]  # 5 images

        response = handler.lambda_handler(event, None)

    assert response["statusCode"] == 422, f"Expected 422, got {response['statusCode']}"
    body = json.loads(response["body"])
    assert body["error"] == "MissingImages"
    assert body["imageCount"] == 5
    print("✓ Too many images (5 instead of 4): 422 response")

def test_s3_failure() -> None:
    """Test: S3 list failure (500)."""
    from botocore.exceptions import ClientError

    event = {"body": json.dumps({"jobId": "test-job-123"})}

    with patch("handler.parse_job_id") as mock_parse, \
         patch("handler.list_uploaded_images") as mock_list:

        mock_parse.return_value = ("test-job-123", None)
        mock_list.side_effect = ClientError(
            error_response={"Error": {"Code": "NoSuchBucket", "Message": "Bucket not found"}},
            operation_name="ListObjects"
        )

        response = handler.lambda_handler(event, None)

    assert response["statusCode"] == 500, f"Expected 500, got {response['statusCode']}"
    body = json.loads(response["body"])
    assert body["error"] == "InternalError"
    assert "Failed to list uploaded images" in body["message"]
    print("✓ S3 failure: 500 response")

def test_dynamodb_failure() -> None:
    """Test: DynamoDB write failure (500)."""
    from botocore.exceptions import ClientError

    event = {"body": json.dumps({"jobId": "test-job-123"})}

    with patch("handler.parse_job_id") as mock_parse, \
         patch("handler.list_uploaded_images") as mock_list, \
         patch("handler.put_job_item") as mock_put:

        mock_parse.return_value = ("test-job-123", None)
        mock_list.return_value = ["s3://bucket/uploads/test-job-123/a.png",
                                  "s3://bucket/uploads/test-job-123/b.png",
                                  "s3://bucket/uploads/test-job-123/c.png",
                                  "s3://bucket/uploads/test-job-123/d.png"]
        mock_put.side_effect = ClientError(
            error_response={"Error": {"Code": "ValidationException", "Message": "Table not found"}},
            operation_name="PutItem"
        )

        response = handler.lambda_handler(event, None)

    assert response["statusCode"] == 500, f"Expected 500, got {response['statusCode']}"
    body = json.loads(response["body"])
    assert body["error"] == "InternalError"
    assert "Failed to record job" in body["message"]
    print("✓ DynamoDB failure: 500 response")

def test_dynamodb_race_condition() -> None:
    """Test: DynamoDB race condition (500)."""
    event = {"body": json.dumps({"jobId": "test-job-123"})}

    with patch("handler.parse_job_id") as mock_parse, \
         patch("handler.list_uploaded_images") as mock_list, \
         patch("handler.put_job_item") as mock_put:

        mock_parse.return_value = ("test-job-123", None)
        mock_list.return_value = ["s3://bucket/uploads/test-job-123/a.png",
                                  "s3://bucket/uploads/test-job-123/b.png",
                                  "s3://bucket/uploads/test-job-123/c.png",
                                  "s3://bucket/uploads/test-job-123/d.png"]
        mock_put.side_effect = RuntimeError("Race condition detected")

        response = handler.lambda_handler(event, None)

    assert response["statusCode"] == 500, f"Expected 500, got {response['statusCode']}"
    body = json.loads(response["body"])
    assert body["error"] == "InternalError"
    assert "race condition" in body["message"]
    print("✓ DynamoDB race condition: 500 response")

if __name__ == "__main__":
    print("Running TASK-05 orchestration tests...\n")

    try:
        test_happy_path_new_job()
        test_duplicate_confirmation()
        test_invalid_job_id()
        test_missing_images()
        test_too_many_images()
        test_s3_failure()
        test_dynamodb_failure()
        test_dynamodb_race_condition()

        print("\n✓ All orchestration tests passed!")
        sys.exit(0)
    except AssertionError as e:
        print(f"\n✗ Test failed: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"\n✗ Unexpected error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
