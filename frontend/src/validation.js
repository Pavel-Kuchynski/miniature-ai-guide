// Pure validation helpers for the reference-image selection step.
// Kept framework-free and side-effect-free so they're trivially testable.

export const REQUIRED_FILE_COUNT = 4;

export const ACCEPTED_MIME_TYPES = [
  "image/jpeg",
  "image/png",
  "image/webp",
  "image/gif",
];

export const MAX_FILE_SIZE_BYTES = 15 * 1024 * 1024; // 15 MB per image

/**
 * Validate a list of selected files against the upload requirements
 * (exactly 4 images, accepted type, size limit).
 *
 * @param {FileList | File[] | null | undefined} files
 * @returns {{ valid: boolean, errors: string[] }}
 */
export function validateSelectedFiles(files) {
  const list = Array.from(files ?? []);
  const errors = [];

  if (list.length !== REQUIRED_FILE_COUNT) {
    errors.push(
      `Please select exactly ${REQUIRED_FILE_COUNT} images (selected ${list.length}).`,
    );
  }

  list.forEach((file, index) => {
    if (!ACCEPTED_MIME_TYPES.includes(file.type)) {
      errors.push(
        `Image ${index + 1} (${file.name}) has an unsupported type: ${file.type || "unknown"}.`,
      );
    }
    if (file.size > MAX_FILE_SIZE_BYTES) {
      errors.push(
        `Image ${index + 1} (${file.name}) exceeds the ${Math.round(
          MAX_FILE_SIZE_BYTES / (1024 * 1024),
        )}MB size limit.`,
      );
    }
  });

  return { valid: errors.length === 0, errors };
}
