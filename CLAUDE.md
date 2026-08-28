# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

AI-powered tool that generates painting guides for miniature models. 
Users upload 4 reference images; the system produces a structured step-by-step PDF painting plan using generative AI. Built on AWS serverless architecture.

Planned architecture (see `docs/progect_structure.md` and `docs/globalIdea.md`):

```
Frontend (S3 hosting)
  │
  ├── Upload flow: API Gateway + Cognito → Lambda (presigned URL) → S3 (upload images)
  └── Generation flow: API Gateway + Cognito → Lambda (start job)
                          → Lambda (Bedrock generation): calls Bedrock AI,
                            uploads result images + result JSON to a
                            separate output S3 bucket under <uuid>/
                          → Lambda (PDF generation): reads that output,
                            renders PDF → S3 output
```

## Repository map
| Path                        |What it is|
|-----------------------------|-----------|
| `backend/lambda_upload/`    |Lambda function that generates 4 presigned S3 PUT URLs for image upload, all under a single UUID-based `uploads/<uuid>/` prefix.|
| `backend/upload-confirmation/` | Lambda that confirms uploaded images in S3 and records the job in DynamoDB.|
| `backend/open_connection/`  | Lambda that authenticates WebSocket connections and stores connection metadata in DynamoDB.|
| `backend/close_connection/` | Lambda that handles closing WebSocket connections and cleans up connection metadata in DynamoDB.|
| `backend/start_job/`        | Lambda function that initiates guide creation: validates job exists and is in "UPLOADED" status, verifies exactly 4 images uploaded to S3, updates job status to "IN_PROGRESS", and triggers guide creation via SQS message.|
| `frontend/`                 | S3-hosted static web app (vanilla JavaScript + Vite) for user interaction: upload images, start guide generation, view progress and download PDF. Includes auth, API client, S3 upload, WebSocket updates, and validation modules.|

## Repository layout
- `backend/lambda_upload/` — Lambda function that issues 4 pre-signed S3 PUT URLs for image upload, all under a single UUID-based `uploads/<uuid>/` prefix.
  - `handler.py` — the Lambda entry point (`lambda_handler`), plus event-parsing helpers.
  - `tests/test_handler.py` — unittest suite, mocks `boto3` S3 client.
  - `requirements.txt` — Lambda dependencies (`boto3`, `boto3-stubs[s3]`).
- Each backend module is expected to be self-contained (its own `requirements.txt`, `tests/`, `README.md`) rather than sharing a monorepo-wide dependency file — follow this pattern when adding new Lambda functions.

## Development commands

### Backend Lambdas
#### `backend/<lambda_name>/`

```bash
# Install dependencies
pip install -r requirements.txt

# Run all tests
python -m unittest discover -s tests

# Run a single test file
python -m unittest tests.test_handler

# Run a single test case
python -m unittest tests.test_handler.TestLambdaUploadHandler.test_generates_four_urls_in_single_uuid_folder
```
### Frontend

Run from `frontend/`:
```bash
# Install dependencies
npm install

# Development server (hot reload)
npm run dev

# Build production bundle
npm run build

# Preview production build locally
npm run preview

# Run tests once
npm test

# Run tests with coverage
npm test -- --coverage

# Lint code
npm run lint

# Format code
npm run format
```

### General Notes

- The project's Python interpreter is `.venv/Scripts/python.exe` (already configured in `.vscode/settings.json`).
- Each backend Lambda module is self-contained with its own `requirements.txt` and `tests/` directory.
- Frontend uses Node.js 18+ and npm; environment config goes in `.env.local` (git-ignored).