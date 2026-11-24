"""Job status values stored in the jobs DynamoDB table."""

from enum import StrEnum


class JobStatus(StrEnum):
    """Values of the `jobStatus` attribute that the start-job flow reads or writes.

    Other statuses written by later steps (for example `PAINTED` or `FAILED`) are not
    listed: this Lambda only needs to know whether a job is still `UPLOADED`.
    """

    UPLOADED = "UPLOADED"
    IN_PROGRESS = "IN_PROGRESS"
