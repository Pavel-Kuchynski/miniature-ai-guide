// Cognito authentication via AWS Amplify (Hosted UI, Authorization Code +
// PKCE flow). Isolated here so the rest of the app never touches the
// `aws-amplify` package directly, mirroring how api.js isolates `fetch`.
//
// Scope of this module (see .tasks/cognito-amplify-setup-task.md):
//   - Configure Amplify Auth against the existing Cognito User Pool/App
//     Client/Hosted UI domain (no Cognito-side settings are created or
//     changed here).
//   - login()/logout() drive the Hosted UI redirect flow.
//   - checkCurrentUser() reports the signed-in user + token info to the
//     console for manual verification.
//
// Not in scope yet: attaching tokens to API requests (see api.js), token
// refresh handling beyond what Amplify does automatically, and any
// backend/API Gateway changes.

import { Amplify } from "aws-amplify";
import {
  fetchAuthSession,
  fetchUserAttributes,
  getCurrentUser,
  signInWithRedirect,
  signOut,
} from "aws-amplify/auth";
import { Hub } from "aws-amplify/utils";

// Cognito User Pool / Hosted UI configuration. These values identify an
// already-provisioned Cognito User Pool and App Client (see
// .tasks/cognito-amplify-setup-task.md) — nothing here creates or modifies
// AWS resources.
const COGNITO_USER_POOL_ID = "eu-central-1_8lJdTr0tx";
const COGNITO_USER_POOL_CLIENT_ID = "6rjlbll5nu6enatl6t380bm25s";
const COGNITO_HOSTED_UI_DOMAIN =
  "eu-central-18ljdtr0tx.auth.eu-central-1.amazoncognito.com";

// Must exactly match the Hosted UI app client's configured callback/sign-out
// URLs (local dev server + deployed CloudFront distribution).
const REDIRECT_URLS = [
  "http://localhost:5173",
  "https://dv0hbhbju2e6x.cloudfront.net",
];

let configured = false;

/**
 * Configure Amplify Auth for Cognito Hosted UI login (Authorization Code +
 * PKCE, scopes `openid`/`email`). Safe to call multiple times; only
 * configures once.
 */
export function configureAuth() {
  if (configured) {
    return;
  }

  Amplify.configure({
    Auth: {
      Cognito: {
        userPoolId: COGNITO_USER_POOL_ID,
        userPoolClientId: COGNITO_USER_POOL_CLIENT_ID,
        loginWith: {
          oauth: {
            domain: COGNITO_HOSTED_UI_DOMAIN,
            scopes: ["openid", "email"],
            redirectSignIn: REDIRECT_URLS,
            redirectSignOut: REDIRECT_URLS,
            responseType: "code",
          },
        },
      },
    },
  });

  configured = true;
}

/**
 * Redirect the browser to the Cognito Hosted UI to sign in. On success,
 * Cognito redirects back to one of REDIRECT_URLS and Amplify exchanges the
 * authorization code for tokens automatically.
 */
export function login() {
  return signInWithRedirect();
}

/**
 * True if the current URL looks like a Cognito Hosted UI OAuth redirect
 * callback (i.e. it carries the `code`/`state` query params from the
 * Authorization Code + PKCE flow) rather than a plain page load.
 *
 * Callers should use this to avoid deciding "not signed in" (and
 * auto-redirecting back to the Hosted UI) before Amplify has had a chance to
 * process the callback and exchange the code for tokens — otherwise a
 * failed/slow exchange could otherwise look identical to "never logged in"
 * and cause a redirect loop.
 *
 * @param {string} [search] Defaults to `window.location.search`.
 */
export function isOAuthRedirectCallback(search = window.location.search) {
  const params = new URLSearchParams(search);
  return params.has("code") && params.has("state");
}

/**
 * Subscribe to Amplify's `auth` Hub channel (e.g. `signInWithRedirect`,
 * `signInWithRedirect_failure`, `signedOut`), used to know when the Hosted UI
 * redirect callback has finished processing.
 *
 * @param {(payload: { event: string, data?: unknown }) => void} callback
 * @returns {() => void} Unsubscribe function.
 */
export function onAuthEvent(callback) {
  return Hub.listen("auth", ({ payload }) => callback(payload));
}

/**
 * End the Cognito session and redirect the browser to the configured
 * sign-out URL.
 */
export function logout() {
  return signOut();
}

/**
 * Look up the currently authenticated user (if any) and log token
 * information (access token, ID token, expiration, email) to the console
 * for manual verification. Does not send tokens anywhere.
 *
 * @returns {Promise<{ username: string, email: string | undefined, accessToken: string, idToken: string, expiresAt: number | undefined } | null>}
 *   Resolves to `null` if no user is currently signed in.
 */
export async function checkCurrentUser() {
  let user;
  try {
    user = await getCurrentUser();
  } catch {
    console.info("[auth] No authenticated user.");
    return null;
  }

  const session = await fetchAuthSession();
  const accessToken = session.tokens?.accessToken?.toString();
  const idToken = session.tokens?.idToken?.toString();
  const expiresAt = session.tokens?.accessToken?.payload?.exp;

  let email;
  try {
    ({ email } = await fetchUserAttributes());
  } catch (error) {
    console.warn("[auth] Could not read user attributes.", error);
  }

  if (!accessToken) {
    console.warn("[auth] Signed in but no access token was found.");
  }
  if (!idToken) {
    console.warn("[auth] Signed in but no ID token was found.");
  }
  if (!expiresAt) {
    console.warn("[auth] Signed in but no token expiration was found.");
  }

  console.info("[auth] Current user:", user.username);
  console.info("[auth] Email:", email ?? "(not available)");
  console.info("[auth] Access token:", accessToken ?? "(missing)");
  console.info("[auth] ID token:", idToken ?? "(missing)");
  console.info(
    "[auth] Token expires at:",
    expiresAt ? new Date(expiresAt * 1000).toISOString() : "(missing)",
  );

  return { username: user.username, email, accessToken, idToken, expiresAt };
}
