---
name: s3-spec-review
description: Review findings for the new S3-Bucket-Specification.md wiki page (2026-07-02) — first review to find an in-page self-contradiction as the primary defect, not cross-page drift.
metadata:
  type: project
---

An aws-architect agent created `S3-Bucket-Specification.md` (wiki repo) from scratch, consolidating all 4
S3 buckets (uploads, Bedrock-generation output, PDF output, frontend hosting) with purpose/IAM/lifecycle
detail and reciprocal Related-Pages links added to 8 other pages (`High-Level-Design.md`, `Data-Model.md`,
`Lambdas.md`, `Home.md`, and the 4 relevant `Lambda---*.md` pages). Reviewed 2026-07-02, findings written to
`C:\programm\miniature-ai-guide\docs\s3_spec_review_findings.md` (review-only, hand to aws-architect —
same process as [[bedrock-generation-review]] and earlier reviews in this set).

**Key finding — new failure shape for this doc set**: the page's own §4 diagram/caption and §5 "naming-
prefix inconsistency" bullet claim buckets 2 and 3 both use a "bare `<uuid>/` prefix," but bucket 3's own
§1.1 detail section (and the §4 diagram itself, three lines above the contradicting caption) documents
bucket 3's actual convention as `pdfs/<jobId>/guide.pdf` — a literal-prefix-then-ID shape identical to
bucket 1's `uploads/<uuid>/`, not bare at all. This is a contradiction **within the same page**, between
its own detail section and its own summary bullet — a new variant of the "table says X, prose contradicts
X" pattern first seen in [[data-model-review]], but this time entirely self-contained on one newly-written
page rather than spanning two pages. Verified against the real source (`Lambda---PDF-Generation.md`) and
confirmed the page's own §1.1 got it right; only the §4/§5 summary claims are wrong. Only bucket 2 is
actually bucket-root-bare.

**Also confirmed NOT a defect**: the task specifically asked me to check whether the page's "5-day PDF
lifecycle" figure was invented (this project's convention requires flagging undecided details, not
presenting invented specifics as settled). Checked and confirmed the 5-day figure genuinely exists in
`High-Level-Design.md` §2/§4/§5 — not invented. Good reminder that a "verify this isn't invented" ask can
resolve clean; don't assume every review task's premise implies a defect will be found.

**Minor finding**: bucket 1's IAM description overstates Start Job's read scope as excluding `GetObject`
("not GetObject"), when the actual source (`Lambda---Start-Job.md`) hedges with "and/or" between
`ListBucket`/`HeadObject`/`GetObject` as candidate existence-check actions — the practical meaning is
preserved but the new page asserts more certainty than its source commits to.

**Trend note**: like [[bedrock-generation-review]], link integrity and reciprocal-link-addition were both
fully clean (all ~30 unique `[[Page Name]]` targets resolve; all 8 reciprocal links confirmed present).
Wiki-wide completeness grep (for "bucket"/"S3"/"CORS"/"CloudFront") found nothing missing from the new
page's 4-bucket inventory. Open-question hedging (bucket 2/3 identity, CORS, encryption/versioning,
frontend hosting mechanism) all correctly preserved from source pages, not glossed over.

**Process note**: could not literally run `git -C "<wiki path>" pull` (no shell/bash tool was available in
this session's toolset — only Read/Grep/Glob/Edit/Write and web tools). Read `.git/FETCH_HEAD` and `.git/HEAD`
directly instead to sanity-check the repo state, then proceeded to read files directly. If a future session
has actual shell access, prefer running the real `git -C` pull/log commands per the task instructions rather
than falling back to reading `.git/FETCH_HEAD`.
