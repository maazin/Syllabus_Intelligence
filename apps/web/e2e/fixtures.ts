import { Page, test as base } from '@playwright/test';

/**
 * Shared stubs for the API boundary.
 *
 * Each helper builds a response that a real endpoint could actually return.
 * Where a field carries a rule from the PRD, the fixture exercises the rule
 * rather than the happy path: `weekOnly` has no `due_at` at all, `tbd` carries
 * no date, and `lowConfidence` sits below the 0.60 threshold that section 10.1
 * says must not auto-accept.
 */

export interface TimelineItemFixture {
  assessment_id: string;
  title: string;
  course: string;
  type: string;
  weight_pct: number | null;
  due_at: string | null;
  due_precision: 'exact_datetime' | 'date_only' | 'week_only' | 'tbd';
  time_inferred: boolean;
  is_group: boolean;
  source_span: string;
  page_ref: number | null;
  edited: boolean;
  confidence: number | null;
  confidence_bucket: 'high' | 'medium' | 'low';
}

export function item(overrides: Partial<TimelineItemFixture> = {}): TimelineItemFixture {
  return {
    assessment_id: `a-${Math.random().toString(36).slice(2, 9)}`,
    title: 'Problem Set 1',
    course: 'COP 4530',
    type: 'problem_set',
    weight_pct: 5,
    due_at: '2026-09-04T23:59:00-04:00',
    due_precision: 'date_only',
    time_inferred: true,
    is_group: false,
    source_span: 'Problem Set 1 is due September 4.',
    page_ref: 2,
    edited: false,
    confidence: 0.93,
    confidence_bucket: 'high',
    ...overrides,
  };
}

/** An item the model was unsure about. Section 10.1 says these cannot
 *  auto-accept and must be shown first. */
export function lowConfidence(overrides: Partial<TimelineItemFixture> = {}) {
  return item({
    assessment_id: 'a-low',
    title: 'Midterm Exam 1',
    type: 'midterm',
    weight_pct: 20,
    due_at: '2026-10-14T23:59:00-04:00',
    confidence: 0.43,
    confidence_bucket: 'low',
    source_span: 'Midterm 1 will be held in class on Tuesday, October 14.',
    ...overrides,
  });
}

/** Section 9.3: a week band never collapses to a day, so it carries no due_at. */
export function weekOnly(overrides: Partial<TimelineItemFixture> = {}) {
  return item({
    assessment_id: 'a-week',
    title: 'Reading Response 2',
    type: 'reading_response',
    due_at: '2026-09-21T00:00:00-04:00',
    due_precision: 'week_only',
    confidence: 0.72,
    confidence_bucket: 'medium',
    source_span: 'Reading response due in Week 5.',
    ...overrides,
  });
}

/** Section 9.3: tbd never reaches a calendar and carries no date at all. */
export function undated(overrides: Partial<TimelineItemFixture> = {}) {
  return item({
    assessment_id: 'a-tbd',
    title: 'Guest Lecture Response',
    type: 'other',
    due_at: null,
    due_precision: 'tbd',
    weight_pct: null,
    confidence: 0.55,
    confidence_bucket: 'low',
    source_span: 'Guest lecture response: date TBA.',
    ...overrides,
  });
}

export function heatmapCell(weekStart: string, hours: number, flags: unknown[] = []) {
  return { week_start: weekStart, effort_hours: hours, flags };
}

export interface StubOptions {
  timeline?: TimelineItemFixture[];
  heatmap?: ReturnType<typeof heatmapCell>[];
  search?: unknown;
  uploadResponse?: unknown;
  failTimeline?: boolean;
}

/** Signs the browser in and stubs every endpoint the app calls. */
export async function stubApi(page: Page, options: StubOptions = {}): Promise<void> {
  // A token in storage is what the auth guard checks, so this stands in for the
  // magic-link round trip that `auth.spec.ts` covers on its own.
  await page.addInitScript(() => {
    localStorage.setItem('si.token', 'e2e-token');
    localStorage.setItem(
      'si.user',
      JSON.stringify({ id: 'u-1', email: 'student@test.edu', verified: true }),
    );
  });

  // The app fetches this before it bootstraps, so an unstubbed round trip to
  // the dev server sits in front of every assertion in the suite. Stubbing it
  // also pins the API base the tests are asserting against.
  await page.route('**/config.json', (route) =>
    route.fulfill({ json: { apiBase: '/api/v1', environment: 'test' } }),
  );

  await page.route('**/api/v1/terms/current', (route) =>
    route.fulfill({
      json: {
        id: 't-1',
        name: 'Fall 2026',
        start_date: '2026-08-24',
        end_date: '2026-12-04',
        finals_start: '2026-12-07',
        finals_end: '2026-12-11',
        is_current: true,
      },
    }),
  );

  await page.route('**/api/v1/timeline*', (route) => {
    if (options.failTimeline) {
      return route.fulfill({ status: 500, json: { detail: 'boom' } });
    }
    return route.fulfill({ json: options.timeline ?? [item()] });
  });

  await page.route('**/api/v1/heatmap*', (route) =>
    route.fulfill({ json: options.heatmap ?? [] }),
  );

  await page.route('**/api/v1/calendar/ics-url', (route) =>
    route.fulfill({ json: { url: 'http://localhost:8100/api/v1/calendar/ics/abc123' } }),
  );

  await page.route('**/api/v1/courses/search*', (route) =>
    route.fulfill({
      json: options.search ?? { total: 0, offset: 0, limit: 25, results: [] },
    }),
  );

  await page.route('**/api/v1/documents', (route) =>
    route.fulfill({
      status: 202,
      json: options.uploadResponse ?? [
        {
          document_id: 'd-1',
          filename: 'syllabus.pdf',
          status: 'queued',
          deduped: false,
          error: null,
        },
      ],
    }),
  );

  await page.route('**/api/v1/documents/*/review-complete', (route) =>
    route.fulfill({ json: { status: 'review_complete', verified: 3 } }),
  );

  await page.route('**/api/v1/assessments/*', (route) =>
    route.fulfill({ json: { status: 'updated' } }),
  );
}

export const test = base;
export { expect } from '@playwright/test';
