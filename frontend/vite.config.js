import { defineConfig } from "vite";

// Frontend is a static site hosted on S3 (no server-side routing/runtime),
// so the build must use relative asset paths and a flat output folder.
export default defineConfig({
  root: "src",
  publicDir: "../public",
  base: "./",
  build: {
    outDir: "../dist",
    emptyOutDir: true,
  },
  server: {
    port: 5173,
  },
});
