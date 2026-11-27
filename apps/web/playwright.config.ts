import { defineConfig } from "@playwright/test";

// The UI runs against a stub API (e2e/stub-api.mjs), so these tests need no database.
export default defineConfig({
  testDir: "./e2e",
  reporter: "list",
  use: { baseURL: "http://localhost:3100" },
  webServer: [
    { command: "node e2e/stub-api.mjs", url: "http://localhost:4010/health", reuseExistingServer: false },
    {
      command: "npm run build && npm run start -- -p 3100",
      url: "http://localhost:3100",
      timeout: 180_000,
      env: { AGENTGIT_API_URL: "http://localhost:4010" },
      reuseExistingServer: false,
    },
  ],
});
