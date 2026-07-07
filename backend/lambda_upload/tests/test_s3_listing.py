"""Tests for S3 image listing functionality (future feature).

These tests are placeholders for the upload confirmation flow's S3 listing
requirements (TASK-03). The presigned URL generator does not list S3, but
a future "upload confirmation" handler will need to verify 4 images are present
under uploads/<jobId>/.

To be implemented when the upload confirmation handler is added to this module.
"""

import pytest

pytestmark = pytest.mark.skip(
    reason="S3 listing functionality (TASK-03) not yet implemented in lambda_upload handler"
)


def test_list_uploaded_images_returns_four_urls() -> None:
    """List all images under uploads/<jobId>/ and return their S3 URLs."""
    # Placeholder for: list_uploaded_images(job_id: str) -> list[str]
    # Returns S3 URLs (s3://bucket/key format) sorted lexicographically
    pass


def test_list_uploaded_images_excludes_zero_byte_markers() -> None:
    """Exclude zero-byte folder marker objects from the listing."""
    # S3 sometimes creates empty "folder" objects with trailing /
    # These should be filtered out
    pass


def test_list_uploaded_images_returns_fewer_than_four_when_available() -> None:
    """Return exact count of images, no artificial padding."""
    pass


def test_list_uploaded_images_returns_more_than_four_when_present() -> None:
    """Return all images even if more than 4 are present."""
    pass


def test_list_uploaded_images_handles_pagination() -> None:
    """Correctly handle S3 list response pagination for >1000 objects."""
    pass


def test_list_uploaded_images_propagates_s3_client_error() -> None:
    """Any ClientError from S3 is propagated unchanged to the caller."""
    pass
