# JavaScript frontend engineering

Standards and conventions for writing, reviewing, refactoring, and extending the frontend JavaScript in this repository: vanilla JS structure, Vite build, AWS Amplify Cognito auth, API client isolation, S3 presigned uploads, WebSocket handling, testing with Vitest, and documentation.

**The full, current guidance lives in `SKILL.md`** — its frontmatter `description` is what actually determines when Claude uses this skill, so treat that file as the source of truth rather than this one. This README exists only as a quick human-readable pointer for people browsing the repo.

## Quick reference

- Full conventions: [`SKILL.md`](./SKILL.md)
- Canonical code examples (API client, error classes, WebSocket client, escaping): [references/code-examples.md](./references/code-examples.md)

## Prerequisites

- Familiarity with vanilla JavaScript (ES2022+), HTML5, and CSS
- Basic understanding of async/await and fetch API
- Knowledge of testing concepts (mocks, assertions)
- Understanding of AWS Cognito and API Gateway concepts (not required, but helpful)
