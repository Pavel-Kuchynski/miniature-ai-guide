# Canonical code examples

These are illustrative shapes, not files to copy verbatim — match naming and structure to what already exists in the repo when it's there, and use these as the fallback pattern when it isn't.

## API client

`ApiError` carries enough context to distinguish network failure from a bad response, and the fetch function takes `fetchImpl`/`baseUrl`/`idToken` as injectable options so tests can swap them out:

```js
export class ApiError extends Error {
  constructor(message, { status, cause } = {}) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.cause = cause;
  }
}

export async function fetchGenerationStatus(
  jobId,
  { fetchImpl = fetch, baseUrl = import.meta.env.VITE_API_BASE_URL, idToken } = {},
) {
  let response;
  try {
    response = await fetchImpl(`${baseUrl}/jobs/${jobId}/status`, {
      headers: { Authorization: `Bearer ${idToken}` },
    });
  } catch (cause) {
    throw new ApiError("Unable to reach the server.", { cause });
  }

  if (!response.ok) {
    throw new ApiError("The server returned an error.", { status: response.status });
  }

  const body = await response.json();
  if (typeof body.status !== "string") {
    throw new ApiError("Received an unexpected response shape.", { status: response.status });
  }

  return body.status;
}
```

Notes:
- The network failure and the bad-response failure are distinguished by which one has `status` set — callers can branch on that without parsing message strings.
- Response shape is validated before it's handed back; a malformed body becomes an `ApiError`, not a downstream `TypeError` from destructuring something unexpected.

## WebSocket client

Connection lifecycle is wrapped in a promise so callers can `await` a socket instead of juggling event listeners themselves. `WebSocketImpl` is injectable for tests, and a timeout guarantees the promise always settles:

```js
export class WebSocketError extends Error {
  constructor(message, { cause } = {}) {
    super(message);
    this.name = "WebSocketError";
    this.cause = cause;
  }
}

export function connectToJobUpdates(
  jobId,
  idToken,
  { WebSocketImpl = WebSocket, timeoutMs = 10_000 } = {},
) {
  return new Promise((resolve, reject) => {
    const url = `${import.meta.env.VITE_WS_BASE_URL}?jobId=${jobId}&token=${idToken}`;
    const socket = new WebSocketImpl(url);

    const timeout = setTimeout(() => {
      socket.close();
      reject(new WebSocketError("Connection timed out."));
    }, timeoutMs);

    socket.addEventListener("open", () => {
      clearTimeout(timeout);
      resolve(socket);
    });

    socket.addEventListener("error", (event) => {
      clearTimeout(timeout);
      reject(new WebSocketError("Connection failed.", { cause: event }));
    });
  });
}
```

Notes:
- The timeout is cleared on both success and failure paths so it never fires after the promise has already settled.
- Callers get a rejected promise with a typed `WebSocketError`, not a raw DOM event, so error handling in views stays consistent with the API client's error shape.

## Escaping

Every piece of dynamic data going into `innerHTML` goes through `escapeHtml` first — including values that "shouldn't" contain markup, like IDs or statuses, since the cost of escaping is near zero and the cost of one missed call is an XSS hole:

```js
export function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

export function renderJobRow(job) {
  return `
    <li data-role="job-row" data-job-id="${escapeHtml(job.id)}">
      <span data-role="job-name">${escapeHtml(job.name)}</span>
      <span data-role="job-status">${escapeHtml(job.status)}</span>
    </li>
  `;
}
```

Notes:
- `data-role`/`data-job-id` selectors let event delegation at the container level (`querySelector('[data-role="job-row"]')`, reading `dataset.jobId`) find these elements without any class name involved.
