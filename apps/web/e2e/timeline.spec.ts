import { expect, heatmapCell, item, stubApi, test, undated, weekOnly } from './fixtures';

/**
 * Timeline and heatmap (PRD sections 6.3, 11; US-5 and US-6).
 */

test.describe('timeline', () => {
  test('merges courses into one list grouped by month', async ({ page }) => {
    await stubApi(page, {
      timeline: [
        item({ title: 'PS 1', course: 'COP 4530', due_at: '2026-09-04T23:59:00-04:00' }),
        item({ title: 'Essay', course: 'ENG 3014', due_at: '2026-10-02T23:59:00-04:00' }),
      ],
    });
    await page.goto('/timeline');

    await expect(page.getByRole('heading', { name: 'September 2026' })).toBeVisible();
    await expect(page.getByRole('heading', { name: 'October 2026' })).toBeVisible();
    await expect(page.getByText('COP 4530').first()).toBeVisible();
    await expect(page.getByText('ENG 3014').first()).toBeVisible();
  });

  test('filters to a single course', async ({ page }) => {
    await stubApi(page, {
      timeline: [
        item({ title: 'PS 1', course: 'COP 4530' }),
        item({ title: 'Essay', course: 'ENG 3014', due_at: '2026-10-02T23:59:00-04:00' }),
      ],
    });
    await page.goto('/timeline');

    await page.getByRole('button', { name: 'ENG 3014' }).click();
    // Scoped to the month list: the title also appears in the "Coming up"
    // summary, and an unscoped match would be ambiguous rather than wrong.
    await expect(page.locator('.entry__title', { hasText: 'Essay' })).toBeVisible();
    await expect(page.locator('.entry__title', { hasText: 'PS 1' })).toHaveCount(0);
  });

  test('undated items sit apart from the dated ones', async ({ page }) => {
    await stubApi(page, { timeline: [item(), undated()] });
    await page.goto('/timeline');

    // Section 9.3: they stay visible without being placed on a day nobody knows.
    await expect(page.getByRole('heading', { name: 'No date announced' })).toBeVisible();
    await expect(page.getByText('Guest Lecture Response')).toBeVisible();
  });

  test('a week band renders as a week on the timeline too', async ({ page }) => {
    await stubApi(page, { timeline: [weekOnly()] });
    await page.goto('/timeline');
    await expect(page.getByText(/Week of/).first()).toBeVisible();
  });

  test('offers the calendar feed with a copyable link', async ({ page }) => {
    await stubApi(page, { timeline: [item()] });
    await page.goto('/timeline');

    await expect(page.getByRole('heading', { name: 'Put this in your own calendar' })).toBeVisible();
    await expect(page.getByText('/api/v1/calendar/ics/abc123', { exact: false })).toBeVisible();
  });

  test('an empty semester points at the next action', async ({ page }) => {
    await stubApi(page, { timeline: [] });
    await page.goto('/timeline');

    await expect(page.getByRole('heading', { name: 'Nothing here yet' })).toBeVisible();
    await expect(page.getByRole('link', { name: 'Upload a syllabus' })).toBeVisible();
  });
});

test.describe('heatmap', () => {
  const term = [
    heatmapCell('2026-08-24', 6),
    heatmapCell('2026-08-31', 9),
    heatmapCell('2026-09-07', 22, [
      {
        kind: 'major_collision',
        severity: 'red',
        explanation: 'Week of Sep 7: 3 major items across 3 courses, all within 4 days.',
        assessment_ids: ['a-1', 'a-2', 'a-3'],
      },
    ]),
  ];

  test('prints hours on every cell rather than relying on color', async ({ page }) => {
    await stubApi(page, { timeline: [item()], heatmap: term });
    await page.goto('/timeline');

    // HIG charting: a color ramp alone is unreadable in grayscale and for a
    // color-blind reader, so the number is always written out.
    await expect(page.getByText('6h')).toBeVisible();
    await expect(page.getByText('9h')).toBeVisible();
    await expect(page.getByText('22h')).toBeVisible();
  });

  test('labels the estimate as an estimate', async ({ page }) => {
    await stubApi(page, { timeline: [item()], heatmap: term });
    await page.goto('/timeline');

    // Section 11.5: the constants are guesses, so the UI must not imply
    // precision it does not have.
    await expect(
      page.getByText('Treat these as a rough guide rather than a measurement.', { exact: false }),
    ).toBeVisible();
  });

  test('a flagged week explains itself in plain language', async ({ page }) => {
    await stubApi(page, { timeline: [item()], heatmap: term });
    await page.goto('/timeline');

    await expect(page.getByText('3 major items across 3 courses', { exact: false })).toBeVisible();
    await expect(page.getByText('Heavy week').first()).toBeVisible();
  });

  test('every cell carries a spoken description', async ({ page }) => {
    await stubApi(page, { timeline: [item()], heatmap: term });
    await page.goto('/timeline');

    const cell = page.locator('.cell').first();
    await expect(cell).toHaveAttribute('aria-label', /Week 1.*About 6 hours of work/);
  });

  test('selecting a week shows what lands in it', async ({ page }) => {
    await stubApi(page, { timeline: [item()], heatmap: term });
    await page.goto('/timeline');

    await page.locator('.cell').nth(2).click();
    await expect(page.getByRole('heading', { name: /Week of Sep 7/ })).toBeVisible();
  });

  test('a quiet week says so rather than showing nothing', async ({ page }) => {
    await stubApi(page, { timeline: [item()], heatmap: term });
    await page.goto('/timeline');

    await page.locator('.cell').first().click();
    await expect(page.getByText('Nothing unusual lands this week.')).toBeVisible();
  });
});
