# Error handling conventions

> The examples below come from one illustrative Lambda (`start-job`, a job
> orchestration handler using DynamoDB, S3, and SQS). Apply the same
> *principles* to other modules — don't force unrelated domains to reuse
> these exact status codes, error names, or resource types.

## Core principles

* Catch the narrowest exception that is meaningful at that boundary.
* Translate internal failures into stable, safe external responses.
* Keep diagnostic detail (exception messages, stack traces, AWS error codes)
  in logs — never in API responses.
* Use HTTP status codes that reflect the actual failure category.
* Do not use bare `except:`.
* Use a broad `except Exception` only at a deliberate top-level safety
  boundary, not as a substitute for handling known failure modes.
* Prefer existing library exceptions (`botocore.exceptions.ClientError`)
  over inventing a custom exception for a failure the library already
  represents.

## Map domain outcomes to status codes explicitly

Decide the status code based on *why* the operation failed, not on which
line of code raised. `start-job` distinguishes:

| Situation | Status | Response helper |
|---|---|---|
| Missing/blank `jobId` in the request | 400 | `_invalid_request_response` |
| `jobId` not found in DynamoDB | 404 | `_not_found_response` |
| Job exists but isn't in the expected state (`UPLOADED`) | 409 | `_conflict_response` |
| Job is valid but a required precondition isn't met (wrong image count) | 422 | `_unprocessable_entity_response` |
| Any downstream AWS failure (S3, DynamoDB, SQS) | 500 | `_internal_error_response` |

Each response helper builds a small, stable JSON body — an `error` code, a
human-readable `message`, and only the additional fields the client
actually needs (e.g. `imageCount` on the 422). None of them include the raw
exception, a stack trace, or AWS's internal error code.

```python
def _conflict_response(message: str) -> Dict[str, Any]:
    """Build the standard `409 Conflict` API-Gateway-style error response."""
    return {
        "statusCode": 409,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({"error": "Conflict", "message": message}),
    }
```

## Catch AWS and configuration failures separately

Each AWS-calling helper (`get_job_status`, `list_uploaded_images`,
`update_job_item`, `trigger_guide_creation`) can fail in two structurally
different ways, and the handler treats them differently:

* `botocore.exceptions.ClientError` — the AWS call reached the service and
  failed there (throttling, access denied, condition failed, resource
  missing). Log the operation and the exception, return a `500` (or a more
  specific code if the failure is expected and meaningful, as below).
* `KeyError` — a required environment variable
  (`UPLOAD_BUCKET_NAME`, `JOBS_TABLE_NAME`, `GUIDE_CREATION_QUEUE_URL`) was
  never set. This is a deployment/configuration bug, not a client error.
  Log it distinctly ("Server misconfiguration: missing environment
  variable %s") and return a `500` that says so without naming the
  variable to the caller.

```python
try:
    job_status = get_job_status(job_id)
except ClientError as error:
    logger.error("DynamoDB get failed for jobId=%r: %s", job_id, error)
    return _internal_error_response("Failed to check job status.")
except KeyError as error:
    logger.error("Server misconfiguration: missing environment variable %s", error)
    return _internal_error_response(
        "Server misconfiguration: missing DynamoDB table name."
    )
```

Keeping these as separate `except` clauses — rather than one broad
`except Exception` — means a real AWS outage and a broken deployment
produce different log signatures, which matters when diagnosing an
incident.

## Treat a specific AWS error code as an expected outcome, not a failure

Not every `ClientError` is a 500. When a specific, anticipated AWS error
code represents a valid domain outcome (for example, a conditional write
losing a race to a concurrent invocation), inspect
`error.response["Error"]["Code"]` and handle that case explicitly instead
of letting it fall through to a generic internal error:

```python
try:
    update_job_item(job_id)
except ClientError as error:
    if error.response["Error"]["Code"] == "ConditionalCheckFailedException":
        logger.info(
            "Job %s was already updated to IN_PROGRESS (race condition handled).",
            job_id,
        )
    else:
        logger.error("DynamoDB update failed for jobId=%r: %s", job_id, error)
        return _internal_error_response("Failed to record job.")
```

This keeps the happy path correct under concurrent invocations without
treating an expected race as an error, while any *other* `ClientError`
still gets logged and surfaced as a 500.

## Let validation errors short-circuit before any AWS call

Input validation happens first and returns its own response tuple rather
than raising, so the handler can bail out before touching any external
service:

```python
def parse_job_id(event: Dict[str, Any]) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    ...
    return None, _invalid_request_response("jobId is required")
```

```python
job_id, error_response = parse_job_id(event)
if error_response is not None:
    return error_response
```

Prefer this `(value, error_response)` pattern — or raising a small custom
exception and catching it once near the top of `lambda_handler` — over
scattering ad hoc `if` checks and early returns through the middle of the
orchestration logic.

## Do not duplicate error handling

Each AWS-calling helper raises unchanged (`ClientError`, `KeyError`); none
of them catch and re-wrap their own errors. All translation from
"exception" to "API response" happens once, at the single call site in
`lambda_handler`. Avoid handling the same exception type in more than one
place for the same operation.

## What "internal error" responses should never contain

`_internal_error_response` and its callers never include:

* the raw exception message or `repr(error)`,
* a stack trace,
* the AWS error code or request ID,
* the name of the missing environment variable,
* any internal resource name (table name, bucket name, queue URL).

That detail belongs in the log line right above the `return`, not in the
body returned to the caller.
