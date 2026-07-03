import { defineConfig } from "vitest/config";

// Separate from vite.config.js (which sets root: "src" for the app build) so
// test discovery runs from the frontend/ root and can pick up src/**/*.test.js.
export default defineConfig({
  test: {
    environment: "jsdom",
    include: ["src/**/*.test.js"],
  },
});
