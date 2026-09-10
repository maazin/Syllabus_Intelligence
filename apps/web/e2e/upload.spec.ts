import { expect, stubApi, test } from './fixtures';

/**
 * Upload (PRD section 6.1, US-1).
 *
 * The requirement worth protecting is that one file failing never blocks the
 * others. That is easy to get wrong, because a single try/catch around the
 * whole batch is the shorter code, and it fails in a way nobody notices until
 * a student uploads five syllabi during week one and gets nothing back.
 */

const PDF = Buffer.from('%PDF-1.7\nfake syllabus for testing\n');

test.describe('upload', () => {
  test.beforeEach(async ({ page }) => {
    await stubApi(page);
    await page.goto('/upload');
  });

  test('accepts files and lists them before uploading', async ({ page }) => {
    await page.setInputFiles('input[type=file]', {
      name: 'syllabus.pdf',
      mimeType: 'application/pdf',
      buffer: PDF,
    });

    await expect(page.getByText('syllabus.pdf')).toBeVisible();
    await expect(page.getByText('Ready to upload')).toBeVisible();
  });

  test('one bad file does not block the others', async ({ page }) => {
    await page.route('**/api/v1/documents', (route) =>
      route.fulfill({
        status: 202,
        json: [
          { document_id: 'd-1', filename: 'good.pdf', status: 'queued', deduped: false, error: null },
          {
            document_id: null,
            filename: 'broken.pdf',
            status: 'failed',
            deduped: false,
            error: 'We could not read this file.',
          },
        ],
      }),
    );

    await page.setInputFiles('input[type=file]', [
      { name: 'good.pdf', mimeType: 'application/pdf', buffer: PDF },
      { name: 'broken.pdf', mimeType: 'application/pdf', buffer: PDF },
    ]);
    await page.getByRole('button', { name: /Read these syllabi/ }).click();

    // Both outcomes are reported, each next to the file that produced it.
    await expect(page.getByText('Reading', { exact: true })).toBeVisible();
    await expect(page.getByText('Could not read', { exact: true })).toBeVisible();
    await expect(page.getByText('We could not read this file.')).toBeVisible();
  });

  test('a deduplicated upload is ready immediately', async ({ page }) => {
    await page.route('**/api/v1/documents', (route) =>
      route.fulfill({
        status: 202,
        json: [
          { document_id: 'd-1', filename: 'shared.pdf', status: 'ready', deduped: true, error: null },
        ],
      }),
    );

    await page.setInputFiles('input[type=file]', {
      name: 'shared.pdf',
      mimeType: 'application/pdf',
      buffer: PDF,
    });
    await page.getByRole('button', { name: /Read this syllabus/ }).click();

    // Section 18.3: someone in the section already uploaded these bytes, so
    // this student skips the wait entirely. Saying so is the experience win.
    await expect(page.getByText('Ready', { exact: true })).toBeVisible();
    await expect(
      page.getByText('Someone in your section already uploaded this', { exact: false }),
    ).toBeVisible();
  });

  test('rejects a file over the size limit before sending it', async ({ page }) => {
    await page.setInputFiles('input[type=file]', {
      name: 'huge.pdf',
      mimeType: 'application/pdf',
      buffer: Buffer.alloc(26 * 1024 * 1024),
    });

    await expect(page.getByText('Could not read', { exact: true })).toBeVisible();
    await expect(page.getByText('Larger than 25 MB')).toBeVisible();
  });

  test('caps the batch at ten files and says what it did', async ({ page }) => {
    const files = Array.from({ length: 12 }, (_, i) => ({
      name: `s${i}.pdf`,
      mimeType: 'application/pdf',
      buffer: PDF,
    }));
    await page.setInputFiles('input[type=file]', files);

    await expect(page.getByRole('alert')).toContainText('first 10');
  });

  test('a queued file can be removed before upload', async ({ page }) => {
    await page.setInputFiles('input[type=file]', {
      name: 'syllabus.pdf',
      mimeType: 'application/pdf',
      buffer: PDF,
    });
    await page.getByRole('button', { name: 'Remove syllabus.pdf' }).click();
    await expect(page.getByText('syllabus.pdf')).toBeHidden();
  });

  test('states what happens to the file before asking for it', async ({ page }) => {
    // Section 15.1 and 15.2. Privacy stated on the screen where the student
    // hands over a document, not buried in a policy page.
    await expect(page.getByText('Your upload stays private to you.', { exact: false })).toBeVisible();
    await expect(
      page.getByText('Nothing reaches your calendar until you have checked', { exact: false }),
    ).toBeVisible();
  });
});
