"""Tests for the `JobStatus` enum.

Pins the stored string values, so a renamed member or a changed value that would break the
jobs DynamoDB table is caught by the test suite.
"""

import pytest

from job_status import JobStatus


class TestJobStatus:
    """Tests for `JobStatus` values and string behavior."""

    def test_members_are_the_statuses_this_lambda_uses(self) -> None:
        """The enum should contain exactly PAINTED, COMPLETED and FAILED."""
        assert {status.name for status in JobStatus} == {"PAINTED", "COMPLETED", "FAILED"}

    @pytest.mark.parametrize("status", list(JobStatus))
    def test_value_equals_member_name(self, status: JobStatus) -> None:
        """Each stored value should match its member name, as written to DynamoDB."""
        assert status.value == status.name

    @pytest.mark.parametrize("status", list(JobStatus))
    def test_str_returns_stored_value(self, status: JobStatus) -> None:
        """`str()` of a member should be the raw value, used when serializing to DynamoDB."""
        assert str(status) == status.value

    def test_members_compare_equal_to_raw_strings(self) -> None:
        """A raw status string read from DynamoDB should compare equal to the member."""
        assert JobStatus.PAINTED == "PAINTED"
        assert "FAILED" == JobStatus.FAILED

    def test_lookup_by_value_returns_member(self) -> None:
        """Constructing from a stored value should return the matching member."""
        assert JobStatus("COMPLETED") is JobStatus.COMPLETED

    def test_unknown_value_raises_value_error(self) -> None:
        """A status outside the enum should not be accepted silently."""
        with pytest.raises(ValueError):
            JobStatus("IN_PROGRESS")
