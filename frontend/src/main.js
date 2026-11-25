// Entry point for the Miniature Painting Guide Generator frontend.
//
// Auth flow: Cognito Hosted UI login/logout via AWS Amplify, see auth.js /
// authView.js. The Cognito ID token is attached as `Authorization: Bearer`
// to API calls (api.js) and as a query parameter to the status WebSocket
// (websocketClient.js). Tokens are never logged.
//
// Upload + generation flow: request presigned S3 PUT URLs (api.js), PUT the
// reference image directly to S3 with progress (uploadClient.js), then
// create the job, open the status WebSocket, and trigger generation, all
// orchestrated by uploadView.js.

import { mountAuthView } from "./authView.js";
import { mountUploadView } from "./uploadView.js";

function init() {
  const authBar = document.querySelector("[data-role='auth-bar']");
  if (authBar) {
    mountAuthView(authBar);
  }

  const app = document.getElementById("app");
  if (!app) {
    return;
  }

  mountUploadView(app);
}

document.addEventListener("DOMContentLoaded", init);
