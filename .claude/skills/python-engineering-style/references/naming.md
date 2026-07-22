# Python Naming Reference

Naming should make the role, scope, and domain meaning of each symbol clear. Follow
PEP 8 and preserve terminology already established by the project.

## General principles

- Prefer descriptive names over abbreviated names.
- Use domain-specific terminology consistently.
- Avoid single-letter names except for short, obvious loop indexes.
- Keep names precise enough to explain what a value represents.
- Do not encode implementation details into public API names unless necessary.
- Match names used by AWS, API Gateway, S3, and the project's external contracts.

## Naming conventions

| Symbol | Convention | Example |
|---|---|---|
| Functions | `snake_case` | `generate_upload_urls` |
| Variables | `snake_case` | `bucket_name` |
| Private functions | Leading underscore plus `snake_case` | `_response` |
| Private variables | Leading underscore plus `snake_case` when appropriate | `_cached_client` |
| Classes | `PascalCase` | `StructuredLoggerAdapter` |
| Exceptions | `PascalCase`, usually ending in `Error` | `InvalidJobIdError` |
| Constants | `UPPER_SNAKE_CASE` | `DEFAULT_EXPIRES_SECONDS` |
| Environment variables | `UPPER_SNAKE_CASE` | `UPLOAD_BUCKET_NAME` |
| Modules | Lowercase `snake_case` | `event_parser` |
| Packages/directories | Lowercase `snake_case` | `lambda_upload` |
| Test files | `test_<module>.py` | `test_handler.py` |
| Test functions | `test_<expected_behavior>` | `test_returns_500_when_bucket_is_missing` |

## Functions and methods

Use verbs that describe the operation performed.

Good examples:

```python
def parse_event(event: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    ...


def sanitize_file_name(file_name: str, index: int) -> str:
    ...


def _generate_upload_urls(file_name: str):
    ...
```

Use a leading underscore for helpers that are implementation details of a module:

```python
def _response(status_code: int, body: Dict[str, Any]) -> Dict[str, Any]:
    ...
```
Use names that describe the result or action rather than generic names:
# Good
parse_expires_in(...)
extract_job_id(...)
_generate_upload_urls(...)

# Avoid
process(...)
handle_data(...)
do_work(...)

The Lambda entry point must use the AWS-compatible name:

```python
def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    ...
```
## Variables
Use descriptive nouns or noun phrases.
```python
bucket_name = os.getenv("UPLOAD_BUCKET_NAME")
folder_id = str(uuid.uuid4())
base_prefix = f"uploads/{folder_id}"
upload_items = []
content_types = []
```
Prefer names that include the unit when a numeric value could be ambiguous:
```python
expires_in = 900
timeout_seconds = 30
max_file_count = 4
```
Avoid vague names:
```python
data = ...
value = ...
result = ...
temp = ...
```
A generic name such as result is acceptable when the surrounding context makes its meaning obvious, for example:
```python
result = _generate_upload_urls(...)
```
### Boolean names
Boolean values and predicates should use a positive question-like prefix:
- is_
- has_
- can_
- should_
Avoid negative or ambiguous boolean names:
```python
# Prefer:
is_configured = ...

# Avoid:
not_missing = ...
flag = ...
```
### Constants and environment variables
Use UPPER_SNAKE_CASE for values that are constant by convention or configuration contract.
```
UPLOAD_BUCKET_NAME
UPLOAD_URL_EXPIRES_SECONDS
DEFAULT_EXPIRES_SECONDS
MAX_UPLOAD_COUNT
```
Environment variable names should match the deployment contract exactly. Do not rename an environment variable in Python merely to make it follow a different style.
Read required configuration using a descriptive local variable:
```python
upload_bucket_name = os.getenv("UPLOAD_BUCKET_NAME")
if not upload_bucket_name:
    raise RuntimeError("Missing required environment variable UPLOAD_BUCKET_NAME")
```
Keep the Python variable in snake_case even when the environment variable uses UPPER_SNAKE_CASE.

### Classes and exceptions
Use PascalCase for classes.
```python
class StructuredLoggerAdapter(logging.LoggerAdapter):
    ...
```
Exception classes should describe the failure and normally end with Error:
```python
class InvalidEventError(Exception):
    ...
```
Use existing library exception names as provided by the library:
```python
from botocore.exceptions import ClientError, ParamValidationError
```
Do not create a custom exception for a failure that is already clearly represented by an existing exception.

### AWS and API terminology
```python
bucket_name
object_key
upload_url
content_type
folder_id
base_prefix
status_code
```
Keep external field names unchanged when they are part of an API contract:
```
{
    "uploadUrl": upload_url,
    "fileName": safe_file_name,
    "contentType": content_type,
    "expiresIn": expires_in,
}
```
Convert external names to Python naming conventions only when assigning them to local Python variables:
```python
upload_url = external_data["uploadUrl"]
safe_file_name = external_data["fileName"]
content_type = external_data["contentType"]
expires_in = external_data["expiresIn"]
```
Do not mix naming styles for the same concept inside Python code. For example, use file_name consistently rather than alternating between file_name and fileName.

### Logging names and context
Use established correlation-field names consistently:
```json
{"jobId": job_id, "stage": "parse_input"}
```
The Python local variable follows Python naming conventions:
```python
job_id = extract_job_id(event or {})
```
The structured log field may preserve the external or logging contract name:
```python
log_context = {"jobId": job_id, "stage": "put_item"}
```
Use stage names that describe the current workflow phase, such as:
```
parse_input
validate_input
put_item
generate_urls
error
```

## Review checklist
* functions and variables use snake_case;\
* classes and exceptions use PascalCase;\
* constants and environment variables use UPPER_SNAKE_CASE;\
* private helpers begin with _;\
* boolean values use is_, has_, can_, or should_;\
* names describe domain meaning and, where useful, units;\
* external API field names remain unchanged;\
* equivalent concepts use the same name throughout the module;\
* test names describe behavior and conditions;\
* abbreviations are avoided unless they are established domain terms;\
* no sensitive value appears in a symbol name intended for logging or output.