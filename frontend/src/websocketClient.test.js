import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

const getIdTokenMock = vi.fn();

vi.mock("./auth.js", () => ({
  getIdToken: getIdTokenMock,
}));

const {
  openGenerationWebSocket,
  getWsBaseUrl,
  WebSocketError,
} = await import("./websocketClient.js");

describe("getWsBaseUrl", () => {
  it("returns the configured VITE_WS_BASE_URL", () => {
    const url = getWsBaseUrl();
    expect(typeof url).toBe("string");
  });

  it("removes trailing slashes from the configured URL", () => {
    // We can't directly modify import.meta.env in tests, so we test the behavior
    // by checking that the function returns a string without trailing slashes
    const url = getWsBaseUrl();
    expect(url).not.toMatch(/\/+$/);
  });
});

describe("openGenerationWebSocket", () => {
  beforeEach(() => {
    getIdTokenMock.mockReset();
    getIdTokenMock.mockResolvedValue("jwt-token-value");
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  it("opens a WebSocket connection with correct URL including jobId and token as query parameters", async () => {
    let capturedUrl = null;
    let openCallback = null;

    const mockWebSocket = {
      addEventListener: vi.fn((event, handler) => {
        if (event === "open") {
          openCallback = handler;
        }
      }),
      close: vi.fn(),
    };

    class WebSocketMock {
      constructor(url) {
        capturedUrl = url;
      }
      addEventListener = mockWebSocket.addEventListener;
      close = mockWebSocket.close;
    }

    const promise = openGenerationWebSocket(
      { jobId: "job-123" },
      { baseUrl: "wss://example.com", WebSocketImpl: WebSocketMock },
    );

    // Allow async operations (awaiting getIdToken) to complete
    await Promise.resolve();

    expect(capturedUrl).toBe(
      "wss://example.com?jobId=job-123&token=jwt-token-value",
    );

    openCallback();
    const ws = await promise;
    expect(ws).toBeInstanceOf(WebSocketMock);
  });

  it("encodes jobId and token in URL to handle special characters", async () => {
    let capturedUrl = null;
    let openCallback = null;

    const mockWebSocket = {
      addEventListener: vi.fn((event, handler) => {
        if (event === "open") {
          openCallback = handler;
        }
      }),
      close: vi.fn(),
    };

    class WebSocketMock {
      constructor(url) {
        capturedUrl = url;
      }
      addEventListener = mockWebSocket.addEventListener;
      close = mockWebSocket.close;
    }

    getIdTokenMock.mockResolvedValue("token-with-special-chars-/+=");

    const promise = openGenerationWebSocket(
      { jobId: "job/with/slashes" },
      { baseUrl: "wss://example.com", WebSocketImpl: WebSocketMock },
    );

    await Promise.resolve();

    expect(capturedUrl).toContain("jobId=job%2Fwith%2Fslashes");
    expect(capturedUrl).toContain("token=token-with-special-chars-");

    openCallback();
    await promise;
  });

  it("resolves with the WebSocket instance when connection opens successfully", async () => {
    let openCallback = null;

    const mockWebSocket = {
      addEventListener: vi.fn((event, handler) => {
        if (event === "open") {
          openCallback = handler;
        }
      }),
      close: vi.fn(),
    };

    class WebSocketMock {
      addEventListener = mockWebSocket.addEventListener;
      close = mockWebSocket.close;
    }

    const promise = openGenerationWebSocket(
      { jobId: "job-123" },
      { baseUrl: "wss://example.com", WebSocketImpl: WebSocketMock },
    );

    await Promise.resolve();

    openCallback();
    const ws = await promise;

    expect(ws).toBeInstanceOf(WebSocketMock);
  });

  it("rejects with WebSocketError when no user is signed in", async () => {
    getIdTokenMock.mockResolvedValue(null);

    await expect(
      openGenerationWebSocket(
        { jobId: "job-123" },
        { baseUrl: "wss://example.com", WebSocketImpl: WebSocket },
      ),
    ).rejects.toBeInstanceOf(WebSocketError);

    await expect(
      openGenerationWebSocket(
        { jobId: "job-123" },
        { baseUrl: "wss://example.com", WebSocketImpl: WebSocket },
      ),
    ).rejects.toMatchObject({
      message: expect.stringContaining("signed in"),
    });
  });

  it("rejects with WebSocketError when WebSocket base URL is not configured", async () => {
    await expect(
      openGenerationWebSocket(
        { jobId: "job-123" },
        { baseUrl: "", WebSocketImpl: WebSocket },
      ),
    ).rejects.toBeInstanceOf(WebSocketError);

    await expect(
      openGenerationWebSocket(
        { jobId: "job-123" },
        { baseUrl: "", WebSocketImpl: WebSocket },
      ),
    ).rejects.toMatchObject({
      message: expect.stringContaining("not configured"),
    });
  });

  it("rejects with WebSocketError when WebSocket constructor throws", async () => {
    const WebSocketMock = vi
      .fn()
      .mockImplementation(() => {
        throw new Error("Invalid URL");
      });

    await expect(
      openGenerationWebSocket(
        { jobId: "job-123" },
        { baseUrl: "wss://example.com", WebSocketImpl: WebSocketMock },
      ),
    ).rejects.toBeInstanceOf(WebSocketError);
  });

  it("rejects with WebSocketError on connection error event", async () => {
    let errorCallback = null;

    const mockWebSocket = {
      addEventListener: vi.fn((event, handler) => {
        if (event === "error") {
          errorCallback = handler;
        }
      }),
      close: vi.fn(),
    };

    class WebSocketMock {
      addEventListener = mockWebSocket.addEventListener;
      close = mockWebSocket.close;
    }

    const promise = openGenerationWebSocket(
      { jobId: "job-123" },
      { baseUrl: "wss://example.com", WebSocketImpl: WebSocketMock },
    );

    await Promise.resolve();

    const errorEvent = new Event("error");
    errorCallback(errorEvent);

    await expect(promise).rejects.toBeInstanceOf(WebSocketError);
  });


  it("rejects with WebSocketError on close event before opening", async () => {
    let closeCallback = null;

    const mockWebSocket = {
      addEventListener: vi.fn((event, handler) => {
        if (event === "close") {
          closeCallback = handler;
        }
      }),
      close: vi.fn(),
    };

    class WebSocketMock {
      addEventListener = mockWebSocket.addEventListener;
      close = mockWebSocket.close;
    }

    const promise = openGenerationWebSocket(
      { jobId: "job-123" },
      { baseUrl: "wss://example.com", WebSocketImpl: WebSocketMock },
    );

    await Promise.resolve();

    closeCallback();

    await expect(promise).rejects.toBeInstanceOf(WebSocketError);
  });

  it("logs debug messages to console", async () => {
    const consoleDebugSpy = vi.spyOn(console, "debug").mockImplementation(() => {});
    let openCallback = null;

    const mockWebSocket = {
      addEventListener: vi.fn((event, handler) => {
        if (event === "open") {
          openCallback = handler;
        }
      }),
      close: vi.fn(),
    };

    class WebSocketMock {
      addEventListener = mockWebSocket.addEventListener;
      close = mockWebSocket.close;
    }

    const promise = openGenerationWebSocket(
      { jobId: "job-123" },
      { baseUrl: "wss://example.com", WebSocketImpl: WebSocketMock },
    );

    await Promise.resolve();

    openCallback();
    await promise;

    expect(consoleDebugSpy).toHaveBeenCalledWith(
      expect.stringContaining("[websocket]"),
      expect.any(String),
    );
    expect(consoleDebugSpy).toHaveBeenCalledWith(
      "[websocket] Connection opened successfully",
    );

    consoleDebugSpy.mockRestore();
  });

  it("logs error messages to console on connection error", async () => {
    const consoleErrorSpy = vi.spyOn(console, "error").mockImplementation(() => {});
    let errorCallback = null;

    const mockWebSocket = {
      addEventListener: vi.fn((event, handler) => {
        if (event === "error") {
          errorCallback = handler;
        }
      }),
      close: vi.fn(),
    };

    class WebSocketMock {
      addEventListener = mockWebSocket.addEventListener;
      close = mockWebSocket.close;
    }

    const promise = openGenerationWebSocket(
      { jobId: "job-123" },
      { baseUrl: "wss://example.com", WebSocketImpl: WebSocketMock },
    );

    await Promise.resolve();

    const errorEvent = new Event("error");
    errorCallback(errorEvent);

    await expect(promise).rejects.toBeInstanceOf(WebSocketError);
    expect(consoleErrorSpy).toHaveBeenCalledWith(
      "[websocket] Connection error",
      errorEvent,
    );

    consoleErrorSpy.mockRestore();
  });

  it("attaches event listeners for open, error, and close events", async () => {
    const mockWebSocket = {
      addEventListener: vi.fn(),
      close: vi.fn(),
    };

    class WebSocketMock {
      addEventListener = mockWebSocket.addEventListener;
      close = mockWebSocket.close;
    }

    openGenerationWebSocket(
      { jobId: "job-123" },
      { baseUrl: "wss://example.com", WebSocketImpl: WebSocketMock },
    );

    // Give the async function a chance to run
    await Promise.resolve();

    expect(mockWebSocket.addEventListener).toHaveBeenCalledWith(
      "open",
      expect.any(Function),
    );
    expect(mockWebSocket.addEventListener).toHaveBeenCalledWith(
      "error",
      expect.any(Function),
    );
    expect(mockWebSocket.addEventListener).toHaveBeenCalledWith(
      "close",
      expect.any(Function),
    );
  });

  it("uses default WebSocket implementation when not provided", async () => {
    let openCallback = null;
    let constructorCalled = false;

    const mockWebSocket = {
      addEventListener: vi.fn((event, handler) => {
        if (event === "open") {
          openCallback = handler;
        }
      }),
      close: vi.fn(),
    };

    class MockGlobalWebSocket {
      constructor() {
        constructorCalled = true;
      }
      addEventListener = mockWebSocket.addEventListener;
      close = mockWebSocket.close;
    }

    global.WebSocket = MockGlobalWebSocket;

    const promise = openGenerationWebSocket(
      { jobId: "job-123" },
      { baseUrl: "wss://example.com" },
    );

    await Promise.resolve();

    expect(constructorCalled).toBe(true);

    openCallback();
    await promise;

    delete global.WebSocket;
  });

  it("uses provided baseUrl when given", async () => {
    let capturedUrl = null;
    let openCallback = null;

    const mockWebSocket = {
      addEventListener: vi.fn((event, handler) => {
        if (event === "open") {
          openCallback = handler;
        }
      }),
      close: vi.fn(),
    };

    class WebSocketMock {
      constructor(url) {
        capturedUrl = url;
      }
      addEventListener = mockWebSocket.addEventListener;
      close = mockWebSocket.close;
    }

    const promise = openGenerationWebSocket(
      { jobId: "job-123" },
      { baseUrl: "wss://custom.example.com", WebSocketImpl: WebSocketMock },
    );

    await Promise.resolve();

    expect(capturedUrl).toContain("wss://custom.example.com");

    openCallback();
    await promise;
  });

  it("awaits getIdToken before constructing WebSocket URL", async () => {
    let capturedUrl = null;
    let openCallback = null;
    let getIdTokenCalled = false;

    getIdTokenMock.mockImplementation(async () => {
      getIdTokenCalled = true;
      return "jwt-token-value";
    });

    const mockWebSocket = {
      addEventListener: vi.fn((event, handler) => {
        if (event === "open") {
          openCallback = handler;
        }
      }),
      close: vi.fn(),
    };

    class WebSocketMock {
      constructor(url) {
        capturedUrl = url;
      }
      addEventListener = mockWebSocket.addEventListener;
      close = mockWebSocket.close;
    }

    const promise = openGenerationWebSocket(
      { jobId: "job-123" },
      { baseUrl: "wss://example.com", WebSocketImpl: WebSocketMock },
    );

    await Promise.resolve();

    // At this point, getIdToken should have been called
    expect(getIdTokenCalled).toBe(true);
    expect(capturedUrl).toContain("jwt-token-value");

    openCallback();
    await promise;
  });

  it("rejects with WebSocketError when connection times out after 10 seconds", async () => {
    vi.useFakeTimers();

    const mockWebSocket = {
      addEventListener: vi.fn(),
      close: vi.fn(),
    };

    class WebSocketMock {
      addEventListener = mockWebSocket.addEventListener;
      close = mockWebSocket.close;
    }

    const promise = openGenerationWebSocket(
      { jobId: "job-123" },
      { baseUrl: "wss://example.com", WebSocketImpl: WebSocketMock },
    );

    await Promise.resolve();

    // Advance time past the 10-second timeout
    vi.advanceTimersByTime(10001);

    await expect(promise).rejects.toBeInstanceOf(WebSocketError);
    await expect(promise).rejects.toMatchObject({
      message: expect.stringContaining("timeout"),
    });

    expect(mockWebSocket.close).toHaveBeenCalled();

    vi.useRealTimers();
  });

  it("does not reject on timeout if connection already opened", async () => {
    vi.useFakeTimers();

    let openCallback = null;

    const mockWebSocket = {
      addEventListener: vi.fn((event, handler) => {
        if (event === "open") {
          openCallback = handler;
        }
      }),
      close: vi.fn(),
    };

    class WebSocketMock {
      addEventListener = mockWebSocket.addEventListener;
      close = mockWebSocket.close;
    }

    const promise = openGenerationWebSocket(
      { jobId: "job-123" },
      { baseUrl: "wss://example.com", WebSocketImpl: WebSocketMock },
    );

    await Promise.resolve();

    // Open the connection before timeout fires
    openCallback();
    const ws = await promise;

    // Advance time past the 10-second timeout
    vi.advanceTimersByTime(10001);

    expect(ws).toBeInstanceOf(WebSocketMock);
    expect(mockWebSocket.close).not.toHaveBeenCalled();

    vi.useRealTimers();
  });

  it("ignores error event if connection already opened", async () => {
    let openCallback = null;
    let errorCallback = null;

    const mockWebSocket = {
      addEventListener: vi.fn((event, handler) => {
        if (event === "open") {
          openCallback = handler;
        } else if (event === "error") {
          errorCallback = handler;
        }
      }),
      close: vi.fn(),
    };

    class WebSocketMock {
      addEventListener = mockWebSocket.addEventListener;
      close = mockWebSocket.close;
    }

    const promise = openGenerationWebSocket(
      { jobId: "job-123" },
      { baseUrl: "wss://example.com", WebSocketImpl: WebSocketMock },
    );

    await Promise.resolve();

    // Open first
    openCallback();
    const ws = await promise;

    // Then trigger error event (should be ignored)
    const errorEvent = new Event("error");
    errorCallback(errorEvent);

    // Promise should still be resolved with the WebSocket
    expect(ws).toBeInstanceOf(WebSocketMock);
  });

  it("ignores close event if connection already opened", async () => {
    let openCallback = null;
    let closeCallback = null;

    const mockWebSocket = {
      addEventListener: vi.fn((event, handler) => {
        if (event === "open") {
          openCallback = handler;
        } else if (event === "close") {
          closeCallback = handler;
        }
      }),
      close: vi.fn(),
    };

    class WebSocketMock {
      addEventListener = mockWebSocket.addEventListener;
      close = mockWebSocket.close;
    }

    const promise = openGenerationWebSocket(
      { jobId: "job-123" },
      { baseUrl: "wss://example.com", WebSocketImpl: WebSocketMock },
    );

    await Promise.resolve();

    // Open first
    openCallback();
    const ws = await promise;

    // Then trigger close event (should be ignored)
    closeCallback();

    // Promise should still be resolved with the WebSocket
    expect(ws).toBeInstanceOf(WebSocketMock);
  });

  it("ignores open event if error already occurred", async () => {
    let openCallback = null;
    let errorCallback = null;

    const mockWebSocket = {
      addEventListener: vi.fn((event, handler) => {
        if (event === "open") {
          openCallback = handler;
        } else if (event === "error") {
          errorCallback = handler;
        }
      }),
      close: vi.fn(),
    };

    class WebSocketMock {
      addEventListener = mockWebSocket.addEventListener;
      close = mockWebSocket.close;
    }

    const promise = openGenerationWebSocket(
      { jobId: "job-123" },
      { baseUrl: "wss://example.com", WebSocketImpl: WebSocketMock },
    );

    await Promise.resolve();

    // Error first
    const errorEvent = new Event("error");
    errorCallback(errorEvent);

    // Then try to open (should be ignored)
    openCallback();

    await expect(promise).rejects.toBeInstanceOf(WebSocketError);
  });

  it("ignores close event if error already occurred", async () => {
    let errorCallback = null;
    let closeCallback = null;

    const mockWebSocket = {
      addEventListener: vi.fn((event, handler) => {
        if (event === "error") {
          errorCallback = handler;
        } else if (event === "close") {
          closeCallback = handler;
        }
      }),
      close: vi.fn(),
    };

    class WebSocketMock {
      addEventListener = mockWebSocket.addEventListener;
      close = mockWebSocket.close;
    }

    const promise = openGenerationWebSocket(
      { jobId: "job-123" },
      { baseUrl: "wss://example.com", WebSocketImpl: WebSocketMock },
    );

    await Promise.resolve();

    // Error first
    const errorEvent = new Event("error");
    errorCallback(errorEvent);

    // Then close (should be ignored)
    closeCallback();

    await expect(promise).rejects.toBeInstanceOf(WebSocketError);
  });

  it("falls back to getWsBaseUrl when baseUrl option is undefined", async () => {
    // Test the nullish coalescing operator (??): when baseUrl is undefined,
    // it falls back to getWsBaseUrl(). Since getWsBaseUrl() may return empty
    // in environments where VITE_WS_BASE_URL is not set, we test both paths.
    let capturedUrl = null;
    let openCallback = null;

    const mockWebSocket = {
      addEventListener: vi.fn((event, handler) => {
        if (event === "open") {
          openCallback = handler;
        }
      }),
      close: vi.fn(),
    };

    class WebSocketMock {
      constructor(url) {
        capturedUrl = url;
      }
      addEventListener = mockWebSocket.addEventListener;
      close = mockWebSocket.close;
    }

    const promise = openGenerationWebSocket(
      { jobId: "job-123" },
      { baseUrl: undefined, WebSocketImpl: WebSocketMock }, // explicitly undefined
    );

    await Promise.resolve();

    // Either the URL was captured (if env var is set) or error was thrown (if not)
    if (capturedUrl === null) {
      // VITE_WS_BASE_URL not configured in environment, so should reject
      await expect(promise).rejects.toBeInstanceOf(WebSocketError);
    } else {
      // VITE_WS_BASE_URL is configured, URL should be constructed
      expect(capturedUrl).toContain("?jobId=job-123");
      openCallback();
      await promise;
    }
  });

  it("does not double-reject when timeout fires after error event", async () => {
    vi.useFakeTimers();

    let errorCallback = null;

    const mockWebSocket = {
      addEventListener: vi.fn((event, handler) => {
        if (event === "error") {
          errorCallback = handler;
        }
      }),
      close: vi.fn(),
    };

    class WebSocketMock {
      addEventListener = mockWebSocket.addEventListener;
      close = mockWebSocket.close;
    }

    const promise = openGenerationWebSocket(
      { jobId: "job-123" },
      { baseUrl: "wss://example.com", WebSocketImpl: WebSocketMock },
    );

    await Promise.resolve();

    // Trigger error before timeout
    const errorEvent = new Event("error");
    errorCallback(errorEvent);

    // Advance time to trigger timeout
    vi.advanceTimersByTime(10001);

    await expect(promise).rejects.toBeInstanceOf(WebSocketError);
    // Should only reject once with the error message, not timeout message
    await expect(promise).rejects.toMatchObject({
      message: expect.stringContaining("failed"),
    });

    vi.useRealTimers();
  });
});
