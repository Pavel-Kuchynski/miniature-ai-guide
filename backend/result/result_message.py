"""Typed view of a message received from the result SQS queue."""

from dataclasses import dataclass
from typing import Optional

from job_status import JobStatus


@dataclass(frozen=True)
class ResultMessage:
    """Validated body of a result-queue message.

    Attributes:
        job_id: Trimmed, non-empty job id.
        status: Job status from the message. Always a `JobStatus` member; unknown values are
            rejected by `parse_message` before this object is built.
        failure_message: Trimmed, client-safe failure reason, or `None` when the message
            has none or it is blank or not a string.
    """

    job_id: str
    status: JobStatus
    failure_message: Optional[str] = None
