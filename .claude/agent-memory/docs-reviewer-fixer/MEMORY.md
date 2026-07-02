# Memory Index

- [High-Level-Design.md review notes](hld_wiki_review.md) — wiki architecture doc lives in sibling repo; missing source doc; 16 issues found 2026-07-01.
- [Lambda pages review notes](lambda_pages_review.md) — 6-page Lambda restructure reviewed 2026-07-01; GitHub wiki link convention confirmed; partial-duplication + link-asymmetry issues found.
- [Data-Model.md review notes](data_model_review.md) — DynamoDB schema doc reviewed 2026-07-01; confirms recurring "resolution doesn't propagate back to old page" pattern; found in-page self-contradiction too.
- [Lambda---Start-Job.md review notes](start_job_page_review.md) — reviewed 2026-07-02, only 2 minor findings, no drift/contradictions; asymmetry from earlier review now fixed.
- [Lambda---PDF-Generation.md review notes](pdf_generation_page_review.md) — reviewed 2026-07-02, only 2 minor findings; found Data-Model.md §3.6 omits a GetItem this Lambda performs.
- [Bedrock Generation Lambda / Step-Functions-removal review](bedrock_generation_review.md) — 7-page wiki + CLAUDE.md reviewed 2026-07-02; 2 findings: Data-Model §3 table gap (3rd occurrence), CLAUDE.md self-contradiction.
- [S3-Bucket-Specification.md review](s3_spec_review.md) — reviewed 2026-07-02; page self-contradicts its own bucket-3 prefix claim (says "bare", own detail section says "pdfs/<jobId>/"); 5-day lifecycle figure confirmed genuine, not invented.
