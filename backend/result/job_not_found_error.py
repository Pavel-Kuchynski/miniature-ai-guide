"""Exception raised when a job item does not exist."""


class JobNotFoundError(Exception):
    """Raised when no job item exists in the jobs DynamoDB table for a job id."""
