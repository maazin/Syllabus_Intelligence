/**
 * Date parsing that does not silently shift a day.
 *
 * `new Date('2026-09-07')` is parsed as UTC midnight by specification, so
 * rendering it with `toLocaleDateString` in any timezone behind UTC shows the
 * *previous* day. The heatmap's week labels hit this exactly: a week starting
 * Sep 7 rendered as "Sep 6" for every student in the Americas.
 *
 * This is the browser-side twin of the `db.localtime` problem on the server.
 * Both come from the same root: a calendar day and an instant are different
 * things, and converting between them without saying which timezone you mean
 * loses a day at the boundary.
 *
 * Rule for this codebase: a date-only string (`YYYY-MM-DD`) is a calendar day
 * and must be parsed as local. A full timestamp carries its own offset and is
 * parsed normally.
 */

const DATE_ONLY = /^(\d{4})-(\d{2})-(\d{2})$/;

/** Parse an API date, treating a bare `YYYY-MM-DD` as a local calendar day. */
export function parseApiDate(value: string | null | undefined): Date | null {
  if (!value) return null;

  const dateOnly = DATE_ONLY.exec(value);
  if (dateOnly) {
    const [, year, month, day] = dateOnly;
    // Constructing from components pins it to local midnight, so the day
    // survives the trip to the display string.
    return new Date(Number(year), Number(month) - 1, Number(day));
  }

  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? null : parsed;
}

export function formatDay(
  value: string | null | undefined,
  options: Intl.DateTimeFormatOptions = { month: 'short', day: 'numeric' },
): string {
  const date = parseApiDate(value);
  return date ? date.toLocaleDateString(undefined, options) : '';
}
