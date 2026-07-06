                 ┌──────────────┐
                 │   Frontend   │
                 │ (S3 hosting) │
                 └──────┬───────┘
                        │
                        │
         ┌──────────────┼──────────────┐
         │                             │

UPLOAD FLOW                    GENERATION FLOW

         ▼                             ▼
API Gateway + Cognito        API Gateway + Cognito
         │                             │
         ▼                             ▼
Lambda: presigned URL        Lambda: start job
         │                             │
         ▼                             ▼
   S3 (upload images)        Step Functions workflow
                                         │
                         ┌───────────────┼───────────────┐
                         ▼               ▼               ▼
                    Bedrock AI     Validate JSON     PDF Lambda
                         │                               │
                         └───────────────┬───────────────┘
                                         ▼
                                      S3 output

> Every API Gateway ↔ Lambda integration above (both flows) must be
> configured as **Lambda Proxy Integration (`AWS_PROXY`)** — see
> [`apiGatewayIntegration.md`](./apiGatewayIntegration.md) for the
> requirement, failure symptom, and console verification checklist.