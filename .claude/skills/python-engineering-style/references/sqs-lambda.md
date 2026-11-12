# SQS-triggered Lambda conventions

Reference implementation: `backend/paint/handler.py`. Use this for any Lambda triggered by
SQS (no API Gateway response). API-Gateway Lambdas follow `error-handling.md` instead.

## Input

* `parse_job_id(event) -> Optional[str]` reads `Records[0].body` and returns `None` for
  every malformed shape: missing or non-list `Records`, non-object record, invalid JSON,
  non-object body, missing/non-string/blank id. It never raises. Return the trimmed value.
* The handler turns `None` into `ValueError("Missing or invalid jobId in SQS message")` after
  logging with `jobId=UNKNOWN`, `stage="parse_input"`.

## Choosing between raising and returning

The choice decides what SQS does with the message.

| Situation | Behavior | Why |
|---|---|---|
| Bad input, job missing, job in wrong status | log, then `raise ValueError(...)` | Retrying cannot help; the message goes to the DLQ. |
| Missing required configuration (`KeyError`) | let it propagate | Deployment bug; must be visible, not reported as a business failure. |
| Failure in the work itself (download, model call, upload) | mark the job `FAILED`, notify the result queue, **return** | The failure is recorded; the message is consumed instead of retried and re-billed. |
| Failure of a bookkeeping step (status write, downstream notify) | log with traceback, **return** | Do not double-report; never mask the original outcome. |

Convert domain exceptions at the handler boundary and keep the cause:
`raise ValueError(f"Job {job_id} not found") from exc`.

## Failure flow

1. Wrap the work in one deliberate `except Exception` (a top-level safety boundary).
2. `log.exception(..., extra={"stage": "<step>_error"})`.
3. `_mark_failed(job_id)`: conditional write to `FAILED`; catches `ClientError`, logs, never raises.
4. `_report_failure(job_id, exc)`: sends `{"jobId", "status", "message"}` to the result queue;
   catches `(ClientError, KeyError)`, logs, never raises.
5. Return.

The client-facing `message` is `f"<Operation> failed: {type(exc).__name__}"`. Never put
`str(exc)` in a queue message, because SDK errors can echo secrets such as part of an API key.

Helpers that must not raise (`_mark_failed`, `_mark_painted`, `_report_failure`) are private,
catch the narrowest exceptions, and say "logging instead of raising" in their docstrings.

## State machine

* Statuses are a `StrEnum` in their own module (`job_status.py`), never string literals.
* Validate the current status before doing work, and encode the same expectation in the write
  (`ConditionExpression="jobStatus = :expected"`). The read is advisory; the condition is the guard.
* Read the job once with `ConsistentRead=True` and return a small frozen dataclass
  (`JobRecord`) instead of several reads or a raw DynamoDB dict.

## Message contracts

Messages carry identifiers, not data: `{"jobId": "<uuid>"}`. Failure messages add `status`
and `message`. Document every input and output message shape in the README.

## Timeouts

The Lambda timeout must exceed the worst case of every call chain inside it. Compute the
budget (for example `(max_retries + 1) * timeout` plus backoff), write it in a comment next to
the constants, and in the README. Otherwise the invocation dies before reaching its `FAILED` write.
