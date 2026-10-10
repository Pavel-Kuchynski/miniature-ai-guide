import { describe, it, expect, vi, beforeEach } from "vitest";

const getIdTokenMock = vi.fn();

vi.mock("./auth.js", () => ({
  getIdToken: getIdTokenMock,
}));

const {
  requestUploadUrls,
  createJob,
  requestInstructionGeneration,
  fetchResultImage,
  fetchResultColors,
  ApiError,
} = await import("./api.js");

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
  uploadItems: Array.from({ length: 1 }, (_, i) => ({
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
        body: JSON.stringify({
          fileNames: ["a.jpg"],
          contentTypes: ["image/jpeg"],
        }),
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
        headers: {
          "Content-Type": "application/json",
          Accept: "application/json",
        },
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
      .mockResolvedValue(
        jsonResponse({ error: "boom" }, { ok: false, status: 500 }),
      );

    await expect(
      requestUploadUrls({ fileNames: [], contentTypes: [] }, { fetchImpl }),
    ).rejects.toMatchObject({ message: "boom", status: 500 });
  });

  it("throws ApiError when the response does not contain exactly 1 upload item", async () => {
    const fetchImpl = vi
      .fn()
      .mockResolvedValue(jsonResponse({ ...VALID_PAYLOAD, uploadItems: [] }));

    await expect(
      requestUploadUrls({ fileNames: [], contentTypes: [] }, { fetchImpl }),
    ).rejects.toBeInstanceOf(ApiError);
  });

  it("throws ApiError when more than 1 upload item is returned", async () => {
    const extra = VALID_PAYLOAD.uploadItems[0];
    const fetchImpl = vi
      .fn()
      .mockResolvedValue(
        jsonResponse({ ...VALID_PAYLOAD, uploadItems: [extra, extra] }),
      );

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

describe("createJob", () => {
  beforeEach(() => {
    getIdTokenMock.mockReset();
    getIdTokenMock.mockResolvedValue("id-token-value");
  });

  it("sends a PUT request with jobId and Cognito ID token", async () => {
    const fetchImpl = vi.fn().mockResolvedValue(jsonResponse({}));

    await createJob(
      { jobId: "job-123" },
      { baseUrl: "https://api.example.com", fetchImpl },
    );

    expect(fetchImpl).toHaveBeenCalledWith(
      "https://api.example.com/jobs",
      expect.objectContaining({
        method: "PUT",
        headers: {
          "Content-Type": "application/json",
          Accept: "application/json",
          Authorization: "Bearer id-token-value",
        },
        body: JSON.stringify({ jobId: "job-123" }),
      }),
    );
  });

  it("omits the Authorization header when no user is signed in", async () => {
    getIdTokenMock.mockResolvedValue(null);
    const fetchImpl = vi.fn().mockResolvedValue(jsonResponse({}));

    await createJob(
      { jobId: "job-123" },
      { baseUrl: "https://api.example.com", fetchImpl },
    );

    expect(fetchImpl).toHaveBeenCalledWith(
      "https://api.example.com/jobs",
      expect.objectContaining({
        headers: {
          "Content-Type": "application/json",
          Accept: "application/json",
        },
      }),
    );
  });

  it("throws ApiError on network failure", async () => {
    const fetchImpl = vi.fn().mockRejectedValue(new TypeError("network down"));

    await expect(
      createJob({ jobId: "job-123" }, { fetchImpl }),
    ).rejects.toBeInstanceOf(ApiError);
  });

  it("throws ApiError with the server message on non-2xx response", async () => {
    const fetchImpl = vi
      .fn()
      .mockResolvedValue(
        jsonResponse({ error: "Invalid job ID" }, { ok: false, status: 400 }),
      );

    await expect(
      createJob({ jobId: "invalid" }, { fetchImpl }),
    ).rejects.toMatchObject({ message: "Invalid job ID", status: 400 });
  });

  it("throws ApiError with generic message if server response is not JSON", async () => {
    const fetchImpl = vi.fn().mockResolvedValue({
      ok: false,
      status: 500,
      json: async () => {
        throw new Error("not json");
      },
    });

    await expect(
      createJob({ jobId: "job-123" }, { fetchImpl }),
    ).rejects.toMatchObject({
      status: 500,
    });
  });

  it("succeeds with a 2xx response", async () => {
    const fetchImpl = vi.fn().mockResolvedValue(jsonResponse({}));

    await createJob(
      { jobId: "job-123" },
      { baseUrl: "https://api.example.com", fetchImpl },
    );

    expect(fetchImpl).toHaveBeenCalled();
  });
});

describe("requestInstructionGeneration", () => {
  beforeEach(() => {
    getIdTokenMock.mockReset();
    getIdTokenMock.mockResolvedValue("id-token-value");
  });

  it("sends a POST request with an empty body to /jobs/<jobId>/instruction", async () => {
    const fetchImpl = vi.fn().mockResolvedValue(jsonResponse({}));

    await requestInstructionGeneration(
      { jobId: "job-123" },
      { baseUrl: "https://api.example.com", fetchImpl },
    );

    expect(fetchImpl).toHaveBeenCalledWith(
      "https://api.example.com/jobs/job-123/instruction",
      expect.objectContaining({
        method: "POST",
        headers: {
          Accept: "application/json",
          Authorization: "Bearer id-token-value",
        },
      }),
    );
    expect(fetchImpl.mock.calls[0][1].body).toBeUndefined();
  });

  it("URL-encodes the jobId path segment", async () => {
    const fetchImpl = vi.fn().mockResolvedValue(jsonResponse({}));

    await requestInstructionGeneration(
      { jobId: "job 123/x" },
      { baseUrl: "https://api.example.com", fetchImpl },
    );

    expect(fetchImpl).toHaveBeenCalledWith(
      "https://api.example.com/jobs/job%20123%2Fx/instruction",
      expect.anything(),
    );
  });

  it("omits the Authorization header when no user is signed in", async () => {
    getIdTokenMock.mockResolvedValue(null);
    const fetchImpl = vi.fn().mockResolvedValue(jsonResponse({}));

    await requestInstructionGeneration(
      { jobId: "job-123" },
      { baseUrl: "https://api.example.com", fetchImpl },
    );

    expect(fetchImpl).toHaveBeenCalledWith(
      "https://api.example.com/jobs/job-123/instruction",
      expect.objectContaining({
        headers: { Accept: "application/json" },
      }),
    );
  });

  it("throws ApiError on network failure", async () => {
    const fetchImpl = vi.fn().mockRejectedValue(new TypeError("network down"));

    await expect(
      requestInstructionGeneration({ jobId: "job-123" }, { fetchImpl }),
    ).rejects.toBeInstanceOf(ApiError);
  });

  it("throws ApiError with the server message on non-2xx response", async () => {
    const fetchImpl = vi
      .fn()
      .mockResolvedValue(
        jsonResponse({ error: "Conflict" }, { ok: false, status: 409 }),
      );

    await expect(
      requestInstructionGeneration({ jobId: "job-123" }, { fetchImpl }),
    ).rejects.toMatchObject({ message: "Conflict", status: 409 });
  });

  it("throws ApiError with generic message if server response is not JSON", async () => {
    const fetchImpl = vi.fn().mockResolvedValue({
      ok: false,
      status: 500,
      json: async () => {
        throw new Error("not json");
      },
    });

    await expect(
      requestInstructionGeneration({ jobId: "job-123" }, { fetchImpl }),
    ).rejects.toMatchObject({ status: 500 });
  });

  it("succeeds with a 2xx response", async () => {
    const fetchImpl = vi.fn().mockResolvedValue(jsonResponse({}));

    await requestInstructionGeneration(
      { jobId: "job-123" },
      { baseUrl: "https://api.example.com", fetchImpl },
    );

    expect(fetchImpl).toHaveBeenCalled();
  });
});

describe("fetchResultImage", () => {
  it("returns the image blob without sending an Authorization header", async () => {
    const blob = new Blob(["png"]);
    const fetchImpl = vi.fn().mockResolvedValue({ ok: true, status: 200, blob: async () => blob });

    await expect(fetchResultImage("https://s3/image.png", { fetchImpl })).resolves.toBe(blob);
    expect(fetchImpl).toHaveBeenCalledWith("https://s3/image.png");
  });

  it("throws ApiError with the status on a non-2xx response", async () => {
    const fetchImpl = vi.fn().mockResolvedValue({ ok: false, status: 403 });

    await expect(fetchResultImage("https://s3/image.png", { fetchImpl })).rejects.toMatchObject({
      name: "ApiError",
      status: 403,
    });
  });

  it("throws ApiError on a network failure", async () => {
    const fetchImpl = vi.fn().mockRejectedValue(new TypeError("Failed to fetch"));

    await expect(fetchResultImage("https://s3/image.png", { fetchImpl })).rejects.toBeInstanceOf(
      ApiError,
    );
  });
});

describe("fetchResultColors", () => {
  const fetchWith = (body) =>
    vi.fn().mockResolvedValue({ ok: true, status: 200, json: async () => body });

  it("returns details with paint normalized to #RRGGBB", async () => {
    const fetchImpl = fetchWith({
      colors: [
        { detail: "armor", paint: "2e4a6b" },
        { detail: "cloak", paint: "#8B1A1A" },
      ],
    });

    await expect(fetchResultColors("https://s3/result.json", { fetchImpl })).resolves.toEqual([
      { detail: "armor", paint: "#2E4A6B" },
      { detail: "cloak", paint: "#8B1A1A" },
    ]);
  });

  it.each([
    ["missing colors", {}],
    ["colors not an array", { colors: "x" }],
    ["invalid hex", { colors: [{ detail: "a", paint: "red;background:url(x)" }] }],
    ["blank detail", { colors: [{ detail: " ", paint: "112233" }] }],
    ["null entry", { colors: [null] }],
  ])("throws ApiError for malformed payload: %s", async (_name, body) => {
    await expect(
      fetchResultColors("https://s3/result.json", { fetchImpl: fetchWith(body) }),
    ).rejects.toBeInstanceOf(ApiError);
  });

  it("throws ApiError when the body is not valid JSON", async () => {
    const fetchImpl = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => {
        throw new SyntaxError("bad");
      },
    });

    await expect(fetchResultColors("https://s3/result.json", { fetchImpl })).rejects.toBeInstanceOf(
      ApiError,
    );
  });

  it("throws ApiError with the status on a non-2xx response", async () => {
    const fetchImpl = vi.fn().mockResolvedValue({ ok: false, status: 404 });

    await expect(fetchResultColors("https://s3/result.json", { fetchImpl })).rejects.toMatchObject({
      status: 404,
    });
  });
});
