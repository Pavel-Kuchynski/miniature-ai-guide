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
      reject(new UploadError("Network error while uploading to S3."));
    };

    xhr.onabort = () => {
      cleanup();
      reject(new UploadError("Upload cancelled."));
    };

    xhr.send(file);
  });
}
