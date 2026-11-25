# Frontend deploy workflow

Rules for `.github/workflows/deploy-frontend.yml` (Vite build, S3 static hosting, optional CloudFront).

- Trigger: push to `main` with `paths: ["frontend/**", ".github/workflows/deploy-frontend.yml"]` plus `workflow_dispatch`. Same `permissions` and `concurrency` pattern as Lambda workflows (group `deploy-frontend`).
- Two jobs: `build`, then `deploy` (`needs: build`).
- Build job: Node 20 with `actions/setup-node@v4` and npm cache keyed on `frontend/package-lock.json`; use `npm ci`, not `npm install`. `VITE_API_BASE_URL` and `VITE_WS_BASE_URL` come from repo variables and are baked in at build time. Upload `frontend/dist` with `actions/upload-artifact@v4` (`if-no-files-found: error`, short retention).
- Deploy job: download the artifact, verify required variables (`FRONTEND_S3_BUCKET`, `AWS_DEPLOY_ROLE_ARN`, `AWS_REGION`, `VITE_API_BASE_URL`, `VITE_WS_BASE_URL`) and fail with `::error::` if any is empty, configure AWS credentials via OIDC, then `aws s3 sync ./dist "s3://${{ vars.FRONTEND_S3_BUCKET }}" --delete --no-progress`.
- `--delete` removes remote files that are not in the build. Mention it in the header comment and flag it in any report.
- CloudFront invalidation (`aws cloudfront create-invalidation --paths "/*"` and `wait invalidation-completed`) runs only if `vars.CLOUDFRONT_DISTRIBUTION_ID` is non-empty.
- Deploy job can sit behind `environment: production` with required reviewers. It is currently commented out; enable it only when the user confirms the GitHub Environment exists.
- The deploy role policy needs `s3:PutObject`, `s3:GetObject`, `s3:DeleteObject`, `s3:ListBucket` on the bucket and `cloudfront:CreateInvalidation` on the distribution ARN only.
