import { defineConfig } from '@playwright/test'
export default defineConfig({
  testDir: './tests', workers: 1, timeout: 45000,
  use: { baseURL: 'http://127.0.0.1:8787', browserName: 'chromium', channel: 'chrome', headless: true, reducedMotion: 'reduce' },
  outputDir: '/private/tmp/retail-assistant-test-results',
})
