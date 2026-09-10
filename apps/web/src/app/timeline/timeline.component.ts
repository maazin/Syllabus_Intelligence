import { CommonModule } from '@angular/common';
import { ChangeDetectionStrategy, Component, computed, inject, signal } from '@angular/core';
import { RouterLink } from '@angular/router';
import { ApiService } from '../core/api.service';
import { parseApiDate } from '../core/dates';
import { TimelineItem } from '../core/models';
import { DueDateComponent } from '../shared/due-date.component';
import { HeatmapComponent } from '../heatmap/heatmap.component';

interface MonthGroup {
  key: string;
  label: string;
  items: TimelineItem[];
}

/**
 * Merged multi-course timeline (PRD section 6.3, US-5).
 *
 * Grouped by month rather than shown as a flat list: a five-course term
 * produces upward of sixty items, and an unbroken scroll of them gives the
 * student no sense of where they are in the term.
 *
 * Items whose date is imprecise are not sorted in among the dated ones and
 * then quietly rendered as though they were certain. They keep their own
 * precision through `si-due-date`, and undated items sit in a separate tray
 * at the end.
 */
@Component({
  selector: 'si-timeline',
  standalone: true,
  imports: [CommonModule, RouterLink, DueDateComponent, HeatmapComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './timeline.component.html',
  styleUrl: './timeline.component.css',
})
export class TimelineComponent {
  private readonly api = inject(ApiService);

  protected readonly items = signal<TimelineItem[]>([]);
  protected readonly loading = signal(true);
  protected readonly error = signal<string | null>(null);
  protected readonly courseFilter = signal<string | null>(null);
  protected readonly feedUrl = signal<string | null>(null);
  protected readonly feedCopied = signal(false);

  protected readonly courses = computed(() => {
    const names = new Set(this.items().map((item) => item.course));
    return [...names].sort();
  });

  protected readonly visible = computed(() => {
    const filter = this.courseFilter();
    return filter ? this.items().filter((item) => item.course === filter) : this.items();
  });

  protected readonly scheduled = computed(() =>
    this.visible().filter((item) => item.due_precision !== 'tbd' && item.due_at),
  );

  /** Section 9.3's tray. These are real graded items with no announced date,
   *  so they stay visible without being placed on a day we do not know. */
  protected readonly undated = computed(() =>
    this.visible().filter((item) => item.due_precision === 'tbd' || !item.due_at),
  );

  protected readonly months = computed<MonthGroup[]>(() => {
    const groups = new Map<string, MonthGroup>();
    for (const item of this.scheduled()) {
      const date = parseApiDate(item.due_at)!;
      const key = `${date.getFullYear()}-${date.getMonth()}`;
      if (!groups.has(key)) {
        groups.set(key, {
          key,
          label: date.toLocaleDateString(undefined, { month: 'long', year: 'numeric' }),
          items: [],
        });
      }
      groups.get(key)!.items.push(item);
    }
    return [...groups.values()];
  });

  protected readonly nextUp = computed(() => {
    const now = Date.now();
    return this.scheduled()
      .filter((item) => (parseApiDate(item.due_at)?.getTime() ?? 0) >= now)
      .slice(0, 3);
  });

  constructor() {
    this.load();
    this.api.icsFeedUrl().subscribe({
      next: (response) => this.feedUrl.set(response.url),
      // A missing feed URL is not worth an error banner; the rest of the page
      // is still useful without it.
      error: () => this.feedUrl.set(null),
    });
  }

  private load(): void {
    this.api.timeline().subscribe({
      next: (items) => {
        this.items.set(items);
        this.loading.set(false);
      },
      error: () => {
        this.error.set('We could not load your timeline. Refresh to try again.');
        this.loading.set(false);
      },
    });
  }

  protected filterBy(course: string | null): void {
    this.courseFilter.set(course);
  }

  protected copyFeed(): void {
    const url = this.feedUrl();
    if (!url) return;
    navigator.clipboard.writeText(url).then(
      () => {
        this.feedCopied.set(true);
        setTimeout(() => this.feedCopied.set(false), 2500);
      },
      () => this.feedCopied.set(false),
    );
  }

  protected typeLabel(type: string): string {
    return type.replace(/_/g, ' ');
  }

  /** A stable per-course accent for the timeline rail. Hue is derived from the
   *  course label so it survives reordering, and stays low-chroma so a
   *  five-course term does not turn into a set of competing bright stripes. */
  protected courseHue(course: string): number {
    let hash = 0;
    for (let i = 0; i < course.length; i += 1) {
      hash = (hash * 31 + course.charCodeAt(i)) % 360;
    }
    return hash;
  }
}
