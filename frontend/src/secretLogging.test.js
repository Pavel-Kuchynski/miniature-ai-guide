// Guards against credentials leaking into the browser console. Drives the real
// auth/api/websocket/upload code paths (only Amplify, fetch, XMLHttpRequest and
// WebSocket are faked), spies on every console method, and asserts that no
// logged argument contains a token, an Authorization value, a `token=` or
// `accessToken=` query parameter, or an S3 presigned-URL signature.

import { inspect } from "node:util";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

const getCurrentUserMock = vi.fn();
const fetchAuthSessionMock = vi.fn();

vi.mock("aws-amplify", () => ({
  Amplify: { configure: vi.fn() },
}));

vi.mock("aws-amplify/auth", () => ({
  signInWithRedirect: vi.fn(),
  signOut: vi.fn(),
  getCurrentUser: getCurrentUserMock,
  fetchAuthSession: fetchAuthSessionMock,
}));

vi.mock("aws-amplify/utils", () => ({
  Hub: { listen: vi.fn() },
}));

const { checkCurrentUser } = await import("./auth.js");
const { requestUploadUrls, createJob, requestInstructionGeneration, ApiError } =
  await import("./api.js");
const { openGenerationWebSocket, WebSocketError } =
  await import("./websocketClient.js");
const { putFileToUrl, UploadError } = await import("./uploadClient.js");

// Distinctive values so a leak is unambiguous in the captured output.
const ID_TOKEN = "eyJraWQiOi.ID-TOKEN-SECRET-VALUE.sig";
const ACCESS_TOKEN = "eyJraWQiOi.ACCESS-TOKEN-SECRET-VALUE.sig";
const PRESIGNED_SIGNATURE = "PRESIGNED-SIGNATURE-SECRET-VALUE";
const EMAIL = "person.private@example.com";
const API_BASE_URL = "https://api.example.com/miniature-ai-guide";
const WS_BASE_URL = "wss://ws.example.com/miniature-ai-guide";
const PRESIGNED_PUT_URL =
  "https://bucket.s3.example.com/uploads/uuid-1/file.jpg" +
  `?X-Amz-Credential=AKIA-EXAMPLE&X-Amz-Signature=${PRESIGNED_SIGNATURE}`;

// Fragments that must never appear in any console argument.
const FORBIDDEN_LOG_FRAGMENTS = [
  ID_TOKEN,
  ACCESS_TOKEN,
  PRESIGNED_SIGNATURE,
  EMAIL,
  "Bearer ",
  "token=",
  "accessToken=",
  "X-Amz-Signature",
  "Authorization",
];

const CONSOLE_METHODS = ["log", "info", "warn", "error", "debug"];
let consoleSpies = [];

function signedInSession() {
  return {
    tokens: {
      accessToken: {
        toString: () => ACCESS_TOKEN,
        payload: { exp: 1893456000 },
      },
      idToken: {
        toString: () => ID_TOKEN,
        payload: { sub: "sub-123", email: EMAIL, email_verified: true },
      },
    },
  };
}

function capturedConsoleOutput() {
  return consoleSpies
    .flatMap((spy) => spy.mock.calls)
    .map((args) =>
      inspect(args, { depth: null, getters: true, showHidden: true }),
    )
    .join("\n");
}

function expectSomethingLogged() {
  // Guards against a vacuous pass: the scenario must actually have logged.
  expect(consoleSpies.some((spy) => spy.mock.calls.length > 0)).toBe(true);
}

function expectNoSecretsLogged() {
  const output = capturedConsoleOutput();
  for (const fragment of FORBIDDEN_LOG_FRAGMENTS) {
    expect(output, `console output leaked "${fragment}"`).not.toContain(
      fragment,
    );
  }
}

function jsonResponse(body, { ok = true, status = 200 } = {}) {
  return { ok, status, json: async () => body };
}

beforeEach(() => {
  vi.clearAllMocks();
  fetchAuthSessionMock.mockResolvedValue(signedInSession());
  consoleSpies = CONSOLE_METHODS.map((method) =>
    vi.spyOn(console, method).mockImplementation(() => {}),
  );
});

afterEach(() => {
  for (const spy of consoleSpies) {
    spy.mockRestore();
  }
  vi.unstubAllGlobals();
});

describe("console output never contains credentials", () => {
  it("checkCurrentUser logs identity facts but not tokens or the email address", async () => {
    getCurrentUserMock.mockResolvedValue({ username: "user-123" });

    await checkCurrentUser();

    expectSomethingLogged();
    expectNoSecretsLogged();
    expect(capturedConsoleOutput()).toContain("sub-123");
  });

  it("requestUploadUrls does not log the Authorization header or presigned URLs", async () => {
    const successFetch = vi.fn().mockResolvedValue(
      jsonResponse({
        uploadItems: [
          { uploadUrl: PRESIGNED_PUT_URL, key: "uploads/uuid-1/file.jpg" },
        ],
      }),
    );
    await requestUploadUrls(
      { fileNames: ["file.jpg"], contentTypes: ["image/jpeg"] },
      { baseUrl: API_BASE_URL, fetchImpl: successFetch },
    );
    expectNoSecretsLogged();

    const errorFetch = vi
      .fn()
      .mockResolvedValue(
        jsonResponse({ error: "Forbidden" }, { ok: false, status: 403 }),
      );
    await expect(
      requestUploadUrls(
        { fileNames: ["file.jpg"], contentTypes: ["image/jpeg"] },
        { baseUrl: API_BASE_URL, fetchImpl: errorFetch },
      ),
    ).rejects.toBeInstanceOf(ApiError);
    expectSomethingLogged();
    expectNoSecretsLogged();

    const networkFetch = vi
      .fn()
      .mockRejectedValue(new TypeError("Failed to fetch"));
    await expect(
      requestUploadUrls(
        { fileNames: ["file.jpg"], contentTypes: ["image/jpeg"] },
        { baseUrl: API_BASE_URL, fetchImpl: networkFetch },
      ),
    ).rejects.toBeInstanceOf(ApiError);
    expectNoSecretsLogged();
  });

  it("createJob and requestInstructionGeneration do not log the Authorization header", async () => {
    const unauthorizedFetch = vi
      .fn()
      .mockResolvedValue(
        jsonResponse({ error: "Unauthorized" }, { ok: false, status: 401 }),
      );
    await expect(
      createJob(
        { jobId: "job-123" },
        { baseUrl: API_BASE_URL, fetchImpl: unauthorizedFetch },
      ),
    ).rejects.toBeInstanceOf(ApiError);
    expectSomethingLogged();
    expectNoSecretsLogged();

    const networkFetch = vi
      .fn()
      .mockRejectedValue(new TypeError("Failed to fetch"));
    await expect(
      requestInstructionGeneration(
        { jobId: "job-123", instruction: "make it gothic" },
        { baseUrl: API_BASE_URL, fetchImpl: networkFetch },
      ),
    ).rejects.toBeInstanceOf(ApiError);
    expectNoSecretsLogged();

    const okFetch = vi
      .fn()
      .mockResolvedValue(jsonResponse({}, { status: 202 }));
    await requestInstructionGeneration(
      { jobId: "job-123", instruction: "make it gothic" },
      { baseUrl: API_BASE_URL, fetchImpl: okFetch },
    );
    expectNoSecretsLogged();
  });

  it("openGenerationWebSocket does not log the token-bearing URL from an error event", async () => {
    let socket;
    const listeners = {};
    class FakeWebSocket {
      constructor(url) {
        this.url = url;
        socket = this;
      }
      addEventListener(type, handler) {
        listeners[type] = handler;
      }
      close() {}
    }

    const promise = openGenerationWebSocket(
      { jobId: "job-123" },
      { baseUrl: WS_BASE_URL, WebSocketImpl: FakeWebSocket },
    );
    await vi.waitFor(() => expect(listeners.error).toBeDefined());

    // Browsers hand over an Event whose target is the WebSocket, and that
    // target's `url` contains `token=<idToken>` and `accessToken=<accessToken>`.
    expect(socket.url).toContain(ID_TOKEN);
    expect(socket.url).toContain(encodeURIComponent(ACCESS_TOKEN));
    listeners.error({ type: "error", target: socket });

    await expect(promise).rejects.toBeInstanceOf(WebSocketError);
    expectSomethingLogged();
    expectNoSecretsLogged();
  });

  it("openGenerationWebSocket does not log the URL when the constructor throws", async () => {
    class ThrowingWebSocket {
      constructor(url) {
        // Browsers echo the full URL into this message.
        throw new SyntaxError(
          `Failed to construct 'WebSocket': The URL '${url}' is invalid.`,
        );
      }
    }

    await expect(
      openGenerationWebSocket(
        { jobId: "job-123" },
        { baseUrl: WS_BASE_URL, WebSocketImpl: ThrowingWebSocket },
      ),
    ).rejects.toBeInstanceOf(WebSocketError);
    expectSomethingLogged();
    expectNoSecretsLogged();
  });

  it("putFileToUrl logs only the origin and path of the presigned S3 URL", async () => {
    class FailingXhr {
      constructor() {
        this.upload = {};
      }
      open() {}
      setRequestHeader() {}
      abort() {}
      send() {
        this.onerror?.();
      }
    }
    vi.stubGlobal("XMLHttpRequest", FailingXhr);

    const file = new Blob(["image-bytes"], { type: "image/jpeg" });
    await expect(putFileToUrl(PRESIGNED_PUT_URL, file)).rejects.toBeInstanceOf(
      UploadError,
    );

    expectSomethingLogged();
    expectNoSecretsLogged();
    expect(capturedConsoleOutput()).toContain(
      "https://bucket.s3.example.com/uploads/uuid-1/file.jpg",
    );
  });
});
