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