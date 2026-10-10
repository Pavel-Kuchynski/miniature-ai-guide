# Bedrock Lambda

AWS Lambda that runs after the paint Lambda. Triggered by an SQS message containing a
`jobId`, it sends the painted image and a prompt to Amazon Bedrock, stores the model
output in S3, notifies the result queue, and marks the job `COMPLETED`.

## Environment variables

| Variable | Description |
|---|---|
| `PAINT_BUCKET_NAME` | S3 bucket with painted images (`painted_images/<jobId>/`) and where `result.json` is written |
| `STATIC_BUCKET_NAME` | S3 bucket containing `prompts/miniature-photo-to-rgb-prompt.md` |
| `JOBS_TABLE_NAME` | DynamoDB table for job status tracking |
| `BEDROCK_MODEL_ID` | Optional. Bedrock model or inference profile ID; must support image input via the Converse API (default `global.anthropic.claude-sonnet-4-6`) |
| `BEDROCK_MAX_TOKENS` | Optional. Max output tokens (default `4096`) |
| `BEDROCK_READ_TIMEOUT_SECONDS` | Optional. Bedrock client read timeout (default `300`) |
| `RESULT_QUEUE_URL` | SQS queue URL notified on completion |

The paint Lambda sends its trigger message to the queue this Lambda is subscribed to
(`PAINT_QUEUE_URL` in the paint Lambda's configuration).

## `lambda_handler(event, context) -> None`

1. `parse_job_id(event)` — read `jobId` from `Records[0].body`; `ValueError` if invalid (DLQ).
2. `get_job_status(job_id)` — `ValueError` if the job is missing or not `PAINTED` (DLQ).
3. `download_painted_image_from_s3(job_id)` — exactly one non-empty `.png` under
   `painted_images/<jobId>/` (`result.json` is ignored).
4. `fetch_prompt_from_s3()` — prompt from `STATIC_BUCKET_NAME`.
5. `invoke_bedrock(image, prompt)` — Bedrock Converse call with prompt text + PNG image.
   The raw reply is logged (stage `bedrock_response`) before parsing, for diagnostics.
6. `parse_model_output(text)` — parses the reply as JSON (a code fence is tolerated) and
   validates `{"colors": [{"detail": "<name>", "paint": "<RRGGBB>"}, ...]}`
   (non-empty list, uppercase 6-digit hex, no `#`). Invalid output fails the job.
7. `upload_result_to_s3(job_id, result)` — writes the validated JSON as-is to
   `painted_images/<jobId>/result.json`.
8. `notify_result(job_id)` — sends `{"jobId": ..., "status": "COMPLETED"}` to `RESULT_QUEUE_URL`.
9. `update_job_status(job_id, "COMPLETED")` — conditional `PAINTED` → `COMPLETED`.

A failure in steps 3–7 sets the job to `FAILED` (conditional on `PAINTED`) and returns
without raising. Failures in steps 8–9 are logged only.

## Development

```bash
# from backend/bedrock/
pip install -r requirements.txt -r requirements-dev.txt
pytest
```

## Deployment

- **Runtime**: Python 3.12
- **Trigger**: SQS (paint queue)
- **Timeout**: set above the Bedrock read timeout (300 s) and the queue visibility timeout above that.

### Example IAM execution role

Replace the `<...>` placeholders. The trust policy lets Lambda assume the role; the
permissions policy grants only what the handler calls.

Trust policy:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {"Service": "lambda.amazonaws.com"},
      "Action": "sts:AssumeRole"
    }
  ]
}
```

Permissions policy:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "Logs",
      "Effect": "Allow",
      "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
      "Resource": "arn:aws:logs:<region>:<account-id>:log-group:/aws/lambda/<function-name>:*"
    },
    {
      "Sid": "ListPaintedImages",
      "Effect": "Allow",
      "Action": "s3:ListBucket",
      "Resource": "arn:aws:s3:::<paint-bucket>",
      "Condition": {"StringLike": {"s3:prefix": "painted_images/*"}}
    },
    {
      "Sid": "ReadPaintedImagesWriteResult",
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:PutObject"],
      "Resource": "arn:aws:s3:::<paint-bucket>/painted_images/*"
    },
    {
      "Sid": "ReadPrompt",
      "Effect": "Allow",
      "Action": "s3:GetObject",
      "Resource": "arn:aws:s3:::<static-bucket>/prompts/miniature-photo-to-rgb-prompt.md"
    },
    {
      "Sid": "JobsTable",
      "Effect": "Allow",
      "Action": ["dynamodb:GetItem", "dynamodb:UpdateItem"],
      "Resource": "arn:aws:dynamodb:<region>:<account-id>:table/<jobs-table>"
    },
    {
      "Sid": "ConsumePaintQueue",
      "Effect": "Allow",
      "Action": ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"],
      "Resource": "arn:aws:sqs:<region>:<account-id>:<paint-queue>"
    },
    {
      "Sid": "SendResult",
      "Effect": "Allow",
      "Action": "sqs:SendMessage",
      "Resource": "arn:aws:sqs:<region>:<account-id>:<result-queue>"
    },
    {
      "Sid": "InvokeBedrock",
      "Effect": "Allow",
      "Action": "bedrock:InvokeModel",
      "Resource": [
        "arn:aws:bedrock:<region>:<account-id>:inference-profile/global.anthropic.claude-sonnet-4-6",
        "arn:aws:bedrock:::foundation-model/anthropic.claude-sonnet-4-6",
        "arn:aws:bedrock:*::foundation-model/anthropic.claude-sonnet-4-6"
      ]
    }
  ]
}
```

Notes:

- The Converse API is authorized by `bedrock:InvokeModel`. A `global.` inference profile
  also needs access to the underlying foundation model in every region it routes to,
  hence the wildcard-region foundation-model ARN. Update both ARNs if you override
  `BEDROCK_MODEL_ID`.
- Add `kms:Decrypt` / `kms:GenerateDataKey` on the key if the buckets, table or queues use a
  customer-managed KMS key.
