import { CommonModule } from '@angular/common';
import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';
import { parseApiDate } from '../core/dates';
import { DuePrecision } from '../core/models';

/**
 * Renders a due date at exactly the precision the syllabus supported.
 *
 * This component exists so PRD section 9.3 is enforced in one place rather
 * than re-implemented on the timeline, the review queue, and the heatmap
 * detail panel. The rule it protects:
 *
 *   exact_datetime  the syllabus stated a time, so show it plainly
 *   date_only       the day is known, the time is not, so any clock time
 *                   shown is marked as ours and explained on hover
 *   week_only       a band across the week, never a single day
 *   tbd             no date at all, and never a calendar entry
 *
 * A student who finds a fabricated exam time on their calendar stops trusting
 * every other date in the app, so an inferred time is always visibly inferred.
 */
@Component({
  selector: 'si-due-date',
  standalone: true,
  imports: [CommonModule],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    @switch (precision()) {
      @case ('tbd') {
        <span class="due due--tbd">
          <span aria-hidden="true" class="due__glyph">?</span>
          <span>Date not announced</span>
        </span>
      }
      @case ('week_only') {
        <span class="due due--week">
          <span aria-hidden="true" class="due__glyph">~</span>
          <span>Week of {{ weekLabel() }}</span>
        </span>
      }
      @default {
        <span class="due">
          <span class="due__date">{{ dayLabel() }}</span>
          @if (timeInferred()) {
            <!-- Muted, with the reason available on hover and to assistive
                 technology. The time is real information, it is simply not
                 something the syllabus said. -->
            <span
              class="due__time due__time--inferred"
              [title]="inferredExplanation"
              [attr.aria-label]="timeLabel() + ', ' + inferredExplanation"
            >
              {{ timeLabel() }}
            </span>
          } @else if (timeLabel()) {
            <span class="due__time">{{ timeLabel() }}</span>
          }
        </span>
      }
    }
  `,
  styles: [
    `
      /* Colours come from custom properties with sensible defaults rather
         than from fixed tokens.
         
         This component gets placed on the page background and also on the
         filled dark "Coming up" block, and Angular's style encapsulation means
         a parent selector cannot reach in here to correct it. Custom
         properties do inherit through encapsulation, so a container that
         changes its ground sets these three and the component follows. Without
         it, black-on-black renders at 1:1. */
      .due {
        display: inline-flex;
        align-items: baseline;
        gap: var(--space-2);
        font-variant-numeric: tabular-nums;
      }

      .due__date {
        color: var(--due-fg, var(--text-primary));
        font-weight: var(--weight-medium);
      }

      .due__time {
        font-size: var(--text-sm);
        color: var(--due-muted, var(--text-secondary));
      }

      /* A dotted underline marks the value as inferred without relying on
         colour alone, so it survives grayscale and color-blind viewing. */
      .due__time--inferred {
        border-bottom: 1px dotted currentColor;
        cursor: help;
      }

      .due--week,
      .due--tbd {
        color: var(--due-muted, var(--text-secondary));
      }

      .due__glyph {
        color: var(--due-muted, var(--text-tertiary));
        font-family: var(--font-mono);
      }
    `,
  ],
})
export class DueDateComponent {
  readonly dueAt = input<string | null>(null);
  readonly precision = input<DuePrecision>('tbd');
  readonly timeInferred = input(false);

  protected readonly inferredExplanation =
    'The syllabus did not give a time. This is the default due time for the campus.';

  private readonly parsed = computed(() => parseApiDate(this.dueAt()));

  protected readonly dayLabel = computed(() => {
    const date = this.parsed();
    if (!date) return 'Date not announced';
    return date.toLocaleDateString(undefined, {
      weekday: 'short',
      month: 'short',
      day: 'numeric',
    });
  });

  protected readonly weekLabel = computed(() => {
    const date = this.parsed();
    if (!date) return '';
    return date.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
  });

  protected readonly timeLabel = computed(() => {
    const date = this.parsed();
    if (!date) return '';
    return date.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' });
  });
}
