# Security rules for workflows

- OIDC only. Never put access keys, secrets or account IDs in workflow files. Config goes through `vars.*`, and genuinely secret values through `secrets.*`.
- Pin actions to the major versions used in the repo: `actions/checkout@v4`, `actions/setup-python@v5`, `actions/setup-node@v4`, `aws-actions/configure-aws-credentials@v4`, `actions/upload-artifact@v4`, `actions/download-artifact@v4`.
- `permissions` at workflow level: `id-token: write` only where AWS auth is needed, `contents: read` always. PR-triggered workflows get `contents: read` only.
- Never interpolate untrusted event data (PR titles, branch names, issue bodies) directly into `run:` scripts; pass it through `env:`.
- Prefer `env:` variables over inline `${{ vars.X }}` inside shell scripts when the value is used more than once.
- When proposing the deploy role's IAM policy in a header or report, scope it to the specific function ARN (`lambda:UpdateFunctionCode`, `lambda:GetFunction`, `lambda:GetFunctionConfiguration` for the waiter), not `lambda:*` on `*`. Call out every wildcard.
- Ideally the role's trust policy `sub` condition is limited to this repo and the `main` branch or the `production` Environment.
- Never run mutating AWS CLI commands from the agent while editing workflows; they run only in the pipeline.
