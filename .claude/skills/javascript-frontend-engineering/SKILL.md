---
name: javascript-frontend-engineering
description: Use this skill whenever writing, reviewing, refactoring, or extending frontend JavaScript code in this repository, especially the static vanilla-JS frontend under frontend/src. Trigger on requests like "write frontend code", "add a page/component", "wire up an API call", "fix a frontend bug", "review this JavaScript code", or any task that creates or modifies .js, .html, or .css files in the frontend. Encodes the repository's conventions for vanilla JS structure, Vite build, AWS Amplify Cognito auth, API client isolation, S3 presigned uploads, WebSocket handling, testing with Vitest, and documentation.
---

# JavaScript frontend engineering style

Use this skill for all frontend code written or reviewed in this repository.

## Project context

The frontend is a static site built with Vite, hosted on S3, and written in vanilla JavaScript (ES2022+), HTML5, and CSS. It talks to the backend only through API Gateway endpoints secured by Cognito. No React/Vue/Svelte framework is used at this stage — keep new code framework-free unless that decision is explicitly revisited.

## Core principles

- Prefer explicit, readable code over clever or compressed code.
- Preserve established repository patterns when modifying existing code.
- Do not rewrite working code merely to match personal preferences.
- Keep functions focused and independently testable where practical.
- Prefer simple, flat designs over premature abstractions.
- Handle async/network failure paths explicitly — loading, error, and empty states for every API call.
- Never expose secrets, AWS credentials, or tokens in logs or UI messages.
- Validate external input at system boundaries.
- Isolate side effects (DOM, network, storage) behind small modules so business logic stays testable.

## Scope and boundaries

- You own: frontend source code (`frontend/src/`), `frontend/package.json`, `frontend/README.md`, tests, Vite/Vitest config, and styling.
- You do NOT own backend Lambda/Python code (`backend/*`) or infrastructure/CI-CD (`.github/workflows/*`, IaC). Hand those concerns off rather than editing them yourself.
- The frontend talks to the backend only through documented API Gateway endpoints. Never assume direct access to S3 buckets, Lambda internals, or AWS credentials from client code.
- If asked to wire up an endpoint that doesn't exist yet, say so explicitly rather than inventing its contract — ask or propose a reasonable interface and flag it as an assumption.

## Formatting

- Follow the existing ESLint/Prettier configuration in `frontend/package.json`.
- Use 2-space indentation. Never use tabs.
- Keep lines to approximately 100 characters.
- Use semicolons and trailing commas consistently with the existing codebase.
- Prefer `const` and `let`; avoid `var`.
- Prefer arrow functions for callbacks and short expressions.
- Use JSDoc for exported functions and complex types.
- Match formatting of nearby existing code.
- Do not introduce new linting/formatting tools or repository-wide configs unless explicitly requested.

## Naming

- Functions and variables: `camelCase`.
- Constants: `UPPER_SNAKE_CASE` for true module-level constants.
- Classes and custom errors: `PascalCase`.
- Private helpers: prefix with `_` only when necessary; prefer module-private scope via closure or separate file.
- Boolean predicates should use names such as `is`, `has`, `can`, or `should`.
- Use descriptive domain-specific names.
- Keep API/domain terminology consistent inside frontend code.

## Module structure

Prefer small, single-purpose modules under `frontend/src/`:

```text
frontend/src/
  index.html          # App entry HTML
  main.js             # Bootstrap: mount views
  styles.css          # Base responsive layout
  api.js              # Backend API client (fetch isolated here)
  uploadClient.js     # Presigned S3 PUT upload with progress
  websocketClient.js  # WebSocket connection for generation status
  validation.js       # Pure file-selection validation
  uploadView.js       # Upload view: selection, preview, orchestration, UI states
  auth.js             # Cognito auth via AWS Amplify
  authView.js         # Login/Logout header UI
  *.test.js           # Vitest tests for the above
```

Do not introduce shared utility packages, generic abstractions, or state libraries unless there is a demonstrated need.

## Framework and build

- The project uses Vite with `root: "src"`, `base: "./"`, and a flat `dist/` output for S3 static hosting.
- Read build-time env vars via `import.meta.env.VITE_*` only.
- Keep client-side routing assumptions minimal; the site has no server-side runtime.
- Do not introduce a frontend framework without explicit approval.

## Authentication

- Use AWS Amplify v6's Auth category, isolated in `auth.js`.
- The app uses Cognito Hosted UI with Authorization Code + PKCE flow.
- Attach the Cognito ID token (not access token) as `Authorization: Bearer <idToken>` to API Gateway requests.
- Handle signed-out / 401 / 403 states explicitly in views.
- Never log tokens or user credentials.

## API client conventions

- Isolate all `fetch` calls in `api.js` so UI code never talks to the network directly.
- Export custom error classes (e.g., `ApiError`) with `status` and optional `cause`.
- Validate response shape before returning; throw `ApiError` for malformed responses.
- Provide `fetchImpl`/`baseUrl` injection options for testability.
- Log CORS/network failures with actionable diagnostics, but keep user-facing messages generic.

## Upload conventions

- Request presigned S3 PUT URLs from the backend, then `PUT` files directly to S3.
- Use `XMLHttpRequest` for S3 uploads because `fetch` has no reliable cross-browser upload progress API.
- The `Content-Type` header sent with the PUT must exactly match the content type used to generate the presigned URL.
- Report per-file progress and support cancellation via `AbortSignal`.
- Surface S3/network errors with retry affordances without disturbing other uploads.

## WebSocket conventions

- Open WebSocket connections with `jobId` and ID token as query parameters when required by the backend.
- Wrap connection lifecycle in `websocketClient.js`.
- Export a `WebSocketError` class and support `WebSocketImpl` injection for tests.
- Set connection timeouts and reject cleanly on error/close before open.

## Error handling

- Catch the narrowest meaningful exception.
- Translate internal failures into stable external responses and UI messages.
- Keep diagnostic details in logs rather than user-facing messages.
- Do not expose stack traces or raw exception messages to users.
- Use custom error classes for meaningful domain-specific error boundaries.

## Validation

Validate external input at the system boundary:

- Required fields and file counts.
- File types and sizes.
- Required configuration (env vars, base URLs).
- Authentication state.

Do not silently invent defaults for required values.

## DOM and views

- Keep views framework-free: render with template strings and `innerHTML`, attach event listeners at the container level.
- Use `data-role` and `data-action` attributes for selectors instead of classes/IDs where possible.
- Escape all dynamic HTML with a dedicated helper (e.g., `escapeHtml`).
- Maintain explicit UI state machines (e.g., `PHASE` constants) rather than implicit DOM state.
- Clean up resources: revoke object URLs, remove event listeners, close WebSockets.

## Testing

- Use Vitest with `jsdom` environment.
- Tests live next to source files as `*.test.js`.
- Mock external dependencies (fetch, XMLHttpRequest, WebSocket, AWS Amplify) — never hit real AWS or network in unit tests.
- Prefer pure functions for data transforms; test them directly.
- Test at minimum:
  - Happy paths;
  - Missing and invalid input;
  - Missing configuration;
  - Authentication failures;
  - Network/API failures;
  - Successful state transitions;
  - Error response mapping.

Run:

```bash
cd frontend
npm test
```

## Documentation

- Add a module-level comment explaining purpose and boundaries.
- Document exported functions with JSDoc when behavior isn't obvious.
- Explain intent, constraints, and non-obvious WHYs.
- Do not add comments that merely restate code.
- Keep `frontend/README.md` synchronized with meaningful module behavior changes.

## Abstraction discipline

Do not introduce generic component libraries, state-management libraries, utility monorepos, or design systems for a single use case without a concrete reason.

Add abstractions only when they solve demonstrated duplication, complexity, or reuse needs.

## AI agent workflow

Before writing or modifying frontend code:

1. Inspect the existing module.
2. Inspect nearby tests.
3. Identify established naming, rendering, and event-handling patterns.
4. Identify error-response and logging conventions.
5. Identify API/auth integration patterns.
6. Preserve existing behavior unless a change is requested.
7. Make the smallest coherent change.
8. Add or update tests for changed behavior.
9. Review the result for naming, logging, errors, security, testability, and accidental behavior changes.

When reviewing existing code:

- Identify actual bugs separately from style preferences;
- Do not call a style preference a bug;
- Preserve intentional existing patterns;
- Recommend changes only when they improve correctness, security, maintainability, or consistency.

## Definition of done

Before finalizing frontend code, verify:

- Naming is consistent;
- Functions have clear responsibilities;
- External input is validated;
- Configuration is validated;
- Async errors are handled at appropriate boundaries;
- UI messages do not leak internals;
- Tokens and credentials are never logged or exposed;
- Network/AWS access is isolated behind client modules;
- Tests cover important success and failure paths;
- No unnecessary abstraction or dependency was introduced;
- Existing behavior was preserved unless explicitly changed;
- `frontend/README.md` is updated if behavior changed.
