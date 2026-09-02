# Paint Lambda

AWS Lambda function that handles painting of previously uploaded reference images.
Triggered by an SQS message containing a `jobId`. Downloads 4 reference images
from S3, reads a painting prompt, calls OpenAI to generate 4 painted images, uploads
them to S3 via presigned PUT URLs, updates job status in DynamoDB, and notifies
the guide creation queue on success.

## Environment variables

| Variable | Description |
|---|---|
| `PAINT_QUEUE_URL` | SQS queue URL that triggers this Lambda |
| `UPLOAD_BUCKET_NAME` | S3 bucket containing reference images (`uploads/<jobId>/`) |
| `STATIC_BUCKET_NAME` | S3 bucket containing the painting prompt file |
| `PAINT_BUCKET_NAME` | S3 bucket where painted images are stored (`painted_images/<jobId>/`) |
| `JOBS_TABLE_NAME` | DynamoDB table for job status tracking |
| `OPENAI_API_KEY` | OpenAI API key for image generation |
| `GUIDE_CREATION_QUEUE_URL` | SQS queue URL to notify on successful painting |

## `lambda_handler(event, context) -> None`

Entry point triggered by SQS. Orchestrates the full paint flow:

1. **Parse `jobId`** from `Records[0].body` via `parse_job_id(event)`.
   - Raises `ValueError` if the jobId cannot be parsed (routes message to DLQ).
2. **Download 4 reference images** from `UPLOAD_BUCKET_NAME` via `download_images_from_s3(job_id)`.
3. **Fetch painting prompt** from `STATIC_BUCKET_NAME/prompts/paint_images_promt.txt` via `fetch_prompt_from_s3()`.
4. **Generate 4 painted images** via OpenAI `images.edit` (`gpt-image-1`) in `generate_painted_images(images, prompt)`.
   - Raises if fewer than 4 images are returned.
5. **Upload painted images** to `PAINT_BUCKET_NAME/painted_images/<jobId>/image_<n>.jpg` via presigned PUT URLs in `upload_painted_images(job_id, images)`.
6. **Update job status to `PAINTED`** in DynamoDB via `update_job_status(job_id, "PAINTED")`.
7. **Notify guide creation queue** via `notify_guide_creation(job_id)`.

On failure at steps 2–5, updates job status to `FAILED` and returns without raising
(the SQS message is consumed). Failures at steps 6–7 are logged but do not affect
the SQS message acknowledgement.

## SQS message format

**Input** (`PAINT_QUEUE_URL`):
```json
{"jobId": "<uuid>"}
```

**Output** (`GUIDE_CREATION_QUEUE_URL`):
```json
{"jobId": "<uuid>"}
```

## Development

```bash
# from backend/paint/
pip install -r requirements.txt
pip install -r requirements-dev.txt
```

### Run all tests
```bash
pytest
```

### Run a single test file
```bash
pytest tests/test_handler.py
```

### Run a single test case
```bash
pytest tests/test_handler.py::TestLambdaHandler::test_happy_path_paints_job
```

### Run tests with verbose output
```bash
pytest -v
```

## Deployment

- **Region**: `eu-central-1` (Frankfurt)
- **Lambda function name**: `paint`
- **Runtime**: Python 3.12
- **Trigger**: SQS (`PAINT_QUEUE_URL`)
