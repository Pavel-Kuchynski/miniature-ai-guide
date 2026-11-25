# Packaging Lambda code in workflows

Runtime and architecture are python3.12 on x86_64. The packaging Python must be 3.12 as well.

## Copying source

```bash
mkdir -p /tmp/lambda-package
rsync -a --exclude 'tests' --exclude 'README.md' --exclude '__pycache__' \
  --exclude 'requirements.txt' --exclude 'requirements-dev.txt' \
  --exclude '*.pyc' --exclude '.pytest_cache' \
  ./ /tmp/lambda-package/
cd /tmp/lambda-package
```

Run it with `working-directory: backend/<module>`.

## Dependencies

- A module that only needs `boto3` bundles nothing (`start_job`, `lambda_upload`, `close_connection`, `upload_confirmation`): no `pip install` into the zip.
- `boto3`, `botocore`, `s3transfer` are provided by the Lambda runtime, and `boto3-stubs*`/`mypy-boto3-*` are dev-only. Filter them out of `requirements.txt` into `/tmp/requirements-lambda.txt` and skip `pip install` if nothing is left:
  ```bash
  grep -viE '^[[:space:]]*(boto3|botocore|s3transfer|boto3-stubs|botocore-stubs|mypy-boto3[a-z0-9_-]*)([^a-z0-9_-]|$)' \
    "${GITHUB_WORKSPACE}/backend/<module>/requirements.txt" > /tmp/requirements-lambda.txt || true
  if grep -qE '^[[:space:]]*[^#[:space:]]' /tmp/requirements-lambda.txt; then
    pip install -r /tmp/requirements-lambda.txt -t . \
      --platform manylinux2014_x86_64 --implementation cp --python-version 3.12 \
      --only-binary=:all: --upgrade
  fi
  ```
- Third-party packages are always installed for the Lambda target (the `--platform ... --only-binary=:all:` flags), not for the runner's platform. See `deploy_bedrock_lambda.yml`, `deploy_open_connection.yml`.

## Compiled dependencies

pydantic-core, Pillow and cryptography ship CPython-specific compiled wheels. Add a fail-fast "Verify compiled dependencies for Lambda target" step after packaging that checks for the `*.cpython-312-x86_64-linux-gnu.so` file of each, as in `deploy_paint_lambda.yml`:

```bash
test -f /tmp/lambda-package/pydantic_core/_pydantic_core.cpython-312-x86_64-linux-gnu.so \
  || { echo "::error::pydantic_core cp312 x86_64 extension missing from package"; exit 1; }
```

If the Lambda moves to arm64, change `--platform` to `manylinux2014_aarch64` and update the verify step.
