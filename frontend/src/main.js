// Entry point for the Miniature Painting Guide Generator frontend.
//
// Upload flow: request presigned S3 PUT URLs from the lambda_upload backend
// (see api.js), then PUT the 4 reference images directly to S3 with
// progress reporting (see uploadClient.js), rendered by uploadView.js.
//
// Generation flow (start-job / status / result endpoints) is not yet
// implemented in the backend; the upload view stops at a "done" state that
// notes this once all 4 images are uploaded.

import { mountUploadView } from "./uploadView.js";

function init() {
  const app = document.getElementById("app");
  if (!app) {
    return;
  }

  mountUploadView(app);
}

document.addEventListener("DOMContentLoaded", init);
