import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

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
 * where the "Generate New Vision" button is shown. Mocks
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
    expect(container.querySelector(".message").textContent).toMatch(
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

  it("renders the palette frame below the images and the wishes field below the palette", () => {
    const container = document.createElement("div");
    mountUploadView(container);
    selectFiles(container, [makeFile("a.jpg")]);

    const palette = container.querySelector("[data-role='palette-pane']");
    const wishes = container.querySelector("[data-role='wishes-input']");
    expect(palette).toBeNull();
    expect(wishes.getAttribute("maxlength")).toBe("500");
    expect(wishes.tagName).toBe("TEXTAREA");
    expect(container.querySelector("label[for='user-wishes']")).not.toBeNull();
  });

  it("keeps the wishes text across re-renders", async () => {
    requestUploadUrls.mockRejectedValue(new Error("boom"));
    const container = document.createElement("div");
    mountUploadView(container);
    selectFiles(container, [makeFile("a.jpg")]);

    const field = container.querySelector("[data-role='wishes-input']");
    field.value = "Make it <dark>";
    field.dispatchEvent(new Event("input", { bubbles: true }));

    container.querySelector("[data-action='start-upload']").click();
    await vi.waitFor(() =>
      expect(container.querySelector(".message--error")).not.toBeNull(),
    );
    expect(container.querySelector("[data-role='wishes-input']").value).toBe(
      "Make it <dark>",
    );
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
    expect(container.querySelector(".message--info")).not.toBeNull();
    expect(container.querySelector(".message").textContent).toBe(
      "Image uploaded successfully to job folder uuid-1.",
    );
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
    expect(container.querySelector(".message--info")).toBeNull();
    expect(container.querySelector(".message--error")).not.toBeNull();
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

    expect(container.querySelector(".message--error").textContent).toMatch(
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
    expect(container.querySelector(".message--info")).not.toBeNull();
  });

  it("shows 'Start Over' button during upload and resets on click", async () => {
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

    expect(container.querySelector(".message--error")).not.toBeNull();
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
    expect(container.querySelector(".message--info")).not.toBeNull();
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

  it("shows the upload button again (disabled) when 'Start Over' is clicked", async () => {
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

    // Click "Start Over"
    const resetBtn = container.querySelector("[data-action='reset']");
    expect(resetBtn).not.toBeNull();
    resetBtn.click();

    // Upload button should be visible again (but disabled, since files are cleared)
    const uploadBtn = container.querySelector("[data-action='start-upload']");
    expect(uploadBtn).not.toBeNull();
    expect(uploadBtn.disabled).toBe(true);
    expect(container.querySelector(".upload-selector")).not.toBeNull();
  });

  it("hides the 'Generate New Vision' button on start", () => {
    const container = document.createElement("div");
    mountUploadView(container);

    expect(
      container.querySelector("[data-action='generate-instruction']"),
    ).toBeNull();
  });

  it("shows the 'Generate New Vision' button after the image is uploaded", async () => {
    const container = document.createElement("div");
    mountUploadView(container);

    await uploadFiles(container);

    const generateBtn = container.querySelector(
      "[data-action='generate-instruction']",
    );
    expect(generateBtn).not.toBeNull();
    expect(generateBtn.disabled).toBe(false);
  });

  it("hides the 'Generate New Vision' button if an image is removed after upload", async () => {
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

  it("hides the 'Generate New Vision' button when 'Start Over' is clicked", async () => {
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

describe("Generate New Vision button", () => {
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

    expect(container.querySelector(".message--error").textContent).toMatch(
      /Job creation failed/,
    );
    expect(requestInstructionGeneration).not.toHaveBeenCalled();
    expect(openGenerationWebSocket).not.toHaveBeenCalled();
  });

  it("locks the wishes field once the instruction request is sent and unlocks it on 'Start Over'", async () => {
    const container = document.createElement("div");
    mountUploadView(container);
    await uploadFiles(container, { jobId: "uuid-1" });

    createJob.mockResolvedValue(undefined);
    openGenerationWebSocket.mockResolvedValue({ addEventListener: vi.fn() });
    requestInstructionGeneration.mockResolvedValue(undefined);
    const wishesInput = () =>
      container.querySelector("[data-role='wishes-input']");
    expect(wishesInput().disabled).toBe(false);

    container.querySelector("[data-action='generate-instruction']").click();
    await flush();
    await flush();

    expect(requestInstructionGeneration).toHaveBeenCalled();
    expect(wishesInput().disabled).toBe(true);

    container.querySelector("[data-action='reset']").click();
    selectFiles(container, [makeFile("b.jpg")]);
    expect(wishesInput().disabled).toBe(false);
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
      instruction: "",
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

    const websocketWarning = container.querySelector(".message--error");
    expect(websocketWarning).not.toBeNull();
    expect(websocketWarning.textContent).toBe(
      "WebSocket connection warning: WebSocket connection timeout.",
    );
    expect(requestInstructionGeneration).toHaveBeenCalled();
    // The warning replaces the "started" message instead of stacking with it.
    expect(container.textContent).not.toMatch(/Instruction generation started/);
    expect(container.querySelectorAll(".message")).toHaveLength(1);
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

    const websocketWarning = container.querySelector(".message--error");
    expect(websocketWarning).not.toBeNull();
    expect(websocketWarning.textContent).toMatch(/WebSocket/);
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

    const message = container.querySelector(".message--info");
    expect(message.textContent).toBe("Instruction generation started.");
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

    expect(container.querySelector(".message--error").textContent).toMatch(
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
      onMessage({
        data: typeof payload === "string" ? payload : JSON.stringify(payload),
      });
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
    const palette = container.querySelector("[data-role='palette-pane']");
    expect(
      pane.querySelector("[data-role='result-image']").getAttribute("src"),
    ).toBe("blob:result");
    expect(pane.querySelector(".color-swatch")).toBeNull();
    expect(palette.querySelectorAll(".color-swatch")).toHaveLength(3);
    expect(
      palette.querySelector(".color-swatch").style.backgroundColor,
    ).not.toBe("");
    expect(palette.querySelectorAll(".color-name")[1].textContent).toBe(
      "gold <trim>",
    );
    expect(closeGenerationWebSocket).toHaveBeenCalledWith(websocket);
  });

  it("shows the nearest Vallejo paint next to a color, escaping its name", async () => {
    fetchResultImage.mockResolvedValue(new Blob(["png"]));
    fetchResultColors.mockResolvedValue([
      {
        detail: "armor",
        paint: "#2E4A6B",
        vallejo: { reference: "70.741", name: "Black <Green>", hex: "#2A2D28" },
      },
      { detail: "skin", paint: "#C49A6C" },
    ]);
    const { container, send } = await startGeneration();

    await send(COMPLETED);

    const entries = container.querySelectorAll("[data-role='vallejo-color']");
    expect(entries).toHaveLength(1);
    expect(entries[0].querySelector(".vallejo-name").textContent).toBe(
      "Vallejo 70.741 Black <Green>",
    );
    expect(entries[0].querySelector(".color-swatch")).toBeNull();
    const swatches = container.querySelectorAll(".color-swatch");
    expect(swatches).toHaveLength(2);
    expect(swatches[0].title).toBe("#2A2D28");
    expect(swatches[1].title).toBe("#C49A6C");
  });

  it("shows a ×N badge for a quantity above 1 next to the comma-joined detail list", async () => {
    fetchResultImage.mockResolvedValue(new Blob(["png"]));
    fetchResultColors.mockResolvedValue([
      {
        detail: "helmet trim, chest armor, shoulder armor",
        paint: "#9B7B2E",
        quantity: 3,
        vallejo: {
          reference: "70.923",
          name: "Japan. Unif. WWII",
          hex: "#987E37",
        },
      },
    ]);
    const { container, send } = await startGeneration();

    await send(COMPLETED);

    const cell = container.querySelector(".color-cell");
    expect(cell.querySelector(".color-name").textContent).toBe(
      "helmet trim, chest armor, shoulder armor",
    );
    expect(cell.querySelector("[data-role='color-quantity']").textContent).toBe(
      "×3",
    );
  });

  it("hides the quantity badge for quantity 1 and for entries without a quantity", async () => {
    fetchResultImage.mockResolvedValue(new Blob(["png"]));
    fetchResultColors.mockResolvedValue([
      { detail: "visor", paint: "#112233", quantity: 1 },
      { detail: "skin", paint: "#C49A6C" },
    ]);
    const { container, send } = await startGeneration();

    await send(COMPLETED);

    expect(container.querySelectorAll(".color-cell")).toHaveLength(2);
    expect(container.querySelector("[data-role='color-quantity']")).toBeNull();
  });

  it("escapes HTML characters in a comma-joined detail list", async () => {
    fetchResultImage.mockResolvedValue(new Blob(["png"]));
    fetchResultColors.mockResolvedValue([
      {
        detail: "helmet <trim>, chest & armor",
        paint: "#9B7B2E",
        quantity: 2,
      },
    ]);
    const { container, send } = await startGeneration();

    await send(COMPLETED);

    const name = container.querySelector(".color-name");
    expect(name.textContent).toBe("helmet <trim>, chest & armor");
    expect(name.innerHTML).toBe("helmet &lt;trim&gt;, chest &amp; armor");
    expect(container.querySelector("trim")).toBeNull();
  });

  it("keeps the palette visible after clicks on a color, the result pane, or the wishes field", async () => {
    fetchResultImage.mockResolvedValue(new Blob(["png"]));
    fetchResultColors.mockResolvedValue([
      { detail: "armor", paint: "#2E4A6B" },
    ]);
    const { container, send } = await startGeneration();
    await send(COMPLETED);

    container.querySelector("[data-action='select-color']").click();
    container.querySelector("[data-role='result-pane']").click();
    container.querySelector("[data-role='wishes-input']").click();
    expect(
      container.querySelector("[data-role='palette-pane']"),
    ).not.toBeNull();
  });

  it("shows no file name or 'Uploaded' text and resets via the result pane close button", async () => {
    fetchResultImage.mockResolvedValue(new Blob(["png"]));
    fetchResultColors.mockResolvedValue([
      { detail: "armor", paint: "#2E4A6B" },
    ]);
    const { container, send } = await startGeneration();
    await send(COMPLETED);

    const source = container.querySelector("[data-role='source-pane']");
    expect(source.textContent).not.toMatch(/Uploaded|\.jpg/);

    container
      .querySelector("[data-role='result-pane'] [data-action='reset']")
      .click();
    expect(container.querySelector("[data-role='result-pane']")).toBeNull();
  });

  it("opens the result image in an overlay and closes it via button, backdrop, or Escape", async () => {
    fetchResultImage.mockResolvedValue(new Blob(["png"]));
    fetchResultColors.mockResolvedValue([
      { detail: "armor", paint: "#2E4A6B" },
    ]);
    const { container, send } = await startGeneration();
    await send(COMPLETED);
    const overlay = () => container.querySelector("[data-role='zoom-overlay']");
    const open = () =>
      container.querySelector("[data-action='zoom-image']").click();
    expect(overlay()).toBeNull();

    open();
    expect(overlay()).not.toBeNull();
    expect(
      overlay().querySelector("[data-role='zoom-image']").getAttribute("src"),
    ).toBe("blob:result");

    // Clicking the enlarged image itself keeps the overlay open.
    overlay().querySelector("[data-role='zoom-image']").click();
    expect(overlay()).not.toBeNull();

    container.querySelector("[data-action='close-zoom']").click();
    expect(overlay()).toBeNull();

    open();
    overlay().click();
    expect(overlay()).toBeNull();

    open();
    container.dispatchEvent(
      new KeyboardEvent("keydown", { key: "Escape", bubbles: true }),
    );
    expect(overlay()).toBeNull();
  });

  it("shows an error and closes the WebSocket when the download fails", async () => {
    const { ApiError } = await import("./api.js");
    fetchResultImage.mockRejectedValue(
      new ApiError("Could not download the result image."),
    );
    fetchResultColors.mockResolvedValue([]);
    const { container, websocket, send } = await startGeneration();

    await send(COMPLETED);

    expect(container.querySelector(".message--error").textContent).toMatch(
      /Could not download the result image/,
    );
    expect(
      container.querySelector("[data-role='result-pane'] .result-placeholder")
        .textContent,
    ).toBe("The processed result will appear here.");
    expect(closeGenerationWebSocket).toHaveBeenCalledWith(websocket);
  });

  it("shows an error and closes the WebSocket for a non-COMPLETED status", async () => {
    const { container, websocket, send } = await startGeneration();

    await send({ jobId: "uuid-1", status: "FAILED" });

    expect(fetchResultImage).not.toHaveBeenCalled();
    const message = container.querySelector(".message--error");
    expect(message.getAttribute("role")).toBe("alert");
    expect(message.textContent).toBe("Generation failed. Please try again.");
    expect(
      container.querySelector("[data-role='result-pane'] [role='alert']"),
    ).toBeNull();
    expect(closeGenerationWebSocket).toHaveBeenCalledWith(websocket);
  });

  it("shows the failure message from the backend and closes the WebSocket", async () => {
    const { container, websocket, send } = await startGeneration();

    await send({
      jobId: "uuid-1",
      status: "FAILED",
      message: "Painting failed: ValueError",
    });

    expect(container.querySelector(".message--error").textContent).toBe(
      "Generation failed: Painting failed: ValueError",
    );
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

  it("shows a pushed message with a non-FAILED status as info", async () => {
    const { container, send } = await startGeneration();

    await send({ jobId: "uuid-1", status: "IN_PROGRESS" });

    const message = container.querySelector(".message");
    expect(message.classList.contains("message--info")).toBe(true);
    expect(message.getAttribute("role")).toBe("status");
  });
});

describe("single message section", () => {
  const MESSAGE_TIMEOUT_MS = 10000;

  function mountView() {
    const container = document.createElement("div");
    mountUploadView(container);
    return container;
  }

  function message(container) {
    return container.querySelector(".message");
  }

  /** Selects two files so the view shows one validation error message. */
  function showValidationMessage(container) {
    selectFiles(container, [makeFile("a.jpg"), makeFile("b.jpg")]);
  }

  /** Uploads one file with fake timers: the view ends in the DONE phase. */
  async function uploadWithFakeTimers(container) {
    selectFiles(container, [makeFile("a.jpg")]);
    requestUploadUrls.mockResolvedValue({
      bucket: "bucket",
      folder: "uuid-1",
      prefix: "uploads/uuid-1",
      expiresIn: 900,
      uploadItems: [
        {
          uploadUrl: "https://s3.example.com/0",
          key: "uploads/uuid-1/a.jpg",
          fileName: "a.jpg",
          contentType: "image/jpeg",
        },
      ],
    });
    putFileToUrl.mockResolvedValue(undefined);
    container.querySelector("[data-action='start-upload']").click();
    await vi.advanceTimersByTimeAsync(0);
  }

  describe("rendering", () => {
    it("renders an info message as message--info with role status", async () => {
      const container = mountView();
      await uploadFiles(container);

      const el = message(container);
      expect(el.classList.contains("message--info")).toBe(true);
      expect(el.classList.contains("message--error")).toBe(false);
      expect(el.getAttribute("role")).toBe("status");
    });

    it("renders an error message as message--error with role alert", () => {
      const container = mountView();

      showValidationMessage(container);

      const el = message(container);
      expect(el.classList.contains("message--error")).toBe(true);
      expect(el.classList.contains("message--info")).toBe(false);
      expect(el.getAttribute("role")).toBe("alert");
    });

    it("renders the message directly after the actions row", async () => {
      const container = mountView();
      await uploadFiles(container);

      const actions = container.querySelector(".upload-actions");
      expect(actions.nextElementSibling).toBe(message(container));
    });

    it("renders no message initially", () => {
      const container = mountView();

      expect(message(container)).toBeNull();
    });

    it("escapes HTML in the message text", () => {
      const container = mountView();

      selectFiles(container, [
        new File(["data"], "<b>x</b>.txt", { type: "text/plain" }),
      ]);

      expect(message(container).textContent).toMatch(/<b>x<\/b>\.txt/);
      expect(message(container).querySelector("b")).toBeNull();
    });

    it("no longer renders the old message sections", async () => {
      const container = mountView();
      showValidationMessage(container);
      expect(container.querySelector(".error-list")).toBeNull();
      expect(container.querySelector(".error-banner")).toBeNull();
      expect(container.querySelector(".upload-success")).toBeNull();

      await uploadFiles(container);
      expect(container.querySelector(".error-list")).toBeNull();
      expect(container.querySelector(".error-banner")).toBeNull();
      expect(container.querySelector(".upload-success")).toBeNull();
    });

    it("joins several validation errors into one message", () => {
      const container = mountView();

      selectFiles(container, [
        new File(["data"], "a.txt", { type: "text/plain" }),
        new File(["data"], "b.txt", { type: "text/plain" }),
      ]);

      const messages = container.querySelectorAll(".message");
      expect(messages).toHaveLength(1);
      expect(messages[0].textContent).toMatch(/exactly 1 image/);
      expect(messages[0].textContent).toMatch(/Image 1 \(a\.txt\)/);
      expect(messages[0].textContent).toMatch(/Image 2 \(b\.txt\)/);
      expect(messages[0].querySelectorAll("li")).toHaveLength(0);
    });

    it("shows a failed per-file upload as an error message and keeps the inline status and Retry button", async () => {
      const container = mountView();
      selectFiles(container, [makeFile("a.jpg")]);
      requestUploadUrls.mockResolvedValue({
        bucket: "bucket",
        folder: "uuid-1",
        prefix: "uploads/uuid-1",
        expiresIn: 900,
        uploadItems: [
          {
            uploadUrl: "https://s3.example.com/0",
            key: "uploads/uuid-1/a.jpg",
            fileName: "a.jpg",
            contentType: "image/jpeg",
          },
        ],
      });
      const { UploadError } = await import("./uploadClient.js");
      putFileToUrl.mockRejectedValue(
        new UploadError("S3 rejected the upload."),
      );

      container.querySelector("[data-action='start-upload']").click();
      await flush();
      await flush();

      expect(message(container).classList.contains("message--error")).toBe(
        true,
      );
      expect(message(container).textContent).toBe("S3 rejected the upload.");
      expect(container.querySelector(".item-status--error").textContent).toBe(
        "S3 rejected the upload.",
      );
      expect(
        container.querySelector("[data-action='retry-item']"),
      ).not.toBeNull();
    });

    it("shows a WebSocket connection failure as an error instead of the started message", async () => {
      const container = mountView();
      await uploadFiles(container);
      const { WebSocketError } = await import("./websocketClient.js");
      createJob.mockResolvedValue(undefined);
      openGenerationWebSocket.mockRejectedValue(
        new WebSocketError("WebSocket connection timeout."),
      );
      requestInstructionGeneration.mockResolvedValue(undefined);

      container.querySelector("[data-action='generate-instruction']").click();
      await flush();
      await flush();

      expect(container.querySelectorAll(".message")).toHaveLength(1);
      expect(message(container).classList.contains("message--error")).toBe(
        true,
      );
      expect(message(container).textContent).toBe(
        "WebSocket connection warning: WebSocket connection timeout.",
      );
    });

    it("omits the folder from the upload message when the folder is missing", async () => {
      const container = mountView();
      selectFiles(container, [makeFile("a.jpg")]);
      requestUploadUrls.mockResolvedValue({
        bucket: "bucket",
        folder: "",
        prefix: "uploads/",
        expiresIn: 900,
        uploadItems: [
          {
            uploadUrl: "https://s3.example.com/0",
            key: "uploads/a.jpg",
            fileName: "a.jpg",
            contentType: "image/jpeg",
          },
        ],
      });
      putFileToUrl.mockResolvedValue(undefined);

      container.querySelector("[data-action='start-upload']").click();
      await flush();
      await flush();

      expect(message(container).textContent).toBe(
        "Image uploaded successfully.",
      );
    });
  });

  describe("replacement", () => {
    it("replaces the previous message with the next one", async () => {
      const container = mountView();
      await uploadFiles(container);
      expect(message(container).textContent).toMatch(/uploaded successfully/);

      createJob.mockResolvedValue(undefined);
      openGenerationWebSocket.mockResolvedValue({ addEventListener: vi.fn() });
      requestInstructionGeneration.mockResolvedValue(undefined);
      container.querySelector("[data-action='generate-instruction']").click();
      await flush();
      await flush();

      expect(container.querySelectorAll(".message")).toHaveLength(1);
      expect(message(container).textContent).toBe(
        "Instruction generation started.",
      );
    });

    it("replaces a validation message with the next validation message", () => {
      const container = mountView();
      showValidationMessage(container);
      const first = message(container).textContent;

      selectFiles(container, [
        makeFile("a.jpg"),
        makeFile("b.jpg"),
        makeFile("c.jpg"),
      ]);

      expect(container.querySelectorAll(".message")).toHaveLength(1);
      expect(message(container).textContent).not.toBe(first);
      expect(message(container).textContent).toMatch(/selected 3/);
    });
  });

  describe("auto-dismiss", () => {
    beforeEach(() => {
      vi.useFakeTimers();
    });

    afterEach(() => {
      vi.useRealTimers();
    });

    it("keeps the message at 9999 ms and removes it at 10000 ms", () => {
      const container = mountView();
      showValidationMessage(container);

      vi.advanceTimersByTime(MESSAGE_TIMEOUT_MS - 1);
      expect(message(container)).not.toBeNull();

      vi.advanceTimersByTime(1);
      expect(message(container)).toBeNull();
    });

    it("restarts the 10-second timer when a new message replaces the old one", () => {
      const container = mountView();
      showValidationMessage(container);
      vi.advanceTimersByTime(6000);

      selectFiles(container, [
        makeFile("a.jpg"),
        makeFile("b.jpg"),
        makeFile("c.jpg"),
      ]);

      // Past the first message's deadline (t = 10000), within the second's.
      vi.advanceTimersByTime(MESSAGE_TIMEOUT_MS - 1);
      expect(message(container)).not.toBeNull();
      expect(message(container).textContent).toMatch(/selected 3/);

      vi.advanceTimersByTime(1);
      expect(message(container)).toBeNull();
    });

    it("does not restart the timer on upload progress re-renders", async () => {
      const container = mountView();
      selectFiles(container, [makeFile("a.jpg")]);
      requestUploadUrls.mockResolvedValue({
        bucket: "bucket",
        folder: "uuid-1",
        prefix: "uploads/uuid-1",
        expiresIn: 900,
        uploadItems: [
          {
            uploadUrl: "https://s3.example.com/0",
            key: "uploads/uuid-1/a.jpg",
            fileName: "a.jpg",
            contentType: "image/jpeg",
          },
        ],
      });
      const { UploadError } = await import("./uploadClient.js");
      let reportProgress;
      putFileToUrl.mockImplementationOnce((url, file, { onProgress }) => {
        reportProgress = onProgress;
        return Promise.reject(new UploadError("S3 rejected the upload."));
      });

      // The failed upload shows the error message at t = 0.
      container.querySelector("[data-action='start-upload']").click();
      await vi.advanceTimersByTimeAsync(0);
      await vi.advanceTimersByTimeAsync(0);
      expect(message(container).textContent).toBe("S3 rejected the upload.");

      // Late progress events re-render the view; the message and its timer stay.
      await vi.advanceTimersByTimeAsync(5000);
      reportProgress(50);
      await vi.advanceTimersByTimeAsync(4000);
      reportProgress(80);
      expect(container.querySelector(".item-status--error")).not.toBeNull();
      await vi.advanceTimersByTimeAsync(MESSAGE_TIMEOUT_MS - 5000 - 4000 - 1);
      expect(message(container)).not.toBeNull();

      await vi.advanceTimersByTimeAsync(1);
      expect(message(container)).toBeNull();
    });

    it("clears the message and its timer on 'Start Over'", async () => {
      const container = mountView();
      await uploadWithFakeTimers(container);
      await vi.advanceTimersByTimeAsync(0);
      expect(message(container)).not.toBeNull();

      container.querySelector("[data-action='reset']").click();

      expect(message(container)).toBeNull();
      expect(vi.getTimerCount()).toBe(0);
    });

    it("clears the message and its timer when a new file is chosen", () => {
      const container = mountView();
      showValidationMessage(container);
      expect(message(container)).not.toBeNull();

      selectFiles(container, [makeFile("a.jpg")]);

      expect(message(container)).toBeNull();
      expect(vi.getTimerCount()).toBe(0);
    });

    it("clears the message and its timer when the image is removed", async () => {
      const container = mountView();
      await uploadWithFakeTimers(container);
      await vi.advanceTimersByTimeAsync(0);
      expect(message(container)).not.toBeNull();

      container.querySelector("[data-action='remove-image']").click();

      expect(message(container)).toBeNull();
      expect(vi.getTimerCount()).toBe(0);
    });

    it("lets a message shown after a clear live the full 10 seconds", async () => {
      const container = mountView();
      await uploadWithFakeTimers(container);
      await vi.advanceTimersByTimeAsync(0);
      vi.advanceTimersByTime(9000);
      container.querySelector("[data-action='reset']").click();

      showValidationMessage(container);
      vi.advanceTimersByTime(MESSAGE_TIMEOUT_MS - 1);

      expect(message(container)).not.toBeNull();
    });
  });

  describe("flows that outlive their job", () => {
    const UPLOAD_RESPONSE = {
      bucket: "bucket",
      folder: "uuid-1",
      prefix: "uploads/uuid-1",
      expiresIn: 900,
      uploadItems: [
        {
          uploadUrl: "https://s3.example.com/0",
          key: "uploads/uuid-1/a.jpg",
          fileName: "a.jpg",
          contentType: "image/jpeg",
        },
      ],
    };
    const COMPLETED = {
      jobId: "uuid-1",
      status: "COMPLETED",
      resultUrl: "https://s3/result.json",
      imageUrl: "https://s3/image_0.png",
    };

    beforeEach(() => {
      vi.useFakeTimers();
    });

    afterEach(() => {
      vi.useRealTimers();
    });

    function deferred() {
      let resolve;
      let reject;
      const promise = new Promise((res, rej) => {
        resolve = res;
        reject = rej;
      });
      return { promise, resolve, reject };
    }

    /** Selects a file and clicks "Upload image"; the S3 upload stays pending. */
    async function startPendingUpload(container, upload) {
      selectFiles(container, [makeFile("a.jpg")]);
      requestUploadUrls.mockResolvedValue(UPLOAD_RESPONSE);
      putFileToUrl.mockReturnValue(upload.promise);
      container.querySelector("[data-action='start-upload']").click();
      await vi.advanceTimersByTimeAsync(0);
    }

    /**
     * Reaches the DONE phase and clicks "Generate New Vision" with the
     * WebSocket open. Returns a function that delivers a pushed message.
     */
    async function startGenerating(container, instruction) {
      await uploadWithFakeTimers(container);
      let onMessage;
      createJob.mockResolvedValue(undefined);
      openGenerationWebSocket.mockResolvedValue({
        addEventListener: vi.fn((type, handler) => {
          if (type === "message") onMessage = handler;
        }),
      });
      requestInstructionGeneration.mockReturnValue(instruction.promise);
      container.querySelector("[data-action='generate-instruction']").click();
      await vi.advanceTimersByTimeAsync(0);
      return async (payload) => {
        onMessage({ data: JSON.stringify(payload) });
        await vi.advanceTimersByTimeAsync(0);
      };
    }

    function expectFreshViewWithoutMessage(container) {
      expect(message(container)).toBeNull();
      expect(vi.getTimerCount()).toBe(0);
      expect(
        container.querySelector("[data-role='file-input']"),
      ).not.toBeNull();
      expect(container.querySelector("[data-action='reset']")).toBeNull();
    }

    it("shows no message when the instruction request succeeds after 'Start Over'", async () => {
      const container = mountView();
      const instruction = deferred();
      await startGenerating(container, instruction);

      container.querySelector("[data-action='reset']").click();
      instruction.resolve(undefined);
      await vi.advanceTimersByTimeAsync(0);

      expectFreshViewWithoutMessage(container);
    });

    it("shows no message when the instruction request fails after 'Start Over'", async () => {
      const container = mountView();
      const instruction = deferred();
      await startGenerating(container, instruction);
      const { ApiError } = await import("./api.js");

      container.querySelector("[data-action='reset']").click();
      instruction.reject(new ApiError("Instruction request failed."));
      await vi.advanceTimersByTimeAsync(0);

      expectFreshViewWithoutMessage(container);
    });

    it("shows no message when job creation fails after 'Start Over'", async () => {
      const container = mountView();
      await uploadWithFakeTimers(container);
      const creation = deferred();
      createJob.mockReturnValue(creation.promise);
      container.querySelector("[data-action='generate-instruction']").click();
      await vi.advanceTimersByTimeAsync(0);
      const { ApiError } = await import("./api.js");

      container.querySelector("[data-action='reset']").click();
      creation.reject(new ApiError("Could not create the job."));
      await vi.advanceTimersByTimeAsync(0);

      expectFreshViewWithoutMessage(container);
    });

    it("shows no message when the S3 upload succeeds after 'Start Over'", async () => {
      const container = mountView();
      const upload = deferred();
      await startPendingUpload(container, upload);

      container.querySelector("[data-action='reset']").click();
      upload.resolve(undefined);
      await vi.advanceTimersByTimeAsync(0);

      expectFreshViewWithoutMessage(container);
    });

    it("shows no message when the S3 upload fails after 'Start Over'", async () => {
      const container = mountView();
      const upload = deferred();
      await startPendingUpload(container, upload);
      const { UploadError } = await import("./uploadClient.js");

      container.querySelector("[data-action='reset']").click();
      upload.reject(new UploadError("S3 rejected the upload."));
      await vi.advanceTimersByTimeAsync(0);

      expectFreshViewWithoutMessage(container);
    });

    it("shows no message when the S3 upload fails after the image is removed", async () => {
      const container = mountView();
      const upload = deferred();
      await startPendingUpload(container, upload);
      const { UploadError } = await import("./uploadClient.js");

      container.querySelector("[data-action='remove-image']").click();
      upload.reject(new UploadError("S3 rejected the upload."));
      await vi.advanceTimersByTimeAsync(0);

      expect(message(container)).toBeNull();
      expect(vi.getTimerCount()).toBe(0);
    });

    it("shows no message when the S3 upload fails after another file is chosen", async () => {
      const container = mountView();
      const upload = deferred();
      await startPendingUpload(container, upload);
      const { UploadError } = await import("./uploadClient.js");

      // The file input is hidden during an upload, so deliver the change event
      // through a detached input that carries the same data attribute.
      const input = document.createElement("input");
      input.setAttribute("data-role", "file-input");
      container.appendChild(input);
      Object.defineProperty(input, "files", {
        value: [makeFile("b.jpg")],
        configurable: true,
      });
      input.dispatchEvent(new Event("change", { bubbles: true }));
      upload.reject(new UploadError("S3 rejected the upload."));
      await vi.advanceTimersByTimeAsync(0);

      expect(message(container)).toBeNull();
      expect(vi.getTimerCount()).toBe(0);
    });

    it("shows no message when the upload URL request fails after 'Start Over'", async () => {
      const container = mountView();
      selectFiles(container, [makeFile("a.jpg")]);
      const urls = deferred();
      requestUploadUrls.mockReturnValue(urls.promise);
      container.querySelector("[data-action='start-upload']").click();
      await vi.advanceTimersByTimeAsync(0);
      const { ApiError } = await import("./api.js");

      container.querySelector("[data-action='reset']").click();
      urls.reject(new ApiError("Could not get an upload URL."));
      await vi.advanceTimersByTimeAsync(0);

      expectFreshViewWithoutMessage(container);
    });

    it("shows no message when the result download fails after 'Start Over'", async () => {
      const container = mountView();
      const send = await startGenerating(container, {
        promise: Promise.resolve(undefined),
      });
      await vi.advanceTimersByTimeAsync(0);
      const download = deferred();
      fetchResultImage.mockReturnValue(download.promise);
      fetchResultColors.mockResolvedValue([]);
      await send(COMPLETED);
      const { ApiError } = await import("./api.js");

      container.querySelector("[data-action='reset']").click();
      download.reject(new ApiError("Could not load the result image."));
      await vi.advanceTimersByTimeAsync(0);

      expectFreshViewWithoutMessage(container);
    });

    it("keeps the failure message when a FAILED push arrives before the instruction request resolves", async () => {
      const container = mountView();
      const instruction = deferred();
      const send = await startGenerating(container, instruction);

      await send({
        jobId: "uuid-1",
        status: "FAILED",
        message: "Out of paint",
      });
      instruction.resolve(undefined);
      await vi.advanceTimersByTimeAsync(0);

      expect(message(container).textContent).toBe(
        "Generation failed: Out of paint",
      );
      expect(message(container).classList.contains("message--error")).toBe(
        true,
      );
    });

    it("clears the old upload error at once when Retry is pressed", async () => {
      const container = mountView();
      selectFiles(container, [makeFile("a.jpg")]);
      requestUploadUrls.mockResolvedValue(UPLOAD_RESPONSE);
      const { UploadError } = await import("./uploadClient.js");
      putFileToUrl.mockRejectedValueOnce(
        new UploadError("S3 rejected the upload."),
      );
      container.querySelector("[data-action='start-upload']").click();
      await vi.advanceTimersByTimeAsync(0);
      await vi.advanceTimersByTimeAsync(0);
      expect(message(container).textContent).toBe("S3 rejected the upload.");

      putFileToUrl.mockReturnValue(new Promise(() => {}));
      container.querySelector("[data-action='retry-item']").click();

      expect(message(container)).toBeNull();
      expect(vi.getTimerCount()).toBe(0);
    });

    it("closes the WebSocket opened while 'Start Over' was pressed during the connection attempt", async () => {
      const container = mountView();
      await uploadWithFakeTimers(container);
      const websocket = { addEventListener: vi.fn() };
      const connection = deferred();
      createJob.mockResolvedValue(undefined);
      openGenerationWebSocket.mockReturnValue(connection.promise);
      container.querySelector("[data-action='generate-instruction']").click();
      await vi.advanceTimersByTimeAsync(0);

      container.querySelector("[data-action='reset']").click();
      connection.resolve(websocket);
      await vi.advanceTimersByTimeAsync(0);

      expect(closeGenerationWebSocket).toHaveBeenCalledWith(websocket);
      expect(requestInstructionGeneration).not.toHaveBeenCalled();
      expectFreshViewWithoutMessage(container);
    });

    it("closes the WebSocket when the instruction request succeeds after 'Start Over'", async () => {
      const container = mountView();
      const instruction = deferred();
      await startGenerating(container, instruction);
      const websocket = await openGenerationWebSocket.mock.results[0].value;

      container.querySelector("[data-action='reset']").click();
      // The reset does not know the socket: the flow has not stored it yet.
      expect(closeGenerationWebSocket).not.toHaveBeenCalledWith(websocket);
      instruction.resolve(undefined);
      await vi.advanceTimersByTimeAsync(0);

      expect(closeGenerationWebSocket).toHaveBeenCalledWith(websocket);
      expectFreshViewWithoutMessage(container);
    });

    it("closes the WebSocket when the instruction request fails after 'Start Over'", async () => {
      const container = mountView();
      const instruction = deferred();
      await startGenerating(container, instruction);
      const websocket = await openGenerationWebSocket.mock.results[0].value;
      const { ApiError } = await import("./api.js");

      container.querySelector("[data-action='reset']").click();
      expect(closeGenerationWebSocket).not.toHaveBeenCalledWith(websocket);
      instruction.reject(new ApiError("Instruction request failed."));
      await vi.advanceTimersByTimeAsync(0);

      expect(closeGenerationWebSocket).toHaveBeenCalledWith(websocket);
      expectFreshViewWithoutMessage(container);
    });

    it("does not close the WebSocket when no 'Start Over' happens", async () => {
      const container = mountView();
      const instruction = deferred();
      await startGenerating(container, instruction);

      instruction.resolve(undefined);
      await vi.advanceTimersByTimeAsync(0);

      expect(closeGenerationWebSocket).not.toHaveBeenCalled();
    });
  });
});
