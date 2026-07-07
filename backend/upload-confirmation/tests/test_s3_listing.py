"""Tests for the `list_uploaded_images` S3 listing helper.

Tests that the helper correctly lists objects under an `uploads/<jobId>/`
prefix, excludes zero-byte markers, handles pagination, and propagates
S3 errors unchanged.
"""

from typing import Iterator

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from handler import list_uploaded_images

BUCKET_NAME = "test-upload-bucket"
JOB_ID = "123e4567-e89b-12d3-a456-426614174000"


class TestListUploadedImages:
    """Tests for the `list_uploaded_images` S3 presence-check helper."""

    @pytest.fixture(autouse=True)
    def _mocked_bucket(self, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
        """Create a mocked S3 bucket and point `UPLOAD_BUCKET_NAME` at it."""
        monkeypatch.setenv("UPLOAD_BUCKET_NAME", BUCKET_NAME)
        with mock_aws():
            s3_client = boto3.client("s3", region_name="us-east-1")
            s3_client.create_bucket(Bucket=BUCKET_NAME)
            self.s3_client = s3_client
            yield

    def _put_object(
        self, key: str, body: bytes = b"fake-image-bytes"
    ) -> None:
        """Upload a test object under the bucket used by these tests."""
        self.s3_client.put_object(Bucket=BUCKET_NAME, Key=key, Body=body)

    def test_list_uploaded_images_returns_four_urls(self) -> None:
        """Exactly 4 uploaded objects should yield 4 sorted S3 URLs."""
        for name in ["b.png", "a.png", "d.png", "c.png"]:
            self._put_object(f"uploads/{JOB_ID}/{name}")

        urls = list_uploaded_images(JOB_ID)

        assert urls == [
            f"s3://{BUCKET_NAME}/uploads/{JOB_ID}/a.png",
            f"s3://{BUCKET_NAME}/uploads/{JOB_ID}/b.png",
            f"s3://{BUCKET_NAME}/uploads/{JOB_ID}/c.png",
            f"s3://{BUCKET_NAME}/uploads/{JOB_ID}/d.png",
        ]

    def test_list_uploaded_images_excludes_zero_byte_markers(self) -> None:
        """Zero-byte folder markers should be excluded from results."""
        self._put_object(f"uploads/{JOB_ID}/", body=b"")
        for name in ["a.png", "b.png", "c.png", "d.png"]:
            self._put_object(f"uploads/{JOB_ID}/{name}")

        urls = list_uploaded_images(JOB_ID)

        assert len(urls) == 4
        assert f"s3://{BUCKET_NAME}/uploads/{JOB_ID}/" not in urls

    def test_list_uploaded_images_returns_fewer_than_four_when_available(
        self,
    ) -> None:
        """Fewer than 4 images should be returned without padding."""
        for name in ["a.png", "b.png"]:
            self._put_object(f"uploads/{JOB_ID}/{name}")

        urls = list_uploaded_images(JOB_ID)

        assert len(urls) == 2

    def test_list_uploaded_images_returns_more_than_four_when_present(
        self,
    ) -> None:
        """More than 4 images should be returned in full."""
        for name in ["a.png", "b.png", "c.png", "d.png", "e.png"]:
            self._put_object(f"uploads/{JOB_ID}/{name}")

        urls = list_uploaded_images(JOB_ID)

        assert len(urls) == 5

    def test_list_uploaded_images_handles_pagination(self) -> None:
        """More than 1000 objects should be paginated correctly."""
        object_count = 1001
        for index in range(object_count):
            self._put_object(f"uploads/{JOB_ID}/img_{index:04d}.png")

        urls = list_uploaded_images(JOB_ID)

        assert len(urls) == object_count

    def test_list_uploaded_images_propagates_s3_client_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A ClientError from S3 should propagate unchanged."""
        monkeypatch.setenv("UPLOAD_BUCKET_NAME", "bucket-that-does-not-exist")

        with pytest.raises(ClientError):
            list_uploaded_images(JOB_ID)
