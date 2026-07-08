// Minimal auth UI: wires the header's Login/Logout buttons to auth.js and
// reflects the current sign-in status. The whole app requires
// authentication, so if the user isn't signed in we auto-redirect to the
// Cognito Hosted UI on load; the Login button remains as a visible
// fallback/manual retry control. No routing/guarding of individual views is
// implemented beyond this — see .tasks/cognito-amplify-setup-task.md.

import {
  checkCurrentUser,
  configureAuth,
  isOAuthRedirectCallback,
  login,
  logout,
  onAuthEvent,
} from "./auth.js";

/**
 * Mount the auth bar into the given container (expects the
 * data-role="auth-bar"/"auth-status" and data-action="login"/"logout"
 * elements to already exist in the DOM, per index.html).
 * @param {HTMLElement} container
 */
export function mountAuthView(container) {
  const statusEl = container.querySelector("[data-role='auth-status']");
  const loginBtn = container.querySelector("[data-action='login']");
  const logoutBtn = container.querySelector("[data-action='logout']");

  if (!statusEl || !loginBtn || !logoutBtn) {
    return;
  }

  configureAuth();

  loginBtn.addEventListener("click", () => {
    login().catch((error) => {
      console.error("[authView] login() failed.", error);
    });
  });

  logoutBtn.addEventListener("click", () => {
    logout().catch((error) => {
      console.error("[authView] logout() failed.", error);
    });
  });

  // While Amplify is still exchanging an authorization code for tokens
  // (i.e. we just landed back from the Hosted UI with ?code=...&state=...
  // in the URL), a `checkCurrentUser()` call can race the exchange and
  // transiently report "not signed in". Wait for the `signInWithRedirect`
  // Hub event in that case instead of auto-redirecting again, which would
  // otherwise risk a redirect loop.
  const awaitingRedirectCallback = isOAuthRedirectCallback();

  const unsubscribe = onAuthEvent((payload) => {
    if (payload.event === "signInWithRedirect") {
      refreshStatus({ autoLoginIfSignedOut: false });
    } else if (payload.event === "signInWithRedirect_failure") {
      console.error(
        "[authView] Cognito Hosted UI redirect sign-in failed.",
        payload.data,
      );
      refreshStatus({ autoLoginIfSignedOut: true });
    } else if (payload.event === "signedOut") {
      refreshStatus({ autoLoginIfSignedOut: false });
    }
  });
  window.addEventListener("beforeunload", unsubscribe, { once: true });

  refreshStatus({ autoLoginIfSignedOut: !awaitingRedirectCallback });

  async function refreshStatus({ autoLoginIfSignedOut }) {
    statusEl.textContent = "Checking sign-in status…";
    loginBtn.hidden = true;
    logoutBtn.hidden = true;

    const user = await checkCurrentUser().catch((error) => {
      console.error("[authView] checkCurrentUser() failed.", error);
      return null;
    });

    if (user) {
      statusEl.textContent = `Signed in as ${user.email ?? user.username}`;
      logoutBtn.hidden = false;
      return;
    }

    statusEl.textContent = "Not signed in.";
    loginBtn.hidden = false;

    if (autoLoginIfSignedOut) {
      statusEl.textContent = "Redirecting to sign-in…";
      login().catch((error) => {
        console.error(
          "[authView] Auto-redirect to Hosted UI login failed.",
          error,
        );
        statusEl.textContent = "Not signed in.";
      });
    }
  }
}
