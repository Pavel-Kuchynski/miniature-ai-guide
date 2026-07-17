// Upload Images view: file selection + preview, requesting presigned S3
// URLs, uploading the 4 reference images with per-file progress, and
// surfacing errors at every step. Rendered as plain DOM/innerHTML — no
// framework is used in this project (see frontend/README.md).

import { requestUploadUrls, createJob, ApiError } from "./api.js";
import { putFileToUrl, UploadError } from "./uploadClient.js";
import {
  openGenerationWebSocket,
  WebSocketError,
} from "./websocketClient.js";
import { REQUIRED_FILE_COUNT, validateSelectedFiles } from "./validation.js";

const PHASE = {
  SELECT: "select",
  REQUESTING_URLS: "requesting-urls",
  UPLOADING: "uploading",
  DONE: "done",
};

/**
 * Mount the upload view into the given container element.
 * @param {HTMLElement} container
 */
export function mountUploadView(container) {
  let state = createInitialState();

  container.addEventListener("change", handleChange);
  container.addEventListener("click", handleClick);

  render();

  function createInitialState() {
    return {
      phase: PHASE.SELECT,
      files: [], // { file, previewUrl }
      validationErrors: [],
      requestError: null,
      folder: null,
      items: [], // { fileName, key, uploadUrl, status, progress, error, fileIndex }
      websocket: null,
      websocketError: null,
    };
  }

  function setState(patch) {
    state = { ...state, ...patch };
    render();
  }

  function revokePreviews(files) {
    files.forEach((f) => URL.revokeObjectURL(f.previewUrl));
  }

  function handleChange(event) {
    const input = event.target.closest("[data-role='file-input']");
    if (!input) return;

    revokePreviews(state.files);
    const fileList = Array.from(input.files ?? []);
    const { errors } = validateSelectedFiles(fileList);
    const files = fileList.map((file) => ({
      file,
      previewUrl: URL.createObjectURL(file),
    }));

    setState({
      phase: PHASE.SELECT,
      files,
      validationErrors: errors,
      requestError: null,
      items: [],
      folder: null,
    });
  }

  function handleClick(event) {
    if (event.target.closest("[data-action='start-upload']")) {
      startUpload();
      return;
    }

    if (event.target.closest("[data-action='prepare-instruction']")) {
      prepareInstruction();
      return;
    }

    const retryBtn = event.target.closest("[data-action='retry-item']");
    if (retryBtn) {
      uploadItem(Number(retryBtn.dataset.index));
      return;
    }

    const removeBtn = event.target.closest("[data-action='remove-image']");
    if (removeBtn) {
      const index = Number(removeBtn.dataset.index);
      const files = state.files.slice();
      URL.revokeObjectURL(files[index].previewUrl);
      files.splice(index, 1);
      setState({
        phase: PHASE.SELECT,
        files,
        validationErrors: [],
        requestError: null,
        items: [],
        folder: null,
      });
      return;
    }

    if (event.target.closest("[data-action='reset']")) {
      revokePreviews(state.files);
      setState(createInitialState());
    }
  }

  async function prepareInstruction() {
    if (!state.folder) return;

    setState({ phase: PHASE.REQUESTING_URLS, requestError: null });

    try {
      await createJob({ jobId: state.folder });
      // Job created successfully. Now open a WebSocket connection to receive
      // generation status updates.
      let websocket = null;
      let websocketError = null;
      try {
        websocket = await openGenerationWebSocket({ jobId: state.folder });
      } catch (error) {
        const message =
          error instanceof WebSocketError
            ? error.message
            : "Unexpected error opening WebSocket connection.";
        websocketError = message;
        console.warn("[uploadView] WebSocket connection failed:", message);
      }

      setState({
        phase: PHASE.DONE,
        requestError: null,
        websocket,
        websocketError,
      });
    } catch (error) {
      const message =
        error instanceof ApiError
          ? error.message
          : "Unexpected error creating job.";
      setState({ phase: PHASE.DONE, requestError: message });
    }
  }

  async function startUpload() {
    const { valid, errors } = validateSelectedFiles(
      state.files.map((f) => f.file),
    );
    if (!valid) {
      setState({ validationErrors: errors });
      return;
    }

    setState({ phase: PHASE.REQUESTING_URLS, requestError: null });

    let response;
    try {
      response = await requestUploadUrls({
        fileNames: state.files.map((f) => f.file.name),
        contentTypes: state.files.map(
          (f) => f.file.type || "application/octet-stream",
        ),
      });
    } catch (error) {
      const message =
        error instanceof ApiError
          ? error.message
          : "Unexpected error requesting upload URLs.";
      setState({ phase: PHASE.SELECT, requestError: message });
      return;
    }

    const items = response.uploadItems.map((item, index) => ({
      ...item,
      status: "pending",
      progress: 0,
      error: null,
      fileIndex: index,
    }));

    setState({ phase: PHASE.UPLOADING, folder: response.folder, items });

    await Promise.all(items.map((_, index) => uploadItem(index)));
  }

  async function uploadItem(index) {
    const item = state.items[index];
    const fileEntry = item ? state.files[item.fileIndex] : null;
    if (!item || !fileEntry) return;

    updateItem(index, { status: "uploading", progress: 0, error: null });

    try {
      await putFileToUrl(item.uploadUrl, fileEntry.file, {
        onProgress: (progress) => updateItem(index, { progress }),
      });
      updateItem(index, { status: "success", progress: 100 });
    } catch (error) {
      const message =
        error instanceof UploadError
          ? error.message
          : "Unexpected upload error.";
      updateItem(index, { status: "error", error: message });
    }

    maybeFinish();
  }

  function updateItem(index, patch) {
    const items = state.items.slice();
    items[index] = { ...items[index], ...patch };
    setState({ items });
  }

  function maybeFinish() {
    if (state.items.length === 0) return;
    const allSettled = state.items.every(
      (item) => item.status === "success" || item.status === "error",
    );
    if (!allSettled) return;

    const allSucceeded = state.items.every((item) => item.status === "success");
    setState({ phase: allSucceeded ? PHASE.DONE : PHASE.UPLOADING });
  }

  function render() {
    container.innerHTML = renderTemplate(state);
  }
}

function escapeHtml(value) {
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function renderTemplate(state) {
  const {
    phase,
    files,
    validationErrors,
    requestError,
    items,
    folder,
    websocketError,
  } = state;

  const hasValidFiles =
    files.length === REQUIRED_FILE_COUNT && validationErrors.length === 0;

  const selectorSection =
    !hasValidFiles && phase === PHASE.SELECT
      ? `
    <div class="upload-selector">
      <label class="file-input-label" for="reference-images">
        Select exactly ${REQUIRED_FILE_COUNT} reference images
      </label>
      <input
        id="reference-images"
        data-role="file-input"
        type="file"
        accept="image/*"
        multiple
      />
    </div>
  `
      : "";

  const validationSection = validationErrors.length
    ? `<ul class="error-list" role="alert">
        ${validationErrors.map((e) => `<li>${escapeHtml(e)}</li>`).join("")}
      </ul>`
    : "";

  const requestErrorSection = requestError
    ? `<p class="error-banner" role="alert">${escapeHtml(requestError)}</p>`
    : "";

  const previewsSection = files.length
    ? `<ul class="preview-grid">
        ${files
          .map(
            (f, i) => `
          <li class="preview-item">
            <div class="preview-item-header">
              <button type="button" class="remove-btn" data-action="remove-image" data-index="${i}" title="Remove this image">×</button>
            </div>
            <img class="preview-thumb" src="${f.previewUrl}" alt="Preview of ${escapeHtml(f.file.name)}" />
            <span class="preview-name">${escapeHtml(f.file.name)}</span>
            ${items[i] ? renderItemStatus(items[i], i) : ""}
          </li>`,
          )
          .join("")}
      </ul>`
    : "";

  const canUpload =
    files.length === REQUIRED_FILE_COUNT &&
    validationErrors.length === 0 &&
    phase === PHASE.SELECT;

  const shouldShowUploadButton = phase !== PHASE.DONE;
  const shouldShowPrepareButton = phase === PHASE.DONE;

  const actionsSection = `
    <div class="upload-actions">
      ${
        shouldShowUploadButton
          ? `<button type="button" data-action="start-upload" ${canUpload ? "" : "disabled"}>
        ${phase === PHASE.REQUESTING_URLS ? "Requesting upload URLs…" : "Upload images"}
      </button>`
          : ""
      }
      ${shouldShowPrepareButton ? `<button type="button" data-action="prepare-instruction">Prepare Instruction</button>` : ""}
      ${phase !== PHASE.SELECT ? `<button type="button" data-action="reset">Start over</button>` : ""}
    </div>
  `;

  const websocketErrorSection =
    websocketError && phase === PHASE.DONE
      ? `<p class="error-banner" role="alert">WebSocket connection warning: ${escapeHtml(websocketError)}</p>`
      : "";

  const doneSection =
    phase === PHASE.DONE
      ? `<div class="upload-success" role="status">
          <p>All ${REQUIRED_FILE_COUNT} images uploaded successfully${
            folder ? ` to job folder <code>${escapeHtml(folder)}</code>` : ""
          }.</p>
          <p class="assumption-note">
            Job created successfully. WebSocket connection is being established to receive
            generation status updates.
          </p>
          ${websocketErrorSection}
        </div>`
      : "";

  return `
    ${selectorSection}
    ${validationSection}
    ${requestErrorSection}
    ${previewsSection}
    ${actionsSection}
    ${doneSection}
  `;
}

function renderItemStatus(item, index) {
  switch (item.status) {
    case "pending":
      return `<span class="item-status item-status--pending">Waiting to upload…</span>`;
    case "uploading":
      return `
        <div class="progress-bar" role="progressbar" aria-valuenow="${item.progress}" aria-valuemin="0" aria-valuemax="100">
          <div class="progress-bar-fill" style="width: ${item.progress}%"></div>
        </div>
        <span class="item-status item-status--uploading">${item.progress}%</span>
      `;
    case "success":
      return `<span class="item-status item-status--success">Uploaded</span>`;
    case "error":
      return `
        <span class="item-status item-status--error">${escapeHtml(item.error ?? "Upload failed.")}</span>
        <button type="button" data-action="retry-item" data-index="${index}">Retry</button>
      `;
    default:
      return "";
  }
}
