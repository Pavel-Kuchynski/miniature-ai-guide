"""Typed view of a message received from the result SQS queue."""

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class ResultMessage:
    """Validated body of a result-queue message.

    Attributes:
        job_id: Trimmed, non-empty job id.
        status: Trimmed, non-empty job status (for example `COMPLETED` or `FAILED`).
        failure_message: Trimmed, client-safe failure reason, or `None` when the message
            has none or it is blank or not a string.
    """

    job_id: str
    status: str
    failure_message: Optional[str] = None
