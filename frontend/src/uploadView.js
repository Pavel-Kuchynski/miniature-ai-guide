// Upload Image view: file selection + preview, requesting a presigned S3
// URL, uploading the single reference image with progress, and surfacing
// errors at every step. The image preview is shown on the left and an empty
// result pane on the right, reserved for the processed result. Rendered as plain DOM/innerHTML — no
// framework is used in this project (see frontend/README.md).

import {
  requestUploadUrls,
  createJob,
  requestInstructionGeneration,
  fetchResultImage,
  fetchResultColors,
  ApiError,
} from "./api.js";
import { putFileToUrl, UploadError } from "./uploadClient.js";
import {
  openGenerationWebSocket,
  closeGenerationWebSocket,
  parseResultMessage,
  RESULT_STATUS_COMPLETED,
  WebSocketError,
} from "./websocketClient.js";
import { REQUIRED_FILE_COUNT, validateSelectedFiles } from "./validation.js";

const PHASE = {
  SELECT: "select",
  REQUESTING_URLS: "requesting-urls",
  UPLOADING: "uploading",
  DONE: "done",
};

const RESULT_STATUS = {
  IDLE: "idle",
  LOADING: "loading",
  SUCCESS: "success",
  ERROR: "error",
};

const INSTRUCTION_STATUS = {
  IDLE: "idle",
  REQUESTING: "requesting",
  SUCCESS: "success",
  ERROR: "error",
};

/**
 * Mount the upload view into the given container element.
 * @param {HTMLElement} container
 */
export function mountUploadView(container) {
  let state = createInitialState();

  container.addEventListener("change", handleChange);
  container.addEventListener("click", handleClick);
  container.addEventListener("input", handleInput);

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
      instructionStatus: INSTRUCTION_STATUS.IDLE,
      instructionError: null,
      resultStatus: RESULT_STATUS.IDLE,
      resultImageUrl: null, // object URL of the downloaded result image
      resultColors: [], // { detail, paint }
      resultError: null,
      wishes: "", // free-text user wishes for the processing
    };
  }

  function setState(patch) {
    state = { ...state, ...patch };
    render();
  }

  /** Release the result image object URL and return the reset result state. */
  function clearResult() {
    if (state.resultImageUrl) URL.revokeObjectURL(state.resultImageUrl);
    return {
      resultStatus: RESULT_STATUS.IDLE,
      resultImageUrl: null,
      resultColors: [],
      resultError: null,
    };
  }

  /**
   * Handle a result message from the status WebSocket: download the image and
   * paint list, show them, and close the connection. The connection is closed
   * for any terminal message (including failures) for the current job; messages
   * for other jobs or malformed messages are ignored.
   */
  async function handleResultMessage(jobId, websocket, event) {
    const message = parseResultMessage(event.data);
    if (!message || message.jobId !== jobId || state.folder !== jobId) return;

    if (message.status !== RESULT_STATUS_COMPLETED) {
      setState({
        resultStatus: RESULT_STATUS.ERROR,
        resultError: "Generation failed. Please try again.",
      });
      closeGenerationWebSocket(websocket);
      return;
    }

    setState({ resultStatus: RESULT_STATUS.LOADING, resultError: null });
    try {
      const [imageBlob, colors] = await Promise.all([
        fetchResultImage(message.imageUrl),
        fetchResultColors(message.resultUrl),
      ]);
      if (state.folder !== jobId) return; // user started over meanwhile
      setState({
        resultStatus: RESULT_STATUS.SUCCESS,
        resultImageUrl: URL.createObjectURL(imageBlob),
        resultColors: colors,
      });
    } catch (error) {
      const text =
        error instanceof ApiError ? error.message : "Unexpected error loading the result.";
      setState({ resultStatus: RESULT_STATUS.ERROR, resultError: text });
    } finally {
      closeGenerationWebSocket(websocket);
    }
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
      instructionStatus: INSTRUCTION_STATUS.IDLE,
      instructionError: null,
      ...clearResult(),
    });
  }

  /** Keep the wishes text in state without re-rendering, so typing is never interrupted. */
  function handleInput(event) {
    const field = event.target.closest("[data-role='wishes-input']");
    if (field) state = { ...state, wishes: field.value };
  }

  function handleClick(event) {
    if (event.target.closest("[data-action='start-upload']")) {
      startUpload();
      return;
    }

    if (event.target.closest("[data-action='generate-instruction']")) {
      generateInstruction();
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
        instructionStatus: INSTRUCTION_STATUS.IDLE,
        instructionError: null,
        ...clearResult(),
      });
      return;
    }

    if (event.target.closest("[data-action='reset']")) {
      revokePreviews(state.files);
      closeGenerationWebSocket(state.websocket);
      clearResult();
      setState(createInitialState());
    }
  }

  /**
   * Runs the full "Generate Instruction" flow for the current job: creates
   * the job record, attempts to open the status WebSocket, and only once
   * both of those have settled, triggers guide/instruction generation via
   * `POST /jobs/<jobId>/instruction`. Guards against firing a new run for
   * the same jobId while a previous one is still in flight (the button is
   * also disabled meanwhile, but this keeps the function itself safe
   * against re-entrant calls).
   */
  async function generateInstruction() {
    if (!state.folder) return;
    if (state.instructionStatus === INSTRUCTION_STATUS.REQUESTING) return;

    const jobId = state.folder;
    setState({
      instructionStatus: INSTRUCTION_STATUS.REQUESTING,
      instructionError: null,
    });

    try {
      await createJob({ jobId });

      // Job created successfully. Now open a WebSocket connection to receive
      // generation status updates.
      let websocket = null;
      let websocketError = null;
      try {
        websocket = await openGenerationWebSocket({ jobId });
      } catch (error) {
        const message =
          error instanceof WebSocketError
            ? error.message
            : "Unexpected error opening WebSocket connection.";
        websocketError = message;
        console.warn("[uploadView] WebSocket connection failed:", message);
      }

      // Listen before triggering generation so a fast result is never missed.
      websocket?.addEventListener("message", (event) =>
        handleResultMessage(jobId, websocket, event),
      );

      // Only trigger guide generation once job creation and the WebSocket
      // connection attempt have both completed.
      await requestInstructionGeneration({ jobId });

      setState({
        websocket,
        websocketError,
        instructionStatus: INSTRUCTION_STATUS.SUCCESS,
      });
    } catch (error) {
      const message =
        error instanceof ApiError
          ? error.message
          : "Unexpected error generating instruction.";
      setState({
        instructionStatus: INSTRUCTION_STATUS.ERROR,
        instructionError: message,
      });
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
          : "Unexpected error requesting the upload URL.";
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
    const active = document.activeElement;
    const hadWishesFocus = container.contains(active) && active.dataset?.role === "wishes-input";
    const selectionStart = hadWishesFocus ? active.selectionStart : null;
    const selectionEnd = hadWishesFocus ? active.selectionEnd : null;

    container.innerHTML = renderTemplate(state);

    // innerHTML replaces the textarea, so restore focus/caret for users typing while
    // async updates (upload progress, result arrival) trigger a re-render.
    if (hadWishesFocus) {
      const field = container.querySelector("[data-role='wishes-input']");
      field?.focus();
      field?.setSelectionRange(selectionStart, selectionEnd);
    }
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
    instructionStatus,
    instructionError,
    resultStatus,
    resultImageUrl,
    resultColors,
    resultError,
    wishes,
  } = state;

  const hasValidFiles =
    files.length === REQUIRED_FILE_COUNT && validationErrors.length === 0;

  const selectorSection =
    !hasValidFiles && phase === PHASE.SELECT
      ? `
    <div class="upload-selector">
      <label class="file-input-label" for="reference-images">
        Select a reference image
      </label>
      <input
        id="reference-images"
        data-role="file-input"
        type="file"
        accept="image/*"
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
              <button type="button" class="remove-btn" data-action="remove-image" data-index="${i}" title="Remove this image" aria-label="Remove this image">×</button>
            </div>
            <img class="preview-thumb" src="${f.previewUrl}" alt="Preview of ${escapeHtml(f.file.name)}" />
            <span class="preview-name">${escapeHtml(f.file.name)}</span>
            ${items[i] ? renderItemStatus(items[i], i) : ""}
          </li>`,
          )
          .join("")}
      </ul>`
    : "";

  // Top row: uploaded image (left) and processed result (right), equal-sized frames.
  // Below: the color palette frame, then the user's wishes field.
  const workspaceSection = files.length
    ? `<div class="workspace">
        <section class="workspace-pane" data-role="source-pane" aria-label="Source image">
          ${previewsSection}
        </section>
        <section class="workspace-pane workspace-pane--result" data-role="result-pane" aria-label="Processed result">
          ${renderResult({ resultStatus, resultImageUrl, resultError })}
        </section>
      </div>
      <section class="palette-pane" data-role="palette-pane" aria-label="Color palette">
        <h2 class="pane-title">Color palette</h2>
        ${renderPalette({ resultStatus, resultColors })}
      </section>
      <section class="wishes-pane" aria-label="Your wishes">
        <label class="pane-title" for="user-wishes">Your wishes for the processing</label>
        <textarea
          id="user-wishes"
          class="wishes-input"
          data-role="wishes-input"
          rows="4"
          maxlength="${WISHES_MAX_LENGTH}"
          placeholder="Describe how you would like the image to be processed…"
        >${escapeHtml(wishes)}</textarea>
      </section>`
    : "";

  const canUpload =
    files.length === REQUIRED_FILE_COUNT &&
    validationErrors.length === 0 &&
    phase === PHASE.SELECT;

  const shouldShowUploadButton = phase !== PHASE.DONE;
  const isGeneratingInstruction =
    instructionStatus === INSTRUCTION_STATUS.REQUESTING;
  const shouldShowGenerateInstructionButton =
    phase === PHASE.DONE && instructionStatus !== INSTRUCTION_STATUS.SUCCESS;

  const actionsSection = `
    <div class="upload-actions">
      ${
        shouldShowUploadButton
          ? `<button type="button" data-action="start-upload" ${canUpload ? "" : "disabled"}>
        ${phase === PHASE.REQUESTING_URLS ? "Requesting upload URL…" : "Upload image"}
      </button>`
          : ""
      }
      ${
        shouldShowGenerateInstructionButton
          ? `<button type="button" data-action="generate-instruction" ${isGeneratingInstruction ? "disabled" : ""}>
        ${isGeneratingInstruction ? "Generating instruction…" : "Generate Instruction"}
      </button>`
          : ""
      }
      ${phase !== PHASE.SELECT ? `<button type="button" data-action="reset">Start over</button>` : ""}
    </div>
  `;

  const instructionErrorSection =
    instructionStatus === INSTRUCTION_STATUS.ERROR && instructionError
      ? `<p class="error-banner" role="alert">${escapeHtml(instructionError)}</p>`
      : "";

  const instructionSuccessSection =
    instructionStatus === INSTRUCTION_STATUS.SUCCESS
      ? `<p class="upload-success" role="status">Instruction generation started.</p>`
      : "";

  const websocketErrorSection =
    websocketError && phase === PHASE.DONE
      ? `<p class="error-banner" role="alert">WebSocket connection warning: ${escapeHtml(websocketError)}</p>`
      : "";

  const doneSection =
    phase === PHASE.DONE
      ? `<div class="upload-success" role="status">
          <p>Image uploaded successfully${
            folder ? ` to job folder <code>${escapeHtml(folder)}</code>` : ""
          }.</p>
          ${websocketErrorSection}
        </div>`
      : "";

  return `
    ${selectorSection}
    ${validationSection}
    ${requestErrorSection}
    ${workspaceSection}
    ${actionsSection}
    ${doneSection}
    ${instructionErrorSection}
    ${instructionSuccessSection}
  `;
}

function renderResult({ resultStatus, resultImageUrl, resultError }) {
  switch (resultStatus) {
    case RESULT_STATUS.LOADING:
      return `<p class="result-placeholder" role="status">Loading the result…</p>`;
    case RESULT_STATUS.ERROR:
      return `<p class="error-banner" role="alert">${escapeHtml(resultError ?? "Could not load the result.")}</p>`;
    case RESULT_STATUS.SUCCESS:
      return `
        <img class="result-image" data-role="result-image" src="${resultImageUrl}" alt="Processed result" />`;
    default:
      return `<p class="result-placeholder">The processed result will appear here.</p>`;
  }
}

const COLOR_TABLE_COLUMNS = 2;
const WISHES_MAX_LENGTH = 1000;

function renderPalette({ resultStatus, resultColors }) {
  if (resultStatus === RESULT_STATUS.SUCCESS && resultColors.length > 0) {
    return renderColorTable(resultColors);
  }
  return `<p class="result-placeholder">The color palette will appear here.</p>`;
}

function renderColorTable(colors) {
  if (colors.length === 0) return "";
  const rows = [];
  for (let i = 0; i < colors.length; i += COLOR_TABLE_COLUMNS) {
    const cells = colors.slice(i, i + COLOR_TABLE_COLUMNS).map(
      ({ detail, paint }) => `
        <td>
          <span class="color-swatch" style="background-color: ${paint}" title="${paint}" role="img" aria-label="${escapeHtml(detail)} color ${paint}"></span>
          <span class="color-name">${escapeHtml(detail)}</span>
        </td>`,
    );
    rows.push(`<tr>${cells.join("")}</tr>`);
  }
  return `<table class="color-table" data-role="color-table"><tbody>${rows.join("")}</tbody></table>`;
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
