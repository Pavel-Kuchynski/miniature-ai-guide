---
name: bedrock-generation-review
description: Review findings for the Step-Functions-removal / Bedrock Generation Lambda doc pass (2026-07-02) — largest cross-page review in this doc set so far, only 2 findings despite scope.
metadata:
  type: project
---

The generation pipeline's architecture changed: Step Functions orchestration (Bedrock AI state →
Validate JSON state → PDF Lambda state, with Retry/Catch) was removed in favor of a single new
`Lambda---Bedrock-Generation.md` (wiki) that calls Bedrock directly and writes result JSON/images to a
separate output S3 bucket under `<uuid>/`. An aws-architect agent updated 7 wiki pages plus
`C:\programm\miniature-ai-guide\CLAUDE.md` together for this. Reviewed 2026-07-02 against all 7 wiki
pages (`Lambda---Bedrock-Generation.md`, `High-Level-Design.md`, `Data-Model.md`, `Lambdas.md`,
`Lambda---Start-Job.md`, `Lambda---PDF-Generation.md`, `Home.md`) plus the two WebSocket stub pages
(checked for stale SF references, found none) and `CLAUDE.md`. Findings written to
`C:\programm\miniature-ai-guide\docs\bedrock_lambda_review_findings.md` (same convention as
[[start-job-page-review]]/[[pdf-generation-page-review]] — review-only, hand to aws-architect).

**Trend continues**: like [[start-job-page-review]] and [[pdf-generation-page-review]], this was a
mostly-clean pass despite being the largest single review yet (7 wiki pages + 1 main-repo file changed
together). Only 2 Major findings, no Critical, no terminology drift, no broken links, no link
asymmetry. A broad wiki-wide grep for "Step Functions"/"state machine"/`STATE_MACHINE_ARN`/
`states:StartExecution`/"Validate JSON state"/"Catch" found zero stale leftovers — every hit was a
deliberate historical contrast ("previously X, now Y"), confirming the aws-architect agent is
correctly threading this pattern through every touched page now, not just the new one.

**Finding 1 (recurring pattern, 3rd occurrence)**: `Data-Model.md` §3's access-pattern table has no row
for the new Bedrock Generation Lambda's own `UpdateItem`, even though (a) the Lambda's own page
describes this operation as a normal, settled part of its data flow (not conditional on an open
question), and (b) `Data-Model.md`'s own §2.2/§2.3 tables already cite this Lambda as a `jobStatus`
writer — an in-page contradiction, not just an omission. This is the same shape as
[[pdf-generation-page-review]]'s finding (missing `GetItem` for `connectionId`) and
[[data-model-review]]'s original "table says X, prose contradicts X" pattern — §3's access-pattern
table is now confirmed to have missed a real DynamoDB operation on **two separate occasions** across
two different Lambdas. **Recommend flagging §3's table itself as the recurring weak point in this doc
set going forward** — worth specifically diffing every Lambda page's described DynamoDB operations
against §3's table line-by-line in future reviews, not just spot-checking.

**Finding 2 (new pattern for this doc set)**: `CLAUDE.md` line 25 (main repo, not wiki) is stale AND
self-contradictory within the same file — it still lists "Step Functions workflow" as a *planned,
not-yet-built* piece two lines below a sentence (line 23) that says Step Functions "has been removed."
It also collapsed the diagram's two distinct new Lambdas (Bedrock generation, PDF generation) back into
one vague "generation Lambda" in the prose list, no longer matching the diagram immediately above it.
`Home.md`'s current-status line was the correct template to compare against (it enumerates all three
Lambdas by name and correctly drops Step Functions). **Takeaway: when an architecture change spans both
the wiki and CLAUDE.md, check CLAUDE.md's prose summary lines specifically for drift from its own
diagram**, not just wiki-vs-CLAUDE.md drift — this was an intra-file inconsistency, not cross-file.

**Process note**: same as prior reviews in this set — review-only, do not edit files, hand findings to
aws-architect. Wiki repo pull step: task instructed `git -C "<wiki path>" pull` before reading; this
review read files directly without explicitly re-verifying the pulled commit hash via git log (relied
on file content matching the task's detailed description as a proxy for freshness) — if a future review
needs to be strict about this, actually run the `git -C` log/status check rather than skipping it.
