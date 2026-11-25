# Backend Lambda deploy workflow

Rules for `.github/workflows/deploy_<module>.yml`. Copy the closest sibling from the table in `SKILL.md` and adapt it.

## Context

- There is no IaC (no CDK, CloudFormation or Terraform). Lambda functions, buckets, tables, queues, API Gateway, Cognito and IAM roles were created by hand in the AWS Console. Workflows only ship code; they never provision or modify infrastructure. Say so in the header comment.
- Each backend Lambda has its own workflow `deploy_<module>.yml`. The frontend has `deploy-frontend.yml` (see `frontend-workflow.md`). PR checks are in `run_unit_tests.yml`, which discovers `backend/*/tests` automatically, so a new module needs no change there.

## File naming and layout

- New backend workflows: `.github/workflows/deploy_<module>.yml` (underscores, module name as the directory). `deploy-frontend.yml` is a historical exception; do not rename existing files.
- `name:` is human readable, e.g. `Deploy start-job Lambda`.
- Top-to-bottom order: `name`, header comment, `on`, `permissions`, `concurrency`, `env`, `jobs`.

## Header comment (required)

Every deploy workflow starts with a comment that states: it is an interim non-CDK pipeline that zips `backend/<module>` and calls `aws lambda update-function-code` on an existing function; it does not provision infrastructure; and a prerequisites list:

- the IAM OIDC provider for `token.actions.githubusercontent.com`;
- the deploy role trusting it (scoped to this repo) with `lambda:UpdateFunctionCode` on the function;
- repo variables: `vars.AWS_DEPLOY_ROLE_ARN`, `vars.AWS_REGION`, `vars.<MODULE>_FUNCTION_NAME`;
- the Lambda itself: handler (`handler.lambda_handler`), environment variables, trigger (SQS, API Gateway, WebSocket), and the buckets, tables and queues those variables reference.

Keep this list in sync with the module's `README.md` and handler config.

## Triggers

```yaml
on:
  push:
    branches:
      - main
    paths:
      - "backend/<module>/**"
      - ".github/workflows/deploy_<module>.yml"
  workflow_dispatch: {}
```

Always path-filter to the module and the workflow file itself, always include `workflow_dispatch`. PR testing is not duplicated here; it belongs to `run_unit_tests.yml`.

## Permissions, concurrency, env

```yaml
permissions:
  id-token: write
  contents: read

concurrency:
  group: deploy-<module-kebab>
  cancel-in-progress: false

env:
  LAMBDA_FUNCTION_NAME: ${{ vars.<MODULE>_FUNCTION_NAME }}
```

- Least privilege: only `id-token: write` (for OIDC) and `contents: read`.
- One concurrency group per workflow, never cancel an in-flight deploy, so two pushes cannot race on `update-function-code`.
- The function name always comes from a repo variable, never hardcoded. Existing names: `START_JOB_FUNCTION_NAME`, `PAINT_FUNCTION_NAME`, `BEDROCK_FUNCTION_NAME`, `RESULT_FUNCTION_NAME`, `OPEN_CONNECTION_FUNCTION_NAME`, `LAMBDA_UPLOAD_FUNCTION_NAME`. Follow `<MODULE>_FUNCTION_NAME`.

## Jobs

Two jobs, `test` then `deploy` (`needs: test`), both `runs-on: ubuntu-latest`.

**`test` ("Run unit tests (pre-deploy gate)")**
- `actions/checkout@v4`, `actions/setup-python@v5` with Python `"3.12"` (must match the Lambda runtime).
- Install with `working-directory: backend/<module>`: `python -m pip install --upgrade pip`, `pip install -r requirements.txt`, and `pip install -r requirements-dev.txt` if the file exists.
- Test command follows the module: `pytest -v` for pytest modules (those with `requirements-dev.txt`: start_job, paint, bedrock, result, upload_confirmation), `python -m unittest discover -s tests` for unittest modules (lambda_upload, open_connection, close_connection). Check the module's README.

**`deploy` ("Package and deploy to AWS Lambda")**
1. `actions/checkout@v4`.
2. If dependencies are bundled: `actions/setup-python@v5` with `"3.12"`.
3. Package into `/tmp/lambda-package` (see `packaging.md`) and `zip -r "${GITHUB_WORKSPACE}/<module>.zip" .`.
4. `aws-actions/configure-aws-credentials@v4` with `role-to-assume: ${{ vars.AWS_DEPLOY_ROLE_ARN }}` and `aws-region: ${{ vars.AWS_REGION }}`.
5. `aws lambda update-function-code --function-name "${LAMBDA_FUNCTION_NAME}" --zip-file fileb://<module>.zip`.
6. `aws lambda wait function-updated --function-name "${LAMBDA_FUNCTION_NAME}"`.

## PR checks (`run_unit_tests.yml`)

Runs on pull requests to `main` plus `workflow_dispatch`, with `contents: read` only. A discover job builds a matrix of `backend/*` directories that have a `tests/` folder; the test job picks pytest or unittest per module; a separate `frontend-test` job runs `npm ci` and `npm test`. Do not add per-module logic here; extend the discovery instead if needed.
