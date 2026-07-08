import { describe, it, expect, vi, beforeEach } from "vitest";

const getIdTokenMock = vi.fn();

vi.mock("./auth.js", () => ({
  getIdToken: getIdTokenMock,
}));

const { requestUploadUrls, ApiError } = await import("./api.js");

function jsonResponse(body, { ok = true, status = 200 } = {}) {
  return {
    ok,
    status,
    json: async () => body,
  };
}

const VALID_PAYLOAD = {
  bucket: "my-bucket",
  folder: "uuid-1",
  prefix: "uploads/uuid-1",
  expiresIn: 900,
  uploadItems: Array.from({ length: 4 }, (_, i) => ({
    uploadUrl: `https://s3.example.com/file_${i + 1}`,
    key: `uploads/uuid-1/file_${i + 1}.jpg`,
    fileName: `file_${i + 1}.jpg`,
    contentType: "image/jpeg",
  })),
};

describe("requestUploadUrls", () => {
  beforeEach(() => {
    getIdTokenMock.mockReset();
    getIdTokenMock.mockResolvedValue("id-token-value");
  });

  it("posts file names/content types with the Cognito ID token as Authorization header", async () => {
    const fetchImpl = vi.fn().mockResolvedValue(jsonResponse(VALID_PAYLOAD));

    const result = await requestUploadUrls(
      { fileNames: ["a.jpg"], contentTypes: ["image/jpeg"] },
      { baseUrl: "https://api.example.com", fetchImpl },
    );

    expect(fetchImpl).toHaveBeenCalledWith(
      "https://api.example.com/upload-urls",
      expect.objectContaining({
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Accept: "application/json",
          Authorization: "Bearer id-token-value",
        },
        body: JSON.stringify({ fileNames: ["a.jpg"], contentTypes: ["image/jpeg"] }),
      }),
    );
    expect(result).toEqual(VALID_PAYLOAD);
  });

  it("omits the Authorization header when no user is signed in", async () => {
    getIdTokenMock.mockResolvedValue(null);
    const fetchImpl = vi.fn().mockResolvedValue(jsonResponse(VALID_PAYLOAD));

    await requestUploadUrls(
      { fileNames: ["a.jpg"], contentTypes: ["image/jpeg"] },
      { baseUrl: "https://api.example.com", fetchImpl },
    );

    expect(fetchImpl).toHaveBeenCalledWith(
      "https://api.example.com/upload-urls",
      expect.objectContaining({
        headers: { "Content-Type": "application/json", Accept: "application/json" },
      }),
    );
  });

  it("throws ApiError on network failure", async () => {
    const fetchImpl = vi.fn().mockRejectedValue(new TypeError("network down"));

    await expect(
      requestUploadUrls({ fileNames: [], contentTypes: [] }, { fetchImpl }),
    ).rejects.toBeInstanceOf(ApiError);
  });

  it("throws ApiError with the server message on non-2xx response", async () => {
    const fetchImpl = vi
      .fn()
      .mockResolvedValue(jsonResponse({ error: "boom" }, { ok: false, status: 500 }));

    await expect(
      requestUploadUrls({ fileNames: [], contentTypes: [] }, { fetchImpl }),
    ).rejects.toMatchObject({ message: "boom", status: 500 });
  });

  it("throws ApiError when fewer than 4 upload items are returned", async () => {
    const fetchImpl = vi
      .fn()
      .mockResolvedValue(jsonResponse({ ...VALID_PAYLOAD, uploadItems: [] }));

    await expect(
      requestUploadUrls({ fileNames: [], contentTypes: [] }, { fetchImpl }),
    ).rejects.toBeInstanceOf(ApiError);
  });

  it("throws ApiError when an upload item is missing its URL", async () => {
    const badPayload = {
      ...VALID_PAYLOAD,
      uploadItems: [
        { ...VALID_PAYLOAD.uploadItems[0], uploadUrl: "" },
        ...VALID_PAYLOAD.uploadItems.slice(1),
      ],
    };
    const fetchImpl = vi.fn().mockResolvedValue(jsonResponse(badPayload));

    await expect(
      requestUploadUrls({ fileNames: [], contentTypes: [] }, { fetchImpl }),
    ).rejects.toBeInstanceOf(ApiError);
  });
});
