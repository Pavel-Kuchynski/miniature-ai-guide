# Python Code Review Guidelines

Review code in context and report only issues that materially affect:

- Correctness
- Security
- Reliability
- Maintainability
- Observability
- Testability
- Repository conventions

Before reviewing:

1. Read the complete relevant module.
2. Read nearby tests.
3. Inspect related modules and the intended workflow.
4. Identify established repository patterns.
5. Determine whether the code is production, test, infrastructure, or prototype code.
6. Prefer the smallest change that fixes the actual problem.

Do not recommend refactors based only on personal preference or theoretical improvements.

---

## Finding categories

Classify every finding as one of the following:

### Bug

The implementation can produce incorrect behavior.

Examples:

- Wrong return value or status code
- Incorrect condition or state transition
- Broken edge case
- Data corruption
- Unhandled required exception

### Security

The implementation creates a meaningful security risk.

Examples:

- Authentication or authorization bypass
- Secret exposure
- Injection vulnerability
- Unsafe token or credential handling
- Insecure cryptographic validation

Distinguish authentication from authorization:

- Authentication: Who is the caller?
- Authorization: May this caller perform this operation on this resource?

A valid JWT does not automatically authorize access to every resource. Check ownership, membership, roles, permissions, or other access-control rules unless a trusted surrounding layer explicitly provides them.

### Reliability

The implementation may fail unpredictably or degrade in production.

Examples:

- Unhandled external-service failures
- Missing network timeout
- Unsafe retry behavior
- Race conditions
- Non-atomic persistence operations
- Incorrect AWS error handling
- Resource lifecycle problems

### Maintainability

The code works but is unnecessarily difficult to understand, test, or change.

Examples:

- Excessively large function with unrelated responsibilities
- Duplicated complex logic
- Hidden side effects
- Misleading names
- Tightly coupled external dependencies

Only report meaningful maintainability problems.

### Observability

Production behavior is unnecessarily difficult to diagnose.

Check for:

- Useful correlation identifiers such as `jobId`, `userId`, `connectionId`, or request ID
- Meaningful workflow stages such as `parse_input`, `validate`, `persist`, and `error`
- Important failures being logged with useful context
- Sensitive data being excluded from logs

Never log passwords, tokens, API keys, private keys, authorization headers, or complete events that may contain secrets.

### Testability

Important behavior is difficult to verify.

Examples:

- Hard-coded external dependencies
- Logic that cannot be isolated
- Missing tests for important branches
- Real AWS calls in unit tests

### Style

The code differs from repository conventions without affecting behavior.

Examples:

- Naming or import-order inconsistencies
- Missing required docstrings
- Formatting differences

Style findings are lower priority. Do not present them as bugs.

---

## Severity levels

Use these levels:

### Critical

Immediate or severe impact.

Examples:

- Authentication or authorization bypass
- Credential exposure
- Severe data corruption
- Destructive security vulnerability

### High

Significant production or security problem that should be fixed before release.

Examples:

- Major data-integrity issue
- Unhandled failure in a critical workflow
- Serious race condition
- Missing resource authorization

### Medium

Meaningful issue that should be addressed but normally does not block deployment.

Examples:

- Unreliable error handling
- Missing important validation
- Difficult-to-diagnose production failure
- Significant maintainability problem

### Low

Minor issue or improvement.

Examples:

- Small naming inconsistency
- Limited duplication
- Minor documentation gap

### Informational

Optional observation or improvement. Do not present it as required.

---

## Review output

Prioritize findings by severity. For each finding, include:

1. Severity
2. Category
3. Location
4. Problem
5. Why it matters
6. Recommended fix

Use this format:

```text
[HIGH] Security — authorization
Location: _validate_request()

Problem:
The authenticated user is validated, but the code does not verify
that the user may access the requested resource.

Why it matters:
A valid token proves identity but does not establish permission
to access a specific resource.

Recommendation:
Verify the authenticated principal against resource ownership
or the applicable access-control rules.
```

Keep findings specific and actionable. Avoid vague comments such as “This could be improved.”

---

## Correctness checklist

Trace the complete flow:

```text
request
  -> parse input
  -> validate input
  -> authenticate
  -> authorize
  -> validate resource
  -> perform operation
  -> persist state
  -> return response
```

Check:

- Required fields and types
- Empty, missing, malformed, and boundary values
- Input size and identifier constraints where relevant
- Return values and status codes
- Data transformations
- Persistence behavior
- State transitions
- Success and failure paths
- Correct ordering of validation and side effects

Do not duplicate validation unnecessarily when a trusted upstream layer guarantees the invariant. A directly exposed Lambda should normally validate untrusted input at its own boundary.

---

## Error handling

Handle errors at the appropriate boundary.

Check for:

- Bare `except` blocks
- Overly broad handlers that hide programming errors
- Swallowed exceptions
- Incorrect status codes
- Raw internal errors returned to clients
- Missing diagnostic logs
- Duplicated error handling

Good error handling should:

1. Catch the appropriate exception.
2. Log useful internal context.
3. Return a stable external error.
4. Avoid leaking implementation details.

A broad `Exception` handler is not automatically a problem. It may be appropriate as a top-level safety boundary or around an unsafe external dependency.

---

## AWS and Lambda checks

For AWS code, review:

- IAM permissions
- Boto3 exception handling
- DynamoDB consistency
- Conditional writes
- Race conditions
- Lambda lifecycle and timeouts
- Retries and idempotency
- External network calls

For DynamoDB, remember that:

```text
get_item()
  -> item exists
  -> update_item()
```

is not atomic. If an item must already exist, use an appropriate `ConditionExpression`. Do not introduce transactions unless they are required.

For Lambda handlers, check:

- Correct entry point
- Event parsing and validation
- Response format
- Environment configuration
- Exception boundaries
- Logging
- AWS client usage
- Timeout-sensitive operations
- Safety of module-level state across warm invocations

Prefer:

```text
lambda_handler
  -> focused helpers
  -> external service operations
```

Avoid unnecessary architectural layers.

---

## External services

For HTTP and other network calls, check:

- Explicit timeout
- Authentication
- Response validation
- Appropriate error handling
- Retry behavior
- Sensitive-data handling

Do not automatically recommend retries. Recommend them only when the operation is safe to repeat, the failure is likely transient, and the retry policy is appropriate.

---

## Secrets and sensitive data

Check logs, exceptions, responses, URLs, query parameters, and persisted records for accidental exposure of:

- Passwords
- Tokens
- Credentials
- API keys
- Private keys
- Personally identifiable information

Assess sensitivity in the context of the application's actual threat model. Do not automatically classify every email address or user ID as a security issue.

---

## Testing review

Check that tests cover important behavior rather than implementation details:

- Happy paths
- Invalid input
- Missing configuration
- Authentication and authorization failures
- Resource-not-found cases
- External-service failures
- Persistence failures
- Response mapping
- Regression cases for discovered bugs

For AWS code:

- Mock Boto3 clients and resources.
- Never call real AWS services from unit tests.

Do not demand tests for trivial behavior already covered indirectly.

---

## Readability and complexity

Prefer names that communicate intent:

```python
job_id
connection_id
expected_issuer
jobs_table_name
```

Short names are acceptable in small, unambiguous scopes.

Look for complexity that makes behavior difficult to reason about:

- Deeply nested conditionals
- Long functions with multiple responsibilities
- Duplicated complex logic
- Hidden side effects
- Unnecessary global state
- Excessive indirection

Split code when a clear responsibility or boundary exists—not merely because a function is long.

Comments should explain why something unusual is necessary, such as:

- An external constraint
- A security decision
- A workaround
- An intentional trade-off

Avoid comments that merely restate the code.

---

## Review discipline

Before suggesting a refactor, ask:

1. Does the current code work correctly?
2. Is there a concrete bug or operational problem?
3. Will the change reduce meaningful complexity?
4. Will it improve testability, security, or reliability?
5. Is the added complexity justified?

If the answer is no, do not recommend the refactor as required. Mention it only as an optional improvement when it provides clear value.

Preserve established repository patterns, including:

- Structured logging
- `StructuredLoggerAdapter`
- Workflow `stage` fields
- Custom domain exceptions
- `_response()` helpers
- Focused extraction helpers
- AWS operations isolated in helpers
- Runtime environment-variable access
- Existing `unittest` conventions

Do not replace an established pattern merely because another approach is more fashionable.

---

## Conclusion

End every review with a concise summary:

```text
Summary:
- 1 high-severity security issue
- 1 medium-severity reliability issue
- No correctness issues found
- Remaining observations are stylistic and non-blocking
```

If no significant problems are found, say so clearly:

```text
No significant correctness, security, or reliability issues found.

The implementation follows the repository's established patterns for
logging, error handling, AWS integration, and testability.

Minor style improvements are optional and do not require changes.
```

Do not invent findings to make a review appear more thorough. The goal is to improve the code, not maximize the number of comments.