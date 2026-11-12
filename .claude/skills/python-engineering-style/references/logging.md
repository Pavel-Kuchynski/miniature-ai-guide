# Logging conventions

Use the repository's existing logging infrastructure.

Prefer structured context:

- jobId
- userId
- connectionId
- stage

Use stage names that identify workflow boundaries:

- parse_input
- authenticate
- extract_user
- validate_job
- store_connection
- error

Log meaningful lifecycle events:

- request received
- validation failure
- authentication failure
- resource not found
- external dependency failure
- successful completion

Do not log:

- JWT tokens
- access tokens
- refresh tokens
- passwords
- API keys
- private keys
- authorization headers

Avoid logging complete Lambda events.

Prefer:

log.info("WebSocket connection attempt")

over:

log.info(f"Received event: {event}")

When handling an exception:

1. log the operation that failed;
2. include correlation identifiers;
3. include the internal exception details in logs;
4. return a stable, safe error response to the caller.

Do not use print() when the repository provides a logging framework.

## Logging infrastructure (reference: `backend/paint`)

Logging is split into three small modules, copied per Lambda module (modules stay
self-contained):

| Module | Contents |
|---|---|
| `json_formatter.py` | `JSONFormatter`, `UNKNOWN = "unknown"` |
| `structured_logger_adapter.py` | `StructuredLoggerAdapter` (merges adapter context with per-call `extra`; per-call wins) |
| `logging_config.py` | `configure_logger(name)`; re-exports the two classes via `__all__` |

Every log line is one JSON object: `timestamp` (UTC ISO-8601), `level`, `jobId`, `stage`,
`message`, plus `exception` when the record has `exc_info`. Missing `jobId` or `stage`
becomes `UNKNOWN`.

Usage:

```python
logger = configure_logger(__name__)           # module level

logger.info("Fetching prompt", extra={"stage": "fetch_prompt"})   # no job context yet

log = StructuredLoggerAdapter(logger, {"jobId": job_id, "stage": "orchestrate"})
log.info("Starting paint job")
log.exception("Failed to paint images: %s", exc, extra={"stage": "paint_error"})
```

Rules:

* Build the adapter in `lambda_handler` as soon as `jobId` is known. Helpers that receive
  `job_id` pass `extra={"jobId": job_id, "stage": ...}`.
* Use lazy `%s` / `%r` formatting with arguments, not f-strings, in log calls.
* Use `log.exception` (keeps the traceback) for caught failures and `log.error` for
  validation failures that have no exception.
* Name the stage after the step. Failures that are caught and swallowed use a `_error`
  suffix (`paint_error`, `update_status_painted_error`, `notify_error`,
  `notify_result_error`); this keeps them searchable in CloudWatch Logs Insights.
  Success: `complete`. Input: `parse_input`.
* When an exception message could contain a secret (API key rejected by a provider), log
  `type(exc).__name__` only, not the exception.
* Never log request bodies, prompts that may contain user text beyond what is needed, or
  presigned URLs (they carry credentials in the query string).
