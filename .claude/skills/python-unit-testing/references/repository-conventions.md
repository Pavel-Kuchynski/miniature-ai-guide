# Repository testing conventions

Reference implementation: `backend/paint/tests/` (`conftest.py`, `test_handler.py`,
`test_logging_config.py`). Copy its structure for new modules.

## Framework

* **New modules use `pytest` + `moto`** (`mock_aws`), as `backend/paint` does.
  Put `pytest` and `moto` in the module's `requirements-dev.txt`, not `requirements.txt`.
* **Older modules that already use `unittest` keep it** (`lambda_upload`, `start_job`, ...).
  Do not convert them, and do not mix frameworks inside one module.
* Never hit real AWS or a real third-party API. `moto` (`mock_aws`) counts as a fake, not as
  real AWS, so it is allowed for S3, DynamoDB, SQS and Secrets Manager. Use
  `unittest.mock.patch` for non-AWS services (OpenAI, HTTP uploads).

## Layout

```text
backend/<module>/tests/
  __init__.py
  conftest.py              # sys.path + default AWS region only
  test_handler.py          # handler and its helpers
  test_logging_config.py   # only if the module owns logging classes
```

`conftest.py` stays minimal. It puts the module root on `sys.path` (handlers use flat
imports such as `from job_status import JobStatus`) and sets
`os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")`. Put fixtures that only one file
uses in that file.

## Structure of `test_handler.py`

1. Module docstring listing what is covered.
2. Imports: stdlib, third-party, then `from handler import ...`.
3. Test constants (`UPLOAD_BUCKET`, `TABLE_NAME`, `JOB_ID`, `FAKE_IMAGE`, ...).
4. Small builders with docstrings (`_make_sqs_event`, `_make_image_bytes`,
   `_put_job`, `_create_dynamo_table`). Build real data rather than hand-written dicts.
5. An `autouse` fixture that resets module-level state, then one `Test<Function>` class per
   function under test, then `TestLambdaHandler` for orchestration.

```python
@pytest.fixture(autouse=True)
def _clear_handler_caches() -> Iterator[None]:
    """Reset module-level caches so one test's client or key cannot leak into the next."""
    _get_client.cache_clear()
    _clear_openai_api_key_cache()
    yield
    _get_client.cache_clear()
    _clear_openai_api_key_cache()
```

Any module-level cache added to a handler needs a reset hook (`cache_clear()` or a
`_clear_*_cache()` helper) and an entry in this fixture.

## Environment variables

Provide required configuration through an `env_vars` fixture and `patch.dict(os.environ, ...)`.
Because handlers read configuration at call time, each test controls its own environment.
Every helper that reads a required variable has a `test_missing_<var>_env_var_raises_key_error`
test.

## What to cover

For each helper: success, each documented `Raises` entry, missing configuration, and the
boundary values of any validation (empty, whitespace-only, wrong type, non-object JSON).

For `lambda_handler`, one test per branch of the orchestration:

* happy path, asserting persisted state (DynamoDB item, SQS message), not just "no error";
* invalid input and resource-not-found raising the documented exception;
* each failing step (download, fetch, generate, upload) leaving the job `FAILED` and sending
  the failure notification;
* failures of the status write and of notifications being logged and not raised;
* a conditional write rejected because another writer changed state (simulate the race with
  a side effect that mutates the item).

Test caches explicitly: reuse within the TTL, refetch after expiry (monkeypatch
`time.monotonic`), forced refresh, and that a failed fetch is not cached.

## Logging assertions

Assert on log records with `caplog`, not on message text alone:

```python
[record] = self._error_records(caplog)
assert record.stage == "update_status_painted_error"
assert record.jobId == JOB_ID
assert record.exc_info is not None
```

Also assert that secrets never reach logs or queue messages (for example that the failure
message holds only the exception class name).

## Naming and style

* Class: `Test<FunctionOrBehavior>`. Test: `test_<expected_behavior>`, for example
  `test_key_rejection_persisting_after_refresh_propagates`.
* One-line docstring on non-obvious tests stating the behavior under test.
* Use `@pytest.mark.parametrize` for input matrices (malformed bodies, error classes).
* Type-hint test methods (`-> None`) and fixtures.
* Build SDK errors the way the SDK raises them (for example `httpx.Response` for OpenAI
  status errors) instead of bare `Exception`.

## Running

```bash
# from backend/<module>/
pip install -r requirements.txt -r requirements-dev.txt
pytest                       # pytest-based modules
python -m unittest discover -s tests   # unittest-based modules
```

State the correct command in the module `README.md`. `python -m unittest` collects zero
tests from plain pytest classes.

## Testing AWS helpers

* Mock `boto3` clients/resources (`moto` or `unittest.mock`). Never call real AWS from unit tests.
* Each AWS interaction lives in its own helper, so tests can patch one helper at a time and
  verify `lambda_handler` branching (status codes, DLQ vs `FAILED`) without simulating a full
  AWS response for every case.
* Test the `ConditionalCheckFailedException` "already updated" path explicitly, not just plain
  success and failure. Races are real production behavior.
* Test every module-level cache: reuse within the TTL, refetch after expiry, forced refresh,
  and that a failed fetch is not cached. Reset caches in an `autouse` fixture.
