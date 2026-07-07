"""Shared pytest fixtures for lambda_upload tests.

Provides sample data for use across all test modules. Tests use direct mocking
with unittest.mock.patch for S3 and DynamoDB interactions.
"""

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Dict

import pytest


@pytest.fixture
def aws_credentials(monkeypatch: Any) -> None:
    """Set dummy AWS credentials for moto mocking.

    Ensures boto3 client initialization doesn't fail due to missing credentials.
    """
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "eu-central-1")


@pytest.fixture
def sample_job_id() -> str:
    """Generate a sample job ID (UUID).

    Returns a valid UUID string suitable for use as jobId.
    """
    return str(uuid.uuid4())


@pytest.fixture
def sample_event() -> Dict[str, Any]:
    """Generate a sample API Gateway Lambda event for presigned URL request.

    Returns a minimal valid event with file names and content types in JSON body.
    """
    return {
        "body": json.dumps({
            "fileNames": ["reference_1.png", "reference_2.png", "reference_3.jpg", "reference_4.jpg"],
            "contentTypes": ["image/png", "image/png", "image/jpeg", "image/jpeg"],
        }),
        "isBase64Encoded": False,
        "headers": {"Content-Type": "application/json"},
    }


@pytest.fixture
def sample_upload_confirmation_event(sample_job_id: str) -> Dict[str, Any]:
    """Generate a sample API Gateway event for upload confirmation request.

    Returns an event with jobId for confirming uploaded images.
    This is for future upload-confirmation handler functionality.
    """
    return {
        "body": json.dumps({
            "jobId": sample_job_id,
        }),
        "isBase64Encoded": False,
        "headers": {"Content-Type": "application/json"},
    }


@pytest.fixture
def sample_job_record(sample_job_id: str) -> Dict[str, Any]:
    """Generate a sample DynamoDB job record.

    Returns a fully populated job item as it would be stored in DynamoDB,
    following the Data-Model.md §2.2 attribute schema.
    """
    now = datetime.now(timezone.utc)
    ttl_seconds = int(now.timestamp()) + (7 * 24 * 60 * 60)  # 7 days from now

    return {
        "jobId": sample_job_id,
        "imageUrls": [
            f"s3://test-bucket/uploads/{sample_job_id}/image_1.png",
            f"s3://test-bucket/uploads/{sample_job_id}/image_2.png",
            f"s3://test-bucket/uploads/{sample_job_id}/image_3.png",
            f"s3://test-bucket/uploads/{sample_job_id}/image_4.png",
        ],
        "jobStatus": "UPLOADED",
        "createdAt": now.isoformat(),
        "updatedAt": now.isoformat(),
        "ttl": ttl_seconds,
    }
