// WebSocket client for backend generation status updates. Handles connection
// lifecycle: opening connection with jobId and JWT token as query parameters,
// verifying connection is established, and keeping it open.
//
// Message handling and connection closing are handled in separate tasks/modules.

import { getIdToken } from "./auth.js";

export class WebSocketError extends Error {
  constructor(message, { cause } = {}) {
    super(message);
    this.name = "WebSocketError";
    if (cause !== undefined) {
      this.cause = cause;
    }
  }
}

const DEFAULT_WS_BASE_URL = "";

/**
 * Get the configured WebSocket base URL from environment variables.
 *
 * @returns {string} The WebSocket base URL, with trailing slashes removed.
 */
export function getWsBaseUrl() {
  const configured = import.meta.env?.VITE_WS_BASE_URL;
  return typeof configured === "string" && configured.length > 0
    ? configured.replace(/\/+$/, "")
    : DEFAULT_WS_BASE_URL;
}

/**
 * Open a WebSocket connection to the backend with jobId and JWT token as query
 * parameters. The connection is kept open to receive generation status updates.
 *
 * @param {{ jobId: string }} params
 * @param {{ baseUrl?: string, WebSocketImpl?: typeof WebSocket }} [options]
 * @returns {Promise<WebSocket>} The opened WebSocket connection.
 * @throws {WebSocketError} If connection cannot be established or times out.
 */
export async function openGenerationWebSocket(
  { jobId },
  { baseUrl, WebSocketImpl = WebSocket } = {},
) {
  const idToken = await getIdToken();

  const wsBaseUrl = baseUrl ?? getWsBaseUrl();
  if (!wsBaseUrl) {
    throw new WebSocketError(
      "WebSocket base URL is not configured. Set VITE_WS_BASE_URL in .env.local.",
    );
  }

  if (!idToken) {
    throw new WebSocketError(
      "No authentication token available. User must be signed in to open a WebSocket connection.",
    );
  }

  // Build WebSocket URL with query parameters
  const url = `${wsBaseUrl}?jobId=${encodeURIComponent(jobId)}&token=${encodeURIComponent(idToken)}`;

  console.debug("[websocket] Opening connection to", url);

  return new Promise((resolve, reject) => {
    let ws;
    let timeout;
    let settled = false;

    try {
      ws = new WebSocketImpl(url);
    } catch (error) {
      reject(
        new WebSocketError(
          "Failed to create WebSocket connection. Check the URL and network configuration.",
          { cause: error },
        ),
      );
      return;
    }

    // Set a timeout to detect if the connection hangs
    timeout = setTimeout(() => {
      ws.close();
      if (!settled) {
        settled = true;
        reject(
          new WebSocketError(
            "WebSocket connection timeout. The server may be unreachable.",
          ),
        );
      }
    }, 10000); // 10 second timeout

    ws.addEventListener("open", () => {
      clearTimeout(timeout);
      if (!settled) {
        settled = true;
        console.debug("[websocket] Connection opened successfully");
        resolve(ws);
      }
    });

    ws.addEventListener("error", (event) => {
      clearTimeout(timeout);
      if (!settled) {
        settled = true;
        console.error("[websocket] Connection error", event);
        reject(
          new WebSocketError("WebSocket connection failed. Check the URL and network.", {
            cause: event,
          }),
        );
      }
    });

    // If the connection closes before the 'open' event fires, reject the promise
    ws.addEventListener("close", () => {
      clearTimeout(timeout);
      // Only reject if we haven't already settled the promise
      if (!settled) {
        settled = true;
        reject(
          new WebSocketError(
            "WebSocket connection closed before opening. Server may have rejected the connection.",
          ),
        );
      }
    });
  });
}
