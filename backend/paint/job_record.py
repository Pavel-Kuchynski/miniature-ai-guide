"""Subset of a job item needed by the paint flow."""

from dataclasses import dataclass
from typing import Optional

from job_status import JobStatus


@dataclass(frozen=True)
class JobRecord:
    """Subset of a job item needed by the paint flow.

    Attributes:
        status: The job's current status.
        instruction: Trimmed optional user instruction, or `None` if absent or blank.
    """

    status: JobStatus
    instruction: Optional[str]
