# miniature-ai-guide

AI-powered tool that helps painters colour miniature models. A user uploads a reference
image of a miniature and the system generates a painted version of it with OpenAI, asks
Amazon Bedrock to identify the colours used, matches them to the nearest Vallejo paints,
and pushes the result back to the browser over a WebSocket. Built on AWS serverless
architecture.

## Architecture

```
Frontend (S3 static hosting, Cognito login)
  │
  ├─ POST /upload-urls            → lambda_upload         presigned S3 PUT URL (uploads/<jobId>/)
  ├─ browser PUT                  → S3 upload bucket
  ├─ PUT  /jobs                   → upload-confirmation   verifies the image, creates the job (UPLOADED)
  ├─ WebSocket $connect           → open_connection       validates Cognito JWT, stores connectionId
  └─ POST /jobs/{jobId}/instruction → start_job           UPLOADED → IN_PROGRESS, sends SQS message
                                         │
                                         ▼
                                       paint              OpenAI paints the reference image → PAINTED
                                         │  SQS
                                         ▼
                                       bedrock            Bedrock extracts the colour list → COMPLETED
                                         │  SQS
                                         ▼
                                       result             matches Vallejo paints, presigned URLs,
                                                          pushes result to the client's WebSocket
  WebSocket $disconnect         → close_connection        removes connection metadata
```

Job state lives in DynamoDB (`UPLOADED` → `IN_PROGRESS` → `PAINTED` → `COMPLETED`, or
`FAILED`). Stages are connected by SQS queues. Diagrams are in `docs/`
(`Architecture design.drawio`).

## Repository layout

| Path | Description | Docs |
|---|---|---|
| `frontend/` | Vanilla JavaScript + Vite static app: Cognito login, image upload, job start, WebSocket progress and result view | [frontend/README.md](frontend/README.md) |
| `backend/lambda_upload/` | Issues a presigned S3 `PUT` URL under a UUID-based `uploads/<uuid>/` prefix | [README](backend/lambda_upload/README.md) |
| `backend/upload-confirmation/` | Confirms the image is in S3 (exactly one) and records the job in DynamoDB | [README](backend/upload-confirmation/README.md) |
| `backend/open_connection/` | WebSocket `$connect`: validates the Cognito JWT and the job, stores the connection | [README](backend/open_connection/README.md) |
| `backend/close_connection/` | WebSocket `$disconnect`: cleans up connection metadata | [README](backend/close_connection/README.md) |
| `backend/start_job/` | Validates the job and image, stores the optional user instruction, sets `IN_PROGRESS`, enqueues the paint step | [README](backend/start_job/README.md) |
| `backend/paint/` | SQS-triggered; generates the painted image with OpenAI and stores it in S3 | [README](backend/paint/README.md) |
| `backend/bedrock/` | SQS-triggered; asks Bedrock for the colour list of the painted image and writes `result.json` | [README](backend/bedrock/README.md) |
| `backend/result/` | SQS-triggered; enriches the result with Vallejo paints and delivers it over WebSocket | [README](backend/result/README.md) |
| `docs/` | Architecture diagram, IAM roles/permissions, S3 CORS setup, prompts | — |
| `.github/workflows/` | Per-module deploy workflows and the PR unit-test workflow | — |

Each backend module is self-contained: its own `handler.py`, `requirements.txt`, `tests/`
and `README.md` (environment variables, request/response shapes, error handling).
Read the module README before changing a module.

## Prerequisites

- Python 3.12 (backend; used by CI)
- Node.js 20 and npm (frontend; the frontend README states 18+ as the minimum)
- AWS account with the serverless resources (API Gateway, Cognito, S3, DynamoDB, SQS,
  Secrets Manager, Bedrock) — infrastructure is not defined in this repository

## Development

### Backend Lambdas

Run from `backend/<module>/`:

```bash
pip install -r requirements.txt
pip install -r requirements-dev.txt   # if the module has one (pytest)
python -m pytest tests/ -v            # modules without requirements-dev.txt: python -m unittest discover -s tests
```

CI uses the same rule. Check the module README for the exact test command.

### Frontend

Run from `frontend/`:

```bash
npm install
npm run dev        # dev server with hot reload
npm run build      # production bundle in dist/
npm test           # Vitest
npm run lint
npm run format
```

Set `VITE_API_BASE_URL` (and the other `VITE_*` values described in the frontend README)
in `frontend/.env.local`, which is git-ignored.

## CI/CD

GitHub Actions in `.github/workflows/`:

- `run_unit_tests.yml` runs every backend module's tests and the frontend tests on pull
  requests to `main`. New `backend/<module>/` directories with a `tests/` folder are picked up
  automatically.
- `deploy_*.yml` deploy each Lambda, and `deploy-frontend.yml` publishes the frontend to S3.

IAM roles and permissions are described in `docs/IAM-PERMISSIONS-AMD-ROLES.md`; S3 CORS for
the result bucket is in `docs/s3-cors-setup.md`.
