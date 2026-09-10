import { defineConfig, devices } from '@playwright/test';

/**
 * End-to-end configuration (PRD section 27).
 *
 * The suite stubs the API at the network boundary rather than running against
 * a live backend. That is deliberate: 234 Python tests already cover what the
 * API returns, and what is untested is how the interface behaves *given* a
 * response. Stubbing lets each test arrange the exact state it cares about,
 * including the ones that are awkward to produce with real data (a
 * low-confidence item, a week band, a document with nothing in it), and it
 * means the suite runs in CI without Postgres, MinIO, or Redis.
 *
 * Section 27 asks for the upload through review to timeline flow at minimum.
 * `accessibility.spec.ts` goes further and turns the design rules into
 * assertions, because contrast and target-size regressions are invisible in
 * review and only show up when someone cannot use the product.
 */
export default defineConfig({
  testDir: './e2e',
  fullyParallel: true,
  forbidOnly: !!process.env['CI'],
  retries: process.env['CI'] ? 1 : 0,
  reporter: process.env['CI'] ? [['github'], ['list']] : 'list',

  use: {
    baseURL: 'http://localhost:4200',
    // Artifacts only for failures, so a green run leaves nothing behind.
    trace: 'on-first-retry',
    screenshot: 'only-on-failure',
  },

  projects: [
    {
      name: 'desktop',
      use: { ...devices['Desktop Chrome'] },
    },
    {
      // The PRD targets students on phones as much as laptops, and the layout
      // switches navigation patterns below 40rem, so that path needs coverage.
      name: 'mobile',
      use: { ...devices['Pixel 7'] },
    },
  ],

  webServer: {
    command: 'npm start',
    url: 'http://localhost:4200',
    reuseExistingServer: !process.env['CI'],
    timeout: 180_000,
  },
});
