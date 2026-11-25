# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

AI-powered tool that helps painters colour miniature models.
Users upload one reference image; OpenAI generates a painted version of it, Amazon Bedrock extracts the colours used, the colours are matched to the nearest Vallejo paints, and the painted image plus the colour list is pushed back to the browser over a WebSocket. Built on AWS serverless architecture. A PDF painting guide (and a periodic S3 cleanup Lambda) was part of the original plan but is not implemented.

Architecture as built (see `README.md`, `docs/Architecture design.drawio` for the wider original plan, and the GitHub wiki for per-module detail):

```
Frontend (S3 static hosting, Cognito login)
  │
  ├─ POST /upload-urls               → lambda_upload        presigned S3 PUT URL (uploads/<jobId>/)
  ├─ PUT  /jobs                      → upload-confirmation  checks exactly 1 image, creates job (UPLOADED)
  ├─ WebSocket $connect              → open_connection      validates Cognito JWT, stores connectionId
  └─ POST /jobs/{jobId}/instruction  → start_job            UPLOADED → IN_PROGRESS, sends SQS message
                                           │ paint queue
                                           ▼
                                         paint              OpenAI paints the image → PAINTED
                                           │ guide-creation queue
                                           ▼
                                         bedrock            Bedrock extracts colours → COMPLETED
                                           │ result queue
                                           ▼
                                         result             Vallejo matching, presigned URLs,
                                                            pushes result to the client's WebSocket
  WebSocket $disconnect              → close_connection     removes connection metadata
```

## Repository map
| Path                        |What it is|
|-----------------------------|-----------|
| `backend/lambda_upload/`    |Lambda function that generates one presigned S3 PUT URL for the reference image under a UUID-based `uploads/<uuid>/` prefix (the UUID is the job ID).|
| `backend/upload-confirmation/` | Lambda that confirms exactly one uploaded image in S3 and records the job in DynamoDB with status "UPLOADED".|
| `backend/open_connection/`  | Lambda for the WebSocket `$connect` route: validates the Cognito JWT and the job, stores connection metadata in DynamoDB.|
| `backend/close_connection/` | Lambda for the WebSocket `$disconnect` route: cleans up connection metadata in DynamoDB.|
| `backend/start_job/`        | Lambda function that initiates guide creation: validates job exists and is in "UPLOADED" status, verifies exactly 1 image uploaded to S3, updates job status to "IN_PROGRESS" (optionally storing the user's instruction), and triggers the paint step via SQS message.|
| `backend/paint/`            | SQS-triggered Lambda: generates the painted image with OpenAI, stores it in S3 (`painted_images/<jobId>/image_0.png`), sets status "PAINTED" and notifies the guide-creation queue; on failure sets "FAILED" and sends a message to the result queue.|
| `backend/bedrock/`          | SQS-triggered Lambda: sends the painted image to Amazon Bedrock for the colour list, stores `result.json`, notifies the result queue and sets status "COMPLETED".|
| `backend/result/`           | SQS-triggered Lambda: enriches the colour list with the nearest Vallejo paints (`result_vallejo.json`), creates presigned URLs and pushes the result to the client's WebSocket.|
| `frontend/`                 | S3-hosted static web app (vanilla JavaScript + Vite) for user interaction: Cognito sign-in, upload one image, start generation, and view the painted image and Vallejo colour list. Includes auth, API client, S3 upload, WebSocket updates, and validation modules.|
| `.github/workflows/`        | `run_unit_tests.yml` (PR test matrix over `backend/*/tests` plus frontend tests), one `deploy_*.yml` per Lambda, and `deploy-frontend.yml`.|
| `docs/`                     | Architecture diagram (`Architecture design.drawio`), IAM roles/permissions, S3 CORS setup, and the OpenAI/Bedrock prompt files.|

## Repository layout
- `backend/lambda_upload/` — Lambda function that issues one pre-signed S3 PUT URL for image upload, under a UUID-based `uploads/<uuid>/` prefix.
  - `handler.py` — the Lambda entry point (`lambda_handler`).
  - `event_parser.py` — event-parsing and sanitizing helpers.
  - `tests/` — unittest suites, mock `boto3` S3 client.
  - `requirements.txt` — Lambda dependencies (`boto3`, `boto3-stubs[s3]`).
- Each backend module is expected to be self-contained (its own `requirements.txt`, `tests/`, `README.md`; `requirements-dev.txt` for test tools) rather than sharing a monorepo-wide dependency file — follow this pattern when adding new Lambda functions.

## Development commands

### Backend Lambdas
#### `backend/<lambda_name>/`

Test framework depends on the module. Newer modules (`upload-confirmation`, `start_job`, `paint`, `bedrock`, `result`) use `pytest` (with `moto` where AWS is faked);
older ones (`lambda_upload`, `open_connection`, `close_connection`) use `unittest`. Use the command in the module's `README.md`.
New modules use `pytest` (see the `python-unit-testing` skill).

```bash
# Install dependencies (add requirements-dev.txt for pytest-based modules)
pip install -r requirements.txt

# unittest-based modules (e.g. lambda_upload)
python -m unittest discover -s tests
python -m unittest tests.test_handler
python -m unittest tests.test_handler.TestLambdaUploadHandler.test_generates_one_url_in_uuid_folder

# pytest-based modules (e.g. paint)
pip install -r requirements-dev.txt
pytest
pytest tests/test_handler.py
pytest tests/test_handler.py::TestLambdaHandler::test_happy_path_paints_job
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