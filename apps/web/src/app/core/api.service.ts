import { HttpClient, HttpParams } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { Observable } from 'rxjs';
import { runtimeConfig } from './runtime-config';
import {
  AuthResponse,
  CourseProfile,
  CourseSummary,
  DocumentStatus,
  HeatmapCell,
  SearchFilters,
  SearchResults,
  Term,
  TimelineItem,
  UploadResult,
} from './models';

/** The single place the app talks to the API (PRD section 23).
 *
 * Every method returns a cold Observable so callers control when a request
 * fires and can cancel it. Nothing here caches: the review screen and the
 * heatmap both need to reflect an edit immediately, and a stale timeline is
 * worse than a slower one.
 */
@Injectable({ providedIn: 'root' })
export class ApiService {
  private readonly http = inject(HttpClient);
  /** Resolved per call rather than captured at construction, so the value
   * cannot be read before `loadRuntimeConfig` has run. */
  private get base(): string {
    return runtimeConfig().apiBase;
  }

  // --- auth ---------------------------------------------------------------

  requestMagicLink(email: string): Observable<{ status: string }> {
    return this.http.post<{ status: string }>(`${this.base}/auth/magic-link`, { email });
  }

  verifyMagicLink(token: string): Observable<AuthResponse> {
    return this.http.post<AuthResponse>(`${this.base}/auth/verify`, { token });
  }

  // --- documents ----------------------------------------------------------

  /** US-1: up to 10 files at once, each with independent status. */
  uploadDocuments(files: File[], sectionId?: string): Observable<UploadResult[]> {
    const form = new FormData();
    for (const file of files) {
      form.append('files', file, file.name);
    }
    const params = sectionId ? new HttpParams().set('section_id', sectionId) : undefined;
    return this.http.post<UploadResult[]>(`${this.base}/documents`, form, { params });
  }

  documentStatus(documentId: string): Observable<DocumentStatus> {
    return this.http.get<DocumentStatus>(`${this.base}/documents/${documentId}`);
  }

  confirmCourse(documentId: string, sectionId: string): Observable<unknown> {
    const params = new HttpParams().set('section_id', sectionId);
    return this.http.post(`${this.base}/documents/${documentId}/confirm-course`, null, { params });
  }

  /** US-3's gate. Nothing reaches the student's real calendar before this. */
  completeReview(documentId: string): Observable<{ status: string; verified: number }> {
    return this.http.post<{ status: string; verified: number }>(
      `${this.base}/documents/${documentId}/review-complete`,
      null,
    );
  }

  /** US-4. Writes to `user_overrides` and appends to `corrections_log`. */
  patchAssessment(assessmentId: string, field: string, value: unknown): Observable<unknown> {
    return this.http.patch(`${this.base}/assessments/${assessmentId}`, { field, value });
  }

  // --- plan ---------------------------------------------------------------

  timeline(options: { from?: string; to?: string; courseId?: string } = {}): Observable<TimelineItem[]> {
    let params = new HttpParams();
    if (options.from) params = params.set('from', options.from);
    if (options.to) params = params.set('to', options.to);
    if (options.courseId) params = params.set('course_id', options.courseId);
    return this.http.get<TimelineItem[]>(`${this.base}/timeline`, { params });
  }

  /** The term the student is in, so the heatmap knows what to ask for. */
  currentTerm(): Observable<Term | null> {
    return this.http.get<Term | null>(`${this.base}/terms/current`);
  }

  heatmap(termId: string): Observable<HeatmapCell[]> {
    const params = new HttpParams().set('term_id', termId);
    return this.http.get<HeatmapCell[]>(`${this.base}/heatmap`, { params });
  }

  // --- catalog and corpus --------------------------------------------------

  catalog(query: string): Observable<CourseSummary[]> {
    const params = new HttpParams().set('q', query);
    return this.http.get<CourseSummary[]>(`${this.base}/courses`, { params });
  }

  searchCourses(filters: SearchFilters): Observable<SearchResults> {
    let params = new HttpParams();
    for (const [key, value] of Object.entries(filters)) {
      // Only send facets the student actually set. Sending `null` would filter
      // on "has no value", which is a different and wrong question.
      if (value !== null && value !== undefined && value !== '') {
        params = params.set(key, String(value));
      }
    }
    return this.http.get<SearchResults>(`${this.base}/courses/search`, { params });
  }

  courseProfiles(courseId: string): Observable<CourseProfile[]> {
    return this.http.get<CourseProfile[]>(`${this.base}/courses/${courseId}/profiles`);
  }

  // --- calendar ------------------------------------------------------------

  icsFeedUrl(): Observable<{ url: string }> {
    return this.http.get<{ url: string }>(`${this.base}/calendar/ics-url`);
  }
}
