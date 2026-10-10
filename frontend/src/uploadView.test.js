import { describe, it, expect, vi, beforeEach } from "vitest";

vi.mock("./api.js", () => {
  class ApiError extends Error {
    constructor(message, { status = null } = {}) {
      super(message);
      this.name = "ApiError";
      this.status = status;
    }
  }
  return {
    requestUploadUrls: vi.fn(),
    createJob: vi.fn(),
    requestInstructionGeneration: vi.fn(),
    fetchResultImage: vi.fn(),
    fetchResultColors: vi.fn(),
    ApiError,
  };
});

vi.mock("./uploadClient.js", () => {
  class UploadError extends Error {
    constructor(message, { status = null } = {}) {
      super(message);
      this.name = "UploadError";
      this.status = status;
    }
  }
  return { putFileToUrl: vi.fn(), UploadError };
});

vi.mock("./websocketClient.js", async () => {
  const actual = await vi.importActual("./websocketClient.js");
  class WebSocketError extends Error {
    constructor(message, { cause } = {}) {
      super(message);
      this.name = "WebSocketError";
      if (cause !== undefined) {
        this.cause = cause;
      }
    }
  }
  return {
    openGenerationWebSocket: vi.fn(),
    closeGenerationWebSocket: vi.fn(),
    parseResultMessage: actual.parseResultMessage,
    RESULT_STATUS_COMPLETED: actual.RESULT_STATUS_COMPLETED,
    WebSocketError,
  };
});

import {
  requestUploadUrls,
  createJob,
  requestInstructionGeneration,
  fetchResultImage,
  fetchResultColors,
} from "./api.js";
import { putFileToUrl } from "./uploadClient.js";
import {
  openGenerationWebSocket,
  closeGenerationWebSocket,
} from "./websocketClient.js";
import { mountUploadView } from "./uploadView.js";

function makeFile(name) {
  return new File(["data"], name, { type: "image/jpeg" });
}

function selectFiles(container, files) {
  const input = container.querySelector("[data-role='file-input']");
  Object.defineProperty(input, "files", { value: files, configurable: true });
  input.dispatchEvent(new Event("change", { bubbles: true }));
}

function flush() {
  return new Promise((resolve) => setTimeout(resolve, 0));
}

/**
 * Drives the view through file selection and upload so it reaches the state
 * where the "Generate Instruction" button is shown. Mocks
 * `requestUploadUrls`/`putFileToUrl` with default successful resolutions
 * unless the caller already configured them.
 */
async function uploadFiles(container, { jobId = "uuid-1" } = {}) {
  const files = [makeFile("a.jpg")];
  selectFiles(container, files);

  requestUploadUrls.mockResolvedValue({
    bucket: "bucket",
    folder: jobId,
    prefix: `uploads/${jobId}`,
    expiresIn: 900,
    uploadItems: files.map((f, i) => ({
      uploadUrl: `https://s3.example.com/${i}`,
      key: `uploads/${jobId}/${f.name}`,
      fileName: f.name,
      contentType: "image/jpeg",
    })),
  });
  putFileToUrl.mockResolvedValue(undefined);

  container.querySelector("[data-action='start-upload']").click();
  await flush();
  await flush();
}

beforeEach(() => {
  vi.clearAllMocks();
  global.URL.createObjectURL = vi.fn(() => "blob:mock");
  global.URL.revokeObjectURL = vi.fn();
});

describe("mountUploadView", () => {
  it("disables the upload button until exactly 1 valid file is selected", () => {
    const container = document.createElement("div");
    mountUploadView(container);

    selectFiles(container, [makeFile("a.jpg"), makeFile("b.jpg")]);

    const button = container.querySelector("[data-action='start-upload']");
    expect(button.disabled).toBe(true);
    expect(container.querySelector(".error-list").textContent).toMatch(
      /exactly 1 image/,
    );
  });

  it("hides the file selector when a valid file is selected", () => {
    const container = document.createElement("div");
    mountUploadView(container);

    const files = [makeFile("a.jpg")];
    selectFiles(container, files);

    expect(container.querySelector(".upload-selector")).toBeNull();
    expect(container.querySelector(".preview-grid")).not.toBeNull();
    expect(
      container.querySelector("[data-action='start-upload']").disabled,
    ).toBe(false);
  });

  it("renders the image on the left and an empty result pane on the right", () => {
    const container = document.createElement("div");
    mountUploadView(container);

    expect(container.querySelector(".workspace")).toBeNull();

    selectFiles(container, [makeFile("a.jpg")]);

    const source = container.querySelector("[data-role='source-pane']");
    const result = container.querySelector("[data-role='result-pane']");
    expect(source.querySelector(".preview-thumb")).not.toBeNull();
    expect(result.querySelector("img")).toBeNull();
    expect(
      source.compareDocumentPosition(result) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });

  it("does not allow selecting multiple files in the file input", () => {
    const container = document.createElement("div");
    mountUploadView(container);

    expect(container.querySelector("[data-role='file-input']").multiple).toBe(
      false,
    );
  });

  it("allows removing an image from the selection by clicking the remove button", () => {
    const container = document.createElement("div");
    mountUploadView(container);

    const files = [makeFile("a.jpg")];
    selectFiles(container, files);

    const removeBtn = container.querySelector("[data-action='remove-image']");
    expect(removeBtn).not.toBeNull();

    removeBtn.click();

    expect(container.querySelector(".upload-selector")).not.toBeNull();
    const previews = container.querySelectorAll(".preview-item");
    expect(previews.length).toBe(0);
  });

  it("shows a single remove button when an image is selected", () => {
    const container = document.createElement("div");
    mountUploadView(container);

    const files = [makeFile("a.jpg")];
    selectFiles(container, files);

    const removeButtons = container.querySelectorAll(
      "[data-action='remove-image']",
    );
    expect(removeButtons.length).toBe(1);
  });

  it("uploads the file and shows the success state", async () => {
    const container = document.createElement("div");
    mountUploadView(container);

    const files = [makeFile("a.jpg")];
    selectFiles(container, files);

    requestUploadUrls.mockResolvedValue({
      bucket: "bucket",
      folder: "uuid-1",
      prefix: "uploads/uuid-1",
      expiresIn: 900,
      uploadItems: files.map((f, i) => ({
        uploadUrl: `https://s3.example.com/${i}`,
        key: `uploads/uuid-1/${f.name}`,
        fileName: f.name,
        contentType: "image/jpeg",
      })),
    });
    putFileToUrl.mockResolvedValue(undefined);

    container.querySelector("[data-action='start-upload']").click();

    // Flush microtasks for the async upload orchestration.
    await new Promise((resolve) => setTimeout(resolve, 0));
    await new Promise((resolve) => setTimeout(resolve, 0));

    expect(putFileToUrl).toHaveBeenCalledTimes(1);
    expect(container.querySelector(".upload-success")).not.toBeNull();
    expect(container.textContent).toMatch(/uuid-1/);
  });

  it("shows a retry button for a file whose upload fails", async () => {
    const container = document.createElement("div");
    mountUploadView(container);

    const files = [makeFile("a.jpg")];
    selectFiles(container, files);

    requestUploadUrls.mockResolvedValue({
      bucket: "bucket",
      folder: "uuid-1",
      prefix: "uploads/uuid-1",
      expiresIn: 900,
      uploadItems: files.map((f, i) => ({
        uploadUrl: `https://s3.example.com/${i}`,
        key: `uploads/uuid-1/${f.name}`,
        fileName: f.name,
        contentType: "image/jpeg",
      })),
    });

    putFileToUrl.mockImplementation((url) =>
      url.endsWith("/0")
        ? Promise.reject(new Error("S3 rejected the upload (HTTP 403)."))
        : Promise.resolve(),
    );

    container.querySelector("[data-action='start-upload']").click();

    await new Promise((resolve) => setTimeout(resolve, 0));
    await new Promise((resolve) => setTimeout(resolve, 0));

    expect(
      container.querySelector("[data-action='retry-item']"),
    ).not.toBeNull();
    expect(container.querySelector(".upload-success")).toBeNull();
  });

  it("surfaces an API error and returns to the select phase", async () => {
    const container = document.createElement("div");
    mountUploadView(container);

    const files = [makeFile("a.jpg")];
    selectFiles(container, files);

    const { ApiError } = await import("./api.js");
    requestUploadUrls.mockRejectedValue(
      new ApiError("Network error while requesting upload URLs."),
    );

    container.querySelector("[data-action='start-upload']").click();

    await new Promise((resolve) => setTimeout(resolve, 0));

    expect(container.querySelector(".error-banner").textContent).toMatch(
      /Network error/,
    );
    expect(
      container.querySelector("[data-action='start-upload']").disabled,
    ).toBe(false);
  });

  it("hides the file selector after all files upload successfully", async () => {
    const container = document.createElement("div");
    mountUploadView(container);

    const files = [makeFile("a.jpg")];
    selectFiles(container, files);

    requestUploadUrls.mockResolvedValue({
      bucket: "bucket",
      folder: "uuid-1",
      prefix: "uploads/uuid-1",
      expiresIn: 900,
      uploadItems: files.map((f, i) => ({
        uploadUrl: `https://s3.example.com/${i}`,
        key: `uploads/uuid-1/${f.name}`,
        fileName: f.name,
        contentType: "image/jpeg",
      })),
    });
    putFileToUrl.mockResolvedValue(undefined);

    container.querySelector("[data-action='start-upload']").click();

    await new Promise((resolve) => setTimeout(resolve, 0));
    await new Promise((resolve) => setTimeout(resolve, 0));

    expect(container.querySelector(".upload-selector")).toBeNull();
    expect(container.querySelector(".upload-success")).not.toBeNull();
  });

  it("shows 'Start over' button during upload and resets on click", async () => {
    const container = document.createElement("div");
    mountUploadView(container);

    const files = [makeFile("a.jpg")];
    selectFiles(container, files);

    requestUploadUrls.mockResolvedValue({
      bucket: "bucket",
      folder: "uuid-1",
      prefix: "uploads/uuid-1",
      expiresIn: 900,
      uploadItems: files.map((f, i) => ({
        uploadUrl: `https://s3.example.com/${i}`,
        key: `uploads/uuid-1/${f.name}`,
        fileName: f.name,
        contentType: "image/jpeg",
      })),
    });
    putFileToUrl.mockResolvedValue(undefined);

    container.querySelector("[data-action='start-upload']").click();

    await new Promise((resolve) => setTimeout(resolve, 0));

    const resetBtn = container.querySelector("[data-action='reset']");
    expect(resetBtn).not.toBeNull();

    resetBtn.click();

    expect(container.querySelector(".upload-selector")).not.toBeNull();
    expect(container.querySelector(".preview-grid")).toBeNull();
  });

  it("allows retrying a failed file upload", async () => {
    const container = document.createElement("div");
    mountUploadView(container);

    const files = [makeFile("a.jpg")];
    selectFiles(container, files);

    requestUploadUrls.mockResolvedValue({
      bucket: "bucket",
      folder: "uuid-1",
      prefix: "uploads/uuid-1",
      expiresIn: 900,
      uploadItems: files.map((f, i) => ({
        uploadUrl: `https://s3.example.com/${i}`,
        key: `uploads/uuid-1/${f.name}`,
        fileName: f.name,
        contentType: "image/jpeg",
      })),
    });

    putFileToUrl.mockRejectedValueOnce(new Error("Network error"));
    putFileToUrl.mockResolvedValue(undefined);

    container.querySelector("[data-action='start-upload']").click();

    await new Promise((resolve) => setTimeout(resolve, 0));
    await new Promise((resolve) => setTimeout(resolve, 0));

    expect(
      container.querySelector("[data-action='retry-item']"),
    ).not.toBeNull();
    expect(putFileToUrl).toHaveBeenCalledTimes(1);

    container.querySelector("[data-action='retry-item']").click();

    await new Promise((resolve) => setTimeout(resolve, 0));

    expect(putFileToUrl).toHaveBeenCalledTimes(2);
  });

  it("shows validation error when trying to upload with invalid files", async () => {
    const container = document.createElement("div");
    mountUploadView(container);

    const files = [makeFile("a.jpg"), makeFile("b.jpg")];
    selectFiles(container, files);

    container.querySelector("[data-action='start-upload']").click();

    await new Promise((resolve) => setTimeout(resolve, 0));

    expect(container.querySelector(".error-list")).not.toBeNull();
    expect(requestUploadUrls).not.toHaveBeenCalled();
  });

  it("calls onProgress callback during upload", async () => {
    const container = document.createElement("div");
    mountUploadView(container);

    const files = [makeFile("a.jpg")];
    selectFiles(container, files);

    const onProgressCallbacks = [];

    requestUploadUrls.mockResolvedValue({
      bucket: "bucket",
      folder: "uuid-1",
      prefix: "uploads/uuid-1",
      expiresIn: 900,
      uploadItems: files.map((f, i) => ({
        uploadUrl: `https://s3.example.com/${i}`,
        key: `uploads/uuid-1/${f.name}`,
        fileName: f.name,
        contentType: "image/jpeg",
      })),
    });

    putFileToUrl.mockImplementation(async (url, file, options) => {
      if (options.onProgress) {
        onProgressCallbacks.push(options.onProgress);
      }
    });

    container.querySelector("[data-action='start-upload']").click();

    await new Promise((resolve) => setTimeout(resolve, 0));
    await new Promise((resolve) => setTimeout(resolve, 0));

    expect(onProgressCallbacks.length).toBe(1);
  });

  it("hides the upload button after all files upload successfully", async () => {
    const container = document.createElement("div");
    mountUploadView(container);

    const files = [makeFile("a.jpg")];
    selectFiles(container, files);

    requestUploadUrls.mockResolvedValue({
      bucket: "bucket",
      folder: "uuid-1",
      prefix: "uploads/uuid-1",
      expiresIn: 900,
      uploadItems: files.map((f, i) => ({
        uploadUrl: `https://s3.example.com/${i}`,
        key: `uploads/uuid-1/${f.name}`,
        fileName: f.name,
        contentType: "image/jpeg",
      })),
    });
    putFileToUrl.mockResolvedValue(undefined);

    container.querySelector("[data-action='start-upload']").click();

    await new Promise((resolve) => setTimeout(resolve, 0));
    await new Promise((resolve) => setTimeout(resolve, 0));

    expect(container.querySelector("[data-action='start-upload']")).toBeNull();
    expect(container.querySelector(".upload-success")).not.toBeNull();
  });

  it("shows the upload button again if an image is removed after upload", async () => {
    const container = document.createElement("div");
    mountUploadView(container);

    const files = [makeFile("a.jpg")];
    selectFiles(container, files);

    requestUploadUrls.mockResolvedValue({
      bucket: "bucket",
      folder: "uuid-1",
      prefix: "uploads/uuid-1",
      expiresIn: 900,
      uploadItems: files.map((f, i) => ({
        uploadUrl: `https://s3.example.com/${i}`,
        key: `uploads/uuid-1/${f.name}`,
        fileName: f.name,
        contentType: "image/jpeg",
      })),
    });
    putFileToUrl.mockResolvedValue(undefined);

    container.querySelector("[data-action='start-upload']").click();

    await new Promise((resolve) => setTimeout(resolve, 0));
    await new Promise((resolve) => setTimeout(resolve, 0));

    // Upload is complete, button should be hidden
    expect(container.querySelector("[data-action='start-upload']")).toBeNull();

    // Remove an image
    const removeBtn = container.querySelector("[data-action='remove-image']");
    expect(removeBtn).not.toBeNull();
    removeBtn.click();

    // Upload button should be visible again (disabled, since no file remains)
    const uploadBtn = container.querySelector("[data-action='start-upload']");
    expect(uploadBtn).not.toBeNull();
    expect(uploadBtn.disabled).toBe(true);
  });

  it("shows the upload button again (disabled) when 'Start over' is clicked", async () => {
    const container = document.createElement("div");
    mountUploadView(container);

    const files = [makeFile("a.jpg")];
    selectFiles(container, files);

    requestUploadUrls.mockResolvedValue({
      bucket: "bucket",
      folder: "uuid-1",
      prefix: "uploads/uuid-1",
      expiresIn: 900,
      uploadItems: files.map((f, i) => ({
        uploadUrl: `https://s3.example.com/${i}`,
        key: `uploads/uuid-1/${f.name}`,
        fileName: f.name,
        contentType: "image/jpeg",
      })),
    });
    putFileToUrl.mockResolvedValue(undefined);

    container.querySelector("[data-action='start-upload']").click();

    await new Promise((resolve) => setTimeout(resolve, 0));
    await new Promise((resolve) => setTimeout(resolve, 0));

    // Upload is complete, button should be hidden
    expect(container.querySelector("[data-action='start-upload']")).toBeNull();

    // Click "Start over"
    const resetBtn = container.querySelector("[data-action='reset']");
    expect(resetBtn).not.toBeNull();
    resetBtn.click();

    // Upload button should be visible again (but disabled, since files are cleared)
    const uploadBtn = container.querySelector("[data-action='start-upload']");
    expect(uploadBtn).not.toBeNull();
    expect(uploadBtn.disabled).toBe(true);
    expect(container.querySelector(".upload-selector")).not.toBeNull();
  });

  it("hides the 'Generate Instruction' button on start", () => {
    const container = document.createElement("div");
    mountUploadView(container);

    expect(
      container.querySelector("[data-action='generate-instruction']"),
    ).toBeNull();
  });

  it("shows the 'Generate Instruction' button after the image is uploaded", async () => {
    const container = document.createElement("div");
    mountUploadView(container);

    await uploadFiles(container);

    const generateBtn = container.querySelector(
      "[data-action='generate-instruction']",
    );
    expect(generateBtn).not.toBeNull();
    expect(generateBtn.disabled).toBe(false);
  });

  it("hides the 'Generate Instruction' button if an image is removed after upload", async () => {
    const container = document.createElement("div");
    mountUploadView(container);

    await uploadFiles(container);

    expect(
      container.querySelector("[data-action='generate-instruction']"),
    ).not.toBeNull();

    const removeBtn = container.querySelector("[data-action='remove-image']");
    removeBtn.click();

    expect(
      container.querySelector("[data-action='generate-instruction']"),
    ).toBeNull();
  });

  it("hides the 'Generate Instruction' button when 'Start over' is clicked", async () => {
    const container = document.createElement("div");
    mountUploadView(container);

    await uploadFiles(container);

    expect(
      container.querySelector("[data-action='generate-instruction']"),
    ).not.toBeNull();

    container.querySelector("[data-action='reset']").click();

    expect(
      container.querySelector("[data-action='generate-instruction']"),
    ).toBeNull();
  });
});

describe("Generate Instruction button", () => {
  it("calls createJob with the folder ID when clicked", async () => {
    const container = document.createElement("div");
    mountUploadView(container);
    await uploadFiles(container, { jobId: "uuid-1" });

    createJob.mockResolvedValue(undefined);
    openGenerationWebSocket.mockResolvedValue({ addEventListener: vi.fn() });
    requestInstructionGeneration.mockResolvedValue(undefined);

    container.querySelector("[data-action='generate-instruction']").click();
    await flush();
    await flush();

    expect(createJob).toHaveBeenCalledWith({ jobId: "uuid-1" });
  });

  it("shows an error message when createJob fails and does not call requestInstructionGeneration", async () => {
    const container = document.createElement("div");
    mountUploadView(container);
    await uploadFiles(container);

    const { ApiError } = await import("./api.js");
    createJob.mockRejectedValue(new ApiError("Job creation failed."));

    container.querySelector("[data-action='generate-instruction']").click();
    await flush();
    await flush();

    expect(container.querySelector(".error-banner").textContent).toMatch(
      /Job creation failed/,
    );
    expect(requestInstructionGeneration).not.toHaveBeenCalled();
    expect(openGenerationWebSocket).not.toHaveBeenCalled();
  });

  it("calls openGenerationWebSocket with the jobId after createJob succeeds", async () => {
    const container = document.createElement("div");
    mountUploadView(container);
    await uploadFiles(container, { jobId: "uuid-1" });

    createJob.mockResolvedValue(undefined);
    const mockWebSocket = { addEventListener: vi.fn() };
    openGenerationWebSocket.mockResolvedValue(mockWebSocket);
    requestInstructionGeneration.mockResolvedValue(undefined);

    container.querySelector("[data-action='generate-instruction']").click();
    await flush();
    await flush();

    expect(openGenerationWebSocket).toHaveBeenCalledWith({ jobId: "uuid-1" });
  });

  it("calls requestInstructionGeneration only after createJob and the WebSocket attempt have both settled", async () => {
    const container = document.createElement("div");
    mountUploadView(container);
    await uploadFiles(container, { jobId: "uuid-1" });

    const callOrder = [];
    createJob.mockImplementation(async () => {
      callOrder.push("createJob");
    });
    openGenerationWebSocket.mockImplementation(async () => {
      callOrder.push("openGenerationWebSocket");
      return { addEventListener: vi.fn() };
    });
    requestInstructionGeneration.mockImplementation(async () => {
      callOrder.push("requestInstructionGeneration");
    });

    container.querySelector("[data-action='generate-instruction']").click();
    await flush();
    await flush();

    expect(callOrder).toEqual([
      "createJob",
      "openGenerationWebSocket",
      "requestInstructionGeneration",
    ]);
    expect(requestInstructionGeneration).toHaveBeenCalledWith({
      jobId: "uuid-1",
    });
  });

  it("shows a warning but still calls requestInstructionGeneration when the WebSocket connection fails", async () => {
    const container = document.createElement("div");
    mountUploadView(container);
    await uploadFiles(container);

    createJob.mockResolvedValue(undefined);
    const { WebSocketError } = await import("./websocketClient.js");
    openGenerationWebSocket.mockRejectedValue(
      new WebSocketError("WebSocket connection timeout."),
    );
    requestInstructionGeneration.mockResolvedValue(undefined);

    container.querySelector("[data-action='generate-instruction']").click();
    await flush();
    await flush();

    const errorBanners = container.querySelectorAll(".error-banner");
    const websocketWarning = Array.from(errorBanners).find((el) =>
      el.textContent.includes("WebSocket"),
    );
    expect(websocketWarning).not.toBeNull();
    expect(websocketWarning.textContent).toMatch(
      /WebSocket connection timeout/,
    );
    expect(requestInstructionGeneration).toHaveBeenCalled();
    expect(container.textContent).toMatch(/Instruction generation started/);
  });

  it("handles unexpected WebSocket errors gracefully", async () => {
    const container = document.createElement("div");
    mountUploadView(container);
    await uploadFiles(container);

    createJob.mockResolvedValue(undefined);
    openGenerationWebSocket.mockRejectedValue(new Error("Unknown error"));
    requestInstructionGeneration.mockResolvedValue(undefined);

    container.querySelector("[data-action='generate-instruction']").click();
    await flush();
    await flush();

    const errorBanners = container.querySelectorAll(".error-banner");
    const websocketWarning = Array.from(errorBanners).find((el) =>
      el.textContent.includes("WebSocket"),
    );
    expect(websocketWarning).not.toBeNull();
    expect(websocketWarning.textContent).toMatch(/Unexpected error/);
  });

  it("shows a success message and hides the button once instruction generation succeeds", async () => {
    const container = document.createElement("div");
    mountUploadView(container);
    await uploadFiles(container);

    createJob.mockResolvedValue(undefined);
    openGenerationWebSocket.mockResolvedValue({ addEventListener: vi.fn() });
    requestInstructionGeneration.mockResolvedValue(undefined);

    container.querySelector("[data-action='generate-instruction']").click();
    await flush();
    await flush();

    expect(container.textContent).toMatch(/Instruction generation started/);
    expect(
      container.querySelector("[data-action='generate-instruction']"),
    ).toBeNull();
  });

  it("shows an error banner and keeps the button visible for retry when requestInstructionGeneration fails", async () => {
    const container = document.createElement("div");
    mountUploadView(container);
    await uploadFiles(container);

    createJob.mockResolvedValue(undefined);
    openGenerationWebSocket.mockResolvedValue({ addEventListener: vi.fn() });
    const { ApiError } = await import("./api.js");
    requestInstructionGeneration.mockRejectedValue(
      new ApiError("jobId is already in progress or completed"),
    );

    container.querySelector("[data-action='generate-instruction']").click();
    await flush();
    await flush();

    expect(container.querySelector(".error-banner").textContent).toMatch(
      /already in progress/,
    );
    expect(
      container.querySelector("[data-action='generate-instruction']"),
    ).not.toBeNull();
  });

  it("disables the button while the flow is in progress", async () => {
    const container = document.createElement("div");
    mountUploadView(container);
    await uploadFiles(container);

    let resolveCreateJob;
    createJob.mockReturnValue(
      new Promise((resolve) => {
        resolveCreateJob = resolve;
      }),
    );

    container.querySelector("[data-action='generate-instruction']").click();
    await flush();

    expect(
      container.querySelector("[data-action='generate-instruction']").disabled,
    ).toBe(true);

    openGenerationWebSocket.mockResolvedValue({ addEventListener: vi.fn() });
    requestInstructionGeneration.mockResolvedValue(undefined);
    resolveCreateJob();
    await flush();
    await flush();

    expect(
      container.querySelector("[data-action='generate-instruction']"),
    ).toBeNull();
  });

  it("does not fire a second run for the same jobId while one is already in flight", async () => {
    const container = document.createElement("div");
    mountUploadView(container);
    await uploadFiles(container);

    let resolveCreateJob;
    createJob.mockReturnValue(
      new Promise((resolve) => {
        resolveCreateJob = resolve;
      }),
    );

    container.querySelector("[data-action='generate-instruction']").click();
    await flush();

    // The button is disabled while the flow is in progress, so a real
    // second click can't reach the handler. Dispatch the click event
    // directly (bypassing that native suppression) to prove the view's own
    // re-entrancy guard also rejects an overlapping run for this jobId.
    container
      .querySelector("[data-action='generate-instruction']")
      .dispatchEvent(new Event("click", { bubbles: true }));
    await flush();

    expect(createJob).toHaveBeenCalledTimes(1);

    openGenerationWebSocket.mockResolvedValue({ addEventListener: vi.fn() });
    requestInstructionGeneration.mockResolvedValue(undefined);
    resolveCreateJob();
    await flush();
    await flush();

    expect(createJob).toHaveBeenCalledTimes(1);
  });
});

describe("Result rendering from WebSocket message", () => {
  const COMPLETED = {
    jobId: "uuid-1",
    status: "COMPLETED",
    resultUrl: "https://s3/result.json",
    imageUrl: "https://s3/image_0.png",
  };

  async function startGeneration() {
    const container = document.createElement("div");
    mountUploadView(container);
    await uploadFiles(container, { jobId: "uuid-1" });

    let onMessage;
    const websocket = {
      addEventListener: vi.fn((type, handler) => {
        if (type === "message") onMessage = handler;
      }),
    };
    createJob.mockResolvedValue(undefined);
    openGenerationWebSocket.mockResolvedValue(websocket);
    requestInstructionGeneration.mockResolvedValue(undefined);
    container.querySelector("[data-action='generate-instruction']").click();
    await flush();
    await flush();
    const send = async (payload) => {
      onMessage({ data: typeof payload === "string" ? payload : JSON.stringify(payload) });
      await flush();
      await flush();
    };
    return { container, websocket, send };
  }

  beforeEach(() => {
    closeGenerationWebSocket.mockClear();
    URL.createObjectURL = vi.fn(() => "blob:result");
    URL.revokeObjectURL = vi.fn();
  });

  it("renders the image and a color table, then closes the WebSocket", async () => {
    fetchResultImage.mockResolvedValue(new Blob(["png"]));
    fetchResultColors.mockResolvedValue([
      { detail: "armor", paint: "#2E4A6B" },
      { detail: "gold <trim>", paint: "#B8862A" },
      { detail: "skin", paint: "#C49A6C" },
    ]);
    const { container, websocket, send } = await startGeneration();

    await send(COMPLETED);

    expect(fetchResultImage).toHaveBeenCalledWith(COMPLETED.imageUrl);
    expect(fetchResultColors).toHaveBeenCalledWith(COMPLETED.resultUrl);
    const pane = container.querySelector("[data-role='result-pane']");
    expect(pane.querySelector("[data-role='result-image']").getAttribute("src")).toBe(
      "blob:result",
    );
    expect(pane.querySelectorAll(".color-swatch")).toHaveLength(3);
    expect(pane.querySelector(".color-swatch").style.backgroundColor).not.toBe("");
    expect(pane.querySelectorAll(".color-name")[1].textContent).toBe("gold <trim>");
    expect(pane.querySelector(".result-placeholder")).toBeNull();
    expect(closeGenerationWebSocket).toHaveBeenCalledWith(websocket);
  });

  it("shows an error and closes the WebSocket when the download fails", async () => {
    const { ApiError } = await import("./api.js");
    fetchResultImage.mockRejectedValue(new ApiError("Could not download the result image."));
    fetchResultColors.mockResolvedValue([]);
    const { container, websocket, send } = await startGeneration();

    await send(COMPLETED);

    expect(
      container.querySelector("[data-role='result-pane'] [role='alert']").textContent,
    ).toMatch(/Could not download the result image/);
    expect(closeGenerationWebSocket).toHaveBeenCalledWith(websocket);
  });

  it("shows an error and closes the WebSocket for a non-COMPLETED status", async () => {
    const { container, websocket, send } = await startGeneration();

    await send({ jobId: "uuid-1", status: "FAILED" });

    expect(fetchResultImage).not.toHaveBeenCalled();
    expect(
      container.querySelector("[data-role='result-pane'] [role='alert']").textContent,
    ).toMatch(/Generation failed/);
    expect(closeGenerationWebSocket).toHaveBeenCalledWith(websocket);
  });

  it("ignores malformed messages and messages for another job", async () => {
    fetchResultImage.mockClear();
    const { container, send } = await startGeneration();

    await send("not json");
    await send({ ...COMPLETED, jobId: "other-job" });

    expect(fetchResultImage).not.toHaveBeenCalled();
    expect(closeGenerationWebSocket).not.toHaveBeenCalled();
    expect(container.querySelector(".result-placeholder")).not.toBeNull();
  });
});
