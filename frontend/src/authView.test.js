import { describe, it, expect, vi, beforeEach } from "vitest";

const configureAuthMock = vi.fn();
const loginMock = vi.fn().mockResolvedValue(undefined);
const logoutMock = vi.fn().mockResolvedValue(undefined);
const checkCurrentUserMock = vi.fn();
const isOAuthRedirectCallbackMock = vi.fn();
const onAuthEventMock = vi.fn().mockReturnValue(() => {});

vi.mock("./auth.js", () => ({
  configureAuth: configureAuthMock,
  login: loginMock,
  logout: logoutMock,
  checkCurrentUser: checkCurrentUserMock,
  isOAuthRedirectCallback: isOAuthRedirectCallbackMock,
  onAuthEvent: onAuthEventMock,
}));

const { mountAuthView } = await import("./authView.js");

function buildAuthBar() {
  const container = document.createElement("div");
  container.innerHTML = `
    <span data-role="auth-status"></span>
    <button type="button" data-action="login" hidden>Login</button>
    <button type="button" data-action="logout" hidden>Logout</button>
  `;
  document.body.appendChild(container);
  return container;
}

async function flushMicrotasks() {
  await Promise.resolve();
  await Promise.resolve();
}

beforeEach(() => {
  vi.clearAllMocks();
  loginMock.mockResolvedValue(undefined);
  logoutMock.mockResolvedValue(undefined);
  onAuthEventMock.mockReturnValue(() => {});
  document.body.innerHTML = "";
});

describe("mountAuthView", () => {
  it("auto-redirects to Hosted UI login when not signed in on a plain page load", async () => {
    isOAuthRedirectCallbackMock.mockReturnValue(false);
    checkCurrentUserMock.mockResolvedValue(null);
    const container = buildAuthBar();

    mountAuthView(container);
    await flushMicrotasks();

    expect(loginMock).toHaveBeenCalledTimes(1);
  });

  it("does not auto-redirect when the user is already signed in", async () => {
    isOAuthRedirectCallbackMock.mockReturnValue(false);
    checkCurrentUserMock.mockResolvedValue({
      username: "user-123",
      email: "user@example.com",
    });
    const container = buildAuthBar();

    mountAuthView(container);
    await flushMicrotasks();

    expect(loginMock).not.toHaveBeenCalled();
    expect(
      container.querySelector("[data-role='auth-status']").textContent,
    ).toContain("user@example.com");
    expect(container.querySelector("[data-action='logout']").hidden).toBe(
      false,
    );
  });

  it("does not auto-redirect while an OAuth redirect callback is still being processed", async () => {
    isOAuthRedirectCallbackMock.mockReturnValue(true);
    checkCurrentUserMock.mockResolvedValue(null);
    const container = buildAuthBar();

    mountAuthView(container);
    await flushMicrotasks();

    expect(loginMock).not.toHaveBeenCalled();
    expect(container.querySelector("[data-action='login']").hidden).toBe(false);
  });

  it("re-checks and shows the signed-in user once the signInWithRedirect Hub event fires", async () => {
    isOAuthRedirectCallbackMock.mockReturnValue(true);
    checkCurrentUserMock.mockResolvedValueOnce(null);
    let capturedListener;
    onAuthEventMock.mockImplementation((cb) => {
      capturedListener = cb;
      return () => {};
    });
    const container = buildAuthBar();

    mountAuthView(container);
    await flushMicrotasks();
    expect(loginMock).not.toHaveBeenCalled();

    checkCurrentUserMock.mockResolvedValueOnce({
      username: "user-123",
      email: "user@example.com",
    });
    capturedListener({ event: "signInWithRedirect" });
    await flushMicrotasks();

    expect(loginMock).not.toHaveBeenCalled();
    expect(
      container.querySelector("[data-role='auth-status']").textContent,
    ).toContain("user@example.com");
  });

  it("clicking Login calls login() directly as a manual fallback", async () => {
    isOAuthRedirectCallbackMock.mockReturnValue(false);
    checkCurrentUserMock.mockResolvedValue({
      username: "user-123",
      email: "user@example.com",
    });
    const container = buildAuthBar();

    mountAuthView(container);
    await flushMicrotasks();
    loginMock.mockClear();

    container.querySelector("[data-action='login']").click();

    expect(loginMock).toHaveBeenCalledTimes(1);
  });

  it("bails out early if required DOM elements are missing", () => {
    const container = document.createElement("div");
    document.body.appendChild(container);

    mountAuthView(container);

    expect(configureAuthMock).not.toHaveBeenCalled();
  });

  it("logs error when login button click fails", async () => {
    const consoleErrorSpy = vi.spyOn(console, "error").mockImplementation(() => {});
    const loginError = new Error("Login failed");
    isOAuthRedirectCallbackMock.mockReturnValue(false);
    checkCurrentUserMock.mockResolvedValue({
      username: "user-123",
      email: "user@example.com",
    });
    loginMock.mockRejectedValueOnce(loginError);
    const container = buildAuthBar();

    mountAuthView(container);
    await flushMicrotasks();
    loginMock.mockClear();

    container.querySelector("[data-action='login']").click();
    await flushMicrotasks();

    expect(consoleErrorSpy).toHaveBeenCalledWith(
      "[authView] login() failed.",
      loginError,
    );
    consoleErrorSpy.mockRestore();
  });

  it("logs error when logout button click fails", async () => {
    const consoleErrorSpy = vi.spyOn(console, "error").mockImplementation(() => {});
    const logoutError = new Error("Logout failed");
    isOAuthRedirectCallbackMock.mockReturnValue(false);
    checkCurrentUserMock.mockResolvedValue({
      username: "user-123",
      email: "user@example.com",
    });
    logoutMock.mockRejectedValueOnce(logoutError);
    const container = buildAuthBar();

    mountAuthView(container);
    await flushMicrotasks();
    logoutMock.mockClear();

    container.querySelector("[data-action='logout']").click();
    await flushMicrotasks();

    expect(consoleErrorSpy).toHaveBeenCalledWith(
      "[authView] logout() failed.",
      logoutError,
    );
    consoleErrorSpy.mockRestore();
  });

  it("handles signInWithRedirect_failure event by refreshing status and enabling auto-login", async () => {
    const consoleErrorSpy = vi.spyOn(console, "error").mockImplementation(() => {});
    isOAuthRedirectCallbackMock.mockReturnValue(true);
    checkCurrentUserMock.mockResolvedValue(null);
    let capturedListener;
    onAuthEventMock.mockImplementation((cb) => {
      capturedListener = cb;
      return () => {};
    });
    const container = buildAuthBar();

    mountAuthView(container);
    await flushMicrotasks();
    loginMock.mockClear();

    capturedListener({
      event: "signInWithRedirect_failure",
      data: { message: "Authorization failed" },
    });
    await flushMicrotasks();

    expect(consoleErrorSpy).toHaveBeenCalledWith(
      "[authView] Cognito Hosted UI redirect sign-in failed.",
      { message: "Authorization failed" },
    );
    expect(loginMock).toHaveBeenCalledTimes(1);
    consoleErrorSpy.mockRestore();
  });

  it("handles signedOut event by refreshing status without auto-login", async () => {
    isOAuthRedirectCallbackMock.mockReturnValue(false);
    checkCurrentUserMock.mockResolvedValueOnce({
      username: "user-123",
      email: "user@example.com",
    });
    let capturedListener;
    onAuthEventMock.mockImplementation((cb) => {
      capturedListener = cb;
      return () => {};
    });
    const container = buildAuthBar();

    mountAuthView(container);
    await flushMicrotasks();
    expect(container.querySelector("[data-role='auth-status']").textContent).toContain(
      "user@example.com",
    );

    checkCurrentUserMock.mockResolvedValueOnce(null);
    loginMock.mockClear();
    capturedListener({ event: "signedOut" });
    await flushMicrotasks();

    expect(container.querySelector("[data-role='auth-status']").textContent).toBe(
      "Not signed in.",
    );
    expect(loginMock).not.toHaveBeenCalled();
    expect(container.querySelector("[data-action='login']").hidden).toBe(false);
  });

  it("logs error when checkCurrentUser fails and shows login button", async () => {
    const consoleErrorSpy = vi.spyOn(console, "error").mockImplementation(() => {});
    const checkError = new Error("Check user failed");
    isOAuthRedirectCallbackMock.mockReturnValue(false);
    checkCurrentUserMock.mockRejectedValueOnce(checkError);
    loginMock.mockRejectedValueOnce(new Error("Login also failed"));
    const container = buildAuthBar();

    mountAuthView(container);
    await flushMicrotasks();

    expect(consoleErrorSpy).toHaveBeenCalledWith(
      "[authView] checkCurrentUser() failed.",
      checkError,
    );
    expect(container.querySelector("[data-role='auth-status']").textContent).toBe(
      "Not signed in.",
    );
    expect(container.querySelector("[data-action='login']").hidden).toBe(false);
    consoleErrorSpy.mockRestore();
  });

  it("handles auto-login failure by showing error status", async () => {
    const consoleErrorSpy = vi.spyOn(console, "error").mockImplementation(() => {});
    const autoLoginError = new Error("Auto-login failed");
    isOAuthRedirectCallbackMock.mockReturnValue(false);
    checkCurrentUserMock.mockResolvedValue(null);
    loginMock.mockRejectedValueOnce(autoLoginError);
    const container = buildAuthBar();

    mountAuthView(container);
    await flushMicrotasks();

    expect(consoleErrorSpy).toHaveBeenCalledWith(
      "[authView] Auto-redirect to Hosted UI login failed.",
      autoLoginError,
    );
    expect(container.querySelector("[data-role='auth-status']").textContent).toBe(
      "Not signed in.",
    );
    consoleErrorSpy.mockRestore();
  });

  it("displays username when user lacks email", async () => {
    isOAuthRedirectCallbackMock.mockReturnValue(false);
    checkCurrentUserMock.mockResolvedValue({
      username: "user-123",
    });
    const container = buildAuthBar();

    mountAuthView(container);
    await flushMicrotasks();

    expect(
      container.querySelector("[data-role='auth-status']").textContent,
    ).toContain("user-123");
    expect(container.querySelector("[data-action='logout']").hidden).toBe(false);
  });
});
