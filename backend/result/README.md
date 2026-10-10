# Result Lambda

AWS Lambda that delivers the finished job result to the client. Triggered by an SQS
message (`RESULT_QUEUE_URL`) sent by the bedrock Lambda. Generates presigned download
URLs for the result files, looks up the job's WebSocket `connectionId` in DynamoDB and
pushes the result to that connection via the API Gateway WebSocket management API.

## Environment variables

| Variable | Description |
|---|---|
| `PAINT_BUCKET_NAME` | S3 bucket holding `painted_images/<jobId>/result.json` and `image_0.png` |
| `JOBS_TABLE_NAME` | DynamoDB jobs table (item has `connectionId`) |
| `VITE_WS_BASE_URL` | WebSocket base URL shared with the frontend, `wss://<api-id>.execute-api.<region>.amazonaws.com/<stage>`; the Lambda converts it to the `https://` management endpoint |

## IAM policy example

Example permissions policy for the Lambda execution role. Replace the `<...>` placeholders
(`<paint-bucket>` = `PAINT_BUCKET_NAME`, `<jobs-table>` = `JOBS_TABLE_NAME`,
`<ws-api-id>` and `<stage>` come from `VITE_WS_BASE_URL`, e.g.
`wss://<ws-api-id>.execute-api.<region>.amazonaws.com/<stage>`).

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "ReadResultQueue",
      "Effect": "Allow",
      "Action": [
        "sqs:ReceiveMessage",
        "sqs:DeleteMessage",
        "sqs:GetQueueAttributes"
      ],
      "Resource": "arn:aws:sqs:<region>:<account-id>:<result-queue-name>"
    },
    {
      "Sid": "ReadJobConnection",
      "Effect": "Allow",
      "Action": "dynamodb:GetItem",
      "Resource": "arn:aws:dynamodb:<region>:<account-id>:table/<jobs-table>"
    },
    {
      "Sid": "SignResultDownloadUrls",
      "Effect": "Allow",
      "Action": "s3:GetObject",
      "Resource": "arn:aws:s3:::<paint-bucket>/painted_images/*"
    },
    {
      "Sid": "PostToWebSocketConnections",
      "Effect": "Allow",
      "Action": "execute-api:ManageConnections",
      "Resource": "arn:aws:execute-api:<region>:<account-id>:<ws-api-id>/<stage>/POST/@connections/*"
    },
    {
      "Sid": "WriteLogs",
      "Effect": "Allow",
      "Action": [
        "logs:CreateLogGroup",
        "logs:CreateLogStream",
        "logs:PutLogEvents"
      ],
      "Resource": "arn:aws:logs:<region>:<account-id>:log-group:/aws/lambda/<function-name>:*"
    }
  ]
}
```

The `s3:GetObject` permission is what the presigned URLs inherit: without it the URLs are
generated successfully but return `403` when the browser downloads the files.

## `lambda_handler(event, context) -> None`

1. `parse_message(event)` reads `jobId` and `status` from `Records[0].body`; raises
   `ValueError` if invalid (message goes to the DLQ).
2. `get_connection_id(job_id)` reads `connectionId` from the jobs table. Unknown job →
   `ValueError`. No `connectionId` (client disconnected) → warning, message acknowledged.
3. For `COMPLETED`, `generate_result_urls(job_id)` creates presigned GET URLs (1 hour).
4. `send_to_connection(connection_id, message)` posts the message. A gone connection
   (HTTP 410) is logged and acknowledged; other AWS errors are re-raised so SQS retries.

## Messages

**Input** (`RESULT_QUEUE_URL`):
```json
{"jobId": "<uuid>", "status": "COMPLETED"}
```

**WebSocket output** (for non-`COMPLETED` statuses only `jobId` and `status` are sent):
```json
{
  "jobId": "<uuid>",
  "status": "COMPLETED",
  "resultUrl": "<presigned_url_to_result.json>",
  "imageUrl": "<presigned_url_to_image_0.png>"
}
```

## Development

```bash
# from backend/result/
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest tests/ -v
```
