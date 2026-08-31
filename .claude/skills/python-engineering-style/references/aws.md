# AWS and external-service conventions

> The examples below come from one illustrative Lambda (`start-job`, which
> reads job state from DynamoDB, verifies uploaded images in S3, and
> triggers downstream work via SQS). Use the equivalent AWS services and
> resource names for whatever the module under review actually integrates
> with — don't force an unrelated module to talk to these same tables,
> buckets, or queues.

## Keep AWS operations behind focused helper functions

Each AWS service gets its own small, single-purpose helper rather than
Lambda calls sprinkled through the orchestration logic:

```python
def list_uploaded_images(job_id: str) -> List[str]: ...
def get_job_status(job_id: str) -> Optional[str]: ...
def update_job_item(job_id: str) -> None: ...
def trigger_guide_creation(job_id: str) -> None: ...
```

`lambda_handler` calls these helpers and reacts to their return values or
exceptions; it never constructs a `boto3` client or builds a request
itself. This keeps the entry point focused on orchestration and makes each
AWS interaction independently testable (mock the helper, or mock the
`boto3` client it creates).

## Read required configuration from the environment at call time

Read environment variables inside the helper that needs them, at call
time — not once at module import — so tests can monkeypatch
`os.environ` per test case:

```python
def list_uploaded_images(job_id: str) -> List[str]:
    bucket_name = os.environ["UPLOAD_BUCKET_NAME"]
    ...
```

Use `os.environ["NAME"]` (not `.get`) for configuration that is *required*
for the operation to make sense. Let the resulting `KeyError` propagate to
the caller rather than silently defaulting — see `error-handling.md` for
how `lambda_handler` turns that into a "server misconfiguration" response.
Do not invent a fallback value for required configuration.

## DynamoDB

### Reads

Use `ConsistentRead=True` when the caller's next decision depends on
having the current state, as `get_job_status` does immediately before
deciding whether to update the item:

```python
response = dynamodb_client.get_item(
    TableName=table_name,
    Key={"jobId": {"S": job_id}},
    ConsistentRead=True,
)
item = response.get("Item")
if item is None:
    return None
```

Treat a missing item (`response.get("Item")` is `None`) as a normal,
expected outcome to return — not an exception to raise or catch.

### Writes: use `ConditionExpression` for invariants that matter

`get_item` followed by `update_item` is **not atomic** — another
invocation could change the item's state in between. Don't rely on the
read to guarantee the write is safe. Instead, encode the invariant
directly in the write's `ConditionExpression`:

```python
dynamodb_client.update_item(
    TableName=table_name,
    Key={"jobId": {"S": job_id}},
    UpdateExpression="SET jobStatus = :status, updatedAt = :now",
    ConditionExpression="jobStatus = :expected",
    ExpressionAttributeValues={
        ":status": {"S": "IN_PROGRESS"},
        ":now": {"S": datetime.datetime.now(datetime.timezone.utc).isoformat()},
        ":expected": {"S": "UPLOADED"},
    },
)
```

This makes the transition atomic: the update only succeeds if the item's
`jobStatus` is still `"UPLOADED"` at write time, regardless of what was
read earlier. Handle `ConditionalCheckFailedException` explicitly at the
call site rather than treating it as a generic failure — whether that
means "someone else already did this, treat as success" (a race the
handler tolerates) or "this really is a conflict" depends on the
operation; see `error-handling.md` for the race-tolerant example.

### Don't assume `update_item` fails when the item doesn't exist

By default, `update_item` will *create* an item if the key doesn't exist,
rather than failing. If the operation requires the item to already exist,
that requirement must be expressed with a `ConditionExpression` (e.g.
`attribute_exists(jobId)` or, as above, a condition on a field's current
value) — don't rely on default `update_item` behavior to enforce it.

### Don't introduce transactions unless required

A single conditional `update_item` is sufficient for a single-item,
single-invariant update. Reach for `transact_write_items` only when an
operation must atomically touch more than one item or table — not by
default.

## S3

### List with pagination, don't assume a single page

`list_objects_v2` truncates results; use the paginator rather than
assuming all objects come back in one response:

```python
s3_client = boto3.client("s3")
paginator = s3_client.get_paginator("list_objects_v2")

keys: List[str] = []
for page in paginator.paginate(Bucket=bucket_name, Prefix=prefix):
    for obj in page.get("Contents", []):
        key = obj["Key"]
        if key == prefix or obj["Size"] == 0:
            continue
        keys.append(key)
```

### Filter out non-data artifacts explicitly, and say why

S3 "folder" placeholder keys (zero-byte objects matching the prefix
itself) are not real data and must be excluded — but only skip them where
they're actually possible, and comment on *why* the filter exists rather
than leaving it unexplained:

```python
if key == prefix or obj["Size"] == 0:
    continue
```

### Don't trust the caller's report of what it uploaded

`list_uploaded_images` re-lists S3 directly rather than trusting a count
or manifest the frontend might have sent, because the frontend "chooses
arbitrary file names and this Lambda must independently verify what
actually landed in S3." When a Lambda's correctness depends on external
state (a bucket, a table), verify that state directly at the point of
use rather than trusting an untrusted caller's claim about it.

## SQS

Keep message construction minimal and serializable — pass only the
identifiers the downstream consumer needs to look up the rest of the
state itself, rather than duplicating data into the message body:

```python
sqs_client.send_message(
    QueueUrl=queue_url,
    MessageBody=json.dumps({"jobId": job_id}),
)
```

## Boto3 client usage

* Create clients (`boto3.client("s3")`, `boto3.client("dynamodb")`,
  `boto3.client("sqs")`) inside the helper function that uses them, not
  as module-level globals, unless there's a demonstrated reuse/performance
  need across many invocations — this keeps helpers straightforward to
  mock in tests and avoids sharing state across warm Lambda invocations
  in ways that are hard to reason about.
* Handle AWS-specific exceptions explicitly
  (`botocore.exceptions.ClientError`) rather than a bare `except Exception`
  around the AWS call.
* Let AWS exceptions propagate unchanged out of the helper — translation
  to an API response happens once, at the call site in `lambda_handler`
  (see `error-handling.md`).

## Testing AWS code

* Mock `boto3` clients/resources (e.g. with `unittest.mock` or `moto`).
  Never call real AWS services from unit tests.
* Because each AWS interaction lives in its own helper
  (`get_job_status`, `list_uploaded_images`, `update_job_item`,
  `trigger_guide_creation`), tests can mock one helper at a time and
  verify `lambda_handler`'s branching (404 vs 409 vs 422 vs 500) without
  needing to simulate a full AWS response for every case.
* Test the `ConditionalCheckFailedException` "already updated" path
  explicitly, not just the plain success and plain-failure paths — races
  are a real production behavior, not an edge case to skip.
