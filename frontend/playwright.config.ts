// Headless UI tests (Chromium) against the real backend with a throwaway data folder (e2e/server.py).
// Run: npm run e2e   (builds the UI first). No screenshots unless a test fails.
import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "e2e",
  timeout: 90_000,
  workers: 1,
  reporter: [["list"]],
  use: {
    baseURL: "http://127.0.0.1:8799",
    headless: true,
    viewport: { width: 1440, height: 900 },
    screenshot: "only-on-failure",
    trace: "off",
  },
  webServer: {
    command: "..\\.venv\\Scripts\\python.exe e2e\\server.py",
    url: "http://127.0.0.1:8799/api/version",
    timeout: 120_000,
    reuseExistingServer: false,
  },
});
