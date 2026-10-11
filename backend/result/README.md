# Result Lambda

AWS Lambda that delivers the finished job result to the client. Triggered by an SQS
message (`RESULT_QUEUE_URL`) sent by the bedrock Lambda. Enriches the bedrock result JSON
with the nearest Vallejo paints, generates presigned download URLs for the result files, looks up the job's WebSocket `connectionId` in DynamoDB and
pushes the result to that connection via the API Gateway WebSocket management API.

## Environment variables

| Variable | Description |
|---|---|
| `PAINT_BUCKET_NAME` | S3 bucket holding `painted_images/<jobId>/result.json` and `image_0.png`; the Lambda also writes `result_vallejo.json` there |
| `STATIC_BUCKET_NAME` | S3 bucket holding the Vallejo palette `vallejo/vallejo_colors.csv` |
| `JOBS_TABLE_NAME` | DynamoDB jobs table (item has `connectionId`) |
| `VITE_WS_BASE_URL` | WebSocket base URL shared with the frontend, `wss://<api-id>.execute-api.<region>.amazonaws.com/<stage>`; the Lambda converts it to the `https://` management endpoint |

## IAM policy example

Example permissions policy for the Lambda execution role. Replace the `<...>` placeholders
(`<paint-bucket>` = `PAINT_BUCKET_NAME`, `<static-bucket>` = `STATIC_BUCKET_NAME`, `<jobs-table>` = `JOBS_TABLE_NAME`,
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
      "Sid": "ReadVallejoPalette",
      "Effect": "Allow",
      "Action": "s3:GetObject",
      "Resource": "arn:aws:s3:::<static-bucket>/vallejo/*"
    },
    {
      "Sid": "ReadWriteResults",
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:PutObject"],
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

The `s3:GetObject` permission on `painted_images/*` is what the presigned URLs inherit: without it the URLs are
generated successfully but return `403` when the browser downloads the files.

## `lambda_handler(event, context) -> None`

1. `parse_message(event)` reads `jobId` and `status` from `Records[0].body`; raises
   `ValueError` if invalid (message goes to the DLQ).
2. `get_connection_id(job_id)` reads `connectionId` from the jobs table. Unknown job →
   `ValueError`. No `connectionId` (client disconnected) → warning, message acknowledged.
3. For `COMPLETED`, `store_vallejo_result(job_id)` reads `result.json`, runs
   `find_vallejo_colors` and writes `result_vallejo.json` next to it (the original is kept).
   Invalid result JSON or palette → `ValueError`; S3 errors are re-raised (SQS retry / DLQ).
4. For `COMPLETED`, `generate_result_urls(job_id)` creates presigned GET URLs (1 hour) for
   `result_vallejo.json` (returned as `resultUrl`) and `image_0.png`.
5. `send_to_connection(connection_id, message)` posts the message. A gone connection
   (HTTP 410) is logged and acknowledged; other AWS errors are re-raised so SQS retries.

## Vallejo colour matching

`find_vallejo_colors(result_json)` loads `vallejo/vallejo_colors.csv` from
`STATIC_BUCKET_NAME` (columns `reference,Color,name,r,g,b,hex`; parsed once per warm
Lambda) and, for each entry of `colors`, picks the palette row with the smallest squared
Euclidean RGB distance to the entry's `paint` hex (`RRGGBB` or `#RRGGBB`). The row is added
as `vallejo_color`; the input is not modified:

```json
{
  "colors": [
    {
      "detail": "armor",
      "paint": "2E4A6B",
      "vallejo_color": {
        "reference": "70.741", "Color": "70.741", "name": "Cam. Black Green",
        "r": 42, "g": 45, "b": 40, "hex": "#2A2D28"
      }
    }
  ]
}
```

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
  "resultUrl": "<presigned_url_to_result_vallejo.json>",
  "imageUrl": "<presigned_url_to_image_0.png>"
}
```

## Development

```bash
# from backend/result/
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest tests/ -v
```
