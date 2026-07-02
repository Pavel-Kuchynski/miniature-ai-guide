# Deploy `backend/lambda_upload` to AWS

## Context

`backend/lambda_upload` is fully implemented and unit-tested but has never been deployed —
there is no infrastructure-as-code, no AWS resources, and no deploy pipeline anywhere in the
repo yet (`CLAUDE.md` explicitly calls out that infra-as-code doesn't exist yet). The goal here
is to stand up just enough AWS infrastructure — the Lambda function itself, its S3 upload
bucket, and a tightly-scoped IAM execution role — so the function can be invoked directly
(via `aws lambda invoke` or a presigned-URL round trip) and verified end-to-end, ahead of the
rest of the architecture (API Gateway, Cognito, the other Lambdas) being built.

AWS CDK (Python) is proposed as the IaC tool, since it matches the repo's Python-first
convention and the aspirational `infra/terraform/ (или cdk)` directory named in
`docs/progect_structure.md`, plus a GitHub Actions deploy workflow using OIDC (no long-lived
AWS keys), mirroring the style of the existing `.github/workflows/run_unit_tests.yml`. These
choices are flagged as assumptions/open decisions below — revisit if Terraform/SAM or a
manual-only workflow is preferred instead.

**Out of scope for this pass:** API Gateway, Cognito, frontend hosting, other Lambdas. Those
come later per the target architecture in `docs/globalIdea.md`.

## What gets built

```
infra/
└── cdk/
    ├── app.py
    ├── cdk.json
    ├── requirements.txt
    └── stacks/
        ├── __init__.py
        └── lambda_upload_stack.py
.github/
└── workflows/
    └── deploy_lambda_upload.yml
```

### `infra/cdk/stacks/lambda_upload_stack.py` — the one CDK stack

- **S3 bucket** (`s3.Bucket`): `block_public_access=BLOCK_ALL`; no bucket-level CORS needed for
  server-side verification (only needed later once a browser PUTs directly — flagged below).
- **IAM execution role** (`iam.Role`, explicit — not CDK's auto-generated default): trusts
  `lambda.amazonaws.com`; attaches `AWSLambdaBasicExecutionRole` for CloudWatch Logs; grants
  **only** `s3:PutObject` scoped to `arn:aws:s3:::<bucket>/uploads/*` (use
  `bucket.grant_put(role, "uploads/*")`) — matches exactly what `handler.py` needs, nothing
  broader (no `GetObject`, `ListBucket`, or bucket-level access).
- **Lambda function** (`lambda_.Function`):
  - `runtime=PYTHON_3_12` (matches `run_unit_tests.yml`'s `python-version: "3.12"`)
  - `handler="handler.lambda_handler"`
  - `code=lambda_.Code.from_asset("../../backend/lambda_upload", exclude=["tests", "requirements.txt", "README.md", "__pycache__"])`
    — boto3 is already in the Lambda managed runtime, so no dependency bundling/Docker asset
    needed; a plain source-directory asset is sufficient.
  - env vars: `UPLOAD_BUCKET_NAME=bucket.bucket_name` (always wired from the created bucket,
    never hardcoded); `UPLOAD_URL_EXPIRES_SECONDS` left unset so the handler's own default
    (900) applies, unless you want it configurable.
  - `timeout` ~10s, `memory_size` 128 MB (handler does no I/O, just local presigned-URL math).
  - Explicit `logs.LogGroup` with finite `retention` (e.g. `ONE_MONTH`) attached via
    `log_group=` — otherwise Lambda's default log group never expires.
- **Stack outputs** (`CfnOutput`): function name/ARN and bucket name, for use in verification
  and in the CI smoke-test step.

### `infra/cdk/requirements.txt`

```
aws-cdk-lib>=2.,<3.
constructs>=10.,<11.
```

Kept separate from `backend/lambda_upload/requirements.txt`, consistent with the "each backend
module is self-contained" convention in `CLAUDE.md`.

### `.github/workflows/deploy_lambda_upload.yml`

Mirrors `run_unit_tests.yml`'s style (`actions/checkout@v4`, `actions/setup-python@v5`,
per-directory `working-directory` steps). Cannot `needs:` across separate workflow files, so
this workflow re-runs the unit tests itself as a pre-deploy gate rather than depending on the
PR workflow's run.

```yaml
on:
  push:
    branches: [main]
  workflow_dispatch: {}

permissions:
  id-token: write   # OIDC
  contents: read

jobs:
  test:
    # same steps as run_unit_tests.yml's test job, working-directory: backend/lambda_upload
  deploy:
    needs: test
    steps:
      - checkout
      - setup-python (3.12) + setup-node (for the CDK CLI)
      - npm install -g aws-cdk
      - pip install -r infra/cdk/requirements.txt
      - aws-actions/configure-aws-credentials@v4:
          role-to-assume: ${{ vars.AWS_DEPLOY_ROLE_ARN }}
          aws-region: ${{ vars.AWS_REGION }}
      - cdk deploy --require-approval never   (working-directory: infra/cdk)
      - optional: aws lambda invoke smoke test against the stack's output function name
```

## One-time AWS account setup (before any deploy)

1. Decide target AWS account + region (open decision below).
2. `cdk bootstrap aws://<ACCOUNT_ID>/<REGION>` — run once, manually, from a developer machine
   with sufficient IAM permissions.
3. Create a GitHub OIDC identity provider in IAM (`token.actions.githubusercontent.com`,
   audience `sts.amazonaws.com`) if one doesn't already exist in the account.
4. Create a "GitHub Actions deploy" IAM role trusting that OIDC provider, scoped via the `sub`
   claim to this repo (e.g. `repo:<org>/<repo>:ref:refs/heads/main`), with permissions to
   assume the CDK bootstrap's deploy/exec/lookup roles (the standard CDK OIDC pattern — keeps
   the GitHub-side role's own policy small rather than granting broad IAM/CloudFormation
   directly).
5. Store the deploy role ARN and region as GitHub Actions repo variables
   (`vars.AWS_DEPLOY_ROLE_ARN`, `vars.AWS_REGION`) — not secrets, since the ARN isn't sensitive
   and OIDC means no access keys exist.

## First deploy (manual, before wiring CI)

From `infra/cdk/`, with local AWS credentials for the target account:

1. `pip install -r requirements.txt`
2. `cdk synth` — sanity-check the rendered template (bucket policy, IAM policy, env vars).
3. `cdk diff` — confirm only new resources (bucket, role, function, log group) are planned.
4. `cdk deploy` — review and accept the IAM permission-change prompt manually.
5. Note the printed outputs (bucket name, function name/ARN).

Do this manual pass first so IaC bugs (wrong asset path, prefix mismatch in the `grant_put`,
wrong runtime) get caught without burning OIDC/CI setup time.

## Verification

1. `aws cloudformation describe-stacks` — confirm `CREATE_COMPLETE`.
2. Direct invoke:
   ```
   aws lambda invoke --function-name <FunctionName> \
     --payload '{"queryStringParameters": {"fileNames": "a.jpg,b.jpg", "contentTypes": "image/jpeg"}}' \
     --cli-binary-format raw-in-base64-out response.json
   ```
   Confirm `statusCode: 200` and 4 `uploadItems` with URLs under `uploads/<uuid>/`.
3. Round-trip one presigned URL: `curl -X PUT --data-binary @somefile.jpg -H "Content-Type: image/jpeg" "<uploadUrl>"`, then `aws s3 ls s3://<bucket>/uploads/<uuid>/` to confirm the object landed.
4. `aws iam get-role-policy` on the execution role — confirm it's exactly `s3:PutObject` on
   `uploads/*`, nothing broader.
5. Check `/aws/lambda/<FunctionName>` log group exists with the configured retention and shows
   invocation logs.

## Open decisions (need input before/while implementing)

1. **IaC tool** — assumed CDK (Python). Confirm, or switch to Terraform/SAM/manual-only.
2. **Scope** — assumed Lambda + S3 + IAM only (no API Gateway/Cognito yet). Confirm.
3. **CI/CD** — assumed a GitHub Actions OIDC deploy workflow on merge to `main` +
   `workflow_dispatch`. Confirm, or keep deploys manual for now.
4. **AWS account + region** — which account and region should this target?
5. **Bucket naming** — CDK auto-generated name vs. an explicit, globally-unique name
   (e.g. `miniature-ai-guide-uploads-dev`)? Matters if you'll want dev/staging/prod later.
6. **Bucket removal policy** — `RETAIN` (safer, leaves orphaned buckets on teardown) vs.
   `DESTROY` + `auto_delete_objects=True` (convenient for early iteration, destructive).
7. **Object lifecycle** — auto-expire `uploads/*` objects after N days (they're transient
   pipeline inputs)?
8. **CORS on the bucket** — not needed for CLI-based verification, but required before a real
   browser can PUT directly to a presigned URL. Wildcard now, tighten to the real frontend
   origin later?
9. **`cdk deploy --require-approval never` in CI** — acceptable to auto-deploy IAM/security
   changes on merge (mitigated by PR review of the CDK code), or add a GitHub Environment
   with required reviewers on the `deploy` job?
10. **Log retention period** for CloudWatch Logs (e.g. 30 vs 90 days)?

## Critical files

- `infra/cdk/stacks/lambda_upload_stack.py` (new)
- `infra/cdk/app.py` (new)
- `infra/cdk/requirements.txt`, `infra/cdk/cdk.json` (new)
- `.github/workflows/deploy_lambda_upload.yml` (new)
- `backend/lambda_upload/handler.py`, `backend/lambda_upload/README.md` (reference — defines
  required env vars and IAM permissions the stack must satisfy)
- `.github/workflows/run_unit_tests.yml` (reference — style/naming pattern to mirror)
