"""Job status values stored in the jobs DynamoDB table."""

from enum import StrEnum


class JobStatus(StrEnum):
    """Values of the `jobStatus` attribute on a job item in the jobs DynamoDB table.

    Only the statuses this Lambda reads or writes are listed. Members compare equal to their
    raw string values, so a status read from DynamoDB can be compared without conversion.
    """

    PAINTED = "PAINTED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
