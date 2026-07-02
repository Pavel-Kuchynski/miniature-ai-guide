---
name: lambda-pages-review
description: Wiki page-set for the 6 Lambda functions (one page each) plus the Lambdas.md index — structure, link convention, and known duplication issue as of 2026-07-01.
metadata:
  type: project
---

Following [[hld-wiki-review]], an aws-architect agent restructured the wiki so each Lambda in
`High-Level-Design.md` §2's component table gets its own page: `Lambdas.md` (index/hub) plus
`Lambda---Presigned-URL.md` (real, implemented), `Lambda---Start-Job.md`,
`Lambda---Periodic-Cleanup.md`, `Lambda---WebSocket-Connect.md`, `Lambda---WebSocket-Disconnect.md`,
`Lambda---PDF-Generation.md` (5 stubs for not-yet-built Lambdas). All live in the wiki repo
(`C:\programm\miniature-ai-guide.wiki`), sibling to the main repo.

**GitHub wiki link convention confirmed working**: a `[[Lambda - Presigned URL]]` link (with
literal spaces around the dash in the page title) maps to filename `Lambda---Presigned-URL.md` —
each space in the title becomes one hyphen, so `" - "` (3 chars) becomes `"---"` (3 hyphens). This
held consistently across all 6 Lambda pages when reviewed 2026-07-01 — no broken links among the
new page set.

**Review conducted 2026-07-01** (this review, separate from the earlier HLD-only review) produced
a 10-item numbered list, saved to `C:\programm\miniature-ai-guide\docs\lambda_pages_review_findings.md`
in the main repo (per project convention: review artifacts go in main-repo `docs/`, not the wiki).
Key findings: `Home.md` still has a broken `[[Backend]]` link that `High-Level-Design.md` §6 was
fixed to avoid (de-linked to plain italic text) — the two pages now use inconsistent conventions
for the same not-yet-created page. Also: §2.1 of the HLD was properly trimmed to a pointer when
lambda_upload detail moved to its own page, but §4 and §5 of the HLD still substantially duplicate
(not just summarize) the Presigned-URL page's trade-offs/risks prose — a partial move, not a full
one. Cross-reference asymmetry: the Presigned-URL page links out to Start-Job and Periodic-Cleanup
(as its downstream dependents) but neither of those stub pages links back — likely accidental,
since the $connect/$disconnect/PDF-Generation trio does reciprocate consistently.

**Process note**: same as [[hld-wiki-review]] — review-only task, do not edit wiki files even when
fixes are obvious; hand the findings list to an aws-architect agent. This is now an established
pattern for this project: aws-architect writes/restructures the wiki, docs-reviewer-fixer reviews
it after the fact and writes findings to `docs/*_review_findings.md` in the main repo.
