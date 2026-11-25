---
name: wiki-full-rewrite-2026-11
description: Nov 2026 full wiki-vs-code rewrite; earlier wiki was a pre-implementation design (4 images, PDF, Step Functions) that the code diverged from; records source-of-truth rules and naming quirks for the wiki repo.
metadata:
  type: project
---

On 2026-11-25 the whole wiki (`C:\programm\miniature-ai-guide.wiki`) was rewritten against the code. The July 2026 reviews ([[bedrock-generation-review]], [[data-model-review]], [[s3-spec-review]]) audited internal wiki consistency only; the pages were design docs that never matched shipped code. The real pipeline: lambda_upload -> upload-confirmation -> (WebSocket open/close) -> start_job -> paint queue -> paint (OpenAI) -> guide-creation queue -> bedrock (Converse) -> result queue -> result (Vallejo + WebSocket). **One** image, no PDF, no Step Functions, statuses UPLOADED/IN_PROGRESS/PAINTED/COMPLETED/FAILED, GSI `connectionId-index`.

**Why:** wiki pages were written from design discussions; "planned/decided" claims (24 h / 5-day S3 lifecycle, CDK, single region) cannot be verified because the repo has no IaC. Mark these "Needs verification" instead of deleting or asserting.

**How to apply:**
- Verify against `backend/<module>/handler.py` first, module README second (READMEs drift: open_connection lists unused COGNITO_CLIENT_ID, close_connection IAM omits Query, upload-confirmation says `PUT /job`). Root `README.md` has an accurate architecture summary.
- Wiki page name convention: `Lambda---<Name>.md` linked as `[[Lambda - <Name>]]`. Exception: Upload Confirmation lives in `Lambda-‐-Upload-Confirmation.md` (U+2010 hyphen, created by the GitHub web UI) and every link to it must use the same U+2010 character; the ASCII duplicate was deleted. Functional page names are kept for Send Result (module `result`) and WebSocket Connect/Disconnect (modules `open_connection`/`close_connection`).
- The wiki is a separate git repo: never commit/push from the agent when the task says the user will; use `git -C` if git is needed.
- Planned-only pages kept as short stubs: PDF Generation, Periodic Cleanup.
