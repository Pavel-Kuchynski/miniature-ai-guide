// Thin client for the backend API Gateway endpoints. All `fetch` calls are
// isolated here so UI code never talks to the network directly, and so this
// module can be swapped/mocked easily in tests.
//
// The presigned-upload-URL endpoint is `POST {API_BASE_URL}/upload-urls`,
// per .tasks/upload_pictures_to_S3.md and backend/lambda_upload's documented
// request/response contract (see backend/lambda_upload/README.md). Cognito
// auth wiring is not yet deployed/documented as of this writing. The base
// URL is read from the `VITE_API_BASE_URL` build-time environment variable
// (see frontend README / .env.local), defaulting to an empty string
// (same-origin), which only makes sense once the API is actually reachable
// from wherever the static site is hosted.

const DEFAULT_API_BASE_URL = "";

export class ApiError extends Error {
  constructor(message, { status = null, cause } = {}) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    if (cause !== undefined) {
      this.cause = cause;
    }
  }
}

export function getApiBaseUrl() {
  const configured = import.meta.env?.VITE_API_BASE_URL;
  return typeof configured === "string" && configured.length > 0
    ? configured.replace(/\/+$/, "")
    : DEFAULT_API_BASE_URL;
}

/**
 * Request 4 presigned S3 PUT upload URLs for the given file names/content
 * types, grouped under a single UUID folder by the backend.
 *
 * @param {{ fileNames: string[], contentTypes: string[] }} params
 * @param {{ baseUrl?: string, fetchImpl?: typeof fetch }} [options]
 * @returns {Promise<{bucket: string, folder: string, prefix: string, uploadItems: Array<{uploadUrl: string, key: string, fileName: string, contentType: string}>, expiresIn: number}>}
 */
export async function requestUploadUrls(
  { fileNames, contentTypes },
  { baseUrl, fetchImpl = fetch } = {},
) {
  const url = `${baseUrl ?? getApiBaseUrl()}/upload-urls`;

  const headers = {
    "Content-Type": "application/json",
    Accept: "application/json",
    // Cognito auth is not wired up yet for this endpoint (see frontend/README.md
    // "Assumptions made"). Once a user pool/app client + login flow exists, attach
    // the ID/access token here, e.g.:
    //   Authorization: `Bearer ${getCognitoIdToken()}`
    // and handle 401/403 responses below by prompting re-authentication instead of
    // treating them as generic request failures.
  };

  // eslint-disable-next-line no-console -- intentional debug aid for diagnosing
  // CORS/network failures against the deployed API Gateway stage.
  console.debug("[api] requestUploadUrls -> POST", url, {
    headers,
    fileNames,
    contentTypes,
  });

  let response;
  try {
    response = await fetchImpl(url, {
      method: "POST",
      headers,
      body: JSON.stringify({ fileNames, contentTypes }),
    });
  } catch (error) {
    // A `fetch` promise rejection here (as opposed to a resolved response with a
    // non-2xx status) almost always means the request never got a response the
    // browser was willing to hand back to JS: either a genuine network/DNS
    // failure, or the browser blocked it per CORS policy (e.g. the preflight
    // OPTIONS request wasn't answered with the expected
    // Access-Control-Allow-* headers, or API Gateway rejected OPTIONS outright
    // with a 403 because no OPTIONS method/CORS is configured on this route).
    // The browser deliberately hides the real cause from JS for CORS failures,
    // so log what we know and point at the API Gateway CORS configuration as
    // the most likely culprit — that's outside this frontend module and needs
    // to be fixed on the API Gateway resource for `/upload-urls` (an OPTIONS
    // method/mock integration returning Access-Control-Allow-* headers).
    console.error(
      "[api] requestUploadUrls failed before receiving a response.",
      "This is typically a CORS problem (missing/incorrect OPTIONS method or",
      "Access-Control-Allow-* headers on the API Gateway route) rather than",
      "something fixable from the frontend. Check the browser's Network tab",
      "for the OPTIONS preflight request/response, and verify CORS is enabled",
      "on the API Gateway resource for:",
      url,
      error,
    );
    throw new ApiError(
      "Could not reach the upload API. This looks like a network or CORS " +
        "configuration issue (see console for details) rather than a problem " +
        "with your request.",
      { cause: error },
    );
  }

  let payload = null;
  try {
    payload = await response.json();
  } catch {
    payload = null;
  }

  if (!response.ok) {
    const message =
      payload?.error || `Upload URL request failed (HTTP ${response.status}).`;
    console.error("[api] requestUploadUrls received an error response.", {
      status: response.status,
      payload,
    });
    throw new ApiError(message, { status: response.status });
  }

  if (!Array.isArray(payload?.uploadItems) || payload.uploadItems.length !== 4) {
    throw new ApiError("Upload URL response did not contain 4 upload items.");
  }

  for (const item of payload.uploadItems) {
    if (!item || typeof item.uploadUrl !== "string" || item.uploadUrl.length === 0) {
      throw new ApiError("Upload URL response contained an invalid upload item.");
    }
  }

  return payload;
}
