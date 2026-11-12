"""Job status values stored in the jobs DynamoDB table."""

from enum import StrEnum


class JobStatus(StrEnum):
    """Values of the `jobStatus` attribute on a job item in the jobs DynamoDB table."""

    IN_PROGRESS = "IN_PROGRESS"
    PAINTED = "PAINTED"
    FAILED = "FAILED"
