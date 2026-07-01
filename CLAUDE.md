# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

AI-powered tool that generates painting guides for miniature models. Users upload 4 reference images; the system produces a structured step-by-step PDF painting plan using generative AI. Built on AWS serverless architecture.

Planned architecture (see `docs/progect_structure.md` and `docs/globalIdea.md`):

```
Frontend (S3 hosting)
  │
  ├── Upload flow: API Gateway + Cognito → Lambda (presigned URL) → S3 (upload images)
  └── Generation flow: API Gateway + Cognito → Lambda (start job) → Step Functions
                                                                       ├── Bedrock AI
                                                                       ├── Validate JSON
                                                                       └── PDF Lambda → S3 output
```

The repo is currently early-stage: only the upload-URL Lambda (`backend/lambda_upload`) is implemented. Other pieces (frontend, generation Lambda, Step Functions workflow, PDF Lambda, infra-as-code) do not exist yet — don't assume files/modules for them are present.

## Repository layout

- `backend/lambda_upload/` — Lambda function that issues 4 pre-signed S3 PUT URLs for image upload, all under a single UUID-based `uploads/<uuid>/` prefix.
  - `handler.py` — the Lambda entry point (`lambda_handler`), plus event-parsing helpers.
  - `tests/test_handler.py` — unittest suite, mocks `boto3` S3 client.
  - `requirements.txt` — Lambda dependencies (`boto3`, `boto3-stubs[s3]`).
- Each backend module is expected to be self-contained (its own `requirements.txt`, `tests/`, `README.md`) rather than sharing a monorepo-wide dependency file — follow this pattern when adding new Lambda functions.

## Development commands

Run from `backend/lambda_upload/`:

```bash
# install deps (use the repo's .venv)
pip install -r requirements.txt

# run all tests
python -m unittest discover -s tests

# run a single test file
python -m unittest tests.test_handler

# run a single test case
python -m unittest tests.test_handler.TestLambdaUploadHandler.test_generates_four_urls_in_single_uuid_folder
```

The project's Python interpreter is `.venv/Scripts/python.exe` (already configured in `.vscode/settings.json`).

## Lambda: `lambda_upload`

- Entry point: `handler.lambda_handler` (see `backend/lambda_upload/README.md`).
- Required env var: `UPLOAD_BUCKET_NAME` — S3 bucket for uploads. Missing → HTTP 500.
- Optional env var: `UPLOAD_URL_EXPIRES_SECONDS` — presigned URL TTL, default `900`.
- Always generates exactly 4 upload items per invocation, regardless of how many file names/content types are provided (missing entries fall back to `file_N.bin` / the first given content type / `application/octet-stream`).
- All 4 files for one request share a single UUID folder (`uploads/<uuid>/...`), generated fresh per invocation — this groups the 4 reference images together for downstream processing.
- `fileNames`/`contentTypes` (or singular `fileName`/`contentType`) can come from query string params or JSON body; body values take precedence over query values when both are present.
