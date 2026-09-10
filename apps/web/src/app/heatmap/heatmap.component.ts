import { CommonModule } from '@angular/common';
import { ChangeDetectionStrategy, Component, computed, inject, input, signal } from '@angular/core';
import { RouterLink } from '@angular/router';
import { ApiService } from '../core/api.service';
import { formatDay } from '../core/dates';
import { HeatmapCell } from '../core/models';

interface WeekView extends HeatmapCell {
  weekNumber: number;
  /** 0 to 5, indexing the sequential ramp in styles.css. */
  intensity: number;
  label: string;
  topSeverity: 'red' | 'yellow' | null;
  selected: boolean;
}

/**
 * Term workload heatmap (PRD sections 6.3 and 11).
 *
 * Two things govern how this is drawn.
 *
 * First, section 11.5: these numbers are an estimate built on constants the
 * PRD itself calls guesses. The UI says so plainly rather than implying a
 * precision the model does not have.
 *
 * Second, the HIG rule that color must never carry meaning alone. Intensity
 * here is a single-hue sequential ramp, every cell prints its own hour count,
 * and flagged weeks carry a text label and a shape marker as well as a color.
 * The chart stays readable in grayscale and for a color-blind student.
 */
@Component({
  selector: 'si-heatmap',
  standalone: true,
  imports: [CommonModule, RouterLink],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './heatmap.component.html',
  styleUrl: './heatmap.component.css',
})
export class HeatmapComponent {
  private readonly api = inject(ApiService);

  readonly termId = input<string>('');

  protected readonly cells = signal<HeatmapCell[]>([]);
  protected readonly loading = signal(true);
  protected readonly error = signal<string | null>(null);
  protected readonly selectedWeek = signal<string | null>(null);

  protected readonly peak = computed(() =>
    Math.max(1, ...this.cells().map((cell) => cell.effort_hours)),
  );

  protected readonly weeks = computed<WeekView[]>(() =>
    this.cells().map((cell, index) => {
      const red = cell.flags.find((flag) => flag.severity === 'red');
      const yellow = cell.flags.find((flag) => flag.severity === 'yellow');
      return {
        ...cell,
        weekNumber: index + 1,
        intensity: this.intensityFor(cell.effort_hours),
        label: this.weekLabel(cell.week_start),
        topSeverity: red ? 'red' : yellow ? 'yellow' : null,
        selected: this.selectedWeek() === cell.week_start,
      };
    }),
  );

  protected readonly selected = computed(() =>
    this.weeks().find((week) => week.week_start === this.selectedWeek()) ?? null,
  );

  protected readonly flaggedWeeks = computed(() =>
    this.weeks().filter((week) => week.flags.length > 0),
  );

  protected readonly busiest = computed(() => {
    const sorted = [...this.weeks()].sort((a, b) => b.effort_hours - a.effort_hours);
    return sorted[0] ?? null;
  });

  constructor() {
    this.load();
  }

  private load(): void {
    const term = this.termId();
    if (term) {
      this.fetch(term);
      return;
    }
    // No term supplied, so resolve the current one first. The component owns
    // this rather than every parent screen having to thread it through.
    this.api.currentTerm().subscribe({
      next: (resolved) => {
        if (resolved) {
          this.fetch(resolved.id);
        } else {
          this.loading.set(false);
        }
      },
      error: () => {
        this.error.set('We could not work out which term you are in.');
        this.loading.set(false);
      },
    });
  }

  private fetch(term: string): void {
    this.api.heatmap(term).subscribe({
      next: (cells) => {
        this.cells.set(cells);
        this.loading.set(false);
      },
      error: () => {
        this.error.set('We could not load your term. Refresh to try again.');
        this.loading.set(false);
      },
    });
  }

  /** Maps hours onto the six-step ramp, scaled against the term's own peak so
   *  a light term still shows variation rather than reading as uniformly empty. */
  private intensityFor(hours: number): number {
    const ratio = hours / this.peak();
    if (ratio <= 0.2) return 0;
    if (ratio <= 0.4) return 1;
    if (ratio <= 0.6) return 2;
    if (ratio <= 0.75) return 3;
    if (ratio <= 0.9) return 4;
    return 5;
  }

  private weekLabel(weekStart: string): string {
    // `week_start` is a bare calendar day. Parsing it with `new Date` would
    // read it as UTC midnight and render the day before in the Americas.
    return formatDay(weekStart) || weekStart;
  }

  protected select(weekStart: string): void {
    this.selectedWeek.update((current) => (current === weekStart ? null : weekStart));
  }

  /** Every cell gets a spoken description, because a screen reader cannot see
   *  the ramp (HIG charting: provide accessibility labels for chart values). */
  protected cellDescription(week: WeekView): string {
    const base = `Week ${week.weekNumber}, starting ${week.label}. About ${Math.round(
      week.effort_hours,
    )} hours of work.`;
    if (week.flags.length === 0) return base;
    return `${base} ${week.flags.map((flag) => flag.explanation).join(' ')}`;
  }

  protected severityLabel(severity: 'red' | 'yellow'): string {
    return severity === 'red' ? 'Heavy week' : 'Busier than usual';
  }
}
