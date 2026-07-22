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