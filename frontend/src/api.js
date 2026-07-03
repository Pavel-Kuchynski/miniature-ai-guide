// Thin client for the backend API Gateway endpoints. All `fetch` calls are
// isolated here so UI code never talks to the network directly, and so this
// module can be swapped/mocked easily in tests.
//
// ASSUMPTION: the presigned-upload-URL endpoint is `POST {API_BASE_URL}/upload`,
// mirroring backend/lambda_upload's documented request/response contract
// (see backend/lambda_upload/README.md). The actual API Gateway route,
// its base URL, and Cognito auth wiring are not yet deployed/documented as
// of this writing — treat this path/base URL as provisional until the
// backend confirms it. The base URL is read from the `VITE_API_BASE_URL`
// build-time environment variable (see frontend README / .env.local),
// defaulting to an empty string (same-origin), which only makes sense once
// the API is actually reachable from wherever the static site is hosted.

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
  const url = `${baseUrl ?? getApiBaseUrl()}/upload`;

  let response;
  try {
    response = await fetchImpl(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ fileNames, contentTypes }),
    });
  } catch (error) {
    throw new ApiError("Network error while requesting upload URLs.", {
      cause: error,
    });
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
