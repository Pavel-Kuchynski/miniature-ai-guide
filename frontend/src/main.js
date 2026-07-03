// Entry point for the Miniature Painting Guide Generator frontend.
//
// This file currently only bootstraps the app shell. Upcoming work:
//   - Upload flow: request presigned S3 PUT URLs from the (not-yet-deployed)
//     API Gateway + lambda_upload backend, then PUT the 4 reference images
//     directly to S3 with progress reporting.
//   - Generation flow: call the (not-yet-implemented) start-job endpoint,
//     then poll a generation-status endpoint until the PDF result is ready.
//
// API calls should be isolated in a dedicated client module (e.g. `api.js`)
// once real endpoints exist, rather than scattered across the UI code.

function init() {
  const app = document.getElementById("app");
  if (!app) {
    return;
  }

  console.log("Miniature Painting Guide Generator frontend initialized.");
}

document.addEventListener("DOMContentLoaded", init);
