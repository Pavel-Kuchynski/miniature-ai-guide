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

const MESSAGE_TYPE = {
  INFO: "info",
  ERROR: "error",
};

const MESSAGE_TIMEOUT_MS = 10000;

/**
 * Mount the upload view into the given container element.
 * @param {HTMLElement} container
 */
export function mountUploadView(container) {
  let state = createInitialState();
  let messageTimer = null;
  // Bumped whenever the user leaves the current job (start over, remove the
  // image, choose another file). Async flows capture it when they start and
  // stop touching state or messages once it has changed.
  let sessionId = 0;

  container.addEventListener("change", handleChange);
  container.addEventListener("click", handleClick);
  container.addEventListener("input", handleInput);
  container.addEventListener("keydown", handleKeydown);

  render();

  function createInitialState() {
    return {
      phase: PHASE.SELECT,
      files: [], // { file, previewUrl }
      validationErrors: [],
      message: null, // { type: "info" | "error", text }, the one visible message
      folder: null,
      items: [], // { fileName, key, uploadUrl, status, progress, error, fileIndex }
      websocket: null,
      instructionStatus: INSTRUCTION_STATUS.IDLE,
      resultStatus: RESULT_STATUS.IDLE,
      resultImageUrl: null, // object URL of the downloaded result image
      resultColors: [], // { detail, paint, quantity?, vallejo? } (one entry per Vallejo paint)
      wishes: "", // free-text user wishes for the processing
      isPaletteVisible: false, // shown when colors arrive, hidden only on reset
      isImageZoomed: false, // result image shown enlarged in an overlay
      isWishesLocked: false, // set once the instruction request is sent, cleared on reset
    };
  }

  function setState(patch) {
    state = { ...state, ...patch };
    render();
  }

  /**
   * Show the one message, replacing any previous one, and restart the
   * auto-dismiss timer. Only this function starts the timer, so re-renders
   * (for example upload progress) never extend how long a message stays.
   * A missing or unknown type is shown as info.
   */
  function showMessage(type, text) {
    clearTimeout(messageTimer);
    const messageType =
      type === MESSAGE_TYPE.ERROR ? MESSAGE_TYPE.ERROR : MESSAGE_TYPE.INFO;
    setState({ message: { type: messageType, text } });
    messageTimer = setTimeout(clearMessage, MESSAGE_TIMEOUT_MS);
  }

  function clearMessage() {
    clearTimeout(messageTimer);
    messageTimer = null;
    if (state.message) setState({ message: null });
  }

  /** Release the result image object URL and return the reset result state. */
  function clearResult() {
    if (state.resultImageUrl) URL.revokeObjectURL(state.resultImageUrl);
    return {
      resultStatus: RESULT_STATUS.IDLE,
      resultImageUrl: null,
      resultColors: [],
      isPaletteVisible: false,
      isImageZoomed: false,
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
    const session = sessionId;

    if (message.status !== RESULT_STATUS_COMPLETED) {
      setState({ resultStatus: RESULT_STATUS.ERROR });
      showMessage(
        message.status === "FAILED" ? MESSAGE_TYPE.ERROR : MESSAGE_TYPE.INFO,
        message.message
          ? `Generation failed: ${message.message}`
          : "Generation failed. Please try again.",
      );
      closeGenerationWebSocket(websocket);
      return;
    }

    setState({ resultStatus: RESULT_STATUS.LOADING });
    try {
      const [imageBlob, colors] = await Promise.all([
        fetchResultImage(message.imageUrl),
        fetchResultColors(message.resultUrl),
      ]);
      if (session !== sessionId) return; // user left the job meanwhile
      setState({
        resultStatus: RESULT_STATUS.SUCCESS,
        resultImageUrl: URL.createObjectURL(imageBlob),
        resultColors: colors,
        isPaletteVisible: colors.length > 0,
      });
    } catch (error) {
      if (session !== sessionId) return;
      const text =
        error instanceof ApiError
          ? error.message
          : "Unexpected error loading the result.";
      setState({ resultStatus: RESULT_STATUS.ERROR });
      showMessage(MESSAGE_TYPE.ERROR, text);
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

    sessionId += 1;
    clearMessage();
    setState({
      phase: PHASE.SELECT,
      files,
      validationErrors: errors,
      items: [],
      folder: null,
      instructionStatus: INSTRUCTION_STATUS.IDLE,
      isWishesLocked: false,
      ...clearResult(),
    });
    if (errors.length > 0) showMessage(MESSAGE_TYPE.ERROR, errors.join(" "));
  }

  /** Keep the wishes text in state without re-rendering, so typing is never interrupted. */
  function handleInput(event) {
    const field = event.target.closest("[data-role='wishes-input']");
    if (field) state = { ...state, wishes: field.value };
  }

  function handleKeydown(event) {
    if (event.key === "Escape" && state.isImageZoomed) closeZoom();
  }

  function openZoom() {
    setState({ isImageZoomed: true });
    container.querySelector("[data-action='close-zoom']")?.focus();
  }

  function closeZoom() {
    setState({ isImageZoomed: false });
    container.querySelector("[data-action='zoom-image']")?.focus();
  }

  function handleClick(event) {
    if (event.target.closest("[data-action='zoom-image']")) {
      openZoom();
      return;
    }

    // Close on the close button or a click on the backdrop, but not on the enlarged image.
    if (
      event.target.closest("[data-action='close-zoom']") ||
      event.target.matches("[data-role='zoom-overlay']")
    ) {
      closeZoom();
      return;
    }

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
      sessionId += 1;
      clearMessage();
      setState({
        phase: PHASE.SELECT,
        files,
        validationErrors: [],
        items: [],
        folder: null,
        instructionStatus: INSTRUCTION_STATUS.IDLE,
        isWishesLocked: false,
        ...clearResult(),
      });
      return;
    }

    if (event.target.closest("[data-action='reset']")) {
      revokePreviews(state.files);
      closeGenerationWebSocket(state.websocket);
      clearResult();
      sessionId += 1;
      clearMessage();
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
    const session = sessionId;
    setState({ instructionStatus: INSTRUCTION_STATUS.REQUESTING });

    // Kept outside the try block so a flow that went stale (the user left the
    // job) can still close the socket it opened: the reset only closes the one
    // stored in state, and this one is stored only once the flow finishes.
    let websocket = null;
    try {
      await createJob({ jobId });
      if (session !== sessionId) return;

      // Job created successfully. Now open a WebSocket connection to receive
      // generation status updates.
      let websocketFailure = null;
      try {
        websocket = await openGenerationWebSocket({ jobId });
      } catch (error) {
        const message =
          error instanceof WebSocketError
            ? error.message
            : "Unexpected error opening WebSocket connection.";
        websocketFailure = message;
        console.warn("[uploadView] WebSocket connection failed:", message);
      }
      if (session !== sessionId) {
        closeGenerationWebSocket(websocket);
        return;
      }

      // Listen before triggering generation so a fast result is never missed.
      websocket?.addEventListener("message", (event) =>
        handleResultMessage(jobId, websocket, event),
      );

      // Only trigger guide generation once job creation and the WebSocket
      // connection attempt have both completed.
      const instruction = state.wishes;
      setState({ isWishesLocked: true });
      await requestInstructionGeneration({ jobId, instruction });
      if (session !== sessionId) {
        closeGenerationWebSocket(websocket);
        return;
      }

      setState({ websocket, instructionStatus: INSTRUCTION_STATUS.SUCCESS });
      // The connection warning is the more important of the two, so it wins.
      if (websocketFailure) {
        showMessage(
          MESSAGE_TYPE.ERROR,
          `WebSocket connection warning: ${websocketFailure}`,
        );
      } else if (state.resultStatus === RESULT_STATUS.IDLE) {
        // A result push that already arrived (for example a fast failure)
        // keeps its message.
        showMessage(MESSAGE_TYPE.INFO, "Instruction generation started.");
      }
    } catch (error) {
      if (session !== sessionId) {
        closeGenerationWebSocket(websocket);
        return;
      }
      const message =
        error instanceof ApiError
          ? error.message
          : "Unexpected error generating instruction.";
      setState({ instructionStatus: INSTRUCTION_STATUS.ERROR });
      showMessage(MESSAGE_TYPE.ERROR, message);
    }
  }

  async function startUpload() {
    const { valid, errors } = validateSelectedFiles(
      state.files.map((f) => f.file),
    );
    if (!valid) {
      setState({ validationErrors: errors });
      showMessage(MESSAGE_TYPE.ERROR, errors.join(" "));
      return;
    }

    const session = sessionId;
    setState({ phase: PHASE.REQUESTING_URLS });

    let response;
    try {
      response = await requestUploadUrls({
        fileNames: state.files.map((f) => f.file.name),
        contentTypes: state.files.map(
          (f) => f.file.type || "application/octet-stream",
        ),
      });
    } catch (error) {
      if (session !== sessionId) return;
      const message =
        error instanceof ApiError
          ? error.message
          : "Unexpected error requesting the upload URL.";
      setState({ phase: PHASE.SELECT });
      showMessage(MESSAGE_TYPE.ERROR, message);
      return;
    }
    if (session !== sessionId) return;

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

    const session = sessionId;
    clearMessage(); // on Retry, drop the previous upload error
    updateItem(index, { status: "uploading", progress: 0, error: null });

    try {
      await putFileToUrl(item.uploadUrl, fileEntry.file, {
        onProgress: (progress) => {
          if (session === sessionId) updateItem(index, { progress });
        },
      });
      if (session !== sessionId) return;
      updateItem(index, { status: "success", progress: 100 });
    } catch (error) {
      if (session !== sessionId) return;
      const message =
        error instanceof UploadError
          ? error.message
          : "Unexpected upload error.";
      updateItem(index, { status: "error", error: message });
      showMessage(MESSAGE_TYPE.ERROR, message);
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
    if (allSucceeded) {
      const { folder } = state;
      showMessage(
        MESSAGE_TYPE.INFO,
        `Image uploaded successfully${folder ? ` to job folder ${folder}` : ""}.`,
      );
    }
  }

  function render() {
    const active = document.activeElement;
    const hadWishesFocus =
      container.contains(active) && active.dataset?.role === "wishes-input";
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
    message,
    items,
    instructionStatus,
    resultStatus,
    resultImageUrl,
    resultColors,
    wishes,
    isWishesLocked,
    isPaletteVisible,
    isImageZoomed,
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
          ${
            phase !== PHASE.SELECT
              ? `<button type="button" class="remove-btn pane-close-btn" data-action="reset" title="Start Over" aria-label="Start Over">×</button>`
              : ""
          }
          ${renderResult({ resultStatus, resultImageUrl })}
        </section>
      </div>
      ${
        isPaletteVisible &&
        resultStatus === RESULT_STATUS.SUCCESS &&
        resultColors.length > 0
          ? `<section class="palette-pane" data-role="palette-pane" aria-label="Neural Color Analysis">
        <h2 class="pane-title">🎨 Neural Color Analysis</h2>
        ${renderColorTable(resultColors)}
      </section>`
          : ""
      }
      <section class="wishes-pane" aria-label="Guide the AI">
        <label class="pane-title" for="user-wishes">✏️ Guide the AI</label>
        <textarea
          id="user-wishes"
          class="wishes-input"
          data-role="wishes-input"
          rows="4"
          maxlength="${WISHES_MAX_LENGTH}"
          ${isWishesLocked ? "disabled" : ""}
          placeholder="Describe your vision for the miniature…"
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
        ${isGeneratingInstruction ? "Generating vision…" : "✨ Generate New Vision"}
      </button>`
          : ""
      }
      ${phase !== PHASE.SELECT ? `<button type="button" data-action="reset">Start Over</button>` : ""}
    </div>
  `;

  const messageSection = message
    ? `<p class="message message--${message.type}" role="${message.type === MESSAGE_TYPE.ERROR ? "alert" : "status"}">${escapeHtml(message.text)}</p>`
    : "";

  return `
    ${isImageZoomed && resultStatus === RESULT_STATUS.SUCCESS ? renderZoomOverlay(resultImageUrl) : ""}
    ${selectorSection}
    ${workspaceSection}
    ${actionsSection}
    ${messageSection}
  `;
}

function renderZoomOverlay(resultImageUrl) {
  return `
    <div class="zoom-overlay" data-role="zoom-overlay">
      <div class="zoom-dialog" role="dialog" aria-modal="true" aria-label="Enlarged processed result">
        <button type="button" class="remove-btn zoom-close-btn" data-action="close-zoom" aria-label="Close enlarged image" title="Close">×</button>
        <img class="zoom-image" data-role="zoom-image" src="${resultImageUrl}" alt="Processed result, enlarged" />
      </div>
    </div>`;
}

function renderResult({ resultStatus, resultImageUrl }) {
  switch (resultStatus) {
    case RESULT_STATUS.LOADING:
      return `<p class="result-placeholder" role="status">Loading the result…</p>`;
    case RESULT_STATUS.SUCCESS:
      return `
        <button type="button" class="zoom-trigger" data-action="zoom-image" aria-label="Enlarge the processed result" title="Click to enlarge">
          <img class="result-image" data-role="result-image" src="${resultImageUrl}" alt="Processed result" />
        </button>`;
    default:
      return `<p class="result-placeholder">The processed result will appear here.</p>`;
  }
}

const WISHES_MAX_LENGTH = 500;

function vallejoLabel({ reference, name }) {
  return `Vallejo ${reference} ${name}`;
}

// One swatch per entry (one entry per Vallejo paint): the nearest Vallejo paint's color when
// known, else the AI's color. `detail` may list several details sharing the paint; a quantity
// above 1 is shown as a "×N" badge so the user sees how many details the paint covers.
function renderColorTable(colors) {
  if (colors.length === 0) return "";
  const cells = colors.map(({ detail, paint, quantity, vallejo }) => {
    const swatchColor = vallejo?.hex ?? paint;
    const ariaLabel = vallejo
      ? `${detail} color ${vallejoLabel(vallejo)}`
      : `${detail} color`;
    const quantityBadge =
      quantity > 1
        ? `<span class="color-quantity" data-role="color-quantity">${escapeHtml(`×${quantity}`)}</span>`
        : "";
    return `
        <li class="color-cell" data-action="select-color">
          <span class="color-swatch" style="background-color: ${swatchColor}" title="${swatchColor}" role="img" aria-label="${escapeHtml(ariaLabel)} ${swatchColor}"></span>
          <span class="color-name">${escapeHtml(detail)}</span>
          ${quantityBadge}
          ${
            vallejo
              ? `<span class="vallejo-color" data-role="vallejo-color"><span class="vallejo-name">${escapeHtml(vallejoLabel(vallejo))}</span></span>`
              : ""
          }
        </li>`;
  });
  return `<ul class="color-table" data-role="color-table">${cells.join("")}</ul>`;
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
      return "";
    case "error":
      return `
        <span class="item-status item-status--error">${escapeHtml(item.error ?? "Upload failed.")}</span>
        <button type="button" data-action="retry-item" data-index="${index}">Retry</button>
      `;
    default:
      return "";
  }
}
