# IAM Configuration roles and permissions.

## Deployment Role.

**Description:** The following IAM roles are required for the deployment of the Lambda function and its associated resources.

**RoleName:** `<github-deploy-role>`\
**Purpose:** This role is assumed by the GitHub Actions workflow to deploy the Lambda function and related resources.\
**Permissions:** The role must have the following permissions:\
- `Lambda_deployment`
```json
{
	"Version": "2012-10-17",
	"Statement": [
		{
			"Effect": "Allow",
			"Action": [
				"lambda:GetFunctionConfiguration",
				"lambda:GetFunction",
				"lambda:UpdateFunctionCode",
				"lambda:UpdateFunctionConfiguration",
				"lambda:PublishVersion",
				"lambda:CreateAlias",
				"lambda:UpdateAlias"
			],
			"Resource": [
                "<lambda-function-arn>"
            ]
		}
	]
}
```
- `Frontend_deployment`
```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "SyncFrontendToS3",
      "Effect": "Allow",
      "Action": [
        "s3:PutObject",
        "s3:GetObject",
        "s3:DeleteObject",
        "s3:ListBucket"
      ],
      "Resource": [
        "<s3-frontend-bucket-arn>",
        "<s3-frontend-bucket-arn>/*"
      ]
    },
    {
      "Sid": "InvalidateCloudFrontCache",
      "Effect": "Allow",
      "Action": [
        "cloudfront:CreateInvalidation",
        "cloudfront:GetInvalidation"
      ],
      "Resource": "<cloudfront-distribution-arn>"
    }
  ]
}
```
---
## S3 Bucket for Frontend Hosting
**Description:** The S3 bucket `<s3-frontend-hosting>` is used to host the frontend assets of the application. It must be configured to allow public read access for the hosted website.\
```json
{
    "Version": "2008-10-17",
    "Id": "PolicyForCloudFrontPrivateContent",
    "Statement": [
        {
            "Sid": "AllowCloudFrontServicePrincipal",
            "Effect": "Allow",
            "Principal": {
                "Service": "cloudfront.amazonaws.com"
            },
            "Action": "s3:GetObject",
            "Resource": "<s3-frontend-bucket-arn>/*",
            "Condition": {
                "StringEquals": {
                    "AWS:SourceArn": "<cloudfront-distribution-arn>"
                }
            }
        }
    ]
}
```
---
## Lambda Upload
**Description:** The Lambda function is responsible for generating pre-signed URLs for file uploads to the S3 bucket `<s3-uploads-bucket>`. It requires specific IAM permissions to perform its tasks.\
```json
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Action": [
                "logs:CreateLogGroup",
                "logs:CreateLogStream",
                "logs:PutLogEvents"
            ],
            "Resource": "*"
        }
    ]
}
```
---
## Lambda Confirmation Uploading
**Description:** The Lambda function responsible for confirming the upload of files to the S3 bucket `<s3-uploads-bucket>`. It requires specific IAM permissions to perform its tasks.\
```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": "logs:CreateLogGroup",
      "Resource": "<log-group-arn>"
    },
    {
      "Effect": "Allow",
      "Action": [
        "logs:CreateLogStream",
        "logs:PutLogEvents"
      ],
      "Resource": [
        "<log-group-arn>"
      ]
    },
    {
      "Effect": "Allow",
      "Action": [
        "s3:ListBucket"
      ],
      "Resource": [
        "<s3-uploads-bucket-arn>"
      ]
    },
    {
      "Effect": "Allow",
      "Action": [
        "dynamodb:PutItem",
        "dynamodb:GetItem"
      ],
      "Resource": "<dynamodb-table-arn>"
    }
  ]
}
```
---

## Lambda Open Connection
**Description:** The Lambda function responsible for opening a connection to the database and performing operations on the `MiniatureGuideJobs` DynamoDB table. It requires specific IAM permissions to perform its tasks.\
```json
{
	"Version": "2012-10-17",
	"Statement": [
		{
			"Effect": "Allow",
			"Action": "logs:CreateLogGroup",
			"Resource": "<log-group-arn>"
		},
		{
			"Effect": "Allow",
			"Action": [
				"logs:CreateLogStream",
				"logs:PutLogEvents"
			],
			"Resource": [
				"<log-group-arn>"
			]
		},
		{
			"Effect": "Allow",
			"Action": [
				"dynamodb:UpdateItem",
				"dynamodb:GetItem"
			],
			"Resource": "<dynamodb-table-arn>"
		}
	]
}
```

## Lambda Close Connection
**Description:** The Lambda function responsible for closing the connection to the database and performing operations on the `MiniatureGuideJobs` DynamoDB table. It requires specific IAM permissions to perform its tasks.\
```json
{
	"Version": "2012-10-17",
	"Statement": [
		{
			"Effect": "Allow",
			"Action": "logs:CreateLogGroup",
			"Resource": "<log-group-arn>"
		},
		{
			"Effect": "Allow",
			"Action": [
				"logs:CreateLogStream",
				"logs:PutLogEvents"
			],
			"Resource": [
				"<log-group-arn>"
			]
		},
		{
			"Effect": "Allow",
			"Action": [
				"dynamodb:GetItem",
				"dynamodb:UpdateItem",
				"dynamodb:Query"
			],
			"Resource": [
				"<dynamodb-table-arn>",
				"<dynamodb-table-arn>/index/*"
			]
		}
	]
}
```
