---
name: data-model-review
description: Review findings for the Data-Model.md wiki page (DynamoDB schema doc) as of the 2026-07-01 review — partial-propagation pattern recurs.
metadata:
  type: project
---

Data-Model.md lives at `C:\programm\miniature-ai-guide.wiki\Data-Model.md`, added after
[[hld-wiki-review]] and [[lambda-pages-review]] to document the DynamoDB schema, access patterns,
GSI analysis, TTL strategy, and billing mode that High-Level-Design.md §2/§4/§5 deferred to it.

**Review conducted 2026-07-01**, review-only (findings handed to aws-architect, not fixed directly —
same [[hld-wiki-review]] process note applies). Produced a 9-item list, returned directly in the
conversation response (no report file written, per instructions).

**Recurring pattern across this project's docs now confirmed a third time**: when a new wiki page
resolves an ambiguity/contradiction from an older page, the resolution is documented on the *new*
page but the *old* page's contradicting text is left untouched, with only a soft "should be read
with this in mind" pointer rather than an actual edit or explicit "supersedes" flag. Found in
[[hld-wiki-review]] (terminology drift) and [[lambda-pages-review]] (duplication + link asymmetry),
and again here:
- HLD §4's TTL bullet ("24-48h, in step with S3 lifecycle") directly contradicts Data-Model.md §4's
  considered TTL design (7 days, deliberately independent of S3 windows) — HLD §4 was never updated,
  unlike HLD §5's DynamoDB bullet which *was* correctly updated to say "Resolved... see Data Model."
- HLD §3.1's diagram and Lambda---Presigned-URL.md's diagram both still literally show "Frontend
  writes {...} to DynamoDB" — the exact direct-browser-write design Data-Model.md §2.2 rejects.
- Stale ad hoc jobStatus strings ("uploaded"/"failed"/"complete") still appear verbatim in HLD
  §3.2/§4/§5, uncorrected after Data-Model.md §2.3 defined the real enum (UPLOADED/IN_PROGRESS/
  SUCCEEDED/FAILED).
- HLD §2's WebSocket API row still says DynamoDB is "extended... with reverse lookup by jobId/uuid,"
  which is exactly the GSI-shaped pattern Data-Model.md's own GSI analysis concludes is unnecessary.

**Also found**: a self-contradiction *within* Data-Model.md itself (not just cross-document) — §2.2's
attribute table says `jobId`/`imageUrls`/`jobStatus` are "Written by: Frontend, at initial PutItem,"
but the very next paragraph on the same page concludes a Lambda (not the frontend) must perform the
write. Worth specifically checking for this "table says X, prose right below contradicts X" shape in
future reviews of this doc set, since it's cheaper to catch than cross-file drift.

**Positive finding**: Lambda-coverage completeness check was fully clean — every Lambda mentioned in
Lambdas.md/HLD §2 (Presigned-URL, Start-Job, $connect, $disconnect, PDF-Generation, Periodic-Cleanup)
has its DynamoDB interaction (or explicit non-interaction) correctly and consistently represented in
Data-Model.md §3's access-pattern table. Wiki-link syntax was also fully clean (9/9 links resolve).

**Suggested general fix direction for aws-architect** (not applied by this review): when a new page
resolves an old page's ambiguity, either (a) actually edit the old page's contradicting text, or (b)
at minimum add an explicit "Note: superseded by [[New Page]] §N" callout at the exact contradicting
line — not just a generic "read this with X in mind" note elsewhere on the new page.
