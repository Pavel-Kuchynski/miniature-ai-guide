# Frontend

Static frontend for the Miniature Painting Guide Generator: vanilla JavaScript (ES2022+),
HTML5, and CSS, bundled with [Vite](https://vitejs.dev/) and deployed to S3 as a static site.

No frontend framework (React/Vue/etc.) is used at this stage — this may change as the app
grows, but keep new code framework-free unless that decision is revisited.

## Prerequisites

- Node.js 20.19+ (or 22.12+) and npm. CI (`.github/workflows/deploy-frontend.yml`,
  `run_unit_tests.yml`) runs Node 20.

## Setup

```bash
cd frontend
npm ci
```

`npm ci` installs exactly what is in `package-lock.json`, as CI does. Use `npm install` only
when you intentionally change dependencies.

## Development

```bash
npm run dev
```

Starts a local dev server (default: http://localhost:5173) with hot reload.

### Environment configuration

To test the app against a real deployed backend, create a `.env.local` file in `frontend/`
(git-ignored, so it's safe to put real values in it):

```
VITE_API_BASE_URL=https://<api-id>.execute-api.<region>.amazonaws.com/<stage>
VITE_WS_BASE_URL=wss://<ws-api-id>.execute-api.<region>.amazonaws.com/<stage>
```

- `VITE_API_BASE_URL` is the base URL of the deployed REST API Gateway stage — find it in the
  API Gateway console (or from whoever deployed the stack) as the "Invoke URL" for the
  relevant stage; it should include the stage path (e.g. `.../miniature-ai-guide`) but not
  a trailing slash or endpoint-specific path segments like `/upload-urls`.
  It's read at build/dev-server start time by Vite (`import.meta.env.VITE_API_BASE_URL`) and
  consumed via `getApiBaseUrl()` in `src/api.js`.
- If `.env.local` is missing or `VITE_API_BASE_URL` is unset, the frontend defaults to `""`
  (same-origin requests), which only works if the frontend and API share an origin —
  for local development against the real backend, you must set it explicitly.
- `VITE_WS_BASE_URL` is the WebSocket API base URL for the status channel (`wss://...`,
  same stage path convention). It is **required** for the generation flow: if it is unset,
  `openGenerationWebSocket()` in `src/websocketClient.js` fails with a "WebSocket base URL is
  not configured" error.
- CI bakes both values into the production build from the GitHub repository variables of the
  same names (see Deployment below). The host names in the workflow comments and in this file
  are examples; they must match those variables.
- Restart `npm run dev` after creating or changing `.env.local` — Vite only reads env files
  on startup.

## Build

```bash
npm run build
```

Outputs a production build to `dist/`. The build uses relative asset paths (`base: "./"`)
and a flat output directory so the contents of `dist/` can be uploaded directly to an S3
bucket configured for static website hosting, with no server-side routing assumptions.

```bash
npm run preview
```

Serves the production build from `dist/` locally, for a final check before deployment.

## Testing

```bash
npm test
```

**Test with coverage**
```bash
npm test -- --coverage
```
Runs the [Vitest](https://vitest.dev/) unit/component test suite (`src/**/*.test.js`) once,
using a `jsdom` environment. Covers the pure validation helpers, the API client (with
`fetch` mocked), the S3 upload client (with `XMLHttpRequest` mocked), the WebSocket client
(with `WebSocket` mocked), the auth module (with Amplify mocked), and the upload view's
state transitions (with `api.js`/`uploadClient.js` mocked).

`src/secretLogging.test.js` is the console-leak guard: it runs the real auth, API, WebSocket
and upload code paths, spies on every `console.*` method, and fails if any logged argument
contains an ID/access token, an `Authorization` value, a `token=` or `accessToken=` query
parameter, or a presigned-URL signature. Add a case there when a new module logs anything near credentials.

CI runs the same suite: the `frontend-test` job of `run_unit_tests.yml` (on pull requests)
and the `test` job of `deploy-frontend.yml` (which gates the build and deploy).

## Linting & formatting

```bash
npm run lint           # ESLint, reports problems
npm run format         # Prettier, rewrites files in place
npm run format:check   # Prettier, checks only, writes nothing
```

`npm run lint` runs `eslint src` with the flat config in `eslint.config.js`: ESLint's
`recommended` rules for browser ES modules, plus Node globals in `*.test.js` files
(`dist/` and `coverage/` are ignored). Vitest APIs are imported from `vitest`, so they are
not declared as globals.

Prettier formats `src/**/*.{js,css,html}` using `.prettierrc` (80 columns, LF line endings,
double quotes, trailing commas everywhere); `.prettierignore` skips `dist`, `coverage` and
`node_modules`. Run `npm run format` before committing.

The `frontend-test` job of `run_unit_tests.yml` runs `npm run lint` and `npm run format:check`
before the tests, and fails the pull request on any violation.

On Windows with `core.autocrlf=true`, files in the working tree may be checked out as CRLF,
and then `format:check` reports every file. `npm run format` rewrites them to LF, and git
normalizes the line endings on commit.

## Project structure

```
frontend/
├── public/               # Static assets copied as-is into dist/ (favicon, images, etc.)
├── src/
│   ├── index.html        # Entry point
│   ├── styles.css        # Base responsive layout + upload view styles
│   ├── main.js           # App bootstrap: mounts the auth bar + upload view
│   ├── validation.js      # Pure file-selection validation (count/type/size)
│   ├── api.js             # Backend API client (upload URLs, job creation, instruction
│   │                       # generation, result download), fetch isolated here
│   ├── uploadClient.js     # Presigned-URL S3 PUT with progress (XMLHttpRequest)
│   ├── websocketClient.js  # Status WebSocket connection lifecycle for the generation flow
│   ├── uploadView.js       # Upload Images view: selection, preview, orchestration, UI states
│   ├── auth.js             # Cognito Hosted UI auth via AWS Amplify (login/logout/getIdToken)
│   ├── authView.js         # Login/Logout header UI, wired to auth.js
│   ├── secretLogging.test.js # Guard: no tokens/signatures in console output
│   └── *.test.js           # Vitest unit/component tests for the above
├── dist/                 # Build output (git-ignored, generated by `npm run build`)
├── package.json
├── vite.config.js
└── vitest.config.js
```

## Logging and secrets

Nothing in `src/` may log a credential. Concretely:

- Never log the ID token, access token, `Authorization` header values, or request header
  objects.
- Never log a URL that carries a query string: the WebSocket URL (`token=<idToken>`,
  `accessToken=<accessToken>`) and presigned S3 URLs (SigV4 signature). Log the endpoint path or `origin + pathname` instead.
- Never pass a browser `WebSocket` error `Event` or a `WebSocket` constructor error to
  `console`, or keep them as `cause`: both expose the URL.
- Verbose debug output (request paths, identity facts such as `sub` and token expiry) is
  gated behind `import.meta.env.DEV`, so it is stripped from production builds.
- Warnings and errors are kept to status codes, error names, and generic messages.

## Upload Image view

`src/uploadView.js` implements the first user-facing screen:

1. The user selects exactly 1 reference image (`src/validation.js` enforces count, accepted
   image MIME types, and a 15MB size limit). The preview is shown in the left pane
   ("Source image"); the right pane is the processed result ("Processed result"). Both frames
   have the same fixed 1:1 aspect ratio. Below them are a "Neural Color Analysis" palette pane
   and a "Guide the AI" wishes textarea (`data-role="wishes-input"`, max 500 characters,
   `WISHES_MAX_LENGTH` in `src/uploadView.js`). The wishes text is sent as `instruction` in
   `POST /jobs/<jobId>/instruction`. Once that request is sent, the textarea is disabled (no
   editing or focus) until "Start Over" resets the view.
   Clicking the processed result image opens it enlarged in an overlay dialog
   (`data-role="zoom-overlay"`); it closes via the × button, a click on the backdrop, or Escape.
2. On "Upload image", the view calls `requestUploadUrls` (`src/api.js`) to get 1 presigned S3
   `PUT` URL under a UUID folder. The backend (`backend/lambda_upload`) must be updated
   separately to return a single upload item; `api.js` rejects any other count.
3. The file is `PUT` directly to its presigned URL (`src/uploadClient.js`, using
   `XMLHttpRequest` for upload-progress events), with a progress bar.
4. Errors are handled explicitly at every stage and reported in the single message section
   (see "Message section" below): request-URL failures (network or non-2xx/malformed
   response) show an error message and return to the selection state; a failed S3 `PUT`
   shows the error message next to the file, with a **Retry** button, and also in the
   message section.
5. Once the upload succeeds, the message section shows a success message naming the job's
   UUID folder, and the view shows a
   **✨ Generate New Vision** button (`data-action="generate-instruction"`; it reads
   "Generating vision…" while running). Clicking it runs the full generation-kickoff flow in
   one click: `createJob` (`PUT /jobs`) creates the DynamoDB job record, then the view attempts
   to open the status WebSocket (`openGenerationWebSocket`, non-fatal if it fails — a warning
   message is shown but the flow continues), and only once both of those have settled does it call
   `requestInstructionGeneration` (`POST /jobs/<jobId>/instruction`, JSON body
   `{ "instruction": "<wishes text>" }`) to trigger guide generation, per
   `backend/start_job`'s documented contract. The button disables while the flow is running,
   is re-entrancy-guarded against firing twice for the same job while a run is in flight, and
   is hidden once instruction generation has been triggered successfully; on failure (from any
   step) it shows an error message and stays visible so the user can retry. If only the
   instruction request fails, the WebSocket opened for that run is closed, and Retry opens
   a new one.

### Message section

All user-facing messages (validation errors, request and upload errors, upload and
generation-start confirmations, the WebSocket warning, generation and download failures)
go through one message section, rendered once under the actions row
(`showMessage()` in `src/uploadView.js`):

- Info messages are green (`message--info`), error messages are red (`message--error`).
  Errors use `role="alert"`, info uses `role="status"`.
- Only one message is visible at a time: a new message replaces the previous one.
- A message disappears on its own after 10 seconds (`MESSAGE_TIMEOUT_MS`). Re-renders, such
  as upload progress, do not extend that time.
- "Start Over", choosing a new file and removing the image clear the message immediately.
- After a generation or download failure the result pane shows the placeholder ("The
  processed result will appear here."); the error text is in the message section, not in
  the pane.

### Assumptions made (flag before deploying)

- **Endpoint**: `POST {VITE_API_BASE_URL}/upload-urls`, matching `backend/lambda_upload`'s
  request contract (`fileNames/contentTypes` in the JSON body). A stage is deployed at this
  path, but as of this writing the API Gateway resource does not have a working CORS
  preflight (`OPTIONS`) response configured — browser requests from the Vite dev server
  (`http://localhost:5173`) fail with a CORS error / 403 on the `OPTIONS` request. This must
  be fixed on the API Gateway side (an `OPTIONS` method on `/upload-urls` — e.g. via "Enable
  CORS" in the console or a mock integration — returning
  `Access-Control-Allow-Origin`/`-Headers`/`-Methods`); it is not something the frontend can
  work around. See `src/api.js` for the client-side diagnostics added to help confirm this.
- **API base URL**: read from the `VITE_API_BASE_URL` build-time env var (set it in a
  `.env.local` file, which is git-ignored), defaulting to `""` (same-origin), via
  `getApiBaseUrl()` in `src/api.js`.
- **Auth**: every `src/api.js` function (`requestUploadUrls`, `createJob`,
  `requestInstructionGeneration`) attaches the signed-in user's Cognito **ID token** (not the
  access token) as `Authorization: Bearer <idToken>`, via `getIdToken()` in `src/auth.js`. The
  ID token is used because the deployed API Gateway Cognito User Pool Authorizer accepts it and
  rejects the access token with `401 Unauthorized`. If no user is signed in, the header is
  omitted; the view does not yet handle 401/403 responses explicitly.
- **Accepted image types/size limit**: `image/jpeg|png|webp|gif`, 15MB per file — not specified
  anywhere in `docs/`, chosen as a reasonable default; adjust in `src/validation.js` if the
  product has different requirements.
- **S3 bucket CORS (separate from the API Gateway CORS above)**: once the presigned-URL
  request succeeds, the browser `PUT`s each file directly to S3
  (`miniature-ai-guide-uploads-dev` as of this writing). This is a *second*, independent CORS
  hop: the S3 bucket itself must have a CORS configuration allowing the frontend's origin,
  or the browser blocks the request and `src/uploadClient.js` surfaces it as "Network error
  while uploading to S3." — this cannot be fixed from frontend code; it requires an
  `S3 PutBucketCors` configuration on the upload bucket, e.g.:

  ```json
  [
    {
      "AllowedOrigins": ["http://localhost:5173", "https://<deployed-frontend-domain>"],
      "AllowedMethods": ["PUT"],
      "AllowedHeaders": ["Content-Type"],
      "ExposeHeaders": ["ETag"],
      "MaxAgeSeconds": 3000
    }
  ]
  ```

  Also note that the `Content-Type` header sent with the `PUT` must exactly match the
  `ContentType` the presigned URL was generated with (`backend/lambda_upload` signs it into
  the URL) — `src/uploadView.js`/`src/uploadClient.js` already keep these in sync by using
  `file.type` consistently for both the URL request and the upload, so this should not need
  further changes on the frontend side.

## Authentication (Cognito Hosted UI via AWS Amplify)

`src/auth.js` configures [AWS Amplify](https://docs.amplify.aws/) v6's `Auth` category against the
already-provisioned Cognito User Pool/App Client/Hosted UI domain, using the Authorization Code +
PKCE OAuth flow with `openid`/`email` scopes. `src/authView.js` wires a Login/Logout button pair in the page header
(`index.html`) to it, and `main.js` mounts that view on load.

- `login()` — redirects the browser to the Cognito Hosted UI.
- `logout()` — ends the Cognito session and redirects back to the app.
- `checkCurrentUser()` — looks up the current session (if any) and returns the username, email,
  tokens, and expiry, or `null` if signed out. In dev builds it logs only non-sensitive facts
  (username, `sub`, email-verified flag, token expiry). Tokens and the email address are never
  logged. Email and other identity claims (`sub`, `email_verified`) are read directly from the
  ID token's payload — this deliberately avoids `fetchUserAttributes()`, which calls Cognito's
  `GetUser` API and fails with `NotAuthorizedException` because the access token here doesn't
  carry the required scopes.
- `getIdToken()` — returns the current ID token (or `null`) for `src/api.js`.
- `getSessionTokens()` — returns `{ idToken, accessToken }` (each a string or `null`) from a
  single `fetchAuthSession()` call, for `src/websocketClient.js` only. Reading both tokens from
  one session keeps the pair consistent across a token refresh, so the ID token's `at_hash`
  always matches the access token.

Tokens are attached to requests as follows: the ID token goes in `Authorization: Bearer` on
REST calls (`src/api.js`) and in the `token` query parameter of the status WebSocket
(`src/websocketClient.js`). The access token is never sent to the REST API; it goes only in the
`accessToken` query parameter of the status WebSocket.

The status WebSocket URL is `<VITE_WS_BASE_URL>?jobId=<jobId>&token=<idToken>&accessToken=<accessToken>`
(each value URL-encoded). The `$connect` Lambda checks the ID token's `at_hash` claim against
`accessToken`; Cognito adds `at_hash` to ID tokens issued by the Hosted UI code flow, so without
`accessToken` the connection is rejected with 401 (contract: `backend/open_connection/README.md`).
If either token is missing, `openGenerationWebSocket()` throws `WebSocketError` ("User must be
signed in") and does not open a connection. Both tokens sit in the query string, so they can
appear in API Gateway access logs, as the ID token already could; the URL is never logged by
the frontend.

The whole app requires authentication: on load, `authView.js` calls `checkCurrentUser()` and,
if it resolves to `null` (not signed in), immediately calls `login()` to redirect to the Hosted
UI — there is no anonymous landing page. The **Login** button in the header remains visible as a
fallback/manual-retry control (e.g. if the auto-redirect fails), and **Logout** appears once
signed in. To avoid a redirect loop right after Cognito redirects back with `?code=...&state=...`
in the URL, `authView.js` checks for that OAuth-callback URL shape
(`isOAuthRedirectCallback()` in `auth.js`) and, if present, waits for Amplify's `signInWithRedirect`
Hub event (`onAuthEvent()` in `auth.js`) to confirm the code exchange finished before deciding
the user is "not signed in" and would otherwise auto-redirect again.

### How to test login locally

1. `npm run dev` and open `http://localhost:5173`.
2. Since no session exists yet, the app should auto-redirect you to the Cognito Hosted UI
   sign-in page (the header briefly shows "Redirecting to sign-in…" before navigating away). If
   for some reason it doesn't, click **Login** in the header manually.
3. Sign in (or sign up) with a Cognito user in pool `eu-central-1_8lJdTr0tx`.
4. Cognito redirects back to `http://localhost:5173`; the header should switch to "Signed in as
   `<email>`" with a **Logout** button (and should *not* redirect you back to the Hosted UI).
5. In the dev-server console, check the username, `sub`, email-verified flag, and token expiry
   logged by `checkCurrentUser()`, confirming the token exchange succeeded. Tokens are not logged.
6. Click **Logout** to end the session and confirm the header returns to "Not signed in." with a
   **Login** button.

The same flow works against the deployed CloudFront origin (`https://dv0hbhbju2e6x.cloudfront.net`)
since both origins are already registered as Hosted UI callback/sign-out URLs.

## Backend integration

This frontend is a static site with no server-side runtime. It talks to the backend
exclusively through documented API Gateway endpoints secured by Cognito:

- **Upload flow**: request presigned S3 `PUT` URLs, then upload the reference image
  directly to S3. Implemented in `src/uploadView.js` (see above).
- **Generation flow**: create the job record (`PUT /jobs`), open a status WebSocket
  (`openGenerationWebSocket`, query `jobId`, `token` and `accessToken`; see Authentication
  above), and trigger guide generation
  (`POST /jobs/<jobId>/instruction`, JSON body `{ "instruction": "<wishes text>" }`). All three
  are chained behind the single **✨ Generate New Vision** button in `src/uploadView.js` (see above).
- **Result rendering**: the backend pushes `{ jobId, status, resultUrl, imageUrl, message? }` over the
  WebSocket (validated by `parseResultMessage` in `src/websocketClient.js`). For `COMPLETED`,
  `uploadView.js` downloads the PNG (`fetchResultImage`) and the paint list
  (`fetchResultColors`, `{ colors: [{ detail, paint, quantity?, vallejo_color? }] }`) from the presigned S3 URLs via
  `src/api.js`, shows the image in the result pane and the paints as a table of color
  swatches with detail names in the separate palette frame, then closes the WebSocket. The
  result JSON has one entry per Vallejo paint: `detail` is a comma-joined list of the details
  sharing that paint, and `quantity` is how many details share it. `fetchResultColors` returns
  `quantity` only when it is a positive integer; when it is absent (older results) or invalid
  the field is left off and no badge is shown. The palette shows a `×N` badge next to the
  detail list when `quantity > 1`. Each swatch uses the nearest Vallejo paint hex, with its
  reference and name below, when `vallejo_color` is present. A non-`COMPLETED` status (for example `FAILED`) shows `Generation failed: <message>` in the
  message section as a red error (every such status, not only `FAILED`), using the backend's
  optional `message` (a generic text when it is absent), and closes the WebSocket. A failed
  download shows a red error message in the same section and also closes the WebSocket. In
  both cases the result pane shows the placeholder.
  The presigned URLs are fetched cross-origin, so the paint S3 bucket needs a CORS rule
  allowing `GET` from the site origin.

No AWS credentials or secrets are ever stored in this frontend; the only credential the
client holds is a Cognito session token obtained through the auth flow.

## Deployment

`.github/workflows/deploy-frontend.yml` deploys on every push to `main` that touches
`frontend/**` (and on manual `workflow_dispatch`). Jobs run in order:

1. `test` — `npm ci` and `npm test`. A failure blocks the build and deploy.
2. `build` — `npm ci` and `npm run build`, with `VITE_API_BASE_URL` and `VITE_WS_BASE_URL`
   taken from the GitHub repository variables of the same name.
3. `deploy` — assumes the AWS OIDC deploy role, runs `aws s3 sync` of `dist/` to
   `FRONTEND_S3_BUCKET` (with `--delete`), and invalidates CloudFront when
   `CLOUDFRONT_DISTRIBUTION_ID` is set. Only this job requests the `id-token: write` permission.

The workflow header comments list the required GitHub variables and the one-time AWS setup
(bucket, OIDC provider, deploy role).

Manual alternative: run `npm run build` and sync the contents of `dist/` to the target S3 bucket
configured for static website hosting.

CORS must be configured on the S3 bucket to allow the frontend's origin to fetch the presigned
S3 URLs for the result image and JSON.
```json

[
  {
    "AllowedOrigins": [
      "https://dxxxxxxxxxxxx.cloudfront.net",
      "http://localhost:5173"
    ],
    "AllowedMethods": [
      "GET"
    ],
    "AllowedHeaders": [
      "*"
    ],
    "ExposeHeaders": [
      "Content-Type",
      "Content-Length"
    ],
    "MaxAgeSeconds": 3000
  }
]

```
