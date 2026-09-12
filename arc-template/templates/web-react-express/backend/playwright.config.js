const { defineConfig } = require('@playwright/test');

// The backend hosts the built frontend on the single workspace port. Read it
// from the environment (the same contract `frontend/vite.config.js` uses)
// instead of hardcoding a default, otherwise E2E navigates to a dead origin.
const baseURL = process.env.PLAYWRIGHT_BASE_URL
  || process.env.ARC_WEB_BASE_URL
  || `http://127.0.0.1:${process.env.ARC_WEB_PORT || 3000}`;

module.exports = defineConfig({
  testDir: './test-e2e',
  testMatch: /.*\.(js|jsx|ts|tsx)$/,
  timeout: 30000,
  use: {
    baseURL,
    trace: 'retain-on-failure',
  },
});
