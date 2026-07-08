import { describe, it, expect, vi, beforeEach } from "vitest";

const configureMock = vi.fn();
const signInWithRedirectMock = vi.fn();
const signOutMock = vi.fn();
const getCurrentUserMock = vi.fn();
const fetchAuthSessionMock = vi.fn();
const hubListenMock = vi.fn();

vi.mock("aws-amplify", () => ({
  Amplify: { configure: configureMock },
}));

vi.mock("aws-amplify/auth", () => ({
  signInWithRedirect: signInWithRedirectMock,
  signOut: signOutMock,
  getCurrentUser: getCurrentUserMock,
  fetchAuthSession: fetchAuthSessionMock,
}));

vi.mock("aws-amplify/utils", () => ({
  Hub: { listen: hubListenMock },
}));

const {
  configureAuth,
  login,
  logout,
  checkCurrentUser,
  isOAuthRedirectCallback,
  onAuthEvent,
} = await import("./auth.js");

beforeEach(() => {
  vi.clearAllMocks();
});

describe("configureAuth", () => {
  it("configures Amplify with the Cognito Hosted UI (Authorization Code + PKCE) settings", () => {
    configureAuth();

    expect(configureMock).toHaveBeenCalledWith(
      expect.objectContaining({
        Auth: expect.objectContaining({
          Cognito: expect.objectContaining({
            userPoolId: "eu-central-1_8lJdTr0tx",
            userPoolClientId: "6rjlbll5nu6enatl6t380bm25s",
            loginWith: expect.objectContaining({
              oauth: expect.objectContaining({
                domain: "eu-central-18ljdtr0tx.auth.eu-central-1.amazoncognito.com",
                scopes: ["openid", "email"],
                redirectSignIn: [
                  "http://localhost:5173",
                  "https://dv0hbhbju2e6x.cloudfront.net",
                ],
                redirectSignOut: [
                  "http://localhost:5173",
                  "https://dv0hbhbju2e6x.cloudfront.net",
                ],
                responseType: "code",
              }),
            }),
          }),
        }),
      }),
    );
  });
});

describe("login/logout", () => {
  it("login() delegates to signInWithRedirect", () => {
    login();
    expect(signInWithRedirectMock).toHaveBeenCalled();
  });

  it("logout() delegates to signOut", () => {
    logout();
    expect(signOutMock).toHaveBeenCalled();
  });
});

describe("checkCurrentUser", () => {
  it("returns null when no user is signed in", async () => {
    getCurrentUserMock.mockRejectedValue(new Error("not signed in"));

    const result = await checkCurrentUser();

    expect(result).toBeNull();
  });

  it("returns token info and email for a signed-in user, reading email from the ID token claims", async () => {
    getCurrentUserMock.mockResolvedValue({ username: "user-123" });
    fetchAuthSessionMock.mockResolvedValue({
      tokens: {
        accessToken: {
          toString: () => "access-token-value",
          payload: { exp: 1234567890 },
        },
        idToken: {
          toString: () => "id-token-value",
          payload: {
            sub: "sub-123",
            email: "user@example.com",
            email_verified: true,
          },
        },
      },
    });

    const result = await checkCurrentUser();

    expect(result).toEqual({
      username: "user-123",
      email: "user@example.com",
      accessToken: "access-token-value",
      idToken: "id-token-value",
      expiresAt: 1234567890,
    });
  });
});

describe("isOAuthRedirectCallback", () => {
  it("is true when the URL has both code and state params (Hosted UI callback)", () => {
    expect(isOAuthRedirectCallback("?code=abc123&state=xyz")).toBe(true);
  });

  it("is false for a plain page load with no query params", () => {
    expect(isOAuthRedirectCallback("")).toBe(false);
  });

  it("is false when only one of code/state is present", () => {
    expect(isOAuthRedirectCallback("?code=abc123")).toBe(false);
    expect(isOAuthRedirectCallback("?state=xyz")).toBe(false);
  });
});

describe("onAuthEvent", () => {
  it("subscribes to the Hub 'auth' channel and unwraps the payload", () => {
    let capturedListener;
    hubListenMock.mockImplementation((channel, listener) => {
      capturedListener = listener;
      return () => {};
    });

    const callback = vi.fn();
    onAuthEvent(callback);

    expect(hubListenMock).toHaveBeenCalledWith("auth", expect.any(Function));

    capturedListener({ payload: { event: "signInWithRedirect" } });
    expect(callback).toHaveBeenCalledWith({ event: "signInWithRedirect" });
  });
});
