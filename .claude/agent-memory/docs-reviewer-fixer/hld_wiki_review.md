---
name: hld-wiki-review
description: Location and known issues for the miniature-ai-guide wiki's High-Level-Design.md architecture doc, as of the 2026-07-01 review.
metadata:
  type: project
---

The architecture doc lives at `C:\programm\miniature-ai-guide.wiki\High-Level-Design.md`, in a
**separate git repo** from the main project (`miniature-ai-guide.wiki`, cloned as a sibling
folder to `miniature-ai-guide`). `Home.md` is the wiki index and links to it as `[[High Level Design]]`.

**Key discovery**: the task briefing referenced `docs/open_question_answers.md` as the source
of prior architecture decisions folded into the HLD doc, but this file does **not exist** in the
main repo. Only `docs/globalIdea.md` and `docs/progect_structure.md` exist under `docs/`, and
neither reflects the many "(decided)" claims in the HLD doc (DynamoDB schema, WebSocket-only
delivery/no-fallback, S3 lifecycle durations, CDK-over-Terraform, single-region). `progect_structure.md`
still literally shows `terraform/ (или cdk)` as an open choice, contradicting the HLD's claim that
this is resolved. Before trusting "(decided)" language in this doc against repo state, verify
against actual docs/ files, not just the HLD's own internal claims — the source of truth for many
of these decisions is apparently an out-of-repo conversation with the project owner, not a
checked-in doc.

**Review conducted 2026-07-01** produced a 16-item numbered list of concrete issues (broken
`[[Architecture]]`/`[[Backend]]` wiki-links with no target pages, §1 vs §2/§4 contradiction on
one-vs-three API Gateway+Cognito surfaces, diagram step-ordering mismatch with prose in §3.2 for
WebSocket $connect timing, undefined $connect/$disconnect implementation mechanism, missing
cleanup Lambda in the component table, terminology drift between "job"/"uuid"/"jobId"/"folder"/"prefix").
Full list was returned directly in the conversation response, not written to a file (per
instructions, no report .md files). If revisiting this doc, re-run a fresh review rather than
assuming these 16 items are still accurate — the [[aws-architect]] agent may have already fixed
some of them since this review.

**Process note**: this agent's role for this project is review-only when asked — do not edit
`High-Level-Design.md` directly even when issues are obvious; hand the list to an aws-architect
agent instead. This was an explicit instruction in the task, not just general caution.
