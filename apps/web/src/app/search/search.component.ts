import { CommonModule } from '@angular/common';
import { ChangeDetectionStrategy, Component, computed, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { RouterLink } from '@angular/router';
import { ApiService } from '../core/api.service';
import { CourseProfile, SearchFilters } from '../core/models';

/**
 * Course search (PRD sections 6.4 and 14.3, US-8).
 *
 * The facet set here is the one the PRD names, and it exists to answer one
 * particular kind of question: "a 3-credit humanities elective, no group
 * project, no attendance policy, papers instead of exams."
 *
 * Two display rules carry over from section 14.2 and are not optional:
 *
 *   - Every profile states where it came from and how old it is. A profile
 *     built on a three-year-old syllabus is still useful as long as it says so.
 *   - Courses with no verified syllabus stay in the results with a prompt to
 *     upload one. Hiding them would remove the only place new syllabi come
 *     from.
 */
@Component({
  selector: 'si-search',
  standalone: true,
  imports: [CommonModule, FormsModule, RouterLink],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './search.component.html',
  styleUrl: './search.component.css',
})
export class SearchComponent {
  private readonly api = inject(ApiService);

  protected readonly query = signal('');
  protected readonly credits = signal<number | null>(null);
  protected readonly mix = signal<string | null>(null);
  protected readonly noGroupProject = signal(false);
  protected readonly noAttendance = signal(false);
  protected readonly maxHours = signal<number | null>(null);

  protected readonly results = signal<CourseProfile[]>([]);
  protected readonly total = signal(0);
  protected readonly loading = signal(false);
  protected readonly searched = signal(false);
  protected readonly error = signal<string | null>(null);

  protected readonly mixOptions = [
    { value: 'paper_heavy', label: 'Mostly papers' },
    { value: 'exam_heavy', label: 'Mostly exams' },
    { value: 'project_heavy', label: 'Mostly projects' },
    { value: 'continuous', label: 'Lots of small work' },
  ];

  protected readonly known = computed(() => this.results().filter((r) => r.known));
  protected readonly unknown = computed(() => this.results().filter((r) => !r.known));

  protected readonly activeFilterCount = computed(() => {
    let count = 0;
    if (this.credits() !== null) count += 1;
    if (this.mix() !== null) count += 1;
    if (this.noGroupProject()) count += 1;
    if (this.noAttendance()) count += 1;
    if (this.maxHours() !== null) count += 1;
    return count;
  });

  protected search(): void {
    this.loading.set(true);
    this.error.set(null);

    const filters: SearchFilters = {
      q: this.query() || undefined,
      credits: this.credits(),
      assessment_mix: this.mix(),
      // A checked box means "exclude courses that have this", which is a
      // filter on false. Leaving it unchecked must send nothing at all, not
      // `true`, or it would silently invert the student's intent.
      has_group_project: this.noGroupProject() ? false : null,
      attendance_graded: this.noAttendance() ? false : null,
      max_weekly_hours: this.maxHours(),
    };

    this.api.searchCourses(filters).subscribe({
      next: (response) => {
        this.results.set(response.results);
        this.total.set(response.total);
        this.loading.set(false);
        this.searched.set(true);
      },
      error: () => {
        this.error.set('Search did not run. Try again in a moment.');
        this.loading.set(false);
      },
    });
  }

  protected clearFilters(): void {
    this.credits.set(null);
    this.mix.set(null);
    this.noGroupProject.set(false);
    this.noAttendance.set(false);
    this.maxHours.set(null);
    this.search();
  }

  protected mixLabel(value: string | null): string {
    return this.mixOptions.find((option) => option.value === value)?.label ?? 'Mixed';
  }

  protected examLabel(type: string | null): string {
    switch (type) {
      case 'cumulative':
        return 'Cumulative final';
      case 'non_cumulative':
        return 'Non-cumulative final';
      case 'final_project':
        return 'Final project, no exam';
      case 'none':
        return 'No final exam';
      default:
        return 'Final exam not stated';
    }
  }
}
