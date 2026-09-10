import { expect, test } from '@playwright/test';

/**
 * Sign-in and route protection (PRD section 15.2).
 *
 * Magic link only, restricted to the institution's domain. The tests here
 * cover the parts a student actually hits when something goes wrong: a wrong
 * address, an expired link, and arriving at a protected page while signed out.
 */

test.describe('sign in', () => {
  test('an unauthenticated visitor is sent to sign in', async ({ page }) => {
    await page.goto('/timeline');
    await expect(page).toHaveURL(/\/signin/);
    await expect(page.getByRole('heading', { name: 'Syllabus Intelligence' })).toBeVisible();
  });

  test('the intended destination is remembered', async ({ page }) => {
    await page.goto('/search');
    // Coming back to where the student was headed matters more than it sounds:
    // a shared link that dumps them on a generic page looks broken.
    await expect(page).toHaveURL(/next=%2Fsearch/);
  });

  test('asks for an email and nothing else', async ({ page }) => {
    await page.goto('/signin');

    await expect(page.getByLabel('Your school email')).toBeVisible();
    // No password field exists, so there is none to leak or reset.
    await expect(page.locator('input[type=password]')).toHaveCount(0);
    await expect(page.getByText('There is no password to remember.')).toBeVisible();
  });

  test('the submit button waits for an address', async ({ page }) => {
    await page.goto('/signin');
    const submit = page.getByRole('button', { name: 'Email me a link' });
    await expect(submit).toBeDisabled();

    await page.getByLabel('Your school email').fill('student@test.edu');
    await expect(submit).toBeEnabled();
  });

  test('confirms which address the link went to', async ({ page }) => {
    await page.route('**/api/v1/auth/magic-link', (route) =>
      route.fulfill({ status: 202, json: { status: 'sent' } }),
    );
    await page.goto('/signin');

    await page.getByLabel('Your school email').fill('student@test.edu');
    await page.getByRole('button', { name: 'Email me a link' }).click();

    // A typo in the address is the likeliest reason nothing arrives, and a
    // student who cannot see what they typed will simply wait.
    await expect(page.getByRole('heading', { name: 'Check your email' })).toBeVisible();
    await expect(page.getByText('student@test.edu')).toBeVisible();
    await expect(page.getByText('University mail filters can be slow', { exact: false })).toBeVisible();
  });

  test('a rejected address explains why', async ({ page }) => {
    await page.route('**/api/v1/auth/magic-link', (route) =>
      route.fulfill({
        status: 400,
        json: { detail: 'Sign-up is limited to @test.edu addresses.' },
      }),
    );
    await page.goto('/signin');

    await page.getByLabel('Your school email').fill('someone@gmail.com');
    await page.getByRole('button', { name: 'Email me a link' }).click();

    await expect(page.getByRole('alert')).toContainText('limited to @test.edu');
  });

  test('a verified link signs the student in and continues', async ({ page }) => {
    await page.route('**/api/v1/auth/verify', (route) =>
      route.fulfill({
        json: {
          access_token: 'token-123',
          user: { id: 'u-1', email: 'student@test.edu', verified: true },
        },
      }),
    );
    await page.route('**/api/v1/timeline*', (route) => route.fulfill({ json: [] }));
    await page.route('**/api/v1/terms/current', (route) => route.fulfill({ json: null }));
    await page.route('**/api/v1/calendar/ics-url', (route) =>
      route.fulfill({ status: 404, json: {} }),
    );

    await page.goto('/signin?token=abc');
    await expect(page).toHaveURL(/\/timeline$/);
  });

  test('an expired link says so and offers another', async ({ page }) => {
    await page.route('**/api/v1/auth/verify', (route) =>
      route.fulfill({ status: 401, json: { detail: 'Token has expired' } }),
    );
    await page.goto('/signin?token=stale');

    await expect(page.getByRole('alert')).toContainText('expired');
    await expect(page.getByRole('button', { name: 'Email me a link' })).toBeVisible();
  });

  test('signing out clears the session', async ({ page }) => {
    await page.addInitScript(() => {
      localStorage.setItem('si.token', 'token-123');
      localStorage.setItem(
        'si.user',
        JSON.stringify({ id: 'u-1', email: 'student@test.edu', verified: true }),
      );
    });
    await page.route('**/api/v1/timeline*', (route) => route.fulfill({ json: [] }));
    await page.route('**/api/v1/terms/current', (route) => route.fulfill({ json: null }));
    await page.route('**/api/v1/calendar/ics-url', (route) =>
      route.fulfill({ status: 404, json: {} }),
    );

    await page.goto('/timeline');
    await page.getByRole('button', { name: 'Sign out' }).click();

    await expect(page).toHaveURL(/\/signin/);
    const token = await page.evaluate(() => localStorage.getItem('si.token'));
    expect(token).toBeNull();
  });

  test('a rejected token ends the session rather than looping', async ({ page }) => {
    await page.addInitScript(() => {
      localStorage.setItem('si.token', 'stale-token');
      localStorage.setItem('si.user', JSON.stringify({ id: 'u-1', email: 'a@test.edu', verified: true }));
    });
    // The interceptor clears a dead token, so the student sees a prompt rather
    // than a sequence of empty screens.
    await page.route('**/api/v1/**', (route) =>
      route.fulfill({ status: 401, json: { detail: 'Not authenticated' } }),
    );

    await page.goto('/timeline');
    await expect(page).toHaveURL(/\/signin/);
  });
});
