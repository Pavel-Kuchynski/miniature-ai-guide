// Uploads a single file directly to S3 via a presigned PUT URL, reporting
// progress. Uses XMLHttpRequest instead of fetch because fetch has no
// cross-browser-reliable upload progress API.

export class UploadError extends Error {
  constructor(message, { status = null, cause } = {}) {
    super(message);
    this.name = "UploadError";
    this.status = status;
    if (cause !== undefined) {
      this.cause = cause;
    }
  }
}

/**
 * @param {string} url Presigned S3 PUT URL.
 * @param {File | Blob} file
 * @param {{ onProgress?: (percent: number) => void, signal?: AbortSignal }} [options]
 * @returns {Promise<void>}
 */
export function putFileToUrl(url, file, { onProgress, signal } = {}) {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(new UploadError("Upload cancelled."));
      return;
    }

    const xhr = new XMLHttpRequest();
    xhr.open("PUT", url, true);

    // IMPORTANT: this Content-Type must exactly match the ContentType that was
    // passed to the backend when the presigned URL was generated (see
    // backend/lambda_upload/handler.py, which signs `ContentType` into the
    // URL). A presigned PUT URL created with ContentType="image/jpeg" will be
    // rejected by S3 with a 403 SignatureDoesNotMatch if the actual PUT
    // request sends a different (or missing) Content-Type header. The caller
    // (uploadView.js) is responsible for requesting URLs with the same
    // content types it later uploads with.
    if (file.type) {
      xhr.setRequestHeader("Content-Type", file.type);
    }

    const onAbort = () => xhr.abort();
    if (signal) {
      signal.addEventListener("abort", onAbort, { once: true });
    }

    const cleanup = () => {
      if (signal) {
        signal.removeEventListener("abort", onAbort);
      }
    };

    xhr.upload.onprogress = (event) => {
      if (onProgress && event.lengthComputable) {
        onProgress(Math.round((event.loaded / event.total) * 100));
      }
    };

    xhr.onload = () => {
      cleanup();
      if (xhr.status >= 200 && xhr.status < 300) {
        onProgress?.(100);
        resolve();
      } else {
        reject(
          new UploadError(`S3 rejected the upload (HTTP ${xhr.status}).`, {
            status: xhr.status,
          }),
        );
      }
    };

    xhr.onerror = () => {
      cleanup();
      // `onerror` fires (with xhr.status === 0 and no readable response body)
      // both for genuine network failures and for CORS failures — the browser
      // deliberately hides the real S3 error response from JS in the CORS
      // case, so `xhr.status`/`xhr.responseText` are useless here. In
      // practice, once the presigned-URL request itself succeeds (i.e. we got
      // this far), a same-shaped failure on the S3 PUT is almost always
      // because the S3 bucket has no (or an incomplete) CORS configuration
      // for the browser origin making the request — that's a bucket-level
      // setting, independent of the presigned URL itself, and can't be fixed
      // from this client code. See frontend/README.md / hand off to
      // infra for the required S3 CORS configuration:
      //   AllowedOrigins: ["http://localhost:5173", "<deployed frontend origin>"]
      //   AllowedMethods: ["PUT"]
      //   AllowedHeaders: ["Content-Type"]
      //   ExposeHeaders: ["ETag"]
      console.error(
        "[uploadClient] PUT to presigned S3 URL failed with no usable response",
        "(xhr.status === 0). This almost always means the S3 bucket is missing",
        "a CORS configuration for this origin (or it doesn't allow PUT / the",
        "Content-Type header) rather than a real network outage. Check the",
        "browser's Network tab for the request to:",
        url,
        "and verify the bucket's CORS rules allow this origin, method PUT,",
        "and header Content-Type.",
      );
      reject(
        new UploadError(
          "Network error while uploading to S3. This is often caused by " +
            "missing S3 bucket CORS configuration rather than an actual " +
            "network outage (see console for details).",
        ),
      );
    };

    xhr.onabort = () => {
      cleanup();
      reject(new UploadError("Upload cancelled."));
    };

    xhr.send(file);
  });
}
