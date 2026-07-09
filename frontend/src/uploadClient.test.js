import { describe, it, expect, vi, beforeEach } from "vitest";
import { putFileToUrl, UploadError } from "./uploadClient.js";

class MockXHR {
  constructor() {
    this.upload = {};
    this.requestHeaders = {};
    MockXHR.instances.push(this);
  }

  open(method, url) {
    this.method = method;
    this.url = url;
  }

  setRequestHeader(key, value) {
    this.requestHeaders[key] = value;
  }

  send(body) {
    this.sentBody = body;
  }

  abort() {
    this.aborted = true;
    this.onabort?.();
  }

  respond(status) {
    this.status = status;
    this.onload?.();
  }

  fail() {
    this.onerror?.();
  }

  progress(loaded, total) {
    this.upload.onprogress?.({ lengthComputable: true, loaded, total });
  }
}

beforeEach(() => {
  MockXHR.instances = [];
  global.XMLHttpRequest = MockXHR;
});

describe("putFileToUrl", () => {
  it("resolves and reports 100% progress on a 2xx response", async () => {
    const onProgress = vi.fn();
    const file = new File(["data"], "a.jpg", { type: "image/jpeg" });

    const promise = putFileToUrl("https://s3.example.com/put", file, {
      onProgress,
    });
    const xhr = MockXHR.instances[0];

    xhr.progress(50, 100);
    xhr.respond(200);

    await expect(promise).resolves.toBeUndefined();
    expect(onProgress).toHaveBeenCalledWith(50);
    expect(onProgress).toHaveBeenCalledWith(100);
    expect(xhr.requestHeaders["Content-Type"]).toBe("image/jpeg");
  });

  it("rejects with UploadError on a non-2xx response", async () => {
    const file = new File(["data"], "a.jpg", { type: "image/jpeg" });

    const promise = putFileToUrl("https://s3.example.com/put", file);
    MockXHR.instances[0].respond(403);

    await expect(promise).rejects.toBeInstanceOf(UploadError);
    await expect(promise).rejects.toMatchObject({ status: 403 });
  });

  it("rejects with UploadError on a network error", async () => {
    const file = new File(["data"], "a.jpg", { type: "image/jpeg" });

    const promise = putFileToUrl("https://s3.example.com/put", file);
    MockXHR.instances[0].fail();

    await expect(promise).rejects.toBeInstanceOf(UploadError);
  });

  it("rejects immediately if the signal is already aborted", async () => {
    const file = new File(["data"], "a.jpg", { type: "image/jpeg" });
    const controller = new AbortController();
    controller.abort();

    await expect(
      putFileToUrl("https://s3.example.com/put", file, {
        signal: controller.signal,
      }),
    ).rejects.toBeInstanceOf(UploadError);
  });

  it("aborts the underlying request when the signal fires mid-upload", async () => {
    const file = new File(["data"], "a.jpg", { type: "image/jpeg" });
    const controller = new AbortController();

    const promise = putFileToUrl("https://s3.example.com/put", file, {
      signal: controller.signal,
    });
    controller.abort();

    await expect(promise).rejects.toBeInstanceOf(UploadError);
    expect(MockXHR.instances[0].aborted).toBe(true);
  });
});
