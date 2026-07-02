# lambda_upload

AWS Lambda function that creates four pre-signed S3 `PUT` upload URLs — one call per generation
request, all four files grouped under a single UUID-based `uploads/<uuid>/` prefix so downstream
processing can find the four reference images for one job together.

## Lambda Handler

- Set Lambda handler to `handler.lambda_handler`.
- Entry point signature: `lambda_handler(event: dict, context) -> dict`, returning an
  API-Gateway-style response (`statusCode`, `headers`, `body` as a JSON string).

## Environment Variables

- `UPLOAD_BUCKET_NAME` (required)
  - S3 bucket name where files will be uploaded.
  - Example: `my-app-upload-bucket`
  - If unset, the function returns HTTP 500.

- `UPLOAD_URL_EXPIRES_SECONDS` (optional)
  - Pre-signed URL lifetime in seconds.
  - Default: `900` (15 minutes).
  - Must be an integer greater than `0` and no more than `604800` (S3's 7-day max for
    presigned URLs). If the value is missing/invalid or out of range, the function returns
    HTTP 500.
  - Example: `600`

## Request input

File names and content types can be supplied via query string parameters or a JSON body,
either as the singular (`fileName` / `contentType`) or plural (`fileNames` / `contentTypes`,
list or comma-separated string) form. When both a query parameter and a body value are
present for the same field, **the body value takes precedence**.

The function always returns exactly 4 upload items, regardless of how many names/types were
supplied:
- Missing file names fall back to `file_1.bin`, `file_2.bin`, etc. (by position).
- Missing content types fall back to the first content type provided, or
  `application/octet-stream` if none was provided.
- Extra names/types beyond the first 4 are ignored.

Supplied file names are sanitized before being used in the S3 object key: only
`[A-Za-z0-9._-]` characters are kept (others replaced with `_`), the name is reduced to its
base name (path components stripped), and it's capped at 255 characters. An empty, `.`, or
`..` result falls back to `file_<n>.bin`.

## Response shape

On success (HTTP 200):

```json
{
  "bucket": "my-app-upload-bucket",
  "folder": "5f2c...-uuid",
  "prefix": "uploads/5f2c...-uuid",
  "uploadItems": [
    {"uploadUrl": "...", "key": "uploads/5f2c.../file_1.bin", "fileName": "file_1.bin", "contentType": "application/octet-stream"}
  ],
  "expiresIn": 900
}
```

On failure (HTTP 500), the body is `{"error": "<message>"}`. Internal exception details are
never included in the response; failures are logged server-side via the `logging` module
instead.

## Development

```bash
# from backend/lambda_upload/
pip install -r requirements.txt

# run all tests
python -m unittest discover -s tests
```
