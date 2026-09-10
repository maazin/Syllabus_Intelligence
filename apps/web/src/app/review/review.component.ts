import { CommonModule } from '@angular/common';
import { ChangeDetectionStrategy, Component, computed, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { ActivatedRoute, Router, RouterLink } from '@angular/router';
import { ApiService } from '../core/api.service';
import { ConfidenceBucket, TimelineItem } from '../core/models';
import { DueDateComponent } from '../shared/due-date.component';

interface ReviewRow extends TimelineItem {
  bucket: ConfidenceBucket;
  confidence: number;
  accepted: boolean;
  expanded: boolean;
  warning: string | null;
}

/**
 * The review screen (PRD sections 6.2 and 10.2).
 *
 * This is the screen the whole product rests on, and it has a hard target:
 * p50 under 90 seconds for a five-course load. If review takes five minutes,
 * activation dies. Every decision here is in service of that number.
 *
 * The shape that gets it there:
 *
 *   - Items are ordered by confidence ascending, so the riskiest thing the
 *     model produced is the first thing the student sees.
 *   - High-confidence items are collapsed and pre-accepted. Most extractions
 *     are correct, and making a student confirm thirty correct rows one at a
 *     time is how you lose them.
 *   - Low-confidence items are expanded and cannot be bulk-accepted. They are
 *     the only rows that require a deliberate action.
 *   - Every row shows the sentence it came from, with its page number, so the
 *     student verifies by glancing rather than reopening the PDF.
 *
 * The progress indicator counts accepted items rather than reviewed ones, so
 * it reads as almost-done from the start. That is honest here: the work
 * genuinely is nearly finished when the screen opens.
 */
@Component({
  selector: 'si-review',
  standalone: true,
  imports: [CommonModule, FormsModule, RouterLink, DueDateComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './review.component.html',
  styleUrl: './review.component.css',
})
export class ReviewComponent {
  private readonly api = inject(ApiService);
  private readonly route = inject(ActivatedRoute);
  private readonly router = inject(Router);

  protected readonly documentId = this.route.snapshot.paramMap.get('documentId') ?? '';
  protected readonly rows = signal<ReviewRow[]>([]);
  protected readonly loading = signal(true);
  protected readonly saving = signal(false);
  protected readonly error = signal<string | null>(null);
  protected readonly editingId = signal<string | null>(null);
  protected readonly draftDate = signal('');

  /** Section 10.2: order by confidence ascending, riskiest first. */
  protected readonly ordered = computed(() =>
    [...this.rows()].sort((a, b) => a.confidence - b.confidence),
  );

  protected readonly needsAttention = computed(() =>
    this.ordered().filter((row) => row.bucket === 'low'),
  );

  protected readonly routine = computed(() =>
    this.ordered().filter((row) => row.bucket !== 'low'),
  );

  protected readonly acceptedCount = computed(
    () => this.rows().filter((row) => row.accepted).length,
  );

  protected readonly total = computed(() => this.rows().length);

  protected readonly progressPercent = computed(() => {
    const total = this.total();
    return total === 0 ? 0 : Math.round((this.acceptedCount() / total) * 100);
  });

  /** The finish button stays disabled until every low-confidence item has had
   *  a deliberate action. Those are precisely the rows most likely to be wrong. */
  protected readonly canFinish = computed(
    () => this.total() > 0 && this.needsAttention().every((row) => row.accepted),
  );

  protected readonly unscheduled = computed(() =>
    this.rows().filter((row) => row.due_precision === 'tbd'),
  );

  constructor() {
    this.load();
  }

  private load(): void {
    this.api.timeline().subscribe({
      next: (items) => {
        this.rows.set(items.map((item) => this.toRow(item)));
        this.loading.set(false);
      },
      error: () => {
        this.error.set('We could not load this syllabus. Refresh to try again.');
        this.loading.set(false);
      },
    });
  }

  private toRow(item: TimelineItem): ReviewRow {
    // The score and bucket come from the server (section 10.1). The client
    // cannot compute them: a weekday mismatch or a failed validation rule is
    // only visible to the pipeline, and those are exactly the rows that must
    // not slip into the auto-accepted group.
    const bucket = item.confidence_bucket;
    return {
      ...item,
      bucket,
      confidence: item.confidence ?? 0,
      // High and medium arrive pre-accepted; low does not.
      accepted: bucket !== 'low',
      expanded: bucket === 'low',
      warning: this.warningFor(item),
    };
  }

  /** Rule-based warnings surface inline (section 6.2). Each states the
   *  situation plainly and leaves the judgment to the student. */
  private warningFor(item: TimelineItem): string | null {
    if (item.due_precision === 'tbd') {
      return 'The syllabus does not give a date. This item stays off your calendar until you add one.';
    }
    if (item.due_precision === 'week_only') {
      return 'The syllabus gives a week rather than a day. This shows as a band across that week.';
    }
    if (item.weight_pct !== null && item.weight_pct > 60) {
      return `This is listed at ${item.weight_pct}% of your grade, which is unusually high. Worth a check.`;
    }
    if (item.confidence !== null && item.confidence < 0.6) {
      return 'We were not confident reading this one. Check it against your syllabus.';
    }
    return null;
  }

  protected toggleExpanded(id: string): void {
    this.rows.update((rows) =>
      rows.map((row) => (row.assessment_id === id ? { ...row, expanded: !row.expanded } : row)),
    );
  }

  protected accept(id: string): void {
    this.rows.update((rows) =>
      rows.map((row) => (row.assessment_id === id ? { ...row, accepted: true } : row)),
    );
  }

  protected acceptAllRoutine(): void {
    const routineIds = new Set(this.routine().map((row) => row.assessment_id));
    this.rows.update((rows) =>
      rows.map((row) => (routineIds.has(row.assessment_id) ? { ...row, accepted: true } : row)),
    );
  }

  protected startEditing(row: ReviewRow): void {
    this.editingId.set(row.assessment_id);
    this.draftDate.set(row.due_at ? row.due_at.slice(0, 10) : '');
  }

  protected cancelEditing(): void {
    this.editingId.set(null);
    this.draftDate.set('');
  }

  /** US-4: an edit persists and is never overwritten by a re-parse. The API
   *  writes it to `user_overrides` and appends to `corrections_log`. */
  protected saveDate(row: ReviewRow): void {
    const value = this.draftDate();
    if (!value) return;

    const iso = `${value}T23:59:00`;
    this.saving.set(true);
    this.api.patchAssessment(row.assessment_id, 'due_at', iso).subscribe({
      next: () => {
        this.rows.update((rows) =>
          rows.map((item) =>
            item.assessment_id === row.assessment_id
              ? {
                  ...item,
                  due_at: iso,
                  due_precision: 'date_only',
                  edited: true,
                  accepted: true,
                  warning: null,
                }
              : item,
          ),
        );
        this.editingId.set(null);
        this.saving.set(false);
      },
      error: () => {
        this.error.set('That change did not save. Check your connection and try again.');
        this.saving.set(false);
      },
    });
  }

  protected finish(): void {
    this.saving.set(true);
    this.api.completeReview(this.documentId).subscribe({
      next: () => {
        this.saving.set(false);
        this.router.navigate(['/timeline']);
      },
      error: () => {
        this.error.set('We could not finish the review. Try again in a moment.');
        this.saving.set(false);
      },
    });
  }

  protected typeLabel(type: string): string {
    return type.replace(/_/g, ' ');
  }
}
