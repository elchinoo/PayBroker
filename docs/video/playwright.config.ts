import { defineConfig } from '@playwright/test';

/**
 * Real Chrome (the machine's installed Google Chrome, no browser download), 1920x1080, video on.
 * Videos land in docs/video/out/ (git-ignored). See record.spec.ts for the environment it needs.
 */
export default defineConfig({
  testDir: '.',
  testMatch: 'record.spec.ts',
  outputDir: './out/raw',
  workers: 1,
  retries: 0,
  timeout: 20 * 60_000,
  reporter: [['list']],
  use: {
    channel: 'chrome',
    headless: !process.env.HEADED,
    viewport: { width: 1920, height: 1080 },
    deviceScaleFactor: 1,
    // needs Playwright's ffmpeg once: `npx playwright install ffmpeg`. REC_VIDEO=off rehearses the actions without encoding.
    video: process.env.REC_VIDEO === 'off' ? 'off' : { mode: 'on', size: { width: 1920, height: 1080 } },
    screenshot: 'off',
    trace: 'off',
    actionTimeout: 15_000,
    navigationTimeout: 30_000,
  },
});
