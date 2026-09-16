/** Types mirroring the API contract in PRD section 23.
 *
 * Hand-written rather than generated, but they must stay in step with
 * `/openapi.json`, which section 23 names as the source of truth once routes
 * exist. If a field here has no counterpart there, one of the two is wrong.
 */

/** Section 9.3. The UI treats these as four genuinely different things, and
 *  never renders one as another. */
export type DuePrecision = 'exact_datetime' | 'date_only' | 'week_only' | 'tbd';

/** Section 10.1's buckets. `high` collapses, `low` demands an explicit action. */
export type ConfidenceBucket = 'high' | 'medium' | 'low';

export type FlagSeverity = 'yellow' | 'red';

export interface User {
  id: string;
  email: string;
  verified: boolean;
}

export interface AuthResponse {
  access_token: string;
  user: User;
}

export interface UploadResult {
  document_id: string | null;
  filename: string;
  /** `ready` means content-hash dedup found an existing parse (18.3), so this
   *  student skips the queue entirely. */
  status: 'queued' | 'ready' | 'failed' | 'parsed' | 'needs_manual_entry';
  deduped: boolean;
  error: string | null;
}

/** One registrar section the student can pick when the match was unsure. */
export interface CourseCandidate {
  section_id: string;
  label: string;
  title: string;
  instructor: string | null;
  meeting_pattern: string | null;
}

export interface CourseMatch {
  confidence: number;
  /** What the syllabus said it was, e.g. "COP 4530". */
  guess: string | null;
  candidates: CourseCandidate[];
}

export type DocumentState =
  | 'queued'
  | 'needs_course'
  | 'succeeded'
  | 'needs_manual_entry'
  | 'failed';

export interface DocumentStatus {
  document_id: string;
  status: DocumentState;
  course_match?: CourseMatch | null;
  error?: string | null;
}

export interface TimelineItem {
  assessment_id: string;
  title: string;
  course: string;
  type: string;
  weight_pct: number | null;
  /** Already converted to the institution's timezone by the API. */
  due_at: string | null;
  due_precision: DuePrecision;
  /** True when the time was supplied by us, not stated in the syllabus.
   *  The UI must show it muted with an explanation (9.3). */
  time_inferred: boolean;
  is_group: boolean;
  source_span: string;
  page_ref: number | null;
  edited: boolean;
  /** Section 10.1's composite score, computed server-side. The client must not
   *  re-derive this: only the pipeline can see a weekday mismatch or a failed
   *  validation rule, and those are the items most worth reviewing. */
  confidence: number | null;
  confidence_bucket: ConfidenceBucket;
}

export interface HeatmapFlag {
  kind: string;
  severity: FlagSeverity;
  explanation: string;
  assessment_ids: string[];
}

export interface HeatmapCell {
  week_start: string;
  effort_hours: number;
  flags: HeatmapFlag[];
}

export interface Term {
  id: string;
  name: string;
  start_date: string;
  end_date: string;
  finals_start: string | null;
  finals_end: string | null;
  is_current: boolean;
}

export interface CourseSummary {
  id: string;
  subject_code: string;
  catalog_number: string;
  title: string;
  credits: number;
  gened_attributes: string[];
}

export interface CourseProfile {
  course_id: string;
  subject_code: string;
  catalog_number: string;
  title: string;
  credits: number;
  gened_attributes: string[];
  /** False for a course with no verified syllabus. It still appears in results
   *  (US-8); the empty state is where uploads come from. */
  known: boolean;
  instructor: string | null;
  term: string | null;
  term_start: string | null;
  /** Always present. Section 14.2 requires every profile to state its source. */
  provenance: string;
  est_weekly_hours: number | null;
  assessment_mix: string | null;
  has_group_project: boolean | null;
  attendance_graded: boolean | null;
  final_exam_type: string | null;
  graded_item_count: number | null;
  est_materials_cost_usd: number | null;
  verification_count: number;
  meeting_pattern: string | null;
  seats_open: number | null;
}

export interface SearchResults {
  total: number;
  offset: number;
  limit: number;
  results: CourseProfile[];
}

export interface SearchFilters {
  q?: string;
  credits?: number | null;
  max_weekly_hours?: number | null;
  assessment_mix?: string | null;
  has_group_project?: boolean | null;
  attendance_graded?: boolean | null;
  final_exam_type?: string | null;
  max_graded_items?: number | null;
  max_materials_cost?: number | null;
  gened_attribute?: string | null;
}
