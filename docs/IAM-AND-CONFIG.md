# IAM and Configuration Handoff

## Overview

This document specifies the IAM permissions and environment configuration required to deploy the `lambda_upload` Lambda function (`backend/lambda_upload/handler.py`). It reflects what was **actually implemented** in TASKS-01–07, not what was proposed in the original design.

---

## Environment Variables

### Required

| Variable | Purpose | Example |
|----------|---------|---------|
| `UPLOAD_BUCKET_NAME` | S3 bucket name where reference images are uploaded | `miniature-ai-guide-uploads-dev` |

**Behavior if missing:** Lambda returns HTTP 500 with error message "Server misconfiguration: UPLOAD_BUCKET_NAME is not set."

### Optional

| Variable | Purpose | Default | Constraints |
|----------|---------|---------|-------------|
| `UPLOAD_URL_EXPIRES_SECONDS` | Pre-signed URL lifetime in seconds | `900` (15 minutes) | Must be integer > 0 and ≤ 604800 (S3 max: 7 days) |

**Behavior if invalid:** Lambda returns HTTP 500 with error message "Server misconfiguration: UPLOAD_URL_EXPIRES_SECONDS is invalid."

---

## IAM Permissions

### Lambda Execution Role

The Lambda function requires **two** IAM permission sets:

#### 1. CloudWatch Logs (Required)
Use the AWS managed policy: **`AWSLambdaBasicExecutionRole`**

This grants the Lambda permission to write structured JSON logs to CloudWatch — essential for debugging and tracing requests via `jobId` correlation.

**Inline policy name:** (Use managed policy, no inline required)

#### 2. S3 Pre-signed URL Generation (Required)
**Inline policy:** `lambda-upload-s3-put-policy` (custom name; adjust as needed)

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "s3:PutObject"
      ],
      "Resource": "arn:aws:s3:::miniature-ai-guide-uploads-dev/uploads/*"
    }
  ]
}
```

**Key details:**
- **Action**: Only `s3:PutObject` is required. The Lambda does NOT need `s3:GetObject` or `s3:ListBucket`.
- **Why `s3:PutObject`?** The Lambda calls `s3_client.generate_presigned_url(ClientMethod="put_object")` to generate signed URLs. These URLs are sent to the frontend, which then directly PUTs objects to S3 using the presigned credential. The Lambda itself does not read or list objects.
- **Resource scope**: Restrict to `uploads/*` prefix to contain uploads to a predictable folder structure.
- **Bucket name**: Replace `miniature-ai-guide-uploads-dev` with your actual upload bucket.

### IAM Actions NOT Required

- `s3:GetObject` — the Lambda does not read objects
- `s3:ListBucket` — the Lambda does not enumerate bucket contents
- `dynamodb:*` — no DynamoDB integration yet (planned for TASK-04)
- `bedrock:*` — no Bedrock integration yet (planned for later tasks)

---

## API Gateway Integration

### Event Format (Proxy Integration)

The handler expects **API Gateway Lambda proxy integration**, where the event is passed as:

```json
{
  "body": "{\"fileNames\": [...], \"contentTypes\": [...]}",
  "queryStringParameters": {"fileName": "...", "contentType": "..."},
  "isBase64Encoded": false,
  "...": "... other API Gateway fields ..."
}
```

### Request Parsing Rules

1. **Query parameters + JSON body** are both supported.
2. **Body takes precedence:** If both query params and JSON body contain the same field (e.g., `fileName`), the body value is used.
3. **Singular and plural forms accepted:**
   - `fileNames` (list/array) or `fileName` (scalar)
   - `contentTypes` (list/array) or `contentType` (scalar)
4. **Base64-encoded bodies** are auto-decoded if `isBase64Encoded: true`.
5. **Fallback behavior:** If fewer than 4 file names/types are provided, missing entries default to `file_1.bin`, `file_2.bin`, etc., and the first provided content type (or `application/octet-stream`).

### Response Format

**Success (HTTP 200):**
```json
{
  "statusCode": 200,
  "headers": {
    "Content-Type": "application/json",
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Content-Type,Authorization",
    "Access-Control-Allow-Methods": "OPTIONS,POST"
  },
  "body": "{\"bucket\": \"...\", \"folder\": \"<uuid>\", \"prefix\": \"uploads/<uuid>\", \"uploadItems\": [...], \"expiresIn\": 900}"
}
```

**Error (HTTP 500):**
```json
{
  "statusCode": 500,
  "headers": { "Content-Type": "application/json", ... },
  "body": "{\"error\": \"<human-readable error message>\"}"
}
```

**CORS headers** are always present to allow cross-origin requests from the frontend.

---

## Pre-Deployment Blockers

### ⚠️ Authentication NOT Implemented

**Status:** The Lambda handler **does not perform any authentication**. No JWT validation, Cognito check, or API key verification happens at the handler level.

**Action required before production:**
1. Configure API Gateway to require **AWS Cognito User Pool authorization** on the POST endpoint.
2. Alternatively, use API Gateway resource policies or AWS IAM roles if your auth flow requires it.
3. Ensure the frontend passes valid Cognito tokens in the `Authorization` header.

The Lambda currently has **no auth guards** — anyone with the API Gateway URL can invoke it. The handler relies on **API Gateway to enforce auth**, not on code-level validation.

### ✓ Ready to Deploy
- S3 bucket exists and is scoped for uploads only (no public access).
- Lambda role has correct IAM permissions (S3 + CloudWatch).
- Environment variables (`UPLOAD_BUCKET_NAME`, `UPLOAD_URL_EXPIRES_SECONDS`) are set.
- API Gateway proxy integration is configured.
- **Cognito auth is wired up at the API Gateway level** (not in the handler).

---

## Logging

All logs are **structured JSON** and include:
- `timestamp` (ISO-8601 UTC)
- `level` (INFO, WARNING, ERROR)
- `jobId` (correlation ID from query param or body; defaults to "unknown")
- `stage` (lifecycle: `parse_input`, `put_item`, `error`)
- `message` (human-readable text)

**Important security note:** Presigned URLs and sensitive credentials are **never logged**.

---

## Deployment Configuration Summary

| Component | Value | Notes |
|-----------|-------|-------|
| **Runtime** | Python 3.12 | Matches AWS Lambda native runtime |
| **Handler** | `handler.lambda_handler` | Defined in `backend/lambda_upload/handler.py` |
| **Memory** | 128 MB | Sufficient for URL generation (no heavy compute) |
| **Timeout** | 10 seconds | Presigned URL generation is fast (~100ms) |
| **VPC** | None | S3 access does not require VPC |
| **Layers** | None (deps in ZIP) | Dependencies bundled in Lambda package |
| **Concurrency** | Unreserved (or as needed) | Stateless; scales horizontally |

---

## Next Steps for CDK Implementation

When writing the CDK stack (`infrastructure/` or similar):

1. **Create S3 bucket** with:
   - Block all public access enabled
   - No versioning (uploads are append-only)
   - Bucket name from config (environment variable or stack parameter)

2. **Create IAM role** for Lambda:
   - Attach `AWSLambdaBasicExecutionRole` (managed policy)
   - Add inline policy: `lambda-upload-s3-put-policy` (see IAM section above)

3. **Create Lambda function** with:
   - Runtime: Python 3.12
   - Handler: `handler.lambda_handler`
   - Role: The IAM role from step 2
   - Environment variables: `UPLOAD_BUCKET_NAME`, optionally `UPLOAD_URL_EXPIRES_SECONDS`

4. **Wire up API Gateway**:
   - Create POST endpoint with Lambda proxy integration
   - **Add Cognito User Pool authorizer** (auth not yet in handler code)
   - Enable CORS (handler already emits CORS headers)

---

## Modifications Needed Before TASK-03+

This document covers only the `lambda_upload` function (HTTP 200/500 presigned URL generation). Upcoming tasks will add:

- **TASK-03:** Image listing validation (new Lambda or endpoint → HTTP 422)
- **TASK-04:** DynamoDB job record creation (new Lambda or endpoint → HTTP 201)
- **TASK-05:** Upload confirmation flow (new Lambda or endpoint → HTTP 200)
- **TASK-06–07:** Bedrock generation and PDF generation (separate Lambdas)

Each new Lambda will require its own IAM role and environment configuration. Update this document as needed.
