---
name: pdf-generation-page-review
description: Review findings for Lambda---PDF-Generation.md rewrite (2026-07-02) — second clean-ish review in this doc set, only 2 minor findings, no contradictions.
metadata:
  type: project
---

`Lambda---PDF-Generation.md` was rewritten by an aws-architect agent (wiki commit `513803d`, master)
to mirror `Lambda---Presigned-URL.md`'s structure, following the same pattern as
[[start-job-page-review]]. Reviewed 2026-07-02 against `High-Level-Design.md`, `Data-Model.md`,
`Lambda---Presigned-URL.md`, `Lambda---Start-Job.md`, `Lambda---WebSocket-Disconnect.md`,
`Lambda---WebSocket-Connect.md`, `Lambda---Periodic-Cleanup.md`. Findings written to
`C:\programm\miniature-ai-guide\docs\pdf_generation_review_findings.md` (per established convention
from [[lambda-pages-review]] — review-only, hand findings to aws-architect, do not edit wiki files).

**Trend confirmed**: this is the *second* consecutive clean-ish review in this doc set (after
[[start-job-page-review]]) — only 2 minor findings, no Critical/Major, no broken links, no
terminology drift, no accuracy contradictions against `jobStatus` enum values, `GoneException`/
`connectionId` cleanup mechanics (verified consistent with `Lambda---WebSocket-Disconnect.md`), or
the Step-Functions-`Catch`-path failure delivery claim. All genuinely-open design questions (PDF
rendering library, idempotency, output key naming, `pdfUrl` storage form) are honestly flagged as
undecided rather than presented as fact — this page's hedging discipline is a strength, not a defect
to flag. Reinforces the [[start-job-page-review]] takeaway: don't assume earlier-pass issue
categories (drift, duplication, link asymmetry) will keep recurring — verify fresh each time.

**The 2 findings**: (1) this page's Purpose/Data-Flow sections describe the Lambda *reading*
`connectionId` from DynamoDB before `PostToConnection`, but `Data-Model.md` §3.6's access-pattern
table — meant to be the authoritative per-component DynamoDB operation catalogue — only lists this
Lambda's `UpdateItem` calls, omitting the `GetItem`/read of `connectionId` entirely. Not a
contradiction, a completeness gap in the *other* page. (2) minor Related-Pages asymmetry: this page
discusses `[[Lambda - WebSocket Connect]]` substantively in-body but doesn't list it in Related
Pages (only WebSocket-Disconnect is listed); separately, this page links to `Lambda---Start-Job.md`
but Start-Job's own Related Pages doesn't reciprocate (same asymmetry shape as
[[lambda-pages-review]]'s original finding, though that review's specific instance was already fixed
on the Start-Job side).

**Confirms `[[data-model-review]]`'s access-pattern table is not fully self-consistent with sibling
pages either** — worth checking `Data-Model.md` §3's table against each Lambda page's own described
DynamoDB operations specifically (not just enum/status values) in future reviews of this set, since
this is the first time a *read* operation (as opposed to a write/enum value) was found undocumented
in the table.
