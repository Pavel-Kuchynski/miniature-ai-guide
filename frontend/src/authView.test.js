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
    expect(container.querySelector("[data-action='login']").hidden).toBe(
      false,
    );
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
});
