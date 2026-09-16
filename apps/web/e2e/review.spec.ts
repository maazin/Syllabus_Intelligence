import { expect, item, lowConfidence, stubApi, test, undated, weekOnly } from './fixtures';

/**
 * Review screen (PRD sections 6.2 and 10.2, US-3 and US-4).
 *
 * The behaviors asserted here are the ones that make review finishable in
 * under 90 seconds without making it careless. Two of them are load-bearing
 * enough that a regression would be a product bug rather than a cosmetic one:
 * a low-confidence item must never arrive pre-accepted, and an item whose time
 * we supplied must never look like one the syllabus stated.
 */

test.describe('review', () => {
  test('shows the confident majority as already handled', async ({ page }) => {
    await stubApi(page, {
      timeline: [item({ title: 'PS 1' }), item({ title: 'PS 2' }), item({ title: 'PS 3' })],
    });
    await page.goto('/review/d-1');

    // Section 10.2: most items are correct, so the screen opens close to done.
    await expect(page.getByText('3 of 3 confirmed')).toBeVisible();
    await expect(page.getByRole('heading', { name: 'Ready to go' })).toBeVisible();
    await expect(page.getByRole('heading', { name: 'Worth a look' })).toBeHidden();
  });

  test('a low-confidence item is separated out and not pre-accepted', async ({ page }) => {
    await stubApi(page, { timeline: [item(), item(), lowConfidence()] });
    await page.goto('/review/d-1');

    await expect(page.getByRole('heading', { name: 'Worth a look' })).toBeVisible();
    await expect(page.getByText('1 to confirm')).toBeVisible();
    // Two of three arrive accepted; the uncertain one does not.
    await expect(page.getByText('2 of 3 confirmed')).toBeVisible();
  });

  test('the finish button stays disabled until uncertain items are handled', async ({ page }) => {
    await stubApi(page, { timeline: [item(), lowConfidence()] });
    await page.goto('/review/d-1');

    const finish = page.getByRole('button', { name: 'Add to my timeline' });
    await expect(finish).toBeDisabled();
    await expect(page.getByText('Confirm the 1 items above to finish.')).toBeVisible();

    await page.getByRole('button', { name: 'Looks right' }).click();
    await expect(finish).toBeEnabled();
  });

  test('the source sentence is visible without opening anything', async ({ page }) => {
    await stubApi(page, { timeline: [lowConfidence()] });
    await page.goto('/review/d-1');

    // US-3: verify by glancing rather than reopening the PDF.
    await expect(
      page.getByText('Midterm 1 will be held in class on Tuesday, October 14.'),
    ).toBeVisible();
    await expect(page.getByText('Page 2')).toBeVisible();
  });

  test('a confident item hides its detail until asked', async ({ page }) => {
    await stubApi(page, { timeline: [item({ source_span: 'A quiet source sentence.' })] });
    await page.goto('/review/d-1');

    await expect(page.getByText('A quiet source sentence.')).toBeHidden();

    const toggle = page.getByRole('button', { name: /Problem Set 1/ });
    await expect(toggle).toHaveAttribute('aria-expanded', 'false');
    await toggle.click();

    await expect(toggle).toHaveAttribute('aria-expanded', 'true');
    await expect(page.getByText('A quiet source sentence.')).toBeVisible();
  });

  test('an inferred time is marked as ours, not the syllabus', async ({ page }) => {
    await stubApi(page, { timeline: [item({ time_inferred: true })] });
    await page.goto('/review/d-1');

    // Section 9.3. A student who finds a fabricated exam time stops trusting
    // every other date, so the marker is not decoration.
    const inferred = page.locator('.due__time--inferred').first();
    await expect(inferred).toBeVisible();
    await expect(inferred).toHaveAttribute(
      'title',
      /did not give a time/,
    );
  });

  test('a stated time carries no inference marker', async ({ page }) => {
    await stubApi(page, {
      timeline: [item({ due_precision: 'exact_datetime', time_inferred: false })],
    });
    await page.goto('/review/d-1');

    await expect(page.locator('.due__time--inferred')).toHaveCount(0);
  });

  test('a week band is shown as a week, never as a day', async ({ page }) => {
    await stubApi(page, { timeline: [weekOnly()] });
    await page.goto('/review/d-1');

    // Section 9.3: it renders as a band and never collapses to a single day.
    await expect(page.getByText(/Week of/)).toBeVisible();

    // A confident item stays collapsed, but carries a marker saying there is
    // something to read, and the explanation is one click away.
    await expect(page.getByLabel('This item has a note')).toBeVisible();
    await page.getByRole('button', { name: /Reading Response 2/ }).click();
    await expect(
      page.getByText('The syllabus gives a week rather than a day.', { exact: false }),
    ).toBeVisible();
  });

  test('undated items get their own tray and stay off the calendar', async ({ page }) => {
    await stubApi(page, { timeline: [item(), undated()] });
    await page.goto('/review/d-1');

    await expect(page.getByRole('heading', { name: 'No date yet' })).toBeVisible();
    await expect(
      page.getByText('They stay off your calendar until you add one.', { exact: false }),
    ).toBeVisible();
  });

  test('bulk confirm clears the confident group in one action', async ({ page }) => {
    await stubApi(page, {
      timeline: [item({ title: 'A' }), item({ title: 'B' }), lowConfidence()],
    });
    await page.goto('/review/d-1');

    await expect(page.getByText('2 of 3 confirmed')).toBeVisible();
    await page.getByRole('button', { name: /Confirm all/ }).click();
    // The uncertain one is untouched: bulk accept must not reach it.
    await expect(page.getByText('2 of 3 confirmed')).toBeVisible();
  });

  test('editing a date saves and marks the item confirmed', async ({ page }) => {
    await stubApi(page, { timeline: [lowConfidence()] });
    await page.goto('/review/d-1');

    await page.getByRole('button', { name: 'Change date' }).click();
    await page.getByLabel('Correct date').fill('2026-10-21');

    const patch = page.waitForRequest(
      (request) => request.url().includes('/assessments/') && request.method() === 'PATCH',
    );
    await page.getByRole('button', { name: 'Save' }).click();

    // US-4: the edit goes to the API, which records it as an override and a
    // correction rather than mutating the shared parse.
    const request = await patch;
    expect(request.postDataJSON()).toMatchObject({ field: 'due_at' });
    await expect(page.getByText('1 of 1 confirmed')).toBeVisible();
  });

  test('finishing calls review-complete and moves to the timeline', async ({ page }) => {
    await stubApi(page, { timeline: [item()] });
    await page.goto('/review/d-1');

    const complete = page.waitForRequest((r) => r.url().includes('/review-complete'));
    await page.getByRole('button', { name: 'Add to my timeline' }).click();

    await complete;
    await expect(page).toHaveURL(/\/timeline$/);
  });

  test('the calendar promise is stated on the screen', async ({ page }) => {
    await stubApi(page, { timeline: [item()] });
    await page.goto('/review/d-1');

    // US-3 gates calendar writes on review. Saying so is what makes a student
    // willing to hand over the file in the first place.
    await expect(
      page.getByText('Nothing goes to your calendar until you finish here.'),
    ).toBeVisible();
  });

  test('an empty document explains itself instead of showing a blank screen', async ({ page }) => {
    await stubApi(page, { timeline: [] });
    await page.goto('/review/d-1');

    // Section 9.4's hard failure: route to manual entry with an apology.
    await expect(page.getByRole('heading', { name: 'Nothing to review yet' })).toBeVisible();
    await expect(page.getByRole('link', { name: 'Add items manually' })).toBeVisible();
  });

  test('a failed load says so rather than looking empty', async ({ page }) => {
    await stubApi(page, { failTimeline: true });
    await page.goto('/review/d-1');

    await expect(page.getByRole('alert')).toContainText('could not load');
  });
});

/**
 * Before there is anything to review (PRD sections 6.1 and 9.4).
 *
 * The parse runs on a worker, and the course match asks the student when it
 * is not sure. These are the states a real upload passes through, and they
 * only exist against a real API, which is why the stubbed suite has to script
 * them explicitly.
 */
test.describe('review, before the list', () => {
  test('waits for the worker and then loads', async ({ page }) => {
    let polls = 0;
    await stubApi(page, {
      timeline: [item()],
      documentStatus: () => ({
        document_id: 'd-1',
        status: polls++ < 2 ? 'queued' : 'succeeded',
      }),
    });
    await page.goto('/review/d-1');

    await expect(page.getByText('Reading your syllabus', { exact: false })).toBeVisible();
    await expect(page.getByRole('heading', { name: 'Ready to go' })).toBeVisible({
      timeout: 10_000,
    });
    expect(polls).toBeGreaterThanOrEqual(3);
  });

  test('asks which course when the match was not confident', async ({ page }) => {
    let confirmed = false;
    await stubApi(page, {
      timeline: [item()],
      documentStatus: () =>
        confirmed
          ? { document_id: 'd-1', status: 'succeeded' }
          : {
              document_id: 'd-1',
              status: 'needs_course',
              course_match: {
                confidence: 0.5,
                guess: 'COP 4530',
                candidates: [
                  {
                    section_id: 's-1',
                    label: 'COP 4530 section 001',
                    title: 'Data Structures',
                    instructor: 'Dr. Alice Nakamura',
                    meeting_pattern: 'MWF 10:00-10:50',
                  },
                  {
                    section_id: 's-2',
                    label: 'COP 4530 section 002',
                    title: 'Data Structures',
                    instructor: 'Dr. Ben Osei',
                    meeting_pattern: 'TR 14:00-15:15',
                  },
                ],
              },
            },
    });
    await page.route('**/api/v1/documents/d-1/confirm-course*', (route) => {
      confirmed = true;
      return route.fulfill({ json: { status: 'confirmed' } });
    });
    await page.goto('/review/d-1');

    await expect(page.getByRole('heading', { name: 'Which course is this?' })).toBeVisible();
    await expect(page.getByText('COP 4530', { exact: false }).first()).toBeVisible();

    // Nothing is preselected: a wrong section puts a wrong exam date on a
    // calendar, so the choice has to be deliberate.
    const confirm = page.getByRole('button', { name: 'This is my course' });
    await expect(confirm).toBeDisabled();

    await page.getByLabel('Your section').selectOption('s-2');
    await expect(confirm).toBeEnabled();
    await confirm.click();

    await expect(page.getByRole('heading', { name: 'Ready to go' })).toBeVisible({
      timeout: 10_000,
    });
  });

  test('a document that could not be read says so and offers manual entry', async ({ page }) => {
    await stubApi(page, {
      documentStatus: {
        document_id: 'd-1',
        status: 'failed',
        error: 'This file does not contain readable text.',
      },
    });
    await page.goto('/review/d-1');

    await expect(page.getByRole('heading', { name: 'We could not read this syllabus' })).toBeVisible();
    await expect(page.getByText('This file does not contain readable text.')).toBeVisible();
    await expect(page.getByRole('link', { name: 'Add items manually' })).toBeVisible();
  });
});
