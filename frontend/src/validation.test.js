import { describe, it, expect } from "vitest";
import {
  validateSelectedFiles,
  REQUIRED_FILE_COUNT,
  MAX_FILE_SIZE_BYTES,
} from "./validation.js";

function makeFile({ name = "a.jpg", type = "image/jpeg", size = 1024 } = {}) {
  const file = new File([new Uint8Array(size)], name, { type });
  return file;
}

describe("validateSelectedFiles", () => {
  it("accepts exactly 4 valid images", () => {
    const files = Array.from({ length: REQUIRED_FILE_COUNT }, (_, i) =>
      makeFile({ name: `img${i}.jpg` }),
    );

    const result = validateSelectedFiles(files);

    expect(result.valid).toBe(true);
    expect(result.errors).toEqual([]);
  });

  it("rejects a count other than 4", () => {
    const result = validateSelectedFiles([makeFile(), makeFile()]);

    expect(result.valid).toBe(false);
    expect(result.errors[0]).toMatch(/exactly 4 images/);
  });

  it("rejects unsupported mime types", () => {
    const files = [
      makeFile({ type: "application/pdf" }),
      makeFile(),
      makeFile(),
      makeFile(),
    ];

    const result = validateSelectedFiles(files);

    expect(result.valid).toBe(false);
    expect(result.errors.some((e) => e.includes("unsupported type"))).toBe(true);
  });

  it("rejects files exceeding the size limit", () => {
    const files = [
      makeFile({ size: MAX_FILE_SIZE_BYTES + 1 }),
      makeFile(),
      makeFile(),
      makeFile(),
    ];

    const result = validateSelectedFiles(files);

    expect(result.valid).toBe(false);
    expect(result.errors.some((e) => e.includes("size limit"))).toBe(true);
  });

  it("treats a missing/undefined file list as zero files", () => {
    const result = validateSelectedFiles(undefined);

    expect(result.valid).toBe(false);
    expect(result.errors).toEqual([
      `Please select exactly ${REQUIRED_FILE_COUNT} images (selected 0).`,
    ]);
  });
});
