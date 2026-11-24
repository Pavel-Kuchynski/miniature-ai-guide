"""Job status values carried by result-queue messages."""

from enum import StrEnum


class JobStatus(StrEnum):
    """`status` values that the bedrock and paint Lambdas send to the result queue."""

    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
