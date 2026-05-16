# lambda_upload

AWS Lambda function that creates a pre-signed S3 upload URL.

## Lambda Handler

- Set Lambda handler to `lambda_upload.handler.lambda_handler`

## Environment Parameters

- `UPLOAD_BUCKET_NAME` (required)
  - S3 bucket name where files will be uploaded.
  - Example: `my-app-upload-bucket`

- `UPLOAD_URL_EXPIRES_SECONDS` (optional)
  - Pre-signed URL lifetime in seconds.
  - Default: `900` (15 minutes)
  - Example: `600`

## Notes

- If `UPLOAD_BUCKET_NAME` is not set, the function returns HTTP 500.
- If `UPLOAD_URL_EXPIRES_SECONDS` is not set, the function uses the default value.
