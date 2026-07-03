import { describe, it, expect, vi, beforeEach } from "vitest";

vi.mock("./api.js", () => {
  class ApiError extends Error {
    constructor(message, { status = null } = {}) {
      super(message);
      this.name = "ApiError";
      this.status = status;
    }
  }
  return { requestUploadUrls: vi.fn(), ApiError };
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

import { requestUploadUrls } from "./api.js";
import { putFileToUrl } from "./uploadClient.js";
import { mountUploadView } from "./uploadView.js";

function makeFile(name) {
  return new File(["data"], name, { type: "image/jpeg" });
}

function selectFiles(container, files) {
  const input = container.querySelector("[data-role='file-input']");
  Object.defineProperty(input, "files", { value: files, configurable: true });
  input.dispatchEvent(new Event("change", { bubbles: true }));
}

beforeEach(() => {
  vi.clearAllMocks();
  global.URL.createObjectURL = vi.fn(() => "blob:mock");
  global.URL.revokeObjectURL = vi.fn();
});

describe("mountUploadView", () => {
  it("disables the upload button until exactly 4 valid files are selected", () => {
    const container = document.createElement("div");
    mountUploadView(container);

    selectFiles(container, [makeFile("a.jpg"), makeFile("b.jpg")]);

    const button = container.querySelector("[data-action='start-upload']");
    expect(button.disabled).toBe(true);
    expect(container.querySelector(".error-list").textContent).toMatch(/exactly 4 images/);
  });

  it("hides the file selector when 4 valid files are selected", () => {
    const container = document.createElement("div");
    mountUploadView(container);

    const files = [makeFile("a.jpg"), makeFile("b.jpg"), makeFile("c.jpg"), makeFile("d.jpg")];
    selectFiles(container, files);

    expect(container.querySelector(".upload-selector")).toBeNull();
    expect(container.querySelector(".preview-grid")).not.toBeNull();
    expect(container.querySelector("[data-action='start-upload']").disabled).toBe(false);
  });

  it("allows removing an image from the selection by clicking the remove button", () => {
    const container = document.createElement("div");
    mountUploadView(container);

    const files = [makeFile("a.jpg"), makeFile("b.jpg"), makeFile("c.jpg"), makeFile("d.jpg")];
    selectFiles(container, files);

    const removeBtn = container.querySelector("[data-action='remove-image']");
    expect(removeBtn).not.toBeNull();

    removeBtn.click();

    expect(container.querySelector(".upload-selector")).not.toBeNull();
    const previews = container.querySelectorAll(".preview-item");
    expect(previews.length).toBe(3);
  });

  it("shows all 4 remove buttons when 4 images are selected", () => {
    const container = document.createElement("div");
    mountUploadView(container);

    const files = [makeFile("a.jpg"), makeFile("b.jpg"), makeFile("c.jpg"), makeFile("d.jpg")];
    selectFiles(container, files);

    const removeButtons = container.querySelectorAll("[data-action='remove-image']");
    expect(removeButtons.length).toBe(4);
  });

  it("uploads all 4 files and shows the success state", async () => {
    const container = document.createElement("div");
    mountUploadView(container);

    const files = [makeFile("a.jpg"), makeFile("b.jpg"), makeFile("c.jpg"), makeFile("d.jpg")];
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

    expect(putFileToUrl).toHaveBeenCalledTimes(4);
    expect(container.querySelector(".upload-success")).not.toBeNull();
    expect(container.textContent).toMatch(/uuid-1/);
  });

  it("shows a retry button for a file whose upload fails", async () => {
    const container = document.createElement("div");
    mountUploadView(container);

    const files = [makeFile("a.jpg"), makeFile("b.jpg"), makeFile("c.jpg"), makeFile("d.jpg")];
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
      url.endsWith("/1") ? Promise.reject(new Error("S3 rejected the upload (HTTP 403).")) : Promise.resolve(),
    );

    container.querySelector("[data-action='start-upload']").click();

    await new Promise((resolve) => setTimeout(resolve, 0));
    await new Promise((resolve) => setTimeout(resolve, 0));

    expect(container.querySelector("[data-action='retry-item']")).not.toBeNull();
    expect(container.querySelector(".upload-success")).toBeNull();
  });

  it("surfaces an API error and returns to the select phase", async () => {
    const container = document.createElement("div");
    mountUploadView(container);

    const files = [makeFile("a.jpg"), makeFile("b.jpg"), makeFile("c.jpg"), makeFile("d.jpg")];
    selectFiles(container, files);

    const { ApiError } = await import("./api.js");
    requestUploadUrls.mockRejectedValue(new ApiError("Network error while requesting upload URLs."));

    container.querySelector("[data-action='start-upload']").click();

    await new Promise((resolve) => setTimeout(resolve, 0));

    expect(container.querySelector(".error-banner").textContent).toMatch(/Network error/);
    expect(container.querySelector("[data-action='start-upload']").disabled).toBe(false);
  });

  it("hides the file selector after all files upload successfully", async () => {
    const container = document.createElement("div");
    mountUploadView(container);

    const files = [makeFile("a.jpg"), makeFile("b.jpg"), makeFile("c.jpg"), makeFile("d.jpg")];
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

    const files = [makeFile("a.jpg"), makeFile("b.jpg"), makeFile("c.jpg"), makeFile("d.jpg")];
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

    const files = [makeFile("a.jpg"), makeFile("b.jpg"), makeFile("c.jpg"), makeFile("d.jpg")];
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
    putFileToUrl.mockResolvedValueOnce(undefined);

    container.querySelector("[data-action='start-upload']").click();

    await new Promise((resolve) => setTimeout(resolve, 0));
    await new Promise((resolve) => setTimeout(resolve, 0));

    expect(container.querySelector("[data-action='retry-item']")).not.toBeNull();
    expect(putFileToUrl).toHaveBeenCalledTimes(4);

    container.querySelector("[data-action='retry-item']").click();

    await new Promise((resolve) => setTimeout(resolve, 0));

    expect(putFileToUrl).toHaveBeenCalledTimes(5);
  });

  it("shows validation error when trying to upload with invalid files", async () => {
    const container = document.createElement("div");
    mountUploadView(container);

    const files = [makeFile("a.jpg"), makeFile("b.jpg"), makeFile("c.jpg")];
    selectFiles(container, files);

    container.querySelector("[data-action='start-upload']").click();

    await new Promise((resolve) => setTimeout(resolve, 0));

    expect(container.querySelector(".error-list")).not.toBeNull();
    expect(requestUploadUrls).not.toHaveBeenCalled();
  });

  it("calls onProgress callback during upload", async () => {
    const container = document.createElement("div");
    mountUploadView(container);

    const files = [makeFile("a.jpg"), makeFile("b.jpg"), makeFile("c.jpg"), makeFile("d.jpg")];
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

    expect(onProgressCallbacks.length).toBe(4);
  });
});
